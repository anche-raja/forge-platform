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
from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage, ToolMessage
from langchain_core.messages.tool import tool_call_chunk

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
leader:
  model: ''
  max_steps: 8
  max_tokens: 2048
  confirm_above_usd: 1.0
  unit_cost_usd: 0.07
  history_messages: 40
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


# ─── the leader's model, scripted ─────────────────────────────────────────────

def text_turn(text: str, *, pieces: int = 3, usage=(1000, 50), stop: str = "end_turn") -> dict:
    """One scripted model turn that only talks."""
    return {"text": text, "tool_calls": [], "pieces": pieces, "usage": usage, "stop": stop}


def tool_turn(*calls, text: str = "", pieces: int = 2, usage=(1000, 50), stop: str = "tool_use") -> dict:
    """One scripted model turn that calls tools.

    Each call is ``{"name", "args"}`` — or ``{"name", "raw_args": "<junk>"}`` to
    stream arguments that are not JSON, which is how a real model's malformed
    call arrives (``invalid_tool_calls``, with ``tool_calls`` empty).
    ``stop=None`` streams a reply that never says why it ended, which is what an
    interrupted stream looks like.
    """
    return {"text": text, "tool_calls": [dict(c) for c in calls], "pieces": pieces,
            "usage": usage, "stop": stop}


def _split(text: str, pieces: int):
    if not text:
        return []
    step = max(1, -(-len(text) // max(1, pieces)))
    return [text[i:i + step] for i in range(0, len(text), step)]


def assert_leader_protocol(messages):
    """Refuse what Bedrock would refuse, where a test can still see it.

    langchain converts an ``AIMessage`` carrying two ``toolUse`` blocks and one
    matching ``toolResult`` without a murmur; the 400 arrives from Bedrock, on a
    real call, in front of a user. So the fake model checks the two invariants
    the leader loop promises — the system block is rebuilt per call and never
    stored, and every tool call is answered — rather than letting a history test
    pass on a request that could never be sent.
    """
    assert messages, "the leader called the model with no messages at all"
    assert isinstance(messages[0], SystemMessage), (
        f"the first message must be the system prompt + state block, got {type(messages[0]).__name__}")
    strays = [i for i, m in enumerate(messages[1:], 1) if isinstance(m, SystemMessage)]
    assert not strays, f"the state block was stored in history at {strays}; it belongs in messages[0] only"

    asked = {}
    for m in messages:
        if isinstance(m, AIMessage):
            for call in m.tool_calls or []:
                asked[call.get("id")] = call.get("name")
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    unanswered = sorted(f"{name}({cid})" for cid, name in asked.items() if cid not in answered)
    assert not unanswered, f"tool calls with no ToolMessage: {', '.join(unanswered)} — Bedrock rejects that turn"
    orphans = sorted(m.tool_call_id for m in messages
                     if isinstance(m, ToolMessage) and m.tool_call_id not in asked)
    assert not orphans, f"tool results with no tool call: {', '.join(orphans)} — Bedrock rejects that turn"
    return messages


class FakeStreamingLLM:
    """A scripted ``ChatBedrockConverse`` that yields real ``AIMessageChunk``s.

    A fake that returns a finished ``AIMessage`` makes every history test
    vacuous, because the machinery a truncated tool call slips through is the
    accumulation itself: ``+`` merges ``tool_call_chunks`` by index and
    ``parse_partial_json`` silently repairs half-streamed arguments, so
    ``{"pack": "javax-to-jakarta", "dry_r`` accumulates to a *valid-looking*
    call with ``dry_run`` gone. The chunk sequence below is the one
    ``_parse_stream_event`` produces (llm.md §2), so the leader's admission
    rules are exercised against the thing they exist to catch.

    ``on_chunk(index, chunk)`` runs after the consumer has processed each chunk
    — that is where a test presses Stop mid-stream.
    """

    def __init__(self, turns, *, on_chunk=None):
        self.turns = list(turns)
        self.calls = []          # the message list of every model call, snapshotted
        self.bound_tools = None
        self.bound_kwargs = {}
        self.on_chunk = on_chunk
        # Bedrock mints a fresh `toolUse` id for every call in a conversation,
        # never one per turn. A fake that restarted at "tu_1" each turn handed
        # the browser three tool rows under one id — chat.js keys its live rows
        # on `tool_id`, so the second run's progress painted over the first
        # row's — and hid that from every test here. The counter runs for the
        # life of the fake so the scripts exercise the ids reality sends.
        self._minted = 0

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = list(tools)
        self.bound_kwargs = dict(kwargs)
        return self

    def stream(self, messages, config=None, **kwargs):
        # The loop appends to the same history list while it runs, so the
        # snapshot has to be taken here or every recorded call looks identical.
        messages = list(messages)
        self.calls.append(messages)
        assert_leader_protocol(messages)
        if not self.turns:
            raise AssertionError(
                f"FakeStreamingLLM: the model was called {len(self.calls)} times; the script has "
                "no turn left. Either the loop did not stop, or the script is short.")
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        for i, chunk in enumerate(self._chunks(turn)):
            yield chunk
            if self.on_chunk is not None:
                self.on_chunk(i, chunk)

    def invoke(self, messages, config=None, **kwargs):
        acc = None
        for chunk in self.stream(messages, config, **kwargs):
            acc = chunk if acc is None else acc + chunk
        return acc

    def _chunks(self, turn):
        pieces = int(turn.get("pieces") or 1)
        index = 0
        yield AIMessageChunk(content=[])                                     # messageStart
        text = str(turn.get("text") or "")
        if text:
            for piece in _split(text, pieces):
                yield AIMessageChunk(content=[{"type": "text", "text": piece, "index": index}])
            yield AIMessageChunk(content=[])                                 # contentBlockStop
            index += 1
        for call in turn.get("tool_calls") or []:
            self._minted += 1
            call_id = str(call.get("id") or f"tu_{self._minted}")
            name = str(call.get("name") or "")
            yield AIMessageChunk(
                content=[{"type": "tool_use", "name": name, "id": call_id, "index": index}],
                tool_call_chunks=[tool_call_chunk(name=name, id=call_id, args=None, index=index)])
            raw = call.get("raw_args")
            raw = raw if raw is not None else json.dumps(call.get("args") or {})
            for piece in _split(raw, pieces):
                yield AIMessageChunk(
                    content=[{"type": "tool_use", "input": piece, "id": None, "index": index}],
                    tool_call_chunks=[tool_call_chunk(name=None, id=None, args=piece, index=index)])
            yield AIMessageChunk(content=[])
            index += 1
        stop = turn.get("stop")
        if stop is not None:
            yield AIMessageChunk(content="", response_metadata={"stopReason": stop})   # messageStop
        usage = turn.get("usage")
        if usage:
            tokens_in, tokens_out = usage
            yield AIMessageChunk(content="", usage_metadata={
                "input_tokens": tokens_in, "output_tokens": tokens_out,
                "total_tokens": tokens_in + tokens_out})                     # metadata
        yield AIMessageChunk(content=[], chunk_position="last")
