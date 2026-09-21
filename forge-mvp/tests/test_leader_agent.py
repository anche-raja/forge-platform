"""The turn loop: stream, admit, execute, answer, repeat.

Everything here runs against :class:`tests.conftest.FakeStreamingLLM`, which
yields real ``AIMessageChunk``s and validates the message list it is handed. Both
halves matter. A fake that returned a finished ``AIMessage`` would make the
truncation tests vacuous — the bug they guard lives in the accumulation, where
``parse_partial_json`` quietly repairs a half-streamed call — and a fake that
accepted any history would make the pairing tests vacuous, because langchain
converts an unanswered tool call without complaining and only Bedrock rejects it.

No AWS: the model class is patched where ``forge.leader.agent`` imported it, and
every paid service call is patched too.
"""

import contextlib
import threading
from dataclasses import replace
from unittest.mock import patch

import pytest
from langchain_core.messages import (AIMessage, AIMessageChunk, HumanMessage, SystemMessage,
                                     ToolMessage)

from forge import service
from forge.leader.agent import LeaderAgent
from forge.leader.convo import Conversation
from forge.leader.tools import TOOL_NAMES, ProjectContext
from tests.conftest import FakeStreamingLLM, assert_leader_protocol, text_turn, tool_turn, write_config

LEGACY = ("package com.corp.user;\n"
          "import javax.persistence.Entity;\n"
          "public class UserAction {}\n")

# What one model call costs at the base config's Opus prices, for 1000 in / 500 out.
CALL_USD = 1000 / 1000 * 0.005 + 500 / 1000 * 0.025


@pytest.fixture
def project(tmp_path):
    base = tmp_path / "proj/src/main/java/com/corp"
    base.mkdir(parents=True)
    (base / "user").mkdir()
    (base / "user/UserAction.java").write_text(LEGACY, encoding="utf-8")
    (base / "Other.java").write_text(
        "package com.corp;\nimport javax.persistence.Entity;\npublic class Other {}\n", encoding="utf-8")
    return tmp_path / "proj"


@pytest.fixture
def ctx(project, tmp_path):
    config = write_config(tmp_path)
    return ProjectContext(source_dir=str(project), output_dir=str(tmp_path / "out"),
                          config=config, base_config=config)


