from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.agents.base import BaseAgent
from forge.config import ForgeConfig
from forge.phases import get_phase
from forge.state import ForgeState
from forge.utils.cost import accrue
from forge.utils.llm_json import extract_json
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)



class JavaUpgradeAgent(BaseAgent):
    def __init__(self, config: ForgeConfig):
        super().__init__(config)
        self.llm = ChatBedrockConverse(
            model=config.transform_model,
            region_name=config.aws_region,
        )

    def run(self, state: ForgeState) -> ForgeState:
        file_status = dict(state["current_file"])
        file_path = file_status["file_path"]
        retry_count = file_status.get("retry_count", 0)

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source_code = f.read()
        except Exception as e:
            file_status["status"] = "MANUAL_REVIEW"
            file_status["error"] = f"Cannot read file: {e}"
            return {**state, "current_file": file_status}

        spec = get_phase(state.get("phase") or file_status.get("phase") or "java21")
        user_content = f"Transform this file:\nFile path: {file_path}\n\n```\n{source_code}\n```"

        if retry_count > 0:
            feedback = file_status.get("review_feedback", "")
            user_content += (
                f"\n\nPREVIOUS REVIEW FEEDBACK (retry {retry_count}):\n{feedback}\n"
                "Address all feedback points in this retry."
            )

        messages = [SystemMessage(content=spec.transform_prompt), HumanMessage(content=user_content)]
        response = self.llm.invoke(messages)
        bedrock_calls = state.get("bedrock_calls", 0) + 1
        cost = accrue(state, response, self.config.transform_model, self.config.get("model_pricing", {}))

        try:
            result = extract_json(response.content)
        except Exception as e:
            _log.warning("Transform output for %s was not valid JSON: %s", file_path, e)
            file_status["status"] = "MANUAL_REVIEW"
            file_status["error"] = f"Failed to parse transform output as JSON: {e}"
            return {**state, "current_file": file_status, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}

        file_status["transform_output"] = result
        # struts-spring6 reports XML configs it replaced with Java @Configuration.
        deleted = result.get("deleted_files") or []
        if isinstance(deleted, list) and deleted:
            file_status["deleted_files"] = [str(d) for d in deleted]
        file_status["transform_model"] = self.config.transform_model
        file_status["status"] = "REVIEWING"

        return {**state, "current_file": file_status, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}
