"""End-to-end runs through migrate.py:main() with every AWS call mocked.

Covers the Phase 0 acceptance criteria the unit tests do not reach — the CLI
path itself, report generation, and that --dry-run writes nothing anywhere.
"""

import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import migrate
from tests.conftest import CLEAN_JAVA, llm_reply, mocked_aws, write_config

LEGACY = """\
package com.corp.user;
import javax.persistence.Entity;
public class UserAction {
    public void handle() {}
}
"""

MIGRATED = """\
package com.corp.user;
import jakarta.persistence.Entity;
public class UserAction {
    public void handle() {}
}
"""


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "proj/src/main/java/com/corp/user/UserAction.java"
    src.parent.mkdir(parents=True)
    src.write_text(LEGACY, encoding="utf-8")
    return tmp_path / "proj"


# The AWS mock lives in conftest so the service and UI tests share it.
_mocked_aws = mocked_aws


def _run(argv):
    with patch.object(sys, "argv", ["migrate.py"] + argv):
        migrate.main()


def test_dry_run_writes_no_files_and_no_dynamodb(tmp_path, project, capsys):
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)   # writes agents.yaml into tmp_path
    out = tmp_path / "migrated"

    with _mocked_aws(cfg):
        with patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status") as put:
            _run([str(project), "--phase", "java21", "--dry-run",
                  "--output-dir", str(out), "--config", str(cfg)])

    assert not list(out.rglob("*.java")), "dry run must not write migrated sources"
    put.assert_not_called()
    assert "Summary:" in capsys.readouterr().out


def test_full_run_writes_migrated_file_with_package_path(tmp_path, project, capsys):
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    out = tmp_path / "migrated"

    with _mocked_aws(cfg):
        with patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"), \
             patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending"):
            _run([str(project), "--phase", "java21",
                  "--output-dir", str(out), "--config", str(cfg)])

    # Acceptance criterion 4: correct package path, jakarta.* not javax.*
    written = out / "src/main/java/com/corp/user/UserAction.java"
    assert written.exists(), sorted(str(p) for p in out.rglob("*"))
    body = written.read_text(encoding="utf-8")
    assert "jakarta.persistence.Entity" in body
    assert "javax.persistence" not in body

    report = (out / "migration-report.md").read_text(encoding="utf-8")
    assert "Files passed (DONE):** 1" in report
    assert "Estimated Bedrock cost:" in report
    assert "1 passed" in capsys.readouterr().out


def test_manual_review_queue_written_for_low_scores(tmp_path, project):
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    out = tmp_path / "migrated"

    with _mocked_aws(cfg, review_score=20):
        with patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"), \
             patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending"):
            _run([str(project), "--phase", "java21",
                  "--output-dir", str(out), "--config", str(cfg)])

    queue = out / "manual-review-queue.json"
    assert queue.exists()
    assert "UserAction.java" in queue.read_text(encoding="utf-8")


def test_no_eligible_files_exits_cleanly(tmp_path):
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()

    with _mocked_aws(cfg):
        with pytest.raises(SystemExit) as exc:
            _run([str(empty), "--phase", "java21", "--config", str(cfg)])
    assert exc.value.code == 0


# ─── context-bearing packs ────────────────────────────────────────────────────

def _webapp_project(tmp_path):
    from tests.test_extract_web_bootstrap import make_module

    return make_module(tmp_path / "proj", java=("LoggingFilter.java", "StartupListener.java"))


def test_webapp_bootstrap_single_file_run_injects_context_and_writes_snapshot(tmp_path, capsys):
    """The step-3 milestone, end to end: web.xml goes to the transform with its
    descriptor set, and the full context lands next to the report."""
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    module = _webapp_project(tmp_path)
    web_xml = module / "src/main/webapp/WEB-INF/web.xml"
    out = tmp_path / "migrated"

    with _mocked_aws(cfg) as mocks:
        with patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"):
            _run([str(tmp_path / "proj"), "--phase", "webapp-bootstrap-jakarta10", "--dry-run",
                  "--file", str(web_xml), "--output-dir", str(out), "--config", str(cfg)])

    human = mocks["upgrade"].return_value.invoke.call_args[0][0][1].content
    assert human.startswith("Transform this file:\nFile path: ")
    assert "=== CONTEXT: web_bootstrap ===" in human
    assert "springSecurityFilterChain" in human and "## filter_chain" in human
    snapshot = out / "migration-context.json"
    assert snapshot.exists(), "written in dry-run too — it is an audit artifact"
    body = snapshot.read_text(encoding="utf-8")
    assert '"context": "web_bootstrap"' in body and "springSecurityFilterChain" in body
    assert "Context snapshot:" in capsys.readouterr().out


