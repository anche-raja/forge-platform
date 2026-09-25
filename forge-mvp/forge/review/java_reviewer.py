from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.config import ForgeConfig, bedrock_client_config, model_max_tokens
from forge.context.inject import context_block_for, coordinates_block, decisions_block
from forge.phases import get_phase
from forge.review.base_reviewer import BaseReviewer
from forge.state import ForgeState
from forge.utils.cost import accrue
from forge.utils.llm_json import extract_json
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

# How many times an unreadable review is asked for again before it scores 0.
_PARSE_RETRIES = 1
_UNREADABLE = "Failed to parse reviewer response"


class JavaReviewer(BaseReviewer):
    def __init__(self, config: ForgeConfig):
        super().__init__(config)
        self.llm = ChatBedrockConverse(
            model=config.review_model,
            region_name=config.aws_region,
            max_tokens=model_max_tokens(config, config.review_model),
            config=bedrock_client_config(config),
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
        human = f"Review this transformed code:\n\n```\n{all_content}\n```"
        # The reviewer sees the same descriptors the transform saw, so "nothing
        # from the descriptors was dropped" is checkable rather than assumed.
        block, _ = context_block_for(state, self.config)
        if block:
            human += "\n\nThe descriptors the transform was given (check nothing was dropped):\n" + block
        # Graded against the same decisions the transform was told, or a
        # Tomcat-correct answer is scored as a Liberty-incomplete one.
        decisions = decisions_block(state, self.config)
        if decisions:
            human += "\n\n" + decisions
        # Check 4 grades "exactly the supplied lists": the reviewer needs the lists.
        coordinates = coordinates_block(state, self.config)
        if coordinates:
            human += "\n\n" + coordinates
        # A reply that cannot be read says nothing about the migration, so it is
        # asked for again before it counts against the file (issue #19): one
        # unquoted key sent a correctly migrated test class to a human at
        # "score 0". The second ask carries the parse error.
        running = dict(state)
        score, result, error = None, None, None
        for attempt in range(1 + _PARSE_RETRIES):
            prompt = human if attempt == 0 else (
                f"{human}\n\nYour previous reply could not be read ({error}). Respond with only the "
                "single JSON object the instructions specify: double-quoted keys, no markdown, no other text."
            )
            response = self.llm.invoke([SystemMessage(content=spec.review_prompt), HumanMessage(content=prompt)])
            running["bedrock_calls"] = running.get("bedrock_calls", 0) + 1
            running["estimated_cost_usd"] = accrue(running, response, self.config.review_model,
                                                   self.config.get("model_pricing", {}))
            try:
                result = extract_json(response.content)
                if not isinstance(result, dict):
                    raise ValueError(f"reply is a {type(result).__name__}, expected an object")
                # A score that is not a number is as unreadable as broken JSON,
                # and int() raising here used to end the whole run.
                score = int(result.get("score", 0))
                break
            except Exception as e:
                error = e
                _log.warning("Reviewer response for %s could not be read (attempt %d): %s",
                             file_status["file_path"], attempt + 1, e)
        bedrock_calls, cost = running["bedrock_calls"], running["estimated_cost_usd"]

        # This review's verdict replaces the last one's, so a unit whose earlier
        # review was unreadable does not carry that error forward.
        if str(file_status.get("error") or "").startswith(_UNREADABLE):
            file_status["error"] = None
        if score is None:
            reason = f"{_UNREADABLE} after {1 + _PARSE_RETRIES} attempts: {error}"
            file_status["review_score"] = 0
            file_status["review_verdict"] = "MANUAL"
            file_status["review_feedback"] = reason
            file_status["error"] = reason
            return {**state, "current_file": file_status, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}

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
