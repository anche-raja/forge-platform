from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.config import ForgeConfig
from forge.phases import get_phase
from forge.review.base_reviewer import BaseReviewer
from forge.state import ForgeState
from forge.utils.cost import accrue
from forge.utils.llm_json import extract_json
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)



class JavaReviewer(BaseReviewer):
    def __init__(self, config: ForgeConfig):
        super().__init__(config)
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

        spec = get_phase(state.get("phase") or file_status.get("phase") or "java21")
        messages = [
            SystemMessage(content=spec.review_prompt),
            HumanMessage(content=f"Review this transformed code:\n\n```\n{all_content}\n```"),
        ]
        response = self.llm.invoke(messages)
        bedrock_calls = state.get("bedrock_calls", 0) + 1
        cost = accrue(state, response, self.config.review_model, self.config.get("model_pricing", {}))

        try:
            result = extract_json(response.content)
        except Exception as e:
            file_status["review_score"] = 0
            file_status["review_verdict"] = "MANUAL"
            file_status["review_feedback"] = f"Failed to parse reviewer response: {e}"
            _log.warning("Reviewer response was not valid JSON: %s", e)
            return {**state, "current_file": file_status, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}

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
        _log.info("Review score %s (%s) for %s", score, verdict, file_status["file_path"])

        return {**state, "current_file": file_status, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}
