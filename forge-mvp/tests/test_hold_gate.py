"""The hold gate: risk_ceiling decides which units a human sees before they are written."""

import contextlib
from pathlib import Path
from unittest.mock import patch

import pytest

from forge.utils.file_writer import STAGING_DIR, discard_staged, promote_staged, staging_root, write_files
from tests.conftest import llm_reply, make_state, write_config
from tests.test_phase0_closeout import _graph_mocks

HIGH_RISK = "package com.corp;\npublic class WebSecurityConfig extends WebSecurityConfigurerAdapter {}\n"
LOW_RISK = "package com.corp;\npublic class Util {}\n"
MIGRATED = "package com.corp;\n// migrated\n"


def _run_graph(tmp_path, source: str, *, decisions=None, build=False, name="Foo.java"):
    """One file through the real graph with every AWS touchpoint mocked."""
    src = tmp_path / "proj/src/main/java/com/corp" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(source, encoding="utf-8")
    overrides = {}
    if decisions is not None:
        overrides["decisions"] = decisions
    if build:
        overrides["build_verification"] = {"enabled": True, "mode": "javac", "command": "", "classpath": "", "timeout_seconds": 60}
    config = write_config(tmp_path, **overrides)

    with contextlib.ExitStack() as stack:
        up = _graph_mocks(stack, str(src), review_score=95)
        up.return_value.invoke.return_value = llm_reply({"files": {str(src): MIGRATED}, "manual_flags": []})
        verifier = stack.enter_context(patch("forge.graph.BuildVerifier"))
        verifier.return_value.verify.return_value = {"verdict": "PASS", "output": "", "command": ""}
        from forge.graph import build_graph

        app = build_graph(config)
        state = make_state(str(src), tmp_path / "proj", phase="javax-to-jakarta", dry_run=False)
        state["output_dir"] = str(tmp_path / "out")
        result = app.invoke(state, config={"configurable": {"thread_id": str(src)}})
    return result, verifier, tmp_path / "out"


# ─── routing ──────────────────────────────────────────────────────────────────

def test_high_risk_under_review_high_is_held_and_staged(tmp_path):
    result, _, out = _run_graph(tmp_path, HIGH_RISK, name="WebSecurityConfig.java")
    fs = result["current_file"]
    assert fs["status"] == "HELD" and fs["risk_tier"] == "HIGH"
    assert fs["hold_reason"] == "risk_ceiling=review-high, risk_tier=HIGH"
    staged = out / STAGING_DIR / "src/main/java/com/corp/WebSecurityConfig.java"
    assert fs["held_paths"] == [str(staged)] and staged.read_text(encoding="utf-8") == MIGRATED
    assert not list((out).rglob("*.java")) or all(STAGING_DIR in str(p) for p in out.rglob("*.java")), \
        "nothing lands in the migrated tree itself"
    assert result["files_held"] == 1 and result["files_passed"] == 0


def test_held_unit_never_runs_build_verification(tmp_path):
    """Nothing was written to output, so there is nothing to compile."""
    result, verifier, _ = _run_graph(tmp_path, HIGH_RISK, build=True, name="WebSecurityConfig.java")
    assert result["current_file"]["status"] == "HELD"
    verifier.return_value.verify.assert_not_called()
    assert result["current_file"]["build_verdict"] is None


def test_auto_ceiling_writes_a_high_risk_unit(tmp_path):
    result, verifier, out = _run_graph(tmp_path, HIGH_RISK, decisions={"risk_ceiling": "auto"},
                                       build=True, name="WebSecurityConfig.java")
    fs = result["current_file"]
    assert fs["status"] == "DONE" and fs["held_paths"] == []
    assert (out / "src/main/java/com/corp/WebSecurityConfig.java").exists()
    assert not (out / STAGING_DIR).exists()
    verifier.return_value.verify.assert_called_once()


def test_review_all_holds_even_a_low_risk_unit(tmp_path):
    result, _, out = _run_graph(tmp_path, LOW_RISK, decisions={"risk_ceiling": "review-all"}, name="Util.java")
    fs = result["current_file"]
    assert fs["status"] == "HELD" and fs["risk_tier"] == "LOW"
    assert (out / STAGING_DIR / "src/main/java/com/corp/Util.java").exists()


def test_low_risk_under_the_default_ceiling_is_written(tmp_path):
    result, _, out = _run_graph(tmp_path, LOW_RISK, name="Util.java")
    assert result["current_file"]["status"] == "DONE"
    assert (out / "src/main/java/com/corp/Util.java").exists()


