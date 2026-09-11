from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.agents.base import BaseAgent
from forge.config import ForgeConfig
from forge.guardrails.bedrock_guardrails import BedrockGuardrails
from forge.state import ForgeState
from forge.utils.cost import accrue
from forge.utils.java_checks import find_unmigrated_javax_imports
from forge.utils.llm_json import extract_json
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

# Package scope is deliberately NOT part of this prompt. Scope is a pre-flight
# concern (guardrails_pre), and treating it as a post-transform blocker caused
# every out-of-scope file to be escalated to manual review even after a passing
# review score.
_SYSTEM = """You are a post-transformation quality checker for a Java migration pipeline.
Given the transformed Java source code, verify:
1. No deprecated patterns remain (Thread.stop, finalize, Calendar, SimpleDateFormat)
2. No security issues were introduced by the transformation
3. Original business logic, error handling, and null checks were preserved

Respond ONLY with valid JSON — no markdown, no explanation:
{"verdict": "PASS"|"BLOCK", "findings": ["<finding>", ...], "reason": "<summary>"}

Use BLOCK only for a clear security regression or destroyed business logic.
Style, naming, and package-convention concerns are findings, never BLOCK."""


class GuardrailsPostAgent(BaseAgent):
    def __init__(self, config: ForgeConfig):
        super().__init__(config)
        self.guardrails = BedrockGuardrails(config)
        self.llm = ChatBedrockConverse(
            model=config.transform_model,
            region_name=config.aws_region,
        )

    def run(self, state: ForgeState) -> ForgeState:
        file_status = dict(state["current_file"])
        transform_output = file_status.get("transform_output") or {}

        # Combine all transformed file contents for evaluation
        all_content = "\n\n".join(
            f"// FILE: {path}\n{content}"
            for path, content in transform_output.get("files", {}).items()
        )

        if not all_content:
            file_status["status"] = "MANUAL_REVIEW"
            file_status["error"] = "No transform output to evaluate"
            return {**state, "current_file": file_status}

        # Step 1: Bedrock Guardrails (OUTPUT)
        gr_result = self.guardrails.evaluate(all_content, "OUTPUT")
        file_status["guardrail_post_verdict"] = gr_result["action"]
        if gr_result["findings"]:
            file_status["guardrail_findings"] = list(file_status.get("guardrail_findings", [])) + gr_result["findings"]

        if gr_result["intervened"]:
            _log.info("Guardrail intervened on output for %s", file_status["file_path"])
            file_status["status"] = "MANUAL_REVIEW"
            return {**state, "current_file": file_status}

        # Step 2: Deterministic Rule 1 enforcement. "Zero javax.* in output" is a
        # mechanical invariant — check it in code rather than asking the model.
        leftover = find_unmigrated_javax_imports(all_content)
        if leftover:
            finding = f"Unmigrated javax.* imports remain: {', '.join(sorted(set(leftover)))}"
            _log.info("%s — %s", file_status["file_path"], finding)
            file_status["guardrail_findings"] = list(file_status.get("guardrail_findings", [])) + [finding]
            file_status["status"] = "MANUAL_REVIEW"
            file_status["error"] = finding
            return {**state, "current_file": file_status}

        # Step 3: Qualitative check — regressions and introduced security issues.
        messages = [
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=f"```java\n{all_content}\n```"),
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
            file_status["status"] = "MANUAL_REVIEW"
            file_status["error"] = result.get("reason", "Blocked by post-transform check")
        # status otherwise stays as-is (REVIEWING → set to DONE by write_file)

        return {**state, "current_file": file_status, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}
