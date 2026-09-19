"""The test-generation graph — one class under test per invocation.

    testgen_pre -> generate -> static_checks -> review -> write_tests -> run_tests -> finish
                                    |             |                          |
                                    +-> increment_retry -> generate <--------+
                                    +-> hold (staged, a human decides) <-----+

Orchestration is code here too. No node returns a node name, the retry budget
is an integer, and the only thing a model contributes to routing is the
reviewer's score — which ``route_review`` compares against a threshold from
``agents.yaml``.

Two edges are worth reading twice. ``static_checks`` sits *before* the review
so a test with JUnit 4 imports never costs a review call; and a test that runs
and fails is taken back out of the output tree before the retry, because a
broken test left in ``src/test/java`` breaks every build that follows.
"""

from pathlib import Path
from typing import Optional

from langgraph.graph import END, StateGraph

from forge.agents.test_gen import TestGenAgent
from forge.config import ForgeConfig
from forge.review.test_reviewer import TestReviewer
from forge.state_store.dynamodb import DynamoDBSaver
from forge.testgen.checks import check_output
from forge.testgen.runner import TestRunner
from forge.testgen.settings import TestGenSettings
from forge.testgen.targets import target_from_unit
from forge.testgen.state import TestGenState
from forge.testgen.writer import remove_written, stage_test_files, write_test_files
from forge.utils.secret_scan import find_secrets
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)