def test_liberty_run_generates_server_xml_under_the_module(tmp_path, capsys):
    """A generated unit has no source; the scan yields it, the transform is
    given the context, and the output lands at the module's Liberty config path.

    A generated unit is HIGH risk by rule, so this run opts out of the default
    hold with risk_ceiling: auto; the sibling test below pins the default."""
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path, decisions={"risk_ceiling": "auto"})
    module = _webapp_project(tmp_path)
    out = tmp_path / "migrated"

    with _mocked_aws(cfg) as mocks:
        with patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"), \
             patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending") as pending:
            _run([str(tmp_path / "proj"), "--phase", "liberty-server-config",
                  "--output-dir", str(out), "--config", str(cfg)])

    human = mocks["upgrade"].return_value.invoke.call_args[0][0][1].content
    assert "No existing file — generate it." in human
    assert "## datasources" in human and "jdbc/ordersDS" in human
    written = out / "orders/src/main/liberty/config/server.xml"
    assert written.exists(), sorted(str(p) for p in out.rglob("*"))
    pending.assert_not_called(), "a generated target is not a PENDING source file"
    stdout = capsys.readouterr().out
    assert "(+1 generated)" in stdout and "server.xml (generated) → DONE" in stdout


# ─── acceptance ───────────────────────────────────────────────────────────────

def test_acceptance_only_checks_an_existing_output_and_records_the_verdict(tmp_path, project, capsys):
    """No migration runs; the phase's checks execute over source + existing
    output, and the verdict is persisted next to the report."""
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    out = tmp_path / "migrated"
    migrated = out / "src/main/java/com/corp/user/UserAction.java"
    migrated.parent.mkdir(parents=True)
    migrated.write_text(MIGRATED, encoding="utf-8")
    (out / "migration-report.md").write_text("# FORGE Migration Report\n", encoding="utf-8")

    with _mocked_aws(cfg) as mocks:
        code = _run_main([str(project), "--phase", "javax-to-jakarta", "--acceptance-only",
                          "--output-dir", str(out), "--config", str(cfg)])

    mocks["upgrade"].return_value.invoke.assert_not_called()
    stdout = capsys.readouterr().out
    assert "Acceptance:" in stdout and "[PASS] no_match" in stdout
    assert code == 0
    record = json.loads((out / "migration-acceptance.json").read_text(encoding="utf-8"))
    assert record["verdict"] == "PASS"
    assert "## Acceptance" in (out / "migration-report.md").read_text(encoding="utf-8")


def test_acceptance_only_fails_loudly_when_the_source_still_has_javax_imports(tmp_path, project, capsys):
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    out = tmp_path / "migrated"
    out.mkdir()   # nothing migrated: the source's javax.persistence import is still there

    with _mocked_aws(cfg):
        code = _run_main([str(project), "--phase", "javax-to-jakarta", "--acceptance-only",
                          "--output-dir", str(out), "--config", str(cfg)])

    assert code == 1
    stdout = capsys.readouterr().out
    assert "Acceptance: FAIL" in stdout and "[FAIL] no_match" in stdout


def test_acceptance_after_a_dry_run_is_skipped_with_a_reason(tmp_path, project, capsys):
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    with _mocked_aws(cfg):
        with patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"):
            _run([str(project), "--phase", "javax-to-jakarta", "--dry-run", "--acceptance",
                  "--output-dir", str(tmp_path / "out"), "--config", str(cfg)])
    assert "Acceptance: skipped — a dry run writes nothing" in capsys.readouterr().out


def _run_main(argv):
    with patch.object(sys, "argv", ["migrate.py"] + argv):
        return migrate.main()


def test_liberty_generated_unit_is_held_by_default(tmp_path, capsys):
    """Under the default risk_ceiling (review-high) a generated server.xml is
    staged and held, never written unseen."""
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    _webapp_project(tmp_path)
    out = tmp_path / "migrated"

    with _mocked_aws(cfg):
        with patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"), \
             patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending"):
            _run([str(tmp_path / "proj"), "--phase", "liberty-server-config",
                  "--output-dir", str(out), "--config", str(cfg)])

    assert not (out / "orders/src/main/liberty/config/server.xml").exists()
    staged = out / ".forge-staging/orders/src/main/liberty/config/server.xml"
    assert staged.exists()
    stdout = capsys.readouterr().out
    assert "server.xml (generated) → HELD" in stdout and "1 held" in stdout
    queue = json.loads((out / "manual-review-queue.json").read_text(encoding="utf-8"))
    (entry,) = queue["entries"]
    assert entry["status"] == "HELD" and entry["held_paths"] == [str(staged)]
    assert "risk_ceiling=review-high" in entry["hold_reason"]
    assert (out / "migration-review.html").exists()


def test_ui_flag_dispatches_to_serve_without_a_source_dir():
    with patch("forge.ui.server.serve", return_value=0) as serve:
        _run(["--ui", "--no-browser", "--port", "9999"])
    serve.assert_called_once_with(port=9999, open_browser=False, strict_port=True)
    with patch("forge.ui.server.serve", return_value=0) as serve:
        _run(["--ui"])
    serve.assert_called_once_with(port=8765, open_browser=True, strict_port=False)
