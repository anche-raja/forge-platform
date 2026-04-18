import json
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.agents.base import BaseAgent
from forge.config import ForgeConfig
from forge.state import ForgeState

_SYSTEM = """You are a Java migration expert. Transform the provided Java source code by applying these rules in order:

Rule 1 — Namespace migration (HIGHEST PRIORITY, zero tolerance):
- javax.servlet.*     → jakarta.servlet.*
- javax.persistence.* → jakarta.persistence.*
- javax.validation.*  → jakarta.validation.*
- javax.transaction.* → jakarta.transaction.*
Every single javax.* import MUST become jakarta.*. Zero javax.* allowed in output.

Rule 2 — Deprecated API replacement:
- Thread.stop()                → throw new InterruptedException("Thread interrupted")
- finalize() method            → add comment: // DEPRECATED: replace with Cleaner API
- StringBuffer in loops        → StringBuilder
- System.runFinalizersOnExit() → remove the call entirely

Rule 3 — Date/Time modernisation:
- new Date() for current time  → Instant.now()
- Calendar usage               → LocalDateTime
- SimpleDateFormat             → DateTimeFormatter
- java.sql.Date                → java.time.LocalDate

Rule 4 — Local variable inference (conservative):
- Apply `var` only where the type is completely obvious from the right-hand side
- Never apply `var` to parameters or fields

Rule 5 — Flag but do not change:
- sun.misc.Unsafe usage        → add comment: // MANUAL: review Unsafe usage
- Illegal reflective access    → add comment: // MANUAL: review reflective access

Respond ONLY with valid JSON — no markdown fences, no explanation:
{
  "files": {"<original_file_path>": "<full_transformed_content>"},
  "manual_flags": [{"file": "<path>", "line": <n>, "reason": "<why>"}]
}"""


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

        user_content = f"Transform this Java file:\nFile path: {file_path}\n\n```java\n{source_code}\n```"

        if retry_count > 0:
            feedback = file_status.get("review_feedback", "")
            user_content += (
                f"\n\nPREVIOUS REVIEW FEEDBACK (retry {retry_count}):\n{feedback}\n"
                "Address all feedback points in this retry."
            )

        messages = [SystemMessage(content=_SYSTEM), HumanMessage(content=user_content)]
        response = self.llm.invoke(messages)
        state["bedrock_calls"] = state.get("bedrock_calls", 0) + 1

        try:
            result = json.loads(response.content)
        except (json.JSONDecodeError, AttributeError):
            file_status["status"] = "MANUAL_REVIEW"
            file_status["error"] = "Failed to parse transform output as JSON"
            return {**state, "current_file": file_status}

        file_status["transform_output"] = result
        file_status["transform_model"] = self.config.transform_model
        file_status["status"] = "REVIEWING"

        return {**state, "current_file": file_status}
