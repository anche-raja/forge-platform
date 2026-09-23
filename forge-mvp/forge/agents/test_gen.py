"""The Test-Gen agent: one class in, one JUnit 5 test class out.

It is the migration's fourth agent and it works the same way as the first:
the prompt lives in ``forge/phases.py`` beside the rubric that grades it, the
model is told nothing it cannot verify, and everything mechanical — which
classes, which destination, which invariants — is decided in code around it.
"""

from pathlib import Path
from typing import Dict, Optional

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.agents.base import BaseAgent
from forge.config import ForgeConfig, bedrock_client_config, model_max_tokens
from forge.phases import get_testgen_spec
from forge.testgen.context import available_test_libraries, build_source_index, render_context
from forge.testgen.settings import TestGenSettings
from forge.testgen.state import TestGenState
from forge.testgen.targets import TestTarget, target_from_unit
from forge.utils.cost import accrue
from forge.utils.llm_json import extract_json
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)


class TestGenAgent(BaseAgent):
    def __init__(self, config: ForgeConfig, settings: Optional[TestGenSettings] = None):
        super().__init__(config)
        self.settings = settings or TestGenSettings.from_config(config)
        self.spec = get_testgen_spec(self.settings.style)
        self.llm = ChatBedrockConverse(
            model=self.settings.model or config.transform_model,
            region_name=config.aws_region,
            max_tokens=model_max_tokens(config, self.settings.model or config.transform_model),
            config=bedrock_client_config(config),
        )
        # Built once per run and cached on the instance, like the context
        # extractors: it is derived from the tree, and no unit changes it.
        self._index: Optional[Dict[str, str]] = None
        self._libraries = None

    # ─── context ─────────────────────────────────────────────────────────────

    def _context(self, state: TestGenState, target: TestTarget, source: str) -> str:
        if self._index is None:
            self._index = build_source_index(state["output_dir"], state["source_dir"])
        if self._libraries is None:
            self._libraries = available_test_libraries(state["source_dir"], state["output_dir"])
        return render_context(target, source, self._index, self._libraries, self.settings.context_max_chars)

    # ─── the call ────────────────────────────────────────────────────────────

    def run(self, state: TestGenState) -> TestGenState:
        unit = dict(state["current_unit"])
        target = target_from_unit(unit)

        try:
            source = Path(unit["file_path"]).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            unit["status"] = "BLOCKED"
            unit["error"] = f"Cannot read the class under test: {e}"
            return {**state, "current_unit": unit}

        user = (
            "Write the unit tests for this class.\n"
            f"Class under test: {target.package + '.' if target.package else ''}{target.type_name}\n"
            f"Kind: {target.kind}\n"
            f"Test class to produce: {target.test_fqcn}\n"
            f"Destination path: {target.test_rel_path}\n\n"
            f"```java\n{source}\n```\n\n"
            + self._context(state, target, source)
        )

        retry_count = unit.get("retry_count") or 0
        if retry_count > 0:
            user += "\n\n" + _feedback_block(unit, retry_count)

        messages = [SystemMessage(content=self.spec.generate_prompt), HumanMessage(content=user)]
        response = self.llm.invoke(messages)
        bedrock_calls = state.get("bedrock_calls", 0) + 1
        cost = accrue(state, response, self.settings.model or self.config.transform_model,
                      self.config.get("model_pricing", {}))

        try:
            result = extract_json(response.content)
        except Exception as e:
            _log.warning("Test generator output for %s was not valid JSON: %s", unit["rel_path"], e)
            unit["status"] = "HELD"
            unit["hold_reason"] = f"generator output was not valid JSON: {e}"
            unit["error"] = str(e)
            return {**state, "current_unit": unit, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}

        files = result.get("files") if isinstance(result, dict) else None
        unit["test_output"] = {"files": {str(k): str(v) for k, v in (files or {}).items()}}
        unit["cases"] = _as_dicts(result.get("cases"))
        unit["untested"] = _as_dicts(result.get("untested"))
        unit["dependencies"] = [str(d) for d in (result.get("dependencies") or []) if str(d).strip()]
        unit["notes"] = [str(n) for n in (result.get("notes") or []) if str(n).strip()]
        unit["generate_model"] = self.settings.model or self.config.transform_model
        unit["status"] = "REVIEWING"
        return {**state, "current_unit": unit, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}


# ─── helpers ──────────────────────────────────────────────────────────────────

def _feedback_block(unit: dict, retry_count: int) -> str:
    """Why the previous attempt was rejected, mechanical failures first.

    Order is the point: a mechanical failure is not a matter of opinion, and
    listing it above the reviewer's prose is what stops the retry trading one
    for the other.
    """
    parts = [f"PREVIOUS ATTEMPT FEEDBACK (retry {retry_count}) — fix every point:"]
    failures = unit.get("check_failures") or []
    if failures:
        parts.append("Mechanical failures (these are not opinions; all of them must be fixed):")
        parts += [f"- {f}" for f in failures]
    log = unit.get("test_output_log")
    if unit.get("test_verdict") == "FAIL" and log:
        parts.append("The test was executed and did not pass:")
        parts.append(log)
        parts.append("Fix the test. Do NOT change the class under test, and do not weaken an "
                     "assertion to make it pass — if the production code is wrong, say so in \"notes\".")
    feedback = unit.get("review_feedback")
    if feedback:
        parts.append("Reviewer feedback:")
        parts.append(str(feedback))
    return "\n".join(parts)


def _as_dicts(value) -> list:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]
