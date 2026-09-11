"""End-to-end runs through migrate.py:main() with every AWS call mocked.

Covers the Phase 0 acceptance criteria the unit tests do not reach — the CLI
path itself, report generation, and that --dry-run writes nothing anywhere.
"""

import contextlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import migrate
from tests.conftest import CLEAN_JAVA, llm_reply, write_config

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


@contextlib.contextmanager
def _mocked_aws(config_path, review_score=95):
    """Patch every AWS touchpoint the CLI reaches."""
    with contextlib.ExitStack() as stack:
        boto_gr = stack.enter_context(patch("forge.guardrails.bedrock_guardrails.boto3"))
        stack.enter_context(patch("forge.state_store.dynamodb.boto3"))
        pre = stack.enter_context(patch("forge.agents.guardrails_pre.ChatBedrockConverse"))
        up = stack.enter_context(patch("forge.agents.java_upgrade.ChatBedrockConverse"))
        rev = stack.enter_context(patch("forge.review.java_reviewer.ChatBedrockConverse"))
        post = stack.enter_context(patch("forge.agents.guardrails_post.ChatBedrockConverse"))
        saver = stack.enter_context(patch("forge.state_store.dynamodb.DynamoDBSaver"))
        metrics = stack.enter_context(patch("forge.utils.telemetry.MetricsEmitter"))

        from langgraph.checkpoint.memory import MemorySaver
        saver.return_value = MemorySaver()

        client = MagicMock()
        client.apply_guardrail.return_value = {"action": "NONE", "assessments": []}
        boto_gr.client.return_value = client

        pre.return_value.invoke.return_value = llm_reply({"verdict": "PASS", "findings": [], "reason": ""})
        post.return_value.invoke.return_value = llm_reply({"verdict": "PASS", "findings": [], "reason": ""})
        rev.return_value.invoke.return_value = llm_reply(
            {"score": review_score, "verdict": "PASS", "feedback": "", "checks": {}}
        )

        def transform(messages):
            # Echo back the path the agent was given, migrated.
            human = messages[1].content
            path = human.split("File path: ", 1)[1].splitlines()[0]
            return llm_reply({"files": {path: MIGRATED}, "manual_flags": []})

        up.return_value.invoke.side_effect = transform
        yield {"metrics": metrics, "upgrade": up}


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
