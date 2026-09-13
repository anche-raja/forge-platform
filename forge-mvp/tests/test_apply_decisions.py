"""--apply-decisions: a reviewer's approve / reject / retry become real."""

import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import migrate
from forge.decisions import Decision, find_entry, load_decisions
from tests.conftest import llm_reply, write_config

HIGH = "package com.corp;\npublic class WebSecurityConfig extends WebSecurityConfigurerAdapter {}\n"
LOW = "package com.corp;\nimport javax.persistence.Entity;\npublic class Util {}\n"
MIGRATED = "package com.corp;\n// migrated by the model\n"
STAGED = Path(".forge-staging/src/main/java/com/corp/WebSecurityConfig.java")
OUT_FILE = Path("src/main/java/com/corp/WebSecurityConfig.java")


@pytest.fixture
def project(tmp_path):
    base = tmp_path / "proj/src/main/java/com/corp"
    base.mkdir(parents=True)
    (base / "WebSecurityConfig.java").write_text(HIGH, encoding="utf-8")
    (base / "Util.java").write_text(LOW, encoding="utf-8")
    return tmp_path / "proj"


@contextlib.contextmanager
def _aws(review_scores=(95,)):
    """Every AWS touchpoint mocked; the reviewer can be sequenced."""
    with contextlib.ExitStack() as stack:
        boto_gr = stack.enter_context(patch("forge.guardrails.bedrock_guardrails.boto3"))
        stack.enter_context(patch("forge.state_store.dynamodb.boto3"))
        pre = stack.enter_context(patch("forge.agents.guardrails_pre.ChatBedrockConverse"))
        up = stack.enter_context(patch("forge.agents.java_upgrade.ChatBedrockConverse"))
        rev = stack.enter_context(patch("forge.review.java_reviewer.ChatBedrockConverse"))
        post = stack.enter_context(patch("forge.agents.guardrails_post.ChatBedrockConverse"))
        saver = stack.enter_context(patch("forge.state_store.dynamodb.DynamoDBSaver"))
        put = stack.enter_context(patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"))
        stack.enter_context(patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending"))
        stack.enter_context(patch("forge.utils.telemetry.MetricsEmitter"))
        from langgraph.checkpoint.memory import MemorySaver
        saver.return_value = MemorySaver()
        client = MagicMock()
        client.apply_guardrail.return_value = {"action": "NONE", "assessments": []}
        boto_gr.client.return_value = client
        pre.return_value.invoke.return_value = llm_reply({"verdict": "PASS", "findings": [], "reason": ""})
        post.return_value.invoke.return_value = llm_reply({"verdict": "PASS", "findings": [], "reason": ""})
        scores = list(review_scores)
        rev.return_value.invoke.side_effect = lambda m: llm_reply(
            {"score": scores.pop(0) if len(scores) > 1 else scores[0], "verdict": "PASS", "feedback": "tighten it", "checks": {}})

        def transform(messages):
            path = messages[1].content.split("File path: ", 1)[1].splitlines()[0]
            return llm_reply({"files": {path: MIGRATED}, "manual_flags": []})

        up.return_value.invoke.side_effect = transform
        yield {"upgrade": up, "put": put}


def _main(argv):
    with patch.object(sys, "argv", ["migrate.py"] + argv):
        return migrate.main()


def _migrate(tmp_path, project, review_scores=(95,), phase="javax-to-jakarta"):
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    out = tmp_path / "migrated"
    with _aws(review_scores):
        _main([str(project), "--phase", phase, "--output-dir", str(out), "--config", str(cfg)])
    return cfg, out


def _decide(out, *decisions):
    path = out / "decisions.json"
    path.write_text(json.dumps({"run": "r1", "decisions": list(decisions)}), encoding="utf-8")
    return path


def _apply(tmp_path, project, cfg, out, path, review_scores=(95,), extra=()):
    with _aws(review_scores) as mocks:
        code = _main([str(project), "--apply-decisions", str(path), "--output-dir", str(out), "--config", str(cfg), *extra])
    return code, mocks


# ─── setup sanity ─────────────────────────────────────────────────────────────

def test_migration_holds_the_high_risk_unit_and_writes_the_low_one(tmp_path, project):
    _, out = _migrate(tmp_path, project)
    assert (out / STAGED).exists() and not (out / OUT_FILE).exists()
    assert (out / "src/main/java/com/corp/Util.java").exists()
    queue = json.loads((out / "manual-review-queue.json").read_text(encoding="utf-8"))
    assert [e["status"] for e in queue["entries"]] == ["HELD"]


# ─── approve / reject / retry ─────────────────────────────────────────────────

def test_approve_promotes_the_staged_file_and_marks_done(tmp_path, project, capsys):
    cfg, out = _migrate(tmp_path, project)
    path = _decide(out, {"file": str(OUT_FILE), "pack": "javax-to-jakarta", "decision": "approve", "note": "looks right"})
    code, mocks = _apply(tmp_path, project, cfg, out, path)

    assert code == 0
    assert (out / OUT_FILE).read_text(encoding="utf-8") == MIGRATED
    assert not (out / ".forge-staging").exists()
    fs = mocks["put"].call_args[0][0]
    assert fs["status"] == "DONE" and fs["human_decision"] == "approve" and fs["human_note"] == "looks right"
    assert fs["human_decided_at"] and fs["written_paths"] == [str(out / OUT_FILE)]
    assert fs["build_verdict"] == "SKIPPED", "build verification is off in the test config; the verdict is still recorded"
    stdout = capsys.readouterr().out
    import re
    assert re.search(r"approve\s+yes\s+DONE", stdout) and "0 file(s) still awaiting review" in stdout
    queue = json.loads((out / "manual-review-queue.json").read_text(encoding="utf-8"))
    assert queue["entries"] == []
    report = (out / "migration-report.md").read_text(encoding="utf-8")
    assert "## Applied decisions" in report and "promoted 1 staged file(s)" in report
    log = (out / "decisions-applied.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(log[0])["decision"] == "approve" and json.loads(log[0])["applied"] is True


def test_approve_of_a_manual_review_entry_writes_from_the_transformed_text(tmp_path, project):
    cfg, out = _migrate(tmp_path, project, review_scores=(20,))   # everything scores MANUAL
    queue = json.loads((out / "manual-review-queue.json").read_text(encoding="utf-8"))
    assert {e["status"] for e in queue["entries"]} == {"MANUAL_REVIEW"}
    path = _decide(out, {"file": "src/main/java/com/corp/Util.java", "pack": "javax-to-jakarta", "decision": "approve"})
    code, mocks = _apply(tmp_path, project, cfg, out, path, review_scores=(20,))
    assert code == 0
    assert (out / "src/main/java/com/corp/Util.java").read_text(encoding="utf-8") == MIGRATED
    assert mocks["put"].call_args[0][0]["status"] == "DONE"


def test_reject_discards_staging_and_records_the_reason(tmp_path, project):
    cfg, out = _migrate(tmp_path, project)
    path = _decide(out, {"file": str(OUT_FILE), "pack": "javax-to-jakarta", "decision": "reject", "note": "must stay on the adapter"})
    code, mocks = _apply(tmp_path, project, cfg, out, path)
    assert code == 0
    assert not (out / STAGED).exists() and not (out / OUT_FILE).exists()
    fs = mocks["put"].call_args[0][0]
    assert fs["status"] == "REJECTED" and fs["error"] == "Rejected by reviewer: must stay on the adapter"
    assert fs["human_decision"] == "reject"


def test_retry_reruns_with_the_human_note_in_the_transform_prompt(tmp_path, project):
    cfg, out = _migrate(tmp_path, project)
    path = _decide(out, {"file": str(OUT_FILE), "pack": "javax-to-jakarta", "decision": "retry",
                         "note": "keep the permitted-path list byte-identical", "rule": "Rule 4"})
    code, mocks = _apply(tmp_path, project, cfg, out, path)

    human = mocks["upgrade"].return_value.invoke.call_args[0][0][1].content
    assert "HUMAN REVIEW FEEDBACK:\nkeep the permitted-path list byte-identical" in human
    assert "takes precedence over automated feedback" in human
    assert "PREVIOUS REVIEW FEEDBACK" not in human, "a fresh budget: retry_count starts at 0"
    # The unit is HIGH risk, so the re-run holds it again; the queue is rewritten with it.
    assert code == 0
    fs = mocks["put"].call_args[0][0]
    assert fs["status"] == "HELD" and fs["human_note"] == "keep the permitted-path list byte-identical"
    assert fs["human_rule"] == "Rule 4"
    queue = json.loads((out / "manual-review-queue.json").read_text(encoding="utf-8"))
    assert [e["status"] for e in queue["entries"]] == ["HELD"]
    assert queue["entries"][0]["human_note"] == "keep the permitted-path list byte-identical"
    assert (out / STAGED).exists(), "restaged by the re-run"


def test_the_human_note_survives_an_automated_retry(tmp_path, project):
    """First review scores 60 → automated retry; the second prompt carries both blocks."""
    cfg, out = _migrate(tmp_path, project)
    path = _decide(out, {"file": str(OUT_FILE), "pack": "javax-to-jakarta", "decision": "retry", "note": "do not touch the realm"})
    _, mocks = _apply(tmp_path, project, cfg, out, path, review_scores=(60, 95))
    calls = mocks["upgrade"].return_value.invoke.call_args_list
    assert len(calls) == 2
    second = calls[1][0][0][1].content
    assert "HUMAN REVIEW FEEDBACK:\ndo not touch the realm" in second
    assert "PREVIOUS REVIEW FEEDBACK (retry 1):\ntighten it" in second
    assert second.index("HUMAN REVIEW FEEDBACK") < second.index("PREVIOUS REVIEW FEEDBACK")


# ─── edge cases ───────────────────────────────────────────────────────────────

def test_unknown_file_is_reported_and_the_rest_still_apply(tmp_path, project, capsys):
    cfg, out = _migrate(tmp_path, project)
    path = _decide(out,
                   {"file": "src/main/java/com/corp/Ghost.java", "pack": "javax-to-jakarta", "decision": "approve"},
                   {"file": "WebSecurityConfig.java", "pack": "javax-to-jakarta", "decision": "approve"})  # basename match
    code, _ = _apply(tmp_path, project, cfg, out, path)
    assert code == 1
    stdout = capsys.readouterr().out
    assert "Ghost.java" in stdout and "not in the review queue" in stdout
    assert (out / OUT_FILE).exists(), "the valid decision still applied"


def test_dry_run_apply_writes_moves_and_reruns_nothing(tmp_path, project, capsys):
    cfg, out = _migrate(tmp_path, project)
    path = _decide(out, {"file": str(OUT_FILE), "pack": "javax-to-jakarta", "decision": "approve"})
    code, mocks = _apply(tmp_path, project, cfg, out, path, extra=("--dry-run",))
    assert code == 0
    assert (out / STAGED).exists() and not (out / OUT_FILE).exists()
    mocks["put"].assert_not_called()
    assert not (out / "decisions-applied.jsonl").exists()
    assert "Dry run — nothing was written" in capsys.readouterr().out


def test_missing_queue_or_bad_decisions_file_fail_cleanly(tmp_path, project, capsys):
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    out = tmp_path / "nothing"
    out.mkdir()
    path = _decide(out, {"file": "x", "pack": "p", "decision": "approve"})
    with _aws():
        assert _main([str(project), "--apply-decisions", str(path), "--output-dir", str(out), "--config", str(cfg)]) == 1
    assert "Cannot apply decisions" in capsys.readouterr().out
    bad = out / "bad.json"
    bad.write_text(json.dumps({"decisions": [{"file": "x", "decision": "maybe"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="needs 'file' and a 'decision'"):
        load_decisions(str(bad))


def test_find_entry_matches_rel_then_abs_then_unique_basename():
    queue = {"entries": [{"rel_path": "a/B.java", "file_path": "/abs/a/B.java"}, {"rel_path": "c/D.java", "file_path": "/abs/c/D.java"},
                         {"rel_path": "e/D.java", "file_path": "/abs/e/D.java"}]}
    assert find_entry(queue, Decision("a/B.java", "p", "approve"))["rel_path"] == "a/B.java"
    assert find_entry(queue, Decision("/abs/a/B.java", "p", "approve"))["rel_path"] == "a/B.java"
    assert find_entry(queue, Decision("B.java", "p", "approve"))["rel_path"] == "a/B.java"
    assert find_entry(queue, Decision("D.java", "p", "approve")) is None, "ambiguous basename"
