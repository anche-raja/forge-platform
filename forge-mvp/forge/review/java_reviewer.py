import json
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.config import ForgeConfig
from forge.rag.retriever import retrieve_context
from forge.review.base_reviewer import BaseReviewer
from forge.state import ForgeState
from forge.utils.jsonio import loads_lenient
from forge.utils.prompts import load_prompt


class JavaReviewer(BaseReviewer):
    def __init__(self, config: ForgeConfig):
        super().__init__(config)
        # System prompt lives in prompts/java_reviewer.md (override via FORGE_PROMPTS_DIR).
        self.system_prompt = load_prompt("java_reviewer.md")
        self.llm = ChatBedrockConverse(
            model=config.review_model,
            region_name=config.aws_region,
        )

    def review(self, state: ForgeState) -> ForgeState:
        file_status = dict(state["current_file"])
        transform_output = file_status.get("transform_output") or {}

        all_content = "\n\n".join(
            f"// FILE: {path}\n{content}"
            for path, content in transform_output.get("files", {}).items()
        )

        if not all_content:
            file_status["review_score"] = 0
            file_status["review_verdict"] = "MANUAL"
            file_status["review_feedback"] = "No transformed content to review"
            return {**state, "current_file": file_status}

        review_content = f"Review this transformed Java code:\n\n```java\n{all_content[:8000]}\n```"
        ctx = retrieve_context(state, self.config, role="review")
        if ctx:
            review_content = f"{ctx}\n\n---\n\n{review_content}"

        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=review_content),
        ]
        response = self.llm.invoke(messages)
        state["bedrock_calls"] = state.get("bedrock_calls", 0) + 1

        try:
            result = loads_lenient(response.content)
        except (json.JSONDecodeError, AttributeError):
            file_status["review_score"] = 0
            file_status["review_verdict"] = "MANUAL"
            file_status["review_feedback"] = "Failed to parse reviewer response"
            return {**state, "current_file": file_status}

        score = int(result.get("score", 0))
        pass_threshold = self.config.get("pass_threshold", 80)
        retry_threshold = self.config.get("retry_threshold", 50)

        if score >= pass_threshold:
            verdict = "PASS"
        elif score >= retry_threshold:
            verdict = "RETRY"
        else:
            verdict = "MANUAL"

        file_status["review_score"] = score
        file_status["review_verdict"] = verdict
        file_status["review_feedback"] = result.get("feedback", "")
        file_status["review_model"] = self.config.review_model

        return {**state, "current_file": file_status}
