"""The chat route, end to end through FastAPI's TestClient with the model scripted.

A chat turn is a job in the same single slot a run takes, streamed over the same
SSE frames, so these tests drive it exactly the way the run tests drive a run.
The model is patched where ``forge.leader.agent`` imported it — the house rule
is to patch the name in the module that bound it — and nothing here touches AWS.
"""

import json
import subprocess
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from forge.review_queue import QUEUE_NAME
from forge.ui import app as app_module
from forge.ui.app import create_app
from forge.ui.jobs import JobRegistry
from tests.conftest import FakeStreamingLLM, text_turn, tool_turn, write_config

LEGACY = ("package com.corp.user;\n"
          "import javax.persistence.Entity;\n"
          "public class UserAction {}\n")
# The bytes a Bedrock sensitiveInformationPolicy finding quotes verbatim. The
# transcript is JSON this very route serves back, so this is the one marker that
# may reach neither an observation nor a card.
SECRET_MARKER = "AKIAIOSFODNN7EXAMPLE9c02"


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
def client(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setattr(app_module, "KEEPALIVE_SECONDS", 0.05)
    app = create_app(JobRegistry())
    with TestClient(app) as c:
        c.registry = app.state.registry
        c.cfg = str(tmp_path / "agents.yaml")
        yield c


def _body(project, tmp_path, client, **extra):
    return {"source_dir": str(project), "output_dir": str(tmp_path / "out"), "config": client.cfg,
            "message": "what should I do here?", **extra}


def _finish(client, job_id):
    assert client.registry.wait(client.registry.get(job_id), timeout=30)
    return client.get(f"/api/runs/{job_id}").json()


def _sse(client, job_id):
    frames = []
    with client.stream("GET", f"/api/runs/{job_id}/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        cur = {}
        for line in r.iter_lines():
            if line.startswith("id: "):
                cur["id"] = int(line[4:])
            elif line.startswith("event: "):
                cur["event"] = line[7:]
            elif line.startswith("data: "):
                cur["data"] = json.loads(line[6:])
            elif line == "" and cur:
                frames.append(cur)
                cur = {}
    return frames


def _turn(client, body, turns, *, on_chunk=None):
    """POST one turn with the model scripted, and wait for the job to finish."""
    fake = FakeStreamingLLM(turns, on_chunk=on_chunk)
    with patch("forge.leader.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value = fake
        r = client.post("/api/chat", json=body)
        assert r.status_code == 202, r.text
        posted = r.json()
        _finish(client, posted["job_id"])
    return posted, fake


# ─── one turn, start to finish ───────────────────────────────────────────────

def test_a_chat_turn_is_a_job_and_its_events_arrive_in_order(client, project, tmp_path):
    posted, _ = _turn(client, _body(project, tmp_path, client),
                      [text_turn("Two packs apply here. Shall I estimate the first?")])

    job = client.get(f"/api/runs/{posted['job_id']}").json()
    assert job["kind"] == "chat" and job["state"] == "done" and job["error"] is None
    assert job["params"] == {"surface": "chat", "conversation_id": posted["conversation_id"], "phase": ""}
    assert job["result"]["conversation_id"] == posted["conversation_id"] and job["result"]["turn"] == 1

    frames = _sse(client, posted["job_id"])
    events = [f["event"] for f in frames]
    assert events[0] == "turn_start" and events[-1] == "done" and events[-2] == "usage"
    assert "assistant_delta" in events and "assistant_message" in events
    assert [f["id"] for f in frames] == list(range(1, len(frames) + 1))
    # The browser draws the user bubble from the stream, so a reload mid-turn
    # does not need the transcript to have caught up.
    assert frames[0]["data"]["user"]["text"] == "what should I do here?"
    assert frames[0]["data"]["job_id"] == posted["job_id"]


def test_the_transcript_is_what_a_reload_renders(client, project, tmp_path):
    posted, _ = _turn(client, _body(project, tmp_path, client), [text_turn("One pack applies.")])

    body = client.get(f"/api/chat/{posted['conversation_id']}").json()
    assert body["conversation_id"] == posted["conversation_id"]
    assert [i["role"] for i in body["transcript"]] == ["user", "assistant"]
    assert body["transcript"][0]["text"] == "what should I do here?"
    assert all(i["job_id"] == posted["job_id"] for i in body["transcript"]), (
        "every item carries the job that wrote it, so a reload can skip the live turn")
    assert body["pending"] == [] and body["completed"] == [] and body["active_job"] is None
    assert body["spend_usd"] == 0.0 and body["leader_cost_usd"] > 0


def test_a_tool_call_puts_its_row_and_its_cards_in_both_the_stream_and_the_transcript(client, project, tmp_path):
    posted, _ = _turn(client, _body(project, tmp_path, client),
                      [tool_turn({"name": "profile_project", "args": {}}, text="Let me look."),
                       text_turn("One pack applies: javax-to-jakarta.")])

    events = [f["event"] for f in _sse(client, posted["job_id"])]
    assert events.count("tool_start") == 1 and events.count("tool_result") == 1
    # Two cards, not one. The wizard's Discover step is gone, so the activation
    # evidence it used to show comes back with the plan as a card of its own —
    # there is nowhere else left in the UI to read it.
    assert events.count("card") == 2

    transcript = client.get(f"/api/chat/{posted['conversation_id']}").json()["transcript"]
    assert [i["role"] for i in transcript] == ["user", "assistant", "tool", "card", "card", "assistant"]
    row = [i for i in transcript if i["role"] == "tool"][0]
    assert row["tool"] == "profile_project" and row["ok"] is True
    assert [i["card"]["kind"] for i in transcript if i["role"] == "card"] == ["plan", "evidence"]


def test_no_matched_secret_ever_reaches_the_transcript_this_route_serves(client, project, tmp_path):
    """R3's last mile. An observation and a card are both reduced, and this is
    the test that proves it for the JSON the browser actually receives — the
    place a leaked credential would sit until someone cleared their storage."""
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / QUEUE_NAME).write_text(json.dumps({
        "version": 2, "run": "run-1", "phase": "javax-to-jakarta", "dry_run": False,
        "source_dir": str(project), "output_dir": str(out),
        "entries": [{
            "file_path": str(project / "src/main/java/com/corp/Other.java"),
            "rel_path": "src/main/java/com/corp/Other.java", "pack": "javax-to-jakarta",
            "status": "MANUAL_REVIEW", "risk_tier": "HIGH", "risk_reasons": ["security configuration"],
            "original": "class Other {}\n", "transformed": {"src/main/java/com/corp/Other.java": "class Other { }\n"},
            "guardrail_findings": ["sensitiveInformationPolicy: {'piiEntities': [{'match': '"
                                   + SECRET_MARKER + "', 'type': 'AWS_ACCESS_KEY'}]}"],
        }],
    }), encoding="utf-8")

    posted, _ = _turn(client, _body(project, tmp_path, client, message="what is waiting on me?"),
                      [tool_turn({"name": "list_held_files", "args": {}}),
                       text_turn("One file is waiting on you.")])

    body = client.get(f"/api/chat/{posted['conversation_id']}").json()
    assert SECRET_MARKER not in json.dumps(body, default=str)
    card = [i for i in body["transcript"] if i["role"] == "card"][0]["card"]
    assert card["kind"] == "review_file" and card["guardrail_findings"] == ["sensitiveInformationPolicy"]
    assert card["diff"], "the card still carries the diff it exists for"


# ─── what the route refuses ──────────────────────────────────────────────────

def test_a_turn_carries_exactly_one_of_a_message_and_an_action(client, project, tmp_path):
    both = _body(project, tmp_path, client, action={"type": "decline", "pending_id": "p1"})
    assert client.post("/api/chat", json=both).status_code == 400

    neither = _body(project, tmp_path, client)
    neither.pop("message")
    r = client.post("/api/chat", json=neither)
    assert r.status_code == 400 and "exactly one" in r.json()["detail"]

    blank = client.post("/api/chat", json=_body(project, tmp_path, client, message="   "))
    assert blank.status_code == 400 and "empty" in blank.json()["detail"]


def test_chat_needs_a_config_and_a_real_source_directory(client, project, tmp_path):
    bad_config = client.post("/api/chat", json=_body(project, tmp_path, client,
                                                     config=str(tmp_path / "nope.yaml")))
    assert bad_config.status_code == 400 and "config not found" in bad_config.json()["detail"]
    bad_source = client.post("/api/chat", json=_body(project, tmp_path, client,
                                                     source_dir=str(tmp_path / "nothing")))
    assert bad_source.status_code == 400


def test_a_conversation_belongs_to_one_project(client, project, tmp_path):
    """Discovery, the completed packs and every parked estimate describe one
    repository. Letting the same chat follow the browser to a second one would
    let evidence gathered on A authorise a paid run on B."""
    posted, _ = _turn(client, _body(project, tmp_path, client), [text_turn("Hello.")])

    moved = _body(project, tmp_path, client, output_dir=str(tmp_path / "somewhere-else"),
                  conversation_id=posted["conversation_id"])
    r = client.post("/api/chat", json=moved)
    assert r.status_code == 400 and "different project" in r.json()["detail"]

    same = _body(project, tmp_path, client, conversation_id=posted["conversation_id"])
    with patch("forge.leader.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value = FakeStreamingLLM([text_turn("Still here.")])
        again = client.post("/api/chat", json=same)
        assert again.status_code == 202
        _finish(client, again.json()["job_id"])
    assert again.json()["conversation_id"] == posted["conversation_id"]


def test_a_second_turn_while_one_is_running_is_409(client, project, tmp_path):
    """One job at a time is the invariant the whole UI rests on, and a chat turn
    takes the same slot — its tools are runs."""
    gate = threading.Event()

    def hold(index, chunk):
        gate.wait(10)

    fake = FakeStreamingLLM([text_turn("Thinking about it.")], on_chunk=hold)
    with patch("forge.leader.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value = fake
        first = client.post("/api/chat", json=_body(project, tmp_path, client)).json()
        second = client.post("/api/chat", json=_body(project, tmp_path, client))
        assert second.status_code == 409 and first["job_id"] in second.json()["detail"]
        assert client.get("/api/health").json()["active"]["kind"] == "chat"
        gate.set()
        assert _finish(client, first["job_id"])["state"] == "done"


def test_stopping_a_turn_leaves_the_job_cancelled_and_runs_no_tool(client, project, tmp_path):
    """Stop has to land before the tool, not after: ``run_migration`` entered
    with cancel already set overwrites the review queue with an empty one."""
    def stop_when_the_arguments_start(index, chunk):
        if any(c.get("args") for c in (chunk.tool_call_chunks or [])):
            client.registry.cancel(client.registry.active())

    script = [tool_turn({"name": "run_pack", "args": {"pack": "javax-to-jakarta", "dry_run": True}},
                        text="Starting the preview.", pieces=3)]
    with patch("forge.service.run_migration") as run:
        posted, _ = _turn(client, _body(project, tmp_path, client, message="preview it"), script,
                          on_chunk=stop_when_the_arguments_start)
        run.assert_not_called()

    assert client.get(f"/api/runs/{posted['job_id']}").json()["state"] == "cancelled"
    events = [f["event"] for f in _sse(client, posted["job_id"])]
    assert "tool_start" not in events
    transcript = client.get(f"/api/chat/{posted['conversation_id']}").json()["transcript"]
    assert transcript[-1]["role"] == "cancelled"


def test_a_turn_the_model_could_not_answer_fails_the_job_and_says_so_in_the_transcript(client, project, tmp_path):
    """The job's ``error`` event lives only in that job's stream. Without a
    transcript item the reload shows the user's message with no reply and no
    explanation."""
    fake = FakeStreamingLLM([RuntimeError("bedrock is down")])
    with patch("forge.leader.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value = fake
        posted = client.post("/api/chat", json=_body(project, tmp_path, client)).json()
        job = _finish(client, posted["job_id"])

    assert job["state"] == "failed" and "bedrock is down" in job["error"]
    assert _sse(client, posted["job_id"])[-1]["event"] == "error"
    transcript = client.get(f"/api/chat/{posted['conversation_id']}").json()["transcript"]
    assert [i["role"] for i in transcript] == ["user", "error"]
    assert "bedrock is down" in transcript[-1]["message"]


# ─── conversations come and go ───────────────────────────────────────────────

def test_an_id_this_process_has_never_seen_is_a_fresh_conversation_not_a_404(client, project, tmp_path):
    """The store dies with the server and the browser keeps the id in
    localStorage, so a 404 here would greet every restart with an error on the
    default page."""
    fresh = client.get("/api/chat/from-a-previous-server").json()
    assert fresh["conversation_id"] != "from-a-previous-server"
    assert fresh["transcript"] == [] and fresh["active_job"] is None

    posted, _ = _turn(client, _body(project, tmp_path, client, conversation_id="from-a-previous-server"),
                      [text_turn("Starting fresh.")])
    assert posted["conversation_id"] != "from-a-previous-server", (
        "the id the POST returns is the authoritative one")
    assert client.get(f"/api/chat/{posted['conversation_id']}").json()["transcript"]


def test_reset_hands_back_a_new_conversation_id(client, project, tmp_path):
    posted, _ = _turn(client, _body(project, tmp_path, client), [text_turn("Hello.")])
    cid = posted["conversation_id"]

    new_id = client.post(f"/api/chat/{cid}/reset").json()["conversation_id"]
    assert new_id != cid
    assert client.get(f"/api/chat/{new_id}").json()["transcript"] == []
    assert client.get(f"/api/chat/{cid}").json()["conversation_id"] != cid, "the old one is gone, not archived"


def test_reset_is_refused_while_that_chat_has_a_turn_running(client, project, tmp_path):
    """The job thread would keep spending into a conversation nothing can
    display any more."""
    gate = threading.Event()

    def hold(index, chunk):
        gate.wait(10)

    with patch("forge.leader.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value = FakeStreamingLLM([text_turn("Working on it.")], on_chunk=hold)
        posted = client.post("/api/chat", json=_body(project, tmp_path, client)).json()
        deadline = time.time() + 10                  # the job thread starts asynchronously
        while client.registry.active() is None and time.time() < deadline:
            time.sleep(0.01)
        refused = client.post(f"/api/chat/{posted['conversation_id']}/reset")
        assert refused.status_code == 409 and "running" in refused.json()["detail"]
        gate.set()
        _finish(client, posted["job_id"])

    assert client.post(f"/api/chat/{posted['conversation_id']}/reset").status_code == 200


def test_a_chat_job_is_visible_to_the_rest_of_the_ui(client, project, tmp_path):
    """The job bar reads ``params.phase`` for whatever is active, so a chat turn
    has to carry the key even though it has no phase to show."""
    posted, _ = _turn(client, _body(project, tmp_path, client), [text_turn("Hello.")])
    listed = client.get("/api/jobs").json()["jobs"]
    assert listed[0]["id"] == posted["job_id"] and listed[0]["kind"] == "chat"
    assert listed[0]["params"]["phase"] == ""
    assert client.get("/api/health").json()["active"] is None


# ─── the chat starts before anyone has named a folder ────────────────────────

def test_a_turn_with_no_project_yet_runs_so_the_leader_can_ask_which_folder(client, project, tmp_path):
    """The owner's objection, in one route.

    The wizard made the browser collect ``source_dir`` in a form before the chat
    would take a word — "I requested to change with prompt instead of this
    project setup". So the first turn has to run with no project at all: the
    leader's job is to ask, and it cannot ask from behind a 400. ``config`` is
    still required, because a leader with no model is not a leader.
    """
    body = {"config": client.cfg, "message": "I want to upgrade an old Java app."}
    posted, _ = _turn(client, body, [text_turn("Which folder is the repository in?")])

    job = client.get(f"/api/runs/{posted['job_id']}").json()
    assert job["state"] == "done" and job["error"] is None, job
    transcript = client.get(f"/api/chat/{posted['conversation_id']}").json()["transcript"]
    assert any(i.get("role") == "assistant" for i in transcript), transcript

    # Still unbound, so the folder the user names next is the one that sticks.
    second = {"config": client.cfg, "conversation_id": posted["conversation_id"],
              "source_dir": str(project), "output_dir": str(tmp_path / "out"),
              "message": "it is in that folder"}
    again, _ = _turn(client, second, [text_turn("Profiling it now.")])
    assert again["conversation_id"] == posted["conversation_id"]


def test_a_source_dir_that_is_there_but_wrong_is_still_a_400(client, tmp_path):
    """Optional is not "unchecked". A path the user typed that does not exist is
    a mistake to show them, not an unbound conversation to carry on with."""
    r = client.post("/api/chat", json={"config": client.cfg, "message": "here",
                                       "source_dir": str(tmp_path / "nothing")})
    assert r.status_code == 400


# ─── the whole journey, with nobody filling in a form ────────────────────────

def _git_here(repo, *args) -> str:
    done = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=False)
    assert done.returncode == 0, f"the test's own `git {' '.join(args)}` failed: {done.stderr.strip()}"
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A one-commit repository, pinned so a commit works on any machine.

    Identity, signing and hooks are set locally because a container has no
    global git identity and ``git commit`` would fail with "Please tell me who
    you are" — indistinguishable, from the outside, from landing refusing.
    """
    root = tmp_path / "repo"
    (root / "src/main/java/com/corp/user").mkdir(parents=True)
    _git_here(root, "init", "-q", "-b", "main")
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    for key, value in (("core.hooksPath", str(hooks)), ("commit.gpgsign", "false"),
                       ("user.email", "tests@forge.invalid"), ("user.name", "FORGE tests")):
        _git_here(root, "config", key, value)
    (root / "src/main/java/com/corp/user/UserAction.java").write_text(LEGACY, encoding="utf-8")
    _git_here(root, "add", "-A")
    _git_here(root, "commit", "-qm", "before FORGE")
    return root


def test_the_chat_can_take_a_project_from_a_sentence_and_land_it_without_a_form(client, repo, tmp_path):
    """The increment, end to end, over the route the browser actually posts to.

    This is the journey the owner asked for — *"I requested to change with
    prompt instead of this project setup"* — and every piece of it was built by
    a different pair of hands: the optional ``source_dir`` in the route, the
    ``set_project`` tool that binds from inside a turn, the always-confirm gate,
    and ``landing``. Each is tested on its own elsewhere. What is only visible
    here is that the conversation stays bound *between requests*: the second and
    third POSTs carry no ``source_dir`` at all, and they still act on the
    repository the user named in a sentence.
    """
    out = tmp_path / "out"
    (out / "src/main/java/com/corp/user").mkdir(parents=True)
    (out / "src/main/java/com/corp/user/UserAction.java").write_text(
        "package com.corp.user;\nimport jakarta.persistence.Entity;\npublic class UserAction {}\n",
        encoding="utf-8")
    # A run records what it wrote; landing takes only recorded files (#25).
    from forge.utils import run_manifest
    run_manifest.record(str(out), "javax-to-jakarta", [str(out / "src/main/java/com/corp/user/UserAction.java")])
    # The artifact that must never be committed: it carries the source verbatim.
    (out / QUEUE_NAME).write_text(json.dumps({"run": "r1", "entries": []}), encoding="utf-8")

    # 1. No folder yet. The leader asks for one, and the turn runs anyway.
    first = {"config": client.cfg, "message": "I have an old Java app to upgrade."}
    posted, _ = _turn(client, first, [text_turn("Which folder is the repository in?")])
    cid = posted["conversation_id"]

    # 2. The user answers in prose; the leader binds the project itself.
    named = {"config": client.cfg, "conversation_id": cid, "message": f"it is at {repo}"}
    _turn(client, named, [
        tool_turn({"name": "set_project", "args": {"source_dir": str(repo), "output_dir": str(out)}}),
        text_turn("Maven, and one pack applies."),
    ])
    body = client.get(f"/api/chat/{cid}").json()
    kinds = [i["card"]["kind"] for i in body["transcript"] if i.get("role") == "card"]
    assert kinds == ["plan", "evidence"], kinds

    # 3. Landing. No source_dir on the wire — the conversation remembers it.
    asked = {"config": client.cfg, "conversation_id": cid, "message": "put it on a branch"}
    _turn(client, asked, [
        tool_turn({"name": "land_on_branch", "args": {"branch": "forge/jakarta"}}),
        text_turn("I need you to confirm that."),
    ])
    body = client.get(f"/api/chat/{cid}").json()
    pending = body["pending"]
    assert len(pending) == 1 and pending[0]["tool"] == "land_on_branch", pending
    assert _git_here(repo, "branch", "--list") == "* main", "nothing may happen before the click"

    # 4. The click. It is the click that lands it, not the model.
    confirm = {"config": client.cfg, "conversation_id": cid,
               "action": {"type": "confirm", "pending_id": pending[0]["pending_id"]}}
    _turn(client, confirm, [text_turn("Committed on forge/jakarta. Nothing was pushed.")])

    assert _git_here(repo, "rev-parse", "--abbrev-ref", "HEAD") == "forge/jakarta"
    committed = _git_here(repo, "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert committed == ["src/main/java/com/corp/user/UserAction.java"], committed
    assert QUEUE_NAME not in _git_here(repo, "ls-files")
    assert _git_here(repo, "remote") == "", "landing never adds a remote and never pushes"

    card = [i["card"] for i in client.get(f"/api/chat/{cid}").json()["transcript"]
            if i.get("role") == "card" and i["card"]["kind"] == "land"]
    assert len(card) == 1 and card[0]["push_command"] == "git push -u origin forge/jakarta"
    assert card[0]["files_changed"] == 1


# ─── the "Trial run" box ─────────────────────────────────────────────────────

def _captured_turn(client, body):
    """POST one turn with the leader replaced, and return (leader config, run ctx)."""
    seen = {}

    class Leader:
        def __init__(self, config):
            seen["leader_config"] = config

        def run_turn(self, convo, ctx, **kwargs):
            seen["ctx"] = ctx
            return {"conversation_id": convo.id}

    with patch("forge.leader.agent.LeaderAgent", Leader):
        r = client.post("/api/chat", json=body)
        assert r.status_code == 202, r.text
        _finish(client, r.json()["job_id"])
    return seen["leader_config"], seen["ctx"]


def test_trial_swaps_the_transform_model_for_runs_and_leaves_the_leader_alone(tmp_path, project, monkeypatch):
    write_config(tmp_path, trial_transform_model="us.anthropic.claude-sonnet-5")
    monkeypatch.setattr(app_module, "KEEPALIVE_SECONDS", 0.05)
    app = create_app(JobRegistry())
    with TestClient(app) as c:
        c.registry, c.cfg = app.state.registry, str(tmp_path / "agents.yaml")
        leader_cfg, ctx = _captured_turn(c, _body(project, tmp_path, c, trial=True))
        assert ctx.config.transform_model == "us.anthropic.claude-sonnet-5"
        assert leader_cfg.transform_model != "us.anthropic.claude-sonnet-5", (
            "the leader falls back to transform_model; the box is about run cost, not the chat")

        _, ctx = _captured_turn(c, _body(project, tmp_path, c))
        assert ctx.config.transform_model == leader_cfg.transform_model, "unticked means the normal model"


def test_trial_with_no_trial_model_configured_is_refused_not_run_on_the_expensive_one(client, project, tmp_path):
    r = client.post("/api/chat", json=_body(project, tmp_path, client, trial=True))
    assert r.status_code == 400 and "trial_transform_model" in r.json()["detail"]
