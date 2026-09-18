"""The service layer: one implementation of a run, callable without argparse or stdout."""

import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import migrate
from forge import service
from forge.config import ForgeConfig
from tests.conftest import mocked_aws, write_config

LEGACY = "package com.corp.user;\nimport javax.persistence.Entity;\npublic class UserAction {}\n"
HIGH = "package com.corp;\npublic class WebSecurityConfig extends WebSecurityConfigurerAdapter {}\n"


@pytest.fixture
def project(tmp_path):
    base = tmp_path / "proj/src/main/java/com/corp"
    base.mkdir(parents=True)
    (base / "user").mkdir()
    (base / "user/UserAction.java").write_text(LEGACY, encoding="utf-8")
    (base / "Other.java").write_text("package com.corp;\nimport javax.persistence.Entity;\npublic class Other {}\n", encoding="utf-8")
    return tmp_path / "proj"


def _run(tmp_path, project, **kw):
    events = []
    cfg = write_config(tmp_path, **kw.pop("config_overrides", {}))
    with mocked_aws(), patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"), \
         patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending"):
        result = service.run_migration(str(project), kw.pop("phase", "javax-to-jakarta"), str(tmp_path / "out"), cfg,
                                       on_event=events.append, **kw)
    return result, events


# ─── run_migration ────────────────────────────────────────────────────────────

def test_run_writes_the_same_artifacts_as_the_cli_and_returns_totals(tmp_path, project):
    result, _ = _run(tmp_path, project)
    out = tmp_path / "out"
    assert (out / "migration-report.md").exists() and (out / "manual-review-queue.json").exists()
    assert (out / "src/main/java/com/corp/user/UserAction.java").exists()
    assert {k: v for k, v in result.totals.items() if k != "cost_usd"} == {"total": 2, "passed": 2, "manual": 0, "blocked": 0, "held": 0, "bedrock_calls": 6}
    assert result.totals["cost_usd"] > 0
    assert result.paths["report"] == str(out / "migration-report.md")
    assert result.paths["page"] is None, "nothing needed review"
    assert not result.cancelled
    summary = json.loads(json.dumps(result.summary()))
    assert summary["queue_count"] == 0 and [f["status"] for f in summary["files"]] == ["DONE", "DONE"]


def test_events_arrive_in_order_and_carry_what_the_cli_prints(tmp_path, project):
    _, events = _run(tmp_path, project)
    types = [e["type"] for e in events]
    assert types == ["start", "file", "file", "summary"]
    assert events[0] == {"type": "start", "phase": "javax-to-jakarta", "files": 2, "generated": 0, "dry_run": False, "total": 2}
    first = events[1]
    assert (first["index"], first["total"], first["label"], first["status"], first["score"]) == (1, 2, "Other.java", "DONE", 95)
    assert events[-1]["passed"] == 2 and events[-1]["report"].endswith("migration-report.md")


def test_dry_run_writes_no_sources_but_writes_the_review_page(tmp_path, project):
    result, events = _run(tmp_path, project, dry_run=True)
    out = tmp_path / "out"
    assert not list(out.rglob("*.java"))
    assert (out / "migration-review.html").exists(), "a dry run lists every transform for review"
    assert result.queue["dry_run"] is True and len(result.queue["entries"]) == 2
    assert any(e["type"] == "queue" and e["count"] == 2 for e in events)


def test_no_eligible_files_is_an_exception_not_an_exit(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    cfg = write_config(tmp_path)
    with mocked_aws(), pytest.raises(service.NoEligibleFiles, match="No eligible files"):
        service.run_migration(str(empty), "javax-to-jakarta", str(tmp_path / "out"), cfg)


def test_cancel_between_units_stops_the_loop_and_still_writes_the_artifacts(tmp_path, project):
    cancel = threading.Event()
    events = []

    def on_event(e):
        events.append(e)
        if e["type"] == "file":
            cancel.set()   # after the first unit

    cfg = write_config(tmp_path)
    with mocked_aws(), patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"), \
         patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending"):
        result = service.run_migration(str(project), "javax-to-jakarta", str(tmp_path / "out"), cfg,
                                       on_event=on_event, cancel=cancel)
    assert result.cancelled and result.totals["total"] == 1
    assert [e["type"] for e in events] == ["start", "file", "cancelled", "summary"]
    assert events[2] == {"type": "cancelled", "done": 1, "total": 2}
    assert (tmp_path / "out/migration-report.md").exists()
    assert (tmp_path / "out/manual-review-queue.json").exists()


def test_generated_unit_emits_snapshot_and_is_held_by_default(tmp_path):
    from tests.test_extract_web_bootstrap import make_module

    make_module(tmp_path / "proj", java=("LoggingFilter.java",))
    cfg = write_config(tmp_path)
    events = []
    with mocked_aws(), patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"), \
         patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending"):
        result = service.run_migration(str(tmp_path / "proj"), "liberty-server-config", str(tmp_path / "out"), cfg,
                                       on_event=events.append)
    assert events[0]["generated"] == 1
    assert any(e["type"] == "snapshot" for e in events)
    file_event = next(e for e in events if e["type"] == "file")
    assert file_event["label"] == "server.xml (generated)" and file_event["status"] == "HELD"
    assert result.totals["held"] == 1 and result.paths["snapshot"].endswith("migration-context.json")


