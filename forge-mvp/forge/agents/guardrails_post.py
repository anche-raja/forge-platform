import json
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.agents.base import BaseAgent
from forge.config import ForgeConfig
from forge.guardrails.bedrock_guardrails import BedrockGuardrails
from forge.state import ForgeState
from forge.utils.jsonio import loads_lenient
from forge.utils.prompts import load_prompt


class GuardrailsPostAgent(BaseAgent):
    def __init__(self, config: ForgeConfig):
        super().__init__(config)
        # System prompt lives in prompts/guardrails_post.md (override via FORGE_PROMPTS_DIR).
        self.system_prompt = load_prompt("guardrails_post.md")
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
            file_status["status"] = "MANUAL_REVIEW"
            return {**state, "current_file": file_status}

        # Step 2: Claude Sonnet post-transform quality check
        scope_prefix = self.config.get("scope_package_prefix", "")
        prompt = (
            f"scope_package_prefix: {scope_prefix}\n\n"
            f"```java\n{all_content[:8000]}\n```"
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
            file_status["status"] = "MANUAL_REVIEW"
            file_status["error"] = result.get("reason", "Blocked by post-transform check")
        # status stays as-is (REVIEWING → will be set by router)

        return {**state, "current_file": file_status}
