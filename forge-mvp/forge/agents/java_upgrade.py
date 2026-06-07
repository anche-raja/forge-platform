import json
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.agents.base import BaseAgent
from forge.config import ForgeConfig
from forge.rag.retriever import retrieve_context
from forge.state import ForgeState
from forge.utils.prompts import load_prompt
from forge.utils.jsonio import loads_lenient


class JavaUpgradeAgent(BaseAgent):
    def __init__(self, config: ForgeConfig):
        super().__init__(config)
        # System prompt lives in prompts/java_upgrade.md so it can be tuned
        # without changing code. Override the dir with FORGE_PROMPTS_DIR.
        self.system_prompt = load_prompt("java_upgrade.md")
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

        user_content = f"Transform this Java file:\nFile path: {file_path}\n\n```java\n{source_code}\n```"

        # Prepend enterprise-standards context (stable prefix → helps prompt caching).
        ctx = retrieve_context(state, self.config, role="transform")
        if ctx:
            user_content = f"{ctx}\n\n---\n\n{user_content}"

        if retry_count > 0:
            feedback = file_status.get("review_feedback", "")
            user_content += (
                f"\n\nPREVIOUS REVIEW FEEDBACK (retry {retry_count}):\n{feedback}\n"
                "Address all feedback points in this retry."
            )

        messages = [SystemMessage(content=self.system_prompt), HumanMessage(content=user_content)]
        response = self.llm.invoke(messages)
        state["bedrock_calls"] = state.get("bedrock_calls", 0) + 1

        try:
            result = loads_lenient(response.content)
        except (json.JSONDecodeError, AttributeError):
            file_status["status"] = "MANUAL_REVIEW"
            file_status["error"] = "Failed to parse transform output as JSON"
            return {**state, "current_file": file_status}

        file_status["transform_output"] = result
        file_status["transform_model"] = self.config.transform_model
        file_status["status"] = "REVIEWING"

        return {**state, "current_file": file_status}
