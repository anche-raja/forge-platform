"""Test Java upgrade agent prompt assembly with and without feedback injection."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

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
    src = tmp_path / "Example.java"
    src.write_text("package com.corp;\nimport javax.persistence.Entity;\npublic class Example {}\n")
    return str(src)


def _mock_transform_response(file_path):
    m = MagicMock()
    m.content = json.dumps({
        "files": {file_path: "package com.corp;\nimport jakarta.persistence.Entity;\npublic class Example {}\n"},
        "manual_flags": [],
    })
    return m


def test_upgrade_no_feedback(config, java_file):
    """retry_count=0: prompt must NOT contain feedback section."""
    with patch("forge.agents.java_upgrade.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value.invoke.return_value = _mock_transform_response(java_file)

        from forge.agents.java_upgrade import JavaUpgradeAgent
        agent = JavaUpgradeAgent(config)

        fs = make_file_status(java_file, "java21")
        state = {
            "current_file": fs, "phase": "java21", "dry_run": False,
            "source_dir": str(Path(java_file).parent), "output_dir": "./migrated",
            "target_java_version": "21", "target_spring_version": "3",
            "files_processed": 0, "files_passed": 0, "files_retried": 0,
            "files_manual": 0, "files_blocked": 0,
            "bedrock_calls": 0, "estimated_cost_usd": 0.0, "messages": [],
        }

        result = agent.run(state)

        invoke_args = MockLLM.return_value.invoke.call_args[0][0]
        human_content = invoke_args[1].content
        assert "PREVIOUS REVIEW FEEDBACK" not in human_content
        assert result["current_file"]["status"] == "REVIEWING"


def test_upgrade_with_feedback(config, java_file):
    """retry_count=1: feedback must appear in the human message."""
    with patch("forge.agents.java_upgrade.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value.invoke.return_value = _mock_transform_response(java_file)

        from forge.agents.java_upgrade import JavaUpgradeAgent
        agent = JavaUpgradeAgent(config)

        fs = make_file_status(java_file, "java21")
        fs["retry_count"] = 1
        fs["review_feedback"] = "javax.persistence.Entity was not migrated"
        state = {
            "current_file": fs, "phase": "java21", "dry_run": False,
            "source_dir": str(Path(java_file).parent), "output_dir": "./migrated",
            "target_java_version": "21", "target_spring_version": "3",
            "files_processed": 0, "files_passed": 0, "files_retried": 0,
            "files_manual": 0, "files_blocked": 0,
            "bedrock_calls": 0, "estimated_cost_usd": 0.0, "messages": [],
        }

        result = agent.run(state)

        invoke_args = MockLLM.return_value.invoke.call_args[0][0]
        human_content = invoke_args[1].content
        assert "PREVIOUS REVIEW FEEDBACK" in human_content
        assert "javax.persistence.Entity was not migrated" in human_content
        assert result["current_file"]["status"] == "REVIEWING"
