"""Regression tests for the Phase 0 acceptance criteria.

Both recorded live runs ended in MANUAL_REVIEW — the second at a passing score
of 80 — because guardrails_post treated a package-scope mismatch as a blocking
condition. These tests pin the corrected behaviour.
"""

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import CLEAN_JAVA, llm_reply, make_state, write_config


def _graph_mocks(stack, file_path, review_score=95, post_verdict="PASS", post_findings=None):
    """Patch every Bedrock touchpoint. Returns the transform mock for assertions."""
    mock_boto3 = stack.enter_context(patch("forge.guardrails.bedrock_guardrails.boto3"))
    pre = stack.enter_context(patch("forge.agents.guardrails_pre.ChatBedrockConverse"))
    up = stack.enter_context(patch("forge.agents.java_upgrade.ChatBedrockConverse"))
    rev = stack.enter_context(patch("forge.review.java_reviewer.ChatBedrockConverse"))
    post = stack.enter_context(patch("forge.agents.guardrails_post.ChatBedrockConverse"))
    saver = stack.enter_context(patch("forge.state_store.dynamodb.DynamoDBSaver"))

    client = MagicMock()
    client.apply_guardrail.return_value = {"action": "NONE", "assessments": []}
    mock_boto3.client.return_value = client

    pre.return_value.invoke.return_value = llm_reply(
        {"verdict": "PASS", "findings": [], "reason": "clean"}
    )
    up.return_value.invoke.return_value = llm_reply(
        {"files": {file_path: CLEAN_JAVA}, "manual_flags": []}
    )
    rev.return_value.invoke.return_value = llm_reply(
        {"score": review_score, "verdict": "PASS", "feedback": "", "checks": {}}
    )
    post.return_value.invoke.return_value = llm_reply(
        {"verdict": post_verdict, "findings": post_findings or [], "reason": ""}
    )

    from langgraph.checkpoint.memory import MemorySaver
    saver.return_value = MemorySaver()
    return up


@pytest.mark.parametrize("score", [80, 95])
def test_passing_score_reaches_done(tmp_path, java_file, score):
    """A score at or above the pass threshold must end at DONE, not MANUAL_REVIEW."""
    import contextlib

    config = write_config(tmp_path, scope_package_prefix="com.corp")
    with contextlib.ExitStack() as stack:
        _graph_mocks(stack, java_file, review_score=score)
        from forge.graph import build_graph

        app = build_graph(config)
        result = app.invoke(
            make_state(java_file, tmp_path),
            config={"configurable": {"thread_id": java_file}},
        )

    fs = result["current_file"]
    assert fs["status"] == "DONE", f"score {score} should pass, got {fs['status']}"
    assert result["files_passed"] == 1


def test_out_of_scope_package_does_not_block(tmp_path, java_file):
    """A package outside scope_package_prefix is advisory — it must still migrate.

    This is the exact shape of the two recorded failures: scope prefix 'com.corp'
    against a file in com.khoubyari.example.domain.
    """
    import contextlib

    config = write_config(tmp_path, scope_package_prefix="com.corp")
    with contextlib.ExitStack() as stack:
        _graph_mocks(
            stack, java_file, review_score=80,
            post_findings=["Package 'com.khoubyari.example.domain' does not match scope prefix"],
        )
        from forge.graph import build_graph

        app = build_graph(config)
        result = app.invoke(
            make_state(java_file, tmp_path),
            config={"configurable": {"thread_id": java_file}},
        )

    fs = result["current_file"]
    assert fs["status"] == "DONE"
    # The finding is still recorded — it is surfaced, just not fatal.
    assert any("scope prefix" in f for f in fs["guardrail_findings"])


def test_leftover_javax_forces_manual_review(tmp_path, java_file):
    """Rule 1 is enforced in code: unmigrated javax.* must never reach DONE,
    even when the reviewer and the post-check model both say PASS."""
    import contextlib

    dirty = CLEAN_JAVA.replace(
        "import jakarta.servlet.http.HttpServletRequest;",
        "import javax.servlet.http.HttpServletRequest;",
    )
    config = write_config(tmp_path)
    with contextlib.ExitStack() as stack:
        up = _graph_mocks(stack, java_file, review_score=99)
        up.return_value.invoke.return_value = llm_reply(
            {"files": {java_file: dirty}, "manual_flags": []}
        )
        from forge.graph import build_graph

        app = build_graph(config)
        result = app.invoke(
            make_state(java_file, tmp_path),
            config={"configurable": {"thread_id": java_file}},
        )

    fs = result["current_file"]
    assert fs["status"] == "MANUAL_REVIEW"
    assert "javax.servlet.http.HttpServletRequest" in fs["error"]


