from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.agents.base import BaseAgent
from forge.config import ForgeConfig, bedrock_client_config, model_max_tokens
from forge.context.inject import context_block_for
from forge.guardrails.bedrock_guardrails import BedrockGuardrails, intervention_reason
from forge.phases import get_phase
from forge.risk import score_unit, thresholds_from
from forge.state import ForgeState
from forge.utils.cost import accrue
from forge.utils.llm_json import extract_json
from forge.utils.secret_scan import find_secrets
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

# This prompt is only used when `preflight_model_check` is explicitly enabled,
# and it deliberately does NOT ask about secrets or PII. Secret detection that
# works by sending the file to a model is not a control — it is the disclosure
# it claims to prevent. That question is answered locally, before this call, by
# forge/utils/secret_scan.py. Package scope is absent for a different reason:
# it is a string comparison the file scanner already made, and asking a model
# about it is what produced both early live-run failures.
_SYSTEM = """You are a code-quality pre-flight checker for a Java migration pipeline.
Given Java source code, check for:
1. Reflection or dynamic class loading that an automated rewrite would break
2. Native calls, or generated code that should not be edited by hand
3. Unreachable or contradictory control flow

Respond ONLY with valid JSON — no markdown, no explanation:
{"verdict": "PASS"|"WARN"|"BLOCK", "findings": ["<finding>", ...], "reason": "<summary>"}

Use BLOCK only when an automated migration would clearly break the file.
Use WARN to record a concern — the pipeline continues and the finding is kept.
Do not comment on secrets, credentials, PII, package names, naming conventions,
or code style.
Use PASS when clean."""


class GuardrailsPreAgent(BaseAgent):
    def __init__(self, config: ForgeConfig):
        super().__init__(config)
        self.guardrails = BedrockGuardrails(config)
        self.llm = ChatBedrockConverse(
            model=config.transform_model,
            region_name=config.aws_region,
            max_tokens=model_max_tokens(config, config.transform_model),
            config=bedrock_client_config(config),
        )

    def run(self, state: ForgeState) -> ForgeState:
        file_status = dict(state["current_file"])
        file_path = file_status["file_path"]

        if file_status.get("generate"):
            # No file to read: the context block is the model's actual input,
            # and vendor descriptors are where credentials leak — screen that.
            source_code, _ = context_block_for(state, self.config)
            source_code = source_code or ""
        else:
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    source_code = f.read()
            except Exception as e:
                _log.error("Cannot read %s: %s", file_path, e)
                file_status["status"] = "BLOCKED"
                file_status["error"] = f"Cannot read file: {e}"
                return {**state, "current_file": file_status}

        # Score before any verdict, so even a file the guardrail blocks carries
        # its risk into the queue a human reads.
        spec = get_phase(state.get("phase") or file_status.get("phase") or "java21")
        score, tier, reasons = score_unit(
            file_path, source_code, spec,
            generate=bool(file_status.get("generate")),
            thresholds=thresholds_from(self.config),
        )
        file_status["risk_score"], file_status["risk_tier"], file_status["risk_reasons"] = score, tier, reasons

        # Step 1: the secret gate. Local, deterministic, and ahead of EVERY
        # remote call — ApplyGuardrail included. A file carrying a credential is
        # refused while its bytes are still in this process, for zero Bedrock
        # calls. This is the control; nothing downstream can substitute for it,
        # because everything downstream is a disclosure.
        scan_cfg = self.config.get("secret_scan", {}) or {}
        if scan_cfg.get("enabled", True):
            secrets = find_secrets(source_code, scan_cfg)
            if secrets:
                # Kinds and line numbers only — never the matched bytes.
                described = [f.describe() for f in secrets]
                file_status["guardrail_findings"] = list(file_status.get("guardrail_findings", [])) + described
                if scan_cfg.get("action", "block") == "block":
                    _log.info("Secret gate held %s, nothing sent: %s", file_path, "; ".join(described))
                    file_status["status"] = "BLOCKED"
                    file_status["guardrail_pre_verdict"] = "SECRET_BLOCKED_LOCALLY"
                    file_status["error"] = "Local secret scan: " + "; ".join(described)
                    return {**state, "current_file": file_status}

        # Step 2: file size. An integer comparison, so no model is asked.
        loc = source_code.count("\n") + 1
        complexity_threshold = int(self.config.get("complexity_block_threshold", 2000) or 0)
        if complexity_threshold and loc > complexity_threshold:
            reason = f"{loc} lines exceeds complexity_block_threshold of {complexity_threshold}"
            _log.info("Too large to migrate automatically: %s — %s", file_path, reason)
            file_status["status"] = "BLOCKED"
            file_status["guardrail_pre_verdict"] = "TOO_LARGE"
            file_status["error"] = reason
            file_status["guardrail_findings"] = list(file_status.get("guardrail_findings", [])) + [reason]
            return {**state, "current_file": file_status}

        # Step 3: Bedrock Guardrails (INPUT)
        gr_result = self.guardrails.evaluate(source_code, "INPUT")
        file_status["guardrail_pre_verdict"] = gr_result["action"]
        if gr_result["findings"]:
            file_status["guardrail_findings"] = list(file_status.get("guardrail_findings", [])) + gr_result["findings"]

        # `guardrail_action: warn` is the owner's dial, like secret_scan.action:
        # the findings stay on the unit and in the report, and the file is migrated.
        if gr_result["intervened"]:
            if str(self.config.get("guardrail_action", "block") or "block").lower() == "warn":
                _log.info("Guardrail intervened on input for %s; warn only, migrating it", file_path)
            else:
                _log.info("Guardrail intervened on input for %s", file_path)
                file_status["status"] = "BLOCKED"
                file_status["error"] = intervention_reason(gr_result, "INPUT")
                return {**state, "current_file": file_status}

        # Step 4: optional qualitative model check. OFF by default: every
        # question it used to answer is now answered locally, and sending source
        # to a model to look for secrets is the disclosure an enterprise secret
        # policy forbids. Enable it only for the migration-safety questions in
        # _SYSTEM, which carry no secret-detection duty.
        if not self.config.get("preflight_model_check", False):
            file_status["status"] = "TRANSFORMING"
            return {**state, "current_file": file_status}

        messages = [
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=f"```java\n{source_code}\n```"),
        ]
        response = self.llm.invoke(messages)
        bedrock_calls = state.get("bedrock_calls", 0) + 1
        cost = accrue(state, response, self.config.transform_model, self.config.get("model_pricing", {}))

        try:
            result = extract_json(response.content)
        except Exception:
            result = {"verdict": "PASS", "findings": [], "reason": "parse error — continuing"}

        verdict = result.get("verdict", "PASS")
        findings = result.get("findings", [])
        if findings:
            file_status["guardrail_findings"] = list(file_status.get("guardrail_findings", [])) + findings

        if verdict == "BLOCK":
            _log.info("Pre-flight BLOCK on %s: %s", file_path, result.get("reason", ""))
            file_status["status"] = "BLOCKED"
            file_status["error"] = result.get("reason") or "Blocked by pre-flight check"
        else:
            file_status["status"] = "TRANSFORMING"

        return {**state, "current_file": file_status, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}
