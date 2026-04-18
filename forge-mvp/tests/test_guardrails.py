"""Test that GUARDRAIL_INTERVENED from Bedrock results in BLOCKED and graph terminates."""

import json
import tempfile
from pathlib import Path
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
    src = tmp_path / "Secrets.java"
    src.write_text(
        "package com.corp;\n"
        "public class Secrets {\n"
        "    private static final String KEY = \"AKIAIOSFODNN7EXAMPLE\";\n"
        "}\n"
    )
    return str(src)


def test_guardrail_intervened_blocks_file(config, java_file):
    """When Bedrock Guardrails returns GUARDRAIL_INTERVENED, file status must be BLOCKED."""
    with patch("forge.guardrails.bedrock_guardrails.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_client.apply_guardrail.return_value = {
            "action": "GUARDRAIL_INTERVENED",
            "assessments": [{"sensitiveInformationPolicy": {"piiEntities": [{"type": "AWS_ACCESS_KEY"}]}}],
        }
        mock_boto3.client.return_value = mock_client

        from forge.agents.guardrails_pre import GuardrailsPreAgent
        agent = GuardrailsPreAgent(config)

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
        assert result["current_file"]["status"] == "BLOCKED"
        assert result["current_file"]["guardrail_pre_verdict"] == "GUARDRAIL_INTERVENED"


def test_guardrail_intervened_terminates_at_blocked_node(config, java_file):
    """Full graph: GUARDRAIL_INTERVENED at pre-check must terminate at blocked → update_state → END."""
    from langgraph.checkpoint.memory import MemorySaver

    with (
        patch("forge.guardrails.bedrock_guardrails.boto3") as mock_boto3,
        patch("forge.state_store.dynamodb.DynamoDBSaver") as MockSaver,
    ):
        mock_client = MagicMock()
        mock_client.apply_guardrail.return_value = {
            "action": "GUARDRAIL_INTERVENED",
            "assessments": [],
        }
        mock_boto3.client.return_value = mock_client
        MockSaver.return_value = MemorySaver()

        from forge.graph import build_graph
        app = build_graph(config)

        initial = {
            "current_file": make_file_status(java_file, "java21"),
            "phase": "java21",
            "dry_run": True,
            "source_dir": str(Path(java_file).parent),
            "output_dir": "./migrated",
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

        assert result["current_file"]["status"] == "BLOCKED"
        assert result["files_blocked"] == 1
        assert result["files_passed"] == 0
        # java_upgrade was never called — no transform_output
        assert result["current_file"]["transform_output"] is None