def test_unknown_ceiling_falls_back_to_review_high(tmp_path, caplog):
    result, _, _ = _run_graph(tmp_path, HIGH_RISK, decisions={"risk_ceiling": "yolo"}, name="WebSecurityConfig.java")
    assert result["current_file"]["status"] == "HELD"
    assert "Unknown risk_ceiling 'yolo'" in caplog.text


def test_manual_review_route_is_unchanged_by_the_gate(tmp_path):
    src = tmp_path / "proj/src/main/java/com/corp/WebSecurityConfig.java"
    src.parent.mkdir(parents=True)
    src.write_text(HIGH_RISK, encoding="utf-8")
    config = write_config(tmp_path)
    with contextlib.ExitStack() as stack:
        _graph_mocks(stack, str(src), review_score=20)   # MANUAL, before the gate is consulted
        from forge.graph import build_graph

        state = make_state(str(src), tmp_path / "proj", phase="javax-to-jakarta", dry_run=False)
        state["output_dir"] = str(tmp_path / "out")
        result = build_graph(config).invoke(state, config={"configurable": {"thread_id": str(src)}})
    assert result["current_file"]["status"] == "MANUAL_REVIEW"
    assert result["current_file"]["held_paths"] == []


def test_dry_run_hold_stages_nothing_but_reports_held(tmp_path):
    src = tmp_path / "proj/src/main/java/com/corp/WebSecurityConfig.java"
    src.parent.mkdir(parents=True)
    src.write_text(HIGH_RISK, encoding="utf-8")
    config = write_config(tmp_path)
    with contextlib.ExitStack() as stack:
        up = _graph_mocks(stack, str(src), review_score=95)
        up.return_value.invoke.return_value = llm_reply({"files": {str(src): MIGRATED}, "manual_flags": []})
        from forge.graph import build_graph

        state = make_state(str(src), tmp_path / "proj", phase="javax-to-jakarta", dry_run=True)
        state["output_dir"] = str(tmp_path / "out")
        result = build_graph(config).invoke(state, config={"configurable": {"thread_id": str(src)}})
    assert result["current_file"]["status"] == "HELD" and result["current_file"]["held_paths"] == []
    assert not (tmp_path / "out").exists()


# ─── report ───────────────────────────────────────────────────────────────────

def test_report_counts_held_and_rejected(tmp_path):
    from forge.state import make_file_status
    from forge.utils.report import generate_report

    held = make_file_status("/p/A.java", "javax-to-jakarta"); held["status"] = "HELD"
    rejected = make_file_status("/p/B.java", "javax-to-jakarta"); rejected["status"] = "REJECTED"
    out = tmp_path / "r.md"
    generate_report(str(out), "javax-to-jakarta", "/p", [held, rejected], bedrock_calls=0)
    body = out.read_text(encoding="utf-8")
    assert "**Files held for review (HELD):** 1" in body
    assert "**Files rejected by reviewer:** 1" in body


# ─── staging primitives ───────────────────────────────────────────────────────

def test_promote_moves_staged_files_into_output_and_prunes_staging(tmp_path):
    out = tmp_path / "out"
    staged = write_files({"src/main/java/com/corp/A.java": "a"}, str(tmp_path), staging_root(str(out)))
    written = promote_staged(str(out), staged)
    assert written == [str(out / "src/main/java/com/corp/A.java")]
    assert Path(written[0]).read_text(encoding="utf-8") == "a"
    assert not (out / STAGING_DIR).exists()


def test_promote_refuses_paths_that_are_not_staged(tmp_path):
    out = tmp_path / "out"
    stray = tmp_path / "elsewhere/A.java"
    stray.parent.mkdir(parents=True)
    stray.write_text("a", encoding="utf-8")
    assert promote_staged(str(out), [str(stray)]) == []
    assert stray.exists()


def test_discard_removes_staged_files_and_leaves_output_alone(tmp_path):
    out = tmp_path / "out"
    (out / "keep.java").parent.mkdir(parents=True)
    (out / "keep.java").write_text("k", encoding="utf-8")
    staged = write_files({"A.java": "a"}, str(tmp_path), staging_root(str(out)))
    discard_staged(str(out), staged + [str(out / "keep.java")])
    assert not (out / STAGING_DIR).exists()
    assert (out / "keep.java").exists(), "discard only touches the staging tree"


def test_write_files_never_escapes_its_root(tmp_path):
    root = tmp_path / "root"
    assert write_files({"../../evil.java": "x"}, str(tmp_path), root) == []
    assert not list(tmp_path.rglob("evil.java"))
