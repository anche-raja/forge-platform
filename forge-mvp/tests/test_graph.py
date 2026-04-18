"""Test that a mock file flows through the full graph and ends at DONE."""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from forge.config import ForgeConfig
from forge.state import make_file_status


@pytest.fixture
def config(tmp_path):
    agents_yaml = tmp_path / "agents.yaml"
    agents_yaml.write_text(
        "transform_model: us.anthropic.claude-sonnet-4-5-20251001-v1:0\n"
        "review_model: us.amazon.nova-pro-v1:0\n"
        "aws_region: us-east-1\n"
        "dynamodb_table: forge-migration-state-test\n"
        "dynamodb_checkpoint_table: forge-langgraph-checkpoints-test\n"
        "guardrail_id: test-guardrail-id\n"
        "guardrail_version: '1'\n"
        "pass_threshold: 80\n"
        "retry_threshold: 50\n"
        "max_retries: 2\n"
        "scope_package_prefix: com.corp\n"
        "complexity_block_threshold: 2000\n"
    )
    return ForgeConfig(str(agents_yaml))


@pytest.fixture
def java_file(tmp_path):
    java_src = tmp_path / "UserAction.java"
    java_src.write_text(
        "package com.corp.user;\n"
        "import javax.persistence.Entity;\n"
        "import javax.servlet.http.HttpServletRequest;\n"
        "public class UserAction {\n"
        "    public void handle(HttpServletRequest req) {}\n"
        "}\n"
    )
    return str(java_src)


def _pass_guardrail():
    return {"action": "NONE", "findings": [], "intervened": False}


def _pass_llm_pre():
    m = MagicMock()
    m.content = json.dumps({"verdict": "PASS", "findings": [], "reason": "clean"})
    return m


def _transform_llm(file_path):
    m = MagicMock()
    m.content = json.dumps({
        "files": {
            file_path: (
                "package com.corp.user;\n"
                "import jakarta.persistence.Entity;\n"
                "import jakarta.servlet.http.HttpServletRequest;\n"
                "public class UserAction {\n"
                "    public void handle(HttpServletRequest req) {}\n"
                "}\n"
            )
        },
        "manual_flags": [],
    })
    return m


def _review_llm_pass():
    m = MagicMock()
    m.content = json.dumps({
        "score": 95,
        "verdict": "PASS",
        "feedback": "",
        "checks": {"namespace": 20, "deprecated": 20, "datetime": 25, "var_inference": 20, "no_regressions": 10},
    })
    return m


def _pass_llm_post():
    m = MagicMock()
    m.content = json.dumps({"verdict": "PASS", "findings": [], "reason": "clean"})
    return m


def test_full_graph_ends_done(config, java_file, tmp_path):
    from langgraph.checkpoint.memory import MemorySaver

    with (
        patch("forge.guardrails.bedrock_guardrails.boto3") as mock_boto3,
        patch("forge.agents.guardrails_pre.ChatBedrockConverse") as MockPreLLM,
        patch("forge.agents.java_upgrade.ChatBedrockConverse") as MockUpgradeLLM,
        patch("forge.review.java_reviewer.ChatBedrockConverse") as MockReviewLLM,
        patch("forge.agents.guardrails_post.ChatBedrockConverse") as MockPostLLM,
        patch("forge.state_store.dynamodb.DynamoDBSaver") as MockSaver,
    ):
        # Wire guardrails mock
        mock_bedrock_client = MagicMock()
        mock_bedrock_client.apply_guardrail.return_value = {
            "action": "NONE", "assessments": []
        }
        mock_boto3.client.return_value = mock_bedrock_client

        MockPreLLM.return_value.invoke.return_value = _pass_llm_pre()
        MockUpgradeLLM.return_value.invoke.return_value = _transform_llm(java_file)
        MockReviewLLM.return_value.invoke.return_value = _review_llm_pass()
        MockPostLLM.return_value.invoke.return_value = _pass_llm_post()
        MockSaver.return_value = MemorySaver()

        from forge.graph import build_graph
        app = build_graph(config)

        initial = {
            "current_file": make_file_status(java_file, "java21"),
            "phase": "java21",
            "dry_run": True,
            "source_dir": str(tmp_path),
            "output_dir": str(tmp_path / "migrated"),
            "target_java_version": "21",
            "target_spring_version": "3",
            "files_processed": 0,
            "files_passed": 0,
            "files_retried": 0,
            "files_manual": 0,
            "files_blocked": 0,
            "bedrock_calls": 0,
            "estimated_cost_usd": 0.0,
            "messages": [],
        }

        result = app.invoke(initial, config={"configurable": {"thread_id": java_file}})

        fs = result["current_file"]
        assert fs["status"] == "DONE", f"Expected DONE, got {fs['status']}"
        assert fs["review_score"] == 95
        assert fs["transform_model"] is not None
        assert fs["review_model"] is not None
        assert result["files_processed"] == 1
        assert result["files_passed"] == 1
