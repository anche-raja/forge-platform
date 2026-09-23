"""The reviewer for generated tests — a different model from the one that wrote them.

The same cross-validation the migration uses, for the same reason: the model
that produced the code is the worst judge of whether it is any good. It returns
an integer; ``route_review`` in the graph picks the branch. Its ``verdict``
string is recorded and never routed on.

What it is *not* asked is anything mechanical. JUnit 4 imports, javax.*, a
missing @Test and the class name were settled by ``forge/testgen/checks.py``
before this call, and a unit that failed one of them never reaches it.
"""

from pathlib import Path
from typing import Optional

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.config import ForgeConfig, bedrock_client_config, model_max_tokens
from forge.phases import get_testgen_spec
from forge.review.base_reviewer import BaseReviewer
from forge.testgen.settings import TestGenSettings
from forge.testgen.state import TestGenState
from forge.utils.cost import accrue
from forge.utils.llm_json import extract_json
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

# The class under test is quoted back to the reviewer so "never invent API" is
# checkable rather than assumed — the same reason the migration reviewer is
# given the descriptors the transform saw.
_MAX_SOURCE_CHARS = 40_000


class TestReviewer(BaseReviewer):
    def __init__(self, config: ForgeConfig, settings: Optional[TestGenSettings] = None):
        super().__init__(config)
        self.settings = settings or TestGenSettings.from_config(config)
        self.spec = get_testgen_spec(self.settings.style)
        self.llm = ChatBedrockConverse(
            model=self.settings.review_model or config.review_model,
            region_name=config.aws_region,
            max_tokens=model_max_tokens(config, self.settings.review_model or config.review_model),
            config=bedrock_client_config(config),
        )

    def review(self, state: TestGenState) -> TestGenState:
        unit = dict(state["current_unit"])
        files = ((unit.get("test_output") or {}).get("files") or {})

        tests = "\n\n".join(f"// FILE: {path}\n{content}" for path, content in sorted(files.items()))
        if not tests.strip():
            unit["review_score"] = 0
            unit["review_verdict"] = "MANUAL"
            unit["review_feedback"] = "No generated test to review"
            return {**state, "current_unit": unit}

        try:
            source = Path(unit["file_path"]).read_text(encoding="utf-8", errors="replace")[:_MAX_SOURCE_CHARS]
        except OSError:
            source = "(the class under test could not be re-read)"

        human = (
            f"Class under test ({unit.get('kind', 'plain')}):\n```java\n{source}\n```\n\n"
            f"Generated test:\n```java\n{tests}\n```"
        )
        messages = [SystemMessage(content=self.spec.review_prompt), HumanMessage(content=human)]
        response = self.llm.invoke(messages)
        bedrock_calls = state.get("bedrock_calls", 0) + 1
        cost = accrue(state, response, self.settings.review_model or self.config.review_model,
                      self.config.get("model_pricing", {}))

        try:
            result = extract_json(response.content)
        except Exception as e:
            unit["review_score"] = 0
            unit["review_verdict"] = "MANUAL"
            unit["review_feedback"] = f"Failed to parse reviewer response: {e}"
            _log.warning("Test reviewer response was not valid JSON: %s", e)
            return {**state, "current_unit": unit, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}

        score = int(result.get("score", 0) or 0)
        if score >= self.settings.pass_threshold:
            verdict = "PASS"
        elif score >= self.settings.retry_threshold:
            verdict = "RETRY"
        else:
            verdict = "MANUAL"

        unit["review_score"] = score
        unit["review_verdict"] = verdict
        unit["review_feedback"] = str(result.get("feedback", "") or "")
        unit["review_model"] = self.settings.review_model or self.config.review_model
        _log.info("Test review score %s (%s) for %s", score, verdict, unit["test_rel_path"])
        return {**state, "current_unit": unit, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}
