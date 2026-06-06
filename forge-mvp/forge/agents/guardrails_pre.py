import json
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.agents.base import BaseAgent
from forge.config import ForgeConfig
from forge.guardrails.bedrock_guardrails import BedrockGuardrails
from forge.state import ForgeState
from forge.utils.jsonio import loads_lenient
from forge.utils.prompts import load_prompt


class GuardrailsPreAgent(BaseAgent):
    def __init__(self, config: ForgeConfig):
        super().__init__(config)
        # System prompt lives in prompts/guardrails_pre.md (override via FORGE_PROMPTS_DIR).
        self.system_prompt = load_prompt("guardrails_pre.md")
        self.guardrails = BedrockGuardrails(config)
        self.llm = ChatBedrockConverse(
            model=config.transform_model,
            region_name=config.aws_region,
        )

    def run(self, state: ForgeState) -> ForgeState:
        file_status = dict(state["current_file"])
        file_path = file_status["file_path"]

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source_code = f.read()
        except Exception as e:
            file_status["status"] = "BLOCKED"
            file_status["error"] = f"Cannot read file: {e}"
            return {**state, "current_file": file_status}

        # Step 1: Bedrock Guardrails (INPUT)
        gr_result = self.guardrails.evaluate(source_code, "INPUT")
        file_status["guardrail_pre_verdict"] = gr_result["action"]
        if gr_result["findings"]:
            file_status["guardrail_findings"] = list(file_status.get("guardrail_findings", [])) + gr_result["findings"]

        if gr_result["intervened"]:
            file_status["status"] = "BLOCKED"
            return {**state, "current_file": file_status}

        # Step 2: Claude Sonnet scope/secrets check
        loc = source_code.count("\n")
        complexity_threshold = self.config.get("complexity_block_threshold", 2000)
        scope_prefix = self.config.get("scope_package_prefix", "")

        prompt = (
            f"scope_package_prefix: {scope_prefix}\n"
            f"complexity_threshold_lines: {complexity_threshold}\n"
            f"file_line_count: {loc}\n\n"
            f"```java\n{source_code[:8000]}\n```"
        )

        messages = [SystemMessage(content=self.system_prompt), HumanMessage(content=prompt)]
        response = self.llm.invoke(messages)
        state["bedrock_calls"] = state.get("bedrock_calls", 0) + 1

        try:
            result = loads_lenient(response.content)
        except (json.JSONDecodeError, AttributeError):
            result = {"verdict": "PASS", "findings": [], "reason": "parse error — continuing"}

        verdict = result.get("verdict", "PASS")
        findings = result.get("findings", [])
        if findings:
            file_status["guardrail_findings"] = list(file_status.get("guardrail_findings", [])) + findings

        if verdict == "BLOCK":
            file_status["status"] = "BLOCKED"
            file_status["error"] = result.get("reason", "Blocked by pre-flight check")
        else:
            file_status["status"] = "TRANSFORMING"

        return {**state, "current_file": file_status}
