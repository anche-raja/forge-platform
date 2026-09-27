import re
from typing import Dict, List, Tuple

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.config import ForgeConfig, bedrock_client_config, model_max_tokens
from forge.context.inject import context_block_for
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

# The rubric's response schema names each check with its maximum:
# ``"java_and_bom": <0-25>``. The loader validated these against the prose
# weights, so they are the numbers a "n/a" check is worth.
_CHECK_MAXIMUM = re.compile(r'"([A-Za-z_]\w*)"\s*:\s*<0-(\d+)>')
_NOT_APPLICABLE = frozenset({"n/a", "na", "n.a.", "not applicable"})


def response_check_maxima(review_prompt: str) -> Tuple[Tuple[str, int], ...]:
    """``((check name, maximum), ...)`` in the order the rubric's JSON schema declares them.

    The schema's own ``"score": <0-100>`` is the total, not a check, and is
    left out.
    """
    return tuple((name, int(cap)) for name, cap in _CHECK_MAXIMUM.findall(review_prompt) if name != "score")


def apply_not_applicable(result: dict, review_prompt: str) -> dict:
    """Score a check the reviewer marked ``"n/a"`` at its maximum, and recompute the total.

    A rubric says a check with nothing to examine earns full points, and a model
    asked to put a number on such a check puts 0 -- on AMS the aggregator and
    EAR poms went to a human that way, at scores of 0 and 5. So the model may
    write "n/a" and the arithmetic is done here, from the maxima the rubric's
    own schema declares. Without an "n/a" the model's score stands untouched;
    with one, every check is re-added: "n/a" at its maximum, a number clamped
    to its range, anything unreadable as 0.
    """
    checks = result.get("checks")
    if not isinstance(checks, dict):
        return result
    maxima = response_check_maxima(review_prompt)
    if not maxima:
        return result

    def not_applicable(value) -> bool:
        return value is None or (isinstance(value, str) and value.strip().lower() in _NOT_APPLICABLE)

    skipped = [name for name, _ in maxima if not_applicable(checks.get(name, 0))]
    if not skipped:
        return result
    rescored: Dict[str, int] = {}
    for name, cap in maxima:
        value = checks.get(name, 0)
        if not_applicable(value):
            rescored[name] = cap
        else:
            try:
                rescored[name] = max(0, min(int(value), cap))
            except (TypeError, ValueError):
                rescored[name] = 0
    out = dict(result)
    out["checks"] = rescored
    out["score"] = sum(rescored.values())
    out["feedback"] = (str(result.get("feedback") or "").rstrip()
                       + f" [n/a, scored at full points: {', '.join(skipped)}]").strip()
    return out


class JavaReviewer(BaseReviewer):
    def __init__(self, config: ForgeConfig):
        super().__init__(config)
        self._llms: Dict[str, ChatBedrockConverse] = {}
        # The default reviewer, built eagerly so a bad model id or region fails
        # where it always did: at construction, not on the first file.
        self.llm = self._llm_for(config.review_model)

    def _llm_for(self, model: str) -> ChatBedrockConverse:
        """One client per reviewer model; a tier that names its own gets its own."""
        if model not in self._llms:
            self._llms[model] = ChatBedrockConverse(
                model=model,
                region_name=self.config.aws_region,
                max_tokens=model_max_tokens(self.config, model),
                config=bedrock_client_config(self.config),
            )
        return self._llms[model]

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
        # A pack's tier may name its own reviewer (review_models in agents.yaml).
        model = self.config.review_model_for(str(getattr(spec, "tier", "") or ""))
        llm = self._llm_for(model)
        human = f"Review this transformed code:\n\n```\n{all_content}\n```"
        # The reviewer sees the same descriptors the transform saw, so "nothing
        # from the descriptors was dropped" is checkable rather than assumed.
        block, _ = context_block_for(state, self.config)
        if block:
            human += "\n\nThe descriptors the transform was given (check nothing was dropped):\n" + block
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
            response = llm.invoke([SystemMessage(content=spec.review_prompt), HumanMessage(content=prompt)])
            running["bedrock_calls"] = running.get("bedrock_calls", 0) + 1
            running["estimated_cost_usd"] = accrue(running, response, model,
                                                   self.config.get("model_pricing", {}))
            try:
                result = extract_json(response.content)
                if not isinstance(result, dict):
                    raise ValueError(f"reply is a {type(result).__name__}, expected an object")
                # A check with nothing to examine is worth its full points, and
                # the adding-up is done here, not by the model.
                result = apply_not_applicable(result, spec.review_prompt)
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
        file_status["review_model"] = model
        _log.info("Review score %s (%s) for %s", score, verdict, file_status["file_path"])

        return {**state, "current_file": file_status, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}