def test_jdk_javax_imports_are_not_flagged(tmp_path, java_file):
    """javax.crypto / javax.sql are JDK packages — rewriting them would break
    the code, so they must not trip the Rule 1 check."""
    import contextlib

    with_jdk = CLEAN_JAVA.replace(
        "public class UserAction {",
        "import javax.crypto.Cipher;\nimport javax.sql.DataSource;\npublic class UserAction {",
    )
    config = write_config(tmp_path)
    with contextlib.ExitStack() as stack:
        up = _graph_mocks(stack, java_file, review_score=90)
        up.return_value.invoke.return_value = llm_reply(
            {"files": {java_file: with_jdk}, "manual_flags": []}
        )
        from forge.graph import build_graph

        app = build_graph(config)
        result = app.invoke(
            make_state(java_file, tmp_path),
            config={"configurable": {"thread_id": java_file}},
        )

    assert result["current_file"]["status"] == "DONE"


def test_cost_accrues_across_the_run(tmp_path, java_file):
    """estimated_cost_usd was declared but never written, leaving the
    FORGE-CostSpike alarm blind. It must now accumulate per Bedrock call."""
    import contextlib

    config = write_config(tmp_path)
    with contextlib.ExitStack() as stack:
        _graph_mocks(stack, java_file, review_score=95)
        from forge.graph import build_graph

        app = build_graph(config)
        result = app.invoke(
            make_state(java_file, tmp_path),
            config={"configurable": {"thread_id": java_file}},
        )

    # 3 calls at 1000 in / 500 out: 2 Opus 4.8 (0.005/0.025) + 1 Nova (0.0008/0.0032).
    # The pre-flight model call is opt-in and off by default — secrets and file
    # size are decided locally now, so nothing is asked of a model before the
    # transform runs.
    expected = 2 * (0.005 + 0.5 * 0.025) + (0.0008 + 0.5 * 0.0032)
    assert result["bedrock_calls"] == 3
    assert result["estimated_cost_usd"] == pytest.approx(expected, rel=1e-6)


# ─── The reviewer's pass is final ─────────────────────────────────────────────



def _run_with_post_block(tmp_path, java_file, **config_overrides):
    import contextlib

    config = write_config(tmp_path, **config_overrides)
    with contextlib.ExitStack() as stack:
        _graph_mocks(stack, java_file, review_score=100, post_verdict="BLOCK",
                     post_findings=["Use of deprecated java.util.Date"])
        from forge.graph import build_graph

        result = build_graph(config).invoke(
            make_state(java_file, tmp_path),
            config={"configurable": {"thread_id": java_file}},
        )
    return result


def test_post_check_block_is_advisory_by_default(tmp_path, java_file):
    """Reviewer PASS 100 + post-check BLOCK: written, the finding kept for the report."""
    result = _run_with_post_block(tmp_path, java_file)
    fs = result["current_file"]
    assert fs["status"] == "DONE"
    assert fs["post_check_verdict"] == "BLOCK"
    assert any("java.util.Date" in f for f in fs["guardrail_findings"])
    assert result["files_manual"] == 0


def test_post_check_block_mode_restores_the_hold(tmp_path, java_file):
    result = _run_with_post_block(tmp_path, java_file, post_model_check="block")
    assert result["current_file"]["status"] == "MANUAL_REVIEW"


def test_post_check_off_skips_the_model_call(tmp_path, java_file):
    """off: two model calls on the happy path (transform + review), not three."""
    result = _run_with_post_block(tmp_path, java_file, post_model_check="off")
    fs = result["current_file"]
    assert fs["status"] == "DONE"
    assert "post_check_verdict" not in fs
    assert result["bedrock_calls"] == 2