@contextlib.contextmanager
def scripted(config, turns, *, on_chunk=None):
    """A LeaderAgent whose model is the script, patched where agent.py imported it."""
    fake = FakeStreamingLLM(turns, on_chunk=on_chunk)
    with patch("forge.leader.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value = fake
        yield LeaderAgent(config), fake


def _run_result(ctx, **totals):
    base = {"total": 2, "passed": 2, "manual": 0, "blocked": 0, "held": 0,
            "bedrock_calls": 6, "cost_usd": 0.42}
    base.update(totals)
    return service.RunResult(phase="javax-to-jakarta", source_dir=ctx.source_dir,
                             output_dir=ctx.output_dir, dry_run=False, statuses=[], totals=base,
                             skipped=[], queue={"entries": []}, paths={})


# ─── an ordinary turn ────────────────────────────────────────────────────────

def test_a_text_only_turn_streams_its_reply_and_stores_one_assistant_message(ctx):
    convo, events = Conversation(), []
    reply = "Two packs apply here. Shall I estimate the first one?"
    with scripted(ctx.config, [text_turn(reply)]) as (agent, fake):
        result = agent.run_turn(convo, ctx, message="what should I do?", emit=events.append, job_id="j1")

    types = [e["type"] for e in events]
    assert types[0] == "turn_start" and types[-1] == "usage" and types[-2] == "assistant_message"
    assert "assistant_delta" in types
    # The stream has to be self-sufficient: a browser that reloads mid-turn
    # skips this turn's transcript items and draws the whole thing from here.
    assert events[0]["user"] == {"role": "user", "text": "what should I do?", "id": 1, "job_id": "j1"}
    assert events[0]["turn"] == 1 and events[0]["conversation_id"] == convo.id
    assert "".join(e["text"] for e in events if e["type"] == "assistant_delta") == reply

    assert [type(m).__name__ for m in convo.history] == ["HumanMessage", "AIMessage"]
    assert convo.history[-1].content == reply and convo.history[-1].tool_calls == []
    assert [i["role"] for i in convo.transcript] == ["user", "assistant"]
    assert all(i["job_id"] == "j1" for i in convo.transcript)
    assert result["steps"] == 1 and result["turn"] == 1 and result["pending"] == []
    assert convo.job_id is None and len(fake.calls) == 1


def test_a_tool_call_is_answered_and_the_pair_reaches_the_next_model_call(ctx):
    convo, events = Conversation(), []
    script = [tool_turn({"name": "profile_project", "args": {}}, text="Let me look at the project."),
              text_turn("One pack applies: javax-to-jakarta.")]
    with scripted(ctx.config, script) as (agent, fake):
        agent.run_turn(convo, ctx, message="what is in here?", emit=events.append, job_id="j1")

    assert [type(m).__name__ for m in convo.history] == [
        "HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    asked = convo.history[1]
    assert [c["name"] for c in asked.tool_calls] == ["profile_project"]
    assert convo.history[2].tool_call_id == asked.tool_calls[0]["id"]
    assert len(fake.calls) == 2, "the tool result goes back to the model"
    assert isinstance(fake.calls[1][-1], ToolMessage), "and it is the last thing the model sees"

    types = [e["type"] for e in events]
    assert types.count("tool_start") == 1 and types.count("tool_result") == 1 and "card" in types
    row = [i for i in convo.transcript if i["role"] == "tool"][0]
    assert row["tool"] == "profile_project" and row["ok"] is True and row["title"]
    # Two cards, not one: the Discover step was deleted, so profile_project now
    # draws its activation table as an `evidence` card beside the `plan` one.
    assert [i["role"] for i in convo.transcript] == [
        "user", "assistant", "tool", "card", "card", "assistant"]
    assert [i["card"]["kind"] for i in convo.transcript if i["role"] == "card"] == ["plan", "evidence"]


def test_history_stores_a_plain_ai_message_and_never_the_streamed_chunk(ctx):
    """Replaying an accumulated chunk raises ``KeyError: 'input'`` inside the
    Bedrock converter when a ``tool_use`` block never received an input delta.
    Rebuilding from ``tool_calls`` lets Bedrock synthesise the block instead."""
    convo = Conversation()
    with scripted(ctx.config, [text_turn("Right.")]) as (agent, fake):
        agent.run_turn(convo, ctx, message="hi", job_id="j1")

    stored = convo.history[-1]
    assert type(stored) is AIMessage and not isinstance(stored, AIMessageChunk)
    assert isinstance(stored.content, str), "a streamed reply's content is a list of blocks; history keeps text"

    assert not any(isinstance(m, SystemMessage) for m in convo.history), (
        "the state block is deterministic and rebuilt every step — storing it would freeze it")
    system = fake.calls[0][0]
    assert isinstance(system, SystemMessage)
    for heading in ("PROJECT", "PLAN", "PROGRESS", "FILES WAITING ON A HUMAN",
                    "PENDING CONFIRMATIONS", "SPEND"):
        assert heading in system.content
    assert "packs you may name" in system.content
    assert "not profiled yet" in system.content


def test_the_leader_is_bound_to_the_closed_catalogue_and_nothing_else(ctx):
    with scripted(ctx.config, [text_turn("hi")]) as (agent, fake):
        assert [t["name"] for t in fake.bound_tools] == list(TOOL_NAMES)
        assert fake.bound_kwargs == {}, (
            "no tool_choice — Bedrock's default 'auto' is what a conductor loop wants")


def test_the_model_is_constructed_the_way_every_other_agent_constructs_one(ctx):
    with patch("forge.leader.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value = FakeStreamingLLM([])
        agent = LeaderAgent(ctx.config)
    kwargs = MockLLM.call_args.kwargs
    assert kwargs["model"] == "us.anthropic.claude-opus-4-8", "an empty leader.model falls back to transform_model"
    assert kwargs["region_name"] == "us-east-1" and kwargs["max_tokens"] == 2048
    assert agent.bedrock_calls == 0 and agent.cost_usd == 0.0


# ─── admission: nothing runs on a half-finished reply ────────────────────────

def test_stopping_mid_stream_never_lets_a_half_streamed_tool_call_run(ctx):
    """The verified failure this exists for: cancelling after the first argument
    delta leaves ``acc.tool_calls`` holding a perfectly valid-looking
    ``run_pack`` whose ``dry_run`` flag has silently vanished — so Stop on a
    proposed dry run would launch a real one. An interrupted stream has no
    stopReason either, so a deny-list on ``max_tokens`` cannot see it.
    """
    cancel = threading.Event()

    def stop_at_the_first_argument_delta(index, chunk):
        if any(c.get("args") for c in (chunk.tool_call_chunks or [])):
            cancel.set()

    convo, events = Conversation(), []
    script = [tool_turn({"name": "run_pack", "args": {"pack": "javax-to-jakarta", "dry_run": True}},
                        text="Starting the dry run now.", pieces=3)]
    with scripted(ctx.config, script, on_chunk=stop_at_the_first_argument_delta) as (agent, fake), \
         patch("forge.service.run_migration") as run:
        agent.run_turn(convo, ctx, message="preview it", emit=events.append, cancel=cancel, job_id="j1")
        run.assert_not_called()

    assert not any(isinstance(m, ToolMessage) for m in convo.history)
    assert convo.history[-1].tool_calls == []
    assert str(convo.history[-1].content).endswith("_(stopped)_")
    assert convo.transcript[-1]["role"] == "cancelled"
    assert len(fake.calls) == 1, "a stopped turn does not ask the model again"
    assert "tool_start" not in [e["type"] for e in events]


def test_a_reply_cut_off_by_max_tokens_runs_nothing_and_says_so(ctx):
    convo, events = Conversation(), []
    script = [tool_turn({"name": "run_pack", "args": {"pack": "javax-to-jakarta"}}, stop="max_tokens")]
    with scripted(ctx.config, script) as (agent, fake), \
         patch("forge.service.run_migration") as run:
        agent.run_turn(convo, ctx, message="go", emit=events.append, job_id="j1")
        run.assert_not_called()

    assert not any(isinstance(m, ToolMessage) for m in convo.history)
    assert "cut short: max_tokens" in convo.history[-1].content
    assert "Nothing was run" in convo.history[-1].content
    assert len(fake.calls) == 1


def test_a_stream_that_never_said_why_it_ended_runs_nothing(ctx):
    """Admission is an allow-list. A deny-list on the stop reasons anyone thought
    of leaves every other one — and ``None`` — executing."""
    convo = Conversation()
    script = [tool_turn({"name": "run_pack", "args": {"pack": "javax-to-jakarta"}},
                        text="On it.", stop=None)]
    with scripted(ctx.config, script) as (agent, fake), \
         patch("forge.service.run_migration") as run:
        agent.run_turn(convo, ctx, message="go", job_id="j1")
        run.assert_not_called()
    assert not any(isinstance(m, ToolMessage) for m in convo.history)
    assert len(fake.calls) == 1


def test_a_tool_call_with_arguments_the_schema_refuses_is_still_answered(ctx):
    """A ``toolUse`` block with no ``toolResult`` is a 400 on the next turn, so
    an invalid call is answered rather than dropped — which is also how the
    model learns to call it properly."""
    convo = Conversation()
    script = [tool_turn({"name": "run_pack", "args": {"dry_run": True}}),
              text_turn("Which pack did you mean?")]
    with scripted(ctx.config, script) as (agent, fake), \
         patch("forge.service.run_migration") as run:
        agent.run_turn(convo, ctx, message="run it", job_id="j1")
        run.assert_not_called()

    answer = [m for m in convo.history if isinstance(m, ToolMessage)][0]
    assert answer.status == "error" and "missing 'pack'" in str(answer.content)
    assert len(fake.calls) == 2, "the fake validated the pairing on the way back in"


def test_arguments_that_were_never_json_are_answered_so_the_model_can_retry(ctx):
    """Garbage arguments land in ``invalid_tool_calls`` with ``tool_calls``
    empty. Dropping them leaves an empty bubble and no way for the model to
    learn; answering them turns a malformed call into one retry."""
    convo = Conversation()
    script = [tool_turn({"name": "run_pack", "raw_args": "not json at all", "id": "tu_9"}),
              text_turn("Sorry — which pack should I run?")]
    with scripted(ctx.config, script) as (agent, fake):
        agent.run_turn(convo, ctx, message="run it", job_id="j1")

    assert [c["id"] for c in convo.history[1].tool_calls] == ["tu_9"]
    answer = [m for m in convo.history if isinstance(m, ToolMessage)][0]
    assert answer.tool_call_id == "tu_9" and "not valid JSON" in str(answer.content)
    assert len(fake.calls) == 2


# ─── the caps ────────────────────────────────────────────────────────────────

def test_the_step_cap_ends_the_turn_with_a_notice_rather_than_looping(ctx, tmp_path):
    config = write_config(tmp_path, leader={"max_steps": 2})
    ctx = replace(ctx, config=config, base_config=config)
    convo, events = Conversation(), []
    script = [tool_turn({"name": "list_held_files", "args": {}}),
              tool_turn({"name": "pack_feedback", "args": {}})]
    with scripted(ctx.config, script) as (agent, fake):
        result = agent.run_turn(convo, ctx, message="keep going", emit=events.append, job_id="j1")

    assert result["steps"] == 2 and len(fake.calls) == 2
    notice = convo.history[-1]
    assert isinstance(notice, AIMessage) and "2 steps" in notice.content and "continue" in notice.content
    assert convo.transcript[-1]["role"] == "assistant"
    assert [e["type"] for e in events][-2:] == ["assistant_message", "usage"]


def test_a_trimmed_history_never_splits_a_tool_call_from_its_answer(ctx):
    """Bedrock rejects a ``toolResult`` with no matching ``toolUse``, so the
    window moves back to a user turn rather than slicing mid-exchange — even
    when that means carrying more messages than the cap asked for."""
    convo = Conversation()
    for n in range(6):
        convo.add_message(HumanMessage(f"turn {n}"))
        convo.add_message(AIMessage(content="", tool_calls=[
            {"name": "list_held_files", "args": {}, "id": f"tu_{n}", "type": "tool_call"}]))
        convo.add_message(ToolMessage(content="{}", tool_call_id=f"tu_{n}"))
        convo.add_message(AIMessage(content=f"done {n}"))

    for limit in (4, 5, 6, 7, 10, 40):
        window = convo.trimmed_history(limit)
        assert window, f"limit {limit} produced an empty window — Bedrock sends '.' for that"
        assert isinstance(window[0], HumanMessage), f"limit {limit} started the window mid-exchange"
        assert_leader_protocol([SystemMessage("s")] + window)
    assert convo.trimmed_history(0) == convo.history, "no cap means no trimming"


def test_every_model_call_in_a_turn_is_paid_for(ctx):
    """A turn with N tool round-trips makes N+1 model calls, each re-sending the
    whole history. Counting the turn rather than the calls under-reports the
    expensive half."""
    convo, events = Conversation(), []
    script = [tool_turn({"name": "pack_feedback", "args": {}}, usage=(1000, 500)),
              text_turn("Nothing has been recorded yet.", usage=(1000, 500))]
    with scripted(ctx.config, script) as (agent, fake):
        result = agent.run_turn(convo, ctx, message="any feedback?", emit=events.append, job_id="j1")

    assert convo.leader_calls == 2 and agent.bedrock_calls == 2
    assert convo.leader_cost_usd == pytest.approx(2 * CALL_USD)
    assert result["leader_cost_usd"] == pytest.approx(2 * CALL_USD)
    usage = [e for e in events if e["type"] == "usage"][0]
    assert usage["leader_calls"] == 2 and usage["model"] == "us.anthropic.claude-opus-4-8"
    assert convo.spend_usd == 0.0, "the leader's own calls are not the pipeline's spend"


# ─── confirmations are the user's, not the model's ───────────────────────────

def test_a_second_gated_call_in_one_turn_is_refused(ctx, tmp_path):
    """Two confirm cards for the same turn are two ways to spend the same
    money, and the user can only mean one of them."""
    config = write_config(tmp_path, leader={"confirm_above_usd": 0.01})
    ctx = replace(ctx, config=config, base_config=config)
    convo = Conversation()
    script = [tool_turn({"name": "run_pack", "args": {"pack": "javax-to-jakarta"}, "id": "tu_1"},
                        {"name": "run_pack", "args": {"pack": "javax-to-jakarta", "dry_run": True},
                         "id": "tu_2"}),
              text_turn("Confirm the first and I will carry on.")]
    with scripted(ctx.config, script) as (agent, fake), \
         patch("forge.service.run_migration") as run:
        agent.run_turn(convo, ctx, message="run it", job_id="j1")
        run.assert_not_called()

    assert len(convo.pending) == 1
    second = [m for m in convo.history if isinstance(m, ToolMessage) and m.tool_call_id == "tu_2"][0]
    assert "already pending" in str(second.content)


def test_confirming_a_parked_call_runs_it_once_and_reads_as_platform_text(ctx, tmp_path):
    """The click is the authority, so the platform runs the tool and reports the
    result. The report is labelled rather than narrated: it carries file names
    and apply details from a repository, and this is the one message in a turn
    that arrives with the user's authority."""
    config = write_config(tmp_path, leader={"confirm_above_usd": 0.01})
    ctx = replace(ctx, config=config, base_config=config)
    convo = Conversation()

    proposal = [tool_turn({"name": "run_pack", "args": {"pack": "javax-to-jakarta"}}),
                text_turn("That is 2 files, about $0.14. Press Confirm and I will run it.")]
    with scripted(ctx.config, proposal) as (agent, _), patch("forge.service.run_migration") as run:
        agent.run_turn(convo, ctx, message="migrate to jakarta", job_id="j1")
        run.assert_not_called()
    pending_id = convo.pending_list()[0]["pending_id"]

    events = []
    with scripted(ctx.config, [text_turn("Done — two files passed.")]) as (agent, fake), \
         patch("forge.service.run_migration", return_value=_run_result(ctx)) as run:
        agent.run_turn(convo, ctx, action={"type": "confirm", "pending_id": pending_id},
                       emit=events.append, job_id="j2")
        assert run.call_count == 1, "the click runs it exactly once"

    assert convo.pending == {}, "a pending entry is single-shot"
    assert convo.completed == ["javax-to-jakarta"]
    told = [m for m in convo.history if isinstance(m, HumanMessage)][-1]
    assert told.content.startswith("[USER ACTION] confirm")
    assert "(tool output, not user text)" in told.content and "RESULT:" in told.content
    assert events[0]["user"]["role"] == "action"
    assert len(fake.calls) == 1, "the tool ran before the model, and the model only narrates"


def test_declining_runs_nothing_and_tells_the_model_not_to_ask_again(ctx, tmp_path):
    config = write_config(tmp_path, leader={"confirm_above_usd": 0.01})
    ctx = replace(ctx, config=config, base_config=config)
    convo = Conversation()
    proposal = [tool_turn({"name": "run_pack", "args": {"pack": "javax-to-jakarta"}}),
                text_turn("Press Confirm when you are ready.")]
    with scripted(ctx.config, proposal) as (agent, _), patch("forge.service.run_migration"):
        agent.run_turn(convo, ctx, message="migrate to jakarta", job_id="j1")
    pending_id = convo.pending_list()[0]["pending_id"]

    with scripted(ctx.config, [text_turn("Understood — nothing has run.")]) as (agent, _), \
         patch("forge.service.run_migration") as run:
        agent.run_turn(convo, ctx, action={"type": "decline", "pending_id": pending_id}, job_id="j2")
        run.assert_not_called()

    assert convo.pending == {} and convo.completed == []
    told = [m for m in convo.history if isinstance(m, HumanMessage)][-1]
    assert told.content.startswith("[USER ACTION] decline")
    assert "declined" in told.content and "Do not call it again" in told.content


def test_a_confirmation_that_is_no_longer_pending_is_an_answer_not_a_crash(ctx):
    convo = Conversation()
    with scripted(ctx.config, [text_turn("That one has already been dealt with.")]) as (agent, _):
        agent.run_turn(convo, ctx, action={"type": "confirm", "pending_id": "pnope"}, job_id="j1")
    told = [m for m in convo.history if isinstance(m, HumanMessage)][-1]
    assert "no longer pending" in told.content


# ─── failure leaves a trace ──────────────────────────────────────────────────

def test_a_failed_turn_leaves_an_error_item_and_a_history_the_next_turn_can_send(ctx):
    """The transcript is what a reload renders, so a turn that dies in the job
    has to say so there — otherwise the user sees their message with no reply
    and no explanation."""
    convo = Conversation()
    script = [tool_turn({"name": "pack_feedback", "args": {}}), RuntimeError("bedrock is down")]
    with scripted(ctx.config, script) as (agent, _):
        with pytest.raises(RuntimeError, match="bedrock is down"):
            agent.run_turn(convo, ctx, message="carry on", job_id="j1")

    assert convo.transcript[-1]["role"] == "error"
    assert "bedrock is down" in convo.transcript[-1]["message"]
    assert convo.job_id is None, "the turn releases the conversation even when it dies"

    with scripted(ctx.config, [text_turn("Back with you.")]) as (agent, fake):
        agent.run_turn(convo, ctx, message="try again", job_id="j2")
    assert len(fake.calls) == 1, "the fake accepted the repaired history"


def test_the_fake_model_refuses_a_history_bedrock_would_refuse():
    """The guard is the whole value of the fake, so it gets a test of its own:
    langchain converts an unanswered tool call without a murmur, and the 400
    arrives from Bedrock at run time in front of a user."""
    call = {"name": "list_held_files", "args": {}, "id": "tu_1", "type": "tool_call"}
    with pytest.raises(AssertionError, match="no ToolMessage"):
        assert_leader_protocol([SystemMessage("s"), HumanMessage("hi"),
                                AIMessage(content="", tool_calls=[call])])
    with pytest.raises(AssertionError, match="no tool call"):
        assert_leader_protocol([SystemMessage("s"), HumanMessage("hi"),
                                ToolMessage(content="{}", tool_call_id="tu_1")])
    with pytest.raises(AssertionError, match="system prompt"):
        assert_leader_protocol([HumanMessage("hi")])
    with pytest.raises(AssertionError, match="belongs in messages"):
        assert_leader_protocol([SystemMessage("s"), HumanMessage("hi"), SystemMessage("again")])
