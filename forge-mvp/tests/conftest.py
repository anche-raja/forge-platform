"""Shared fixtures and mock helpers.

The three original test modules each carried an identical copy of the config
and state fixtures; they live here now so a new config key only has to be added
in one place.
"""

import json
from pathlib import Path
import contextlib
from unittest.mock import MagicMock, patch

import pytest

from forge.config import ForgeConfig
from forge.state import make_file_status

_BASE_YAML = """\
transform_model: us.anthropic.claude-opus-4-8
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
  us.anthropic.claude-opus-4-8:
    input_per_1k: 0.005
    output_per_1k: 0.025
  us.amazon.nova-pro-v1:0:
    input_per_1k: 0.0008
    output_per_1k: 0.0032
build_verification:
  enabled: false
  mode: javac
  command: ''
  classpath: ''
  timeout_seconds: 300
context:
  max_chars: 60000
test_generation:
  enabled: true
  style: junit5
  pass_threshold: 75
  retry_threshold: 50
  max_retries: 1
  overwrite: false
  max_source_chars: 60000
  context_max_chars: 12000
  kinds: []
  run_tests:
    enabled: false
    mode: maven
    command: ''
    timeout_seconds: 900
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


@contextlib.contextmanager
def mocked_aws(config_path=None, review_score: int = 95):
    """Patch every AWS touchpoint a run reaches.

    forge.graph binds DynamoDBSaver at import, so both the source name and the
    graph's copy are patched — a module imported before the patch would
    otherwise keep the real class.
    """
    with contextlib.ExitStack() as stack:
        boto_gr = stack.enter_context(patch("forge.guardrails.bedrock_guardrails.boto3"))
        stack.enter_context(patch("forge.state_store.dynamodb.boto3"))
        pre = stack.enter_context(patch("forge.agents.guardrails_pre.ChatBedrockConverse"))
        up = stack.enter_context(patch("forge.agents.java_upgrade.ChatBedrockConverse"))
        rev = stack.enter_context(patch("forge.review.java_reviewer.ChatBedrockConverse"))
        post = stack.enter_context(patch("forge.agents.guardrails_post.ChatBedrockConverse"))
        saver = stack.enter_context(patch("forge.state_store.dynamodb.DynamoDBSaver"))
        graph_saver = stack.enter_context(patch("forge.graph.DynamoDBSaver"))
        metrics = stack.enter_context(patch("forge.utils.telemetry.MetricsEmitter"))

        from langgraph.checkpoint.memory import MemorySaver
        saver.return_value = MemorySaver()
        graph_saver.return_value = saver.return_value

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
            return llm_reply({"files": {path: MIGRATED_JAVA}, "manual_flags": []})

        up.return_value.invoke.side_effect = transform
        yield {"metrics": metrics, "upgrade": up, "review": rev, "pre": pre, "post": post}


def generated_test(fqcn: str) -> str:
    """A minimal JUnit 5 test class that passes every mechanical check."""
    package, _, cls = fqcn.rpartition(".")
    head = f"package {package};\n\n" if package else ""
    return (
        head
        + "import org.junit.jupiter.api.Test;\n"
        + "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        + f"class {cls} {{\n"
        + "    @Test\n"
        + "    void addsUpTheWayItAlwaysDid() {\n"
        + "        assertEquals(2, 1 + 1);\n"
        + "    }\n"
        + "}\n"
    )


@contextlib.contextmanager
def mocked_testgen(review_score: int = 90, content=None, payload=None):
    """Patch the two models test generation calls, plus the checkpointer.

    ``content`` is a callable taking the test's FQCN and returning the file
    body, so a test can inject a deliberately broken one; ``payload`` replaces
    the whole generator response.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("forge.state_store.dynamodb.boto3"))
        gen = stack.enter_context(patch("forge.agents.test_gen.ChatBedrockConverse"))
        rev = stack.enter_context(patch("forge.review.test_reviewer.ChatBedrockConverse"))
        saver = stack.enter_context(patch("forge.testgen.graph.DynamoDBSaver"))

        from langgraph.checkpoint.memory import MemorySaver
        saver.return_value = MemorySaver()

        body = content or generated_test

        def generate(messages):
            human = messages[1].content
            fqcn = human.split("Test class to produce: ", 1)[1].splitlines()[0].strip()
            if payload is not None:
                return llm_reply(payload)
            path = "src/test/java/" + fqcn.replace(".", "/") + ".java"
            return llm_reply({"files": {path: body(fqcn)}, "cases": [{"name": "addsUpTheWayItAlwaysDid"}],
                              "untested": [], "dependencies": ["org.junit.jupiter:junit-jupiter"], "notes": []})

        gen.return_value.invoke.side_effect = generate
        rev.return_value.invoke.return_value = llm_reply(
            {"score": review_score, "verdict": "PASS", "feedback": "", "checks": {}}
        )
        yield {"generate": gen, "review": rev}


MIGRATED_JAVA = """\
package com.corp.user;
import jakarta.persistence.Entity;
public class UserAction {
    public void handle() {}
}
"""


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
