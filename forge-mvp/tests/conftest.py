"""Shared fixtures and mock helpers.

The three original test modules each carried an identical copy of the config
and state fixtures; they live here now so a new config key only has to be added
in one place.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from forge.config import ForgeConfig
from forge.state import make_file_status

_BASE_YAML = """\
transform_model: us.anthropic.claude-sonnet-4-5-20250929-v1:0
review_model: us.amazon.nova-pro-v1:0
aws_region: us-east-1
dynamodb_table: forge-migration-state-test
dynamodb_checkpoint_table: forge-langgraph-checkpoints-test
guardrail_id: test-guardrail-id
guardrail_version: '1'
cloudwatch_namespace: FORGE/Migration
emit_cloudwatch_metrics: false
pass_threshold: 80
retry_threshold: 50
max_retries: 2
scope_package_prefix: ''
complexity_block_threshold: 2000
model_pricing:
  us.anthropic.claude-sonnet-4-5-20250929-v1:0:
    input_per_1k: 0.003
    output_per_1k: 0.015
  us.amazon.nova-pro-v1:0:
    input_per_1k: 0.0008
    output_per_1k: 0.0032
build_verification:
  enabled: false
  mode: javac
  command: ''
  classpath: ''
  timeout_seconds: 300
"""


def write_config(tmp_path: Path, **overrides) -> ForgeConfig:
    """Build a ForgeConfig from the base YAML plus scalar overrides."""
    import yaml

    cfg = yaml.safe_load(_BASE_YAML)
    cfg.update(overrides)
    path = tmp_path / "agents.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return ForgeConfig(str(path))


@pytest.fixture
def config(tmp_path):
    return write_config(tmp_path)


@pytest.fixture
def java_file(tmp_path):
    src = tmp_path / "UserAction.java"
    src.write_text(
        "package com.corp.user;\n"
        "import javax.persistence.Entity;\n"
        "import javax.servlet.http.HttpServletRequest;\n"
        "public class UserAction {\n"
        "    public void handle(HttpServletRequest req) {}\n"
        "}\n"
    )
    return str(src)


def make_state(file_path: str, tmp_path: Path, phase: str = "java21", dry_run: bool = True, **overrides) -> dict:
    state = {
        "current_file": make_file_status(file_path, phase),
        "phase": phase,
        "dry_run": dry_run,
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
    state.update(overrides)
    return state


def llm_reply(payload: dict) -> MagicMock:
    """A mock LangChain response carrying JSON content and usage metadata."""
    m = MagicMock()
    m.content = json.dumps(payload)
    m.usage_metadata = {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}
    return m


CLEAN_JAVA = (
    "package com.corp.user;\n"
    "import jakarta.persistence.Entity;\n"
    "import jakarta.servlet.http.HttpServletRequest;\n"
    "public class UserAction {\n"
    "    public void handle(HttpServletRequest req) {}\n"
    "}\n"
)