def build_testgen_graph(config: ForgeConfig, settings: Optional[TestGenSettings] = None,
                        runner: Optional[TestRunner] = None):
    settings = settings or TestGenSettings.from_config(config)
    generator = TestGenAgent(config, settings)
    reviewer = TestReviewer(config, settings)
    scan_cfg = config.get("secret_scan", {}) or {}

    # ─── nodes ────────────────────────────────────────────────────────────────

    def testgen_pre(state: TestGenState) -> TestGenState:
        """Local gates only. Nothing here is a model call, and nothing leaves the machine."""
        unit = dict(state["current_unit"])
        try:
            source = Path(unit["file_path"]).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            unit["status"] = "BLOCKED"
            unit["error"] = f"Cannot read the class under test: {e}"
            return {**state, "current_unit": unit}

        # The secret gate, ahead of every remote call — the same control the
        # migration applies, for the same reason. This code came out of the
        # pipeline, but a run over an output directory written by someone else
        # (or edited since) has proved nothing about it.
        if scan_cfg.get("enabled", True):
            findings = find_secrets(source, scan_cfg)
            if findings:
                described = [f.describe() for f in findings]
                unit["scan_findings"] = described
                if scan_cfg.get("action", "block") == "block":
                    _log.info("Secret gate held %s, nothing sent", unit["rel_path"])
                    unit["status"] = "BLOCKED"
                    unit["error"] = "Local secret scan: " + "; ".join(described)
                    return {**state, "current_unit": unit}

        # Size, as an integer comparison. A class this large is not a unit.
        if settings.max_source_chars and len(source) > settings.max_source_chars:
            unit["status"] = "BLOCKED"
            unit["error"] = (f"{len(source)} characters exceeds test_generation.max_source_chars "
                             f"of {settings.max_source_chars}")
            return {**state, "current_unit": unit}

        unit["status"] = "GENERATING"
        return {**state, "current_unit": unit}

    def generate(state: TestGenState) -> TestGenState:
        return generator.run(state)

    def static_checks(state: TestGenState) -> TestGenState:
        """The mechanical invariants, in code. Cheaper and surer than asking."""
        unit = dict(state["current_unit"])
        target = target_from_unit(unit)
        files = ((unit.get("test_output") or {}).get("files") or {})
        failures = check_output(files, target)

        # A generated test is a file this pipeline is about to write to disk. A
        # credential invented into a fixture would be committed with it.
        if scan_cfg.get("enabled", True):
            for path, content in sorted(files.items()):
                for finding in find_secrets(content, scan_cfg):
                    failures.append(f"{path}: generated test contains {finding.describe()}")

        unit["check_failures"] = failures
        return {**state, "current_unit": unit}

    def review(state: TestGenState) -> TestGenState:
        return reviewer.review(state)

    def write_tests(state: TestGenState) -> TestGenState:
        unit = dict(state["current_unit"])
        files = ((unit.get("test_output") or {}).get("files") or {})
        if state.get("dry_run"):
            unit["status"] = "GENERATED"
            unit["written_paths"] = []
            return {**state, "current_unit": unit}
        unit["written_paths"] = write_test_files(files, target_from_unit(unit), state["output_dir"])
        unit["status"] = "GENERATED"
        return {**state, "current_unit": unit}

    def run_tests(state: TestGenState) -> TestGenState:
        unit = dict(state["current_unit"])
        if runner is None or state.get("dry_run"):
            unit["test_verdict"] = "SKIPPED"
            unit["test_output_log"] = "dry run — nothing was written to run" if state.get("dry_run") else ""
            return {**state, "current_unit": unit}

        result = runner.run(target_from_unit(unit), unit.get("written_paths") or [])
        unit["test_verdict"] = result["verdict"]
        unit["test_output_log"] = result["output"]
        if result["verdict"] == "FAIL":
            # Out of the tree before anything else happens: a failing test in
            # src/test/java breaks every build that follows, and whether it is
            # the test or the migrated code that is wrong is a human's call.
            remove_written(unit.get("written_paths") or [], state["output_dir"])
            unit["written_paths"] = []
        return {**state, "current_unit": unit}

    def hold(state: TestGenState) -> TestGenState:
        """Stage instead of write — the test exists, but not in the build."""
        unit = dict(state["current_unit"])
        files = ((unit.get("test_output") or {}).get("files") or {})
        if files and not state.get("dry_run"):
            unit["held_paths"] = stage_test_files(files, target_from_unit(unit), state["output_dir"])
        unit["status"] = "HELD"
        unit["hold_reason"] = unit.get("hold_reason") or _hold_reason(unit, settings)
        _log.info("Held generated test for %s: %s", unit["rel_path"], unit["hold_reason"])
        return {**state, "current_unit": unit}

    def blocked(state: TestGenState) -> TestGenState:
        unit = dict(state["current_unit"])
        unit["status"] = "BLOCKED"
        return {**state, "current_unit": unit}

    def increment_retry(state: TestGenState) -> TestGenState:
        unit = dict(state["current_unit"])
        unit["retry_count"] = (unit.get("retry_count") or 0) + 1
        return {**state, "current_unit": unit}

    def finish(state: TestGenState) -> TestGenState:
        unit = state["current_unit"]
        status = unit.get("status")
        new_state = dict(state)
        new_state["units_processed"] = state.get("units_processed", 0) + 1
        if status == "GENERATED":
            new_state["units_generated"] = state.get("units_generated", 0) + 1
        elif status == "HELD":
            new_state["units_held"] = state.get("units_held", 0) + 1
        elif status == "BLOCKED":
            new_state["units_blocked"] = state.get("units_blocked", 0) + 1
        return new_state

    # ─── routing ──────────────────────────────────────────────────────────────

    def route_pre(state: TestGenState) -> str:
        return "blocked" if state["current_unit"].get("status") == "BLOCKED" else "generate"

    def route_generate(state: TestGenState) -> str:
        return "hold" if state["current_unit"].get("status") == "HELD" else "static_checks"

    def route_checks(state: TestGenState) -> str:
        unit = state["current_unit"]
        if not unit.get("check_failures"):
            return "review"
        if (unit.get("retry_count") or 0) < settings.max_retries:
            return "increment_retry"
        return "hold"

    def route_review(state: TestGenState) -> str:
        unit = state["current_unit"]
        score = unit.get("review_score") or 0
        if score >= settings.pass_threshold:
            return "write_tests"
        if score >= settings.retry_threshold and (unit.get("retry_count") or 0) < settings.max_retries:
            return "increment_retry"
        return "hold"

    def route_run(state: TestGenState) -> str:
        unit = state["current_unit"]
        if unit.get("test_verdict") != "FAIL":
            return "finish"
        if (unit.get("retry_count") or 0) < settings.max_retries:
            return "increment_retry"
        return "hold"

    # ─── graph ────────────────────────────────────────────────────────────────

    graph = StateGraph(TestGenState)
    graph.add_node("testgen_pre", testgen_pre)
    graph.add_node("generate", generate)
    graph.add_node("static_checks", static_checks)
    graph.add_node("review", review)
    graph.add_node("write_tests", write_tests)
    graph.add_node("run_tests", run_tests)
    graph.add_node("hold", hold)
    graph.add_node("blocked", blocked)
    graph.add_node("increment_retry", increment_retry)
    graph.add_node("finish", finish)

    graph.set_entry_point("testgen_pre")
    graph.add_conditional_edges("testgen_pre", route_pre, {"blocked": "blocked", "generate": "generate"})
    graph.add_conditional_edges("generate", route_generate, {"hold": "hold", "static_checks": "static_checks"})
    graph.add_conditional_edges("static_checks", route_checks, {
        "review": "review", "increment_retry": "increment_retry", "hold": "hold",
    })
    graph.add_conditional_edges("review", route_review, {
        "write_tests": "write_tests", "increment_retry": "increment_retry", "hold": "hold",
    })
    graph.add_edge("increment_retry", "generate")
    graph.add_edge("write_tests", "run_tests")
    graph.add_conditional_edges("run_tests", route_run, {
        "finish": "finish", "increment_retry": "increment_retry", "hold": "hold",
    })
    graph.add_edge("hold", "finish")
    graph.add_edge("blocked", "finish")
    graph.add_edge("finish", END)

    return graph.compile(checkpointer=DynamoDBSaver(config))


def _hold_reason(unit: dict, settings: TestGenSettings) -> str:
    if unit.get("check_failures"):
        return "failed mechanical checks after "f"{unit.get('retry_count') or 0} retry(s): " + \
               "; ".join(unit["check_failures"][:3])
    if unit.get("test_verdict") == "FAIL":
        return "the generated test ran and failed"
    score = unit.get("review_score")
    if score is not None:
        return f"review score {score} is below the pass threshold of {settings.pass_threshold}"
    return "held for review"