def test_run_acceptance_flag_attaches_the_outcome(tmp_path, project):
    result, events = _run(tmp_path, project, run_acceptance=True)
    assert result.acceptance is not None and result.acceptance.report is not None
    assert result.acceptance.report.verdict in ("PASS", "INCOMPLETE", "FAIL")
    assert any(e["type"] == "acceptance" for e in events)
    assert result.paths["acceptance"].endswith("migration-acceptance.json")


def test_acceptance_is_skipped_in_dry_run_with_a_reason(tmp_path, project):
    result, events = _run(tmp_path, project, dry_run=True, run_acceptance=True)
    assert result.acceptance.report is None and "dry run" in result.acceptance.skipped_reason
    assert any(e["type"] == "acceptance_skipped" for e in events)


# ─── discover / packs / feedback ──────────────────────────────────────────────

def test_discover_returns_summary_activations_order_and_paths(tmp_path, project):
    result = service.discover(str(project), str(tmp_path / "out"))
    assert "Discovery —" in result["summary"]
    assert "javax-to-jakarta" in [a["pack"] for a in result["activations"]]
    assert result["order"] and Path(result["paths"]["yaml"]).exists()
    assert result["decisions"]["risk_ceiling"] == "review-high"


def test_packs_lists_registry_order_with_runnable_flags():
    listed = service.packs()
    ids = [p["id"] for p in listed]
    assert ids.index("javax-to-jakarta") < ids.index("struts2-modernize")
    by_id = {p["id"]: p for p in listed}
    assert by_id["javax-to-jakarta"]["runnable"] is True
    assert by_id["struts2-to-springmvc6"]["runnable"] is False and by_id["struts2-to-springmvc6"]["complete"] is True
    assert by_id["ejb2-to-spring"]["complete"] is False


def test_feedback_is_json_safe(tmp_path):
    (tmp_path / "decisions.json").write_text(json.dumps({"run": "r", "decisions": [
        {"file": "a/A.java", "pack": "javax-to-jakarta", "decision": "retry", "note": "Rule 2: leave javax.sql"}]}), encoding="utf-8")
    result = service.feedback(str(tmp_path))
    json.dumps(result)
    assert result["notes"] == 1 and result["groups"]["javax-to-jakarta"]["rows"][0]["rule"] == "Rule 2"


# ─── apply ────────────────────────────────────────────────────────────────────

def test_apply_from_dicts_approves_and_rejects_invalid_ones_like_load_decisions(tmp_path, project):
    (project / "src/main/java/com/corp/WebSecurityConfig.java").write_text(HIGH, encoding="utf-8")
    _run(tmp_path, project)   # holds the HIGH unit
    cfg = write_config(tmp_path)
    events = []
    with mocked_aws(), patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"):
        result = service.apply([{"file": "src/main/java/com/corp/WebSecurityConfig.java", "pack": "javax-to-jakarta",
                                 "decision": "approve"}], str(project), str(tmp_path / "out"), cfg, on_event=events.append)
        with pytest.raises(ValueError, match="needs 'file' and a 'decision'"):
            service.apply([{"file": "x", "decision": "maybe"}], str(project), str(tmp_path / "out"), cfg)
    assert result.all_applied and result.outcomes[0].status_after == "DONE"
    assert (tmp_path / "out/src/main/java/com/corp/WebSecurityConfig.java").exists()
    assert [e["type"] for e in events] == ["apply_outcome", "apply_done"]
    assert json.dumps(result.to_json())


# ─── config overrides ─────────────────────────────────────────────────────────

def test_with_overrides_merges_nested_decisions_and_leaves_the_original_untouched(tmp_path):
    cfg = write_config(tmp_path, decisions={"web_framework": "modernize-in-place", "risk_ceiling": "review-high"})
    over = cfg.with_overrides({"decisions": {"risk_ceiling": "auto"}, "max_retries": 1})
    assert over.get("decisions") == {"web_framework": "modernize-in-place", "risk_ceiling": "auto"}
    assert over.get("max_retries") == 1 and over.transform_model == cfg.transform_model
    assert cfg.get("decisions")["risk_ceiling"] == "review-high" and cfg.get("max_retries") == 2


def test_config_can_be_built_from_a_dict():
    cfg = ForgeConfig(data={"transform_model": "m", "decisions": {"risk_ceiling": "auto"}})
    assert cfg.transform_model == "m" and cfg.get("decisions")["risk_ceiling"] == "auto"


# ─── CLI parity ───────────────────────────────────────────────────────────────

def test_cli_prints_exactly_the_historical_lines(tmp_path, project, capsys):
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    out = tmp_path / "out"
    with mocked_aws(), patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"), \
         patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending"), \
         patch.object(sys, "argv", ["migrate.py", str(project), "--phase", "javax-to-jakarta",
                                    "--output-dir", str(out), "--config", str(cfg)]):
        migrate.main()
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert lines == [
        "FORGE — phase: javax-to-jakarta | files: 2 | dry-run: False",
        "[1/2] Other.java → DONE, score: 95",
        "[2/2] UserAction.java → DONE, score: 95",
        "Summary: 2 passed | 0 manual | 0 blocked | 6 Bedrock calls",
        f"Report: {out / 'migration-report.md'}",
    ]
