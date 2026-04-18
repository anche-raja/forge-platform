import json
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.config import ForgeConfig
from forge.review.base_reviewer import BaseReviewer
from forge.state import ForgeState

_SYSTEM = """You are a Java migration code reviewer. Score the transformed Java code on 5 checks (total 100 points).

Check 1 — Namespace completeness (20 pts):
Zero javax.* imports remain. All replaced with jakarta.*. Full 20 if clean, 0 if any javax.* found.

Check 2 — Deprecated API removal (20 pts):
No Thread.stop(), no finalize() bodies, no Calendar, no SimpleDateFormat. Partial credit allowed.

Check 3 — Date/Time modernisation (25 pts):
Instant.now() replaces new Date(), LocalDateTime replaces Calendar, DateTimeFormatter replaces SimpleDateFormat. Partial credit allowed.

Check 4 — Safe var inference (20 pts):
var used only where type is obvious from RHS. Never on parameters or fields. Partial credit allowed.

Check 5 — No regressions (15 pts):
Original structure preserved. Error handling intact. Null checks preserved. No logic changes.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON — no markdown, no explanation:
{
  "score": <0-100>,
  "verdict": "PASS"|"RETRY"|"MANUAL",
  "feedback": "<specific actionable issues for retry, or empty string if PASS>",
  "checks": {
    "namespace": <0-20>,
    "deprecated": <0-20>,
    "datetime": <0-25>,
    "var_inference": <0-20>,
    "no_regressions": <0-15>
  }
}"""


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

        messages = [
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=f"Review this transformed Java code:\n\n```java\n{all_content[:8000]}\n```"),
        ]
        response = self.llm.invoke(messages)
        state["bedrock_calls"] = state.get("bedrock_calls", 0) + 1

        try:
            result = json.loads(response.content)
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
