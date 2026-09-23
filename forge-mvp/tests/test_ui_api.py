"""The local UI's API, end to end through FastAPI's TestClient with AWS mocked."""

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from forge.ui import app as app_module
from forge.ui.app import create_app
from forge.ui.jobs import JobBusy, JobRegistry
from tests.conftest import mocked_aws, write_config

LEGACY = "package com.corp.user;\nimport javax.persistence.Entity;\npublic class UserAction {}\n"
HIGH = "package com.corp;\npublic class WebSecurityConfig extends WebSecurityConfigurerAdapter {}\n"


@pytest.fixture
def project(tmp_path):
    base = tmp_path / "proj/src/main/java/com/corp"
    base.mkdir(parents=True)
    (base / "user").mkdir()
    (base / "user/UserAction.java").write_text(LEGACY, encoding="utf-8")
    (base / "Other.java").write_text("package com.corp;\nimport javax.persistence.Entity;\npublic class Other {}\n", encoding="utf-8")
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


@pytest.fixture
def aws():
    with mocked_aws() as mocks, patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"), \
         patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending"):
        yield mocks


def _run_body(project, tmp_path, client, **extra):
    return {"source_dir": str(project), "output_dir": str(tmp_path / "out"), "config": client.cfg,
            "phase": "javax-to-jakarta", **extra}


def _finish(client, job_id):
    assert client.registry.wait(client.registry.get(job_id), timeout=30)
    return client.get(f"/api/runs/{job_id}").json()


def _sse(client, job_id, headers=None):
    frames = []
    with client.stream("GET", f"/api/runs/{job_id}/events", headers=headers or {}) as r:
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


# ─── static / packs / discover ────────────────────────────────────────────────

def test_index_and_health(client):
    assert client.get("/").status_code == 200
    h = client.get("/api/health").json()
    assert h["ok"] and h["active"] is None


def test_packs_lists_runnable_phases_and_decision_options(client):
    data = client.get("/api/packs").json()
    assert "javax-to-jakarta" in data["runnable"] and "java21" in data["phases"]
    assert data["decision_defaults"]["risk_ceiling"] == "review-high"
    assert data["decision_options"]["risk_ceiling"] == ["auto", "review-high", "review-all"]
    assert any(p["id"] == "struts2-modernize" and p["runnable"] for p in data["packs"])


def test_discover_profiles_the_project(client, project, tmp_path):
    r = client.post("/api/discover", json={"source_dir": str(project), "output_dir": str(tmp_path / "out")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "javax-to-jakarta" in [a["pack"] for a in data["activations"]]
    assert Path(data["paths"]["yaml"]).exists()
    assert client.post("/api/discover", json={"source_dir": str(tmp_path / "nope")}).status_code == 400


# ─── runs ─────────────────────────────────────────────────────────────────────

def test_run_reaches_done_with_totals_and_artifacts(client, project, tmp_path, aws):
    r = client.post("/api/runs", json=_run_body(project, tmp_path, client))
    assert r.status_code == 202, r.text
    job = _finish(client, r.json()["job_id"])
    assert job["state"] == "done" and job["error"] is None
    assert job["result"]["totals"]["passed"] == 2 and job["result"]["queue_count"] == 0
    assert (tmp_path / "out/src/main/java/com/corp/user/UserAction.java").exists()
    listed = client.get("/api/jobs").json()["jobs"]
    assert listed[0]["id"] == job["id"] and listed[0]["kind"] == "run"


def test_bad_requests(client, project, tmp_path):
    assert client.post("/api/runs", json=_run_body(project, tmp_path, client, phase="ejb2-to-spring")).status_code == 400
    assert client.post("/api/runs", json=_run_body(project, tmp_path, client, source_dir=str(tmp_path / "x"))).status_code == 400
    assert client.post("/api/runs", json=_run_body(project, tmp_path, client, config="/nope/agents.yaml")).status_code == 400
    assert client.post("/api/runs", json=_run_body(project, tmp_path, client, decisions={"risk_ceiling": "yolo"})).status_code == 400
    assert client.get("/api/runs/nope").status_code == 404


def test_a_broken_config_is_a_400_with_its_message_not_a_500(client, project, tmp_path):
    """An unreadable agents.yaml has to explain itself through the API.

    ConfigError used to escape `_config` on the request thread. FastAPI has no
    handler for it, so the browser got a plain-text "Internal Server Error"
    while the one message that says how to repair the file stayed behind in the
    server's terminal. Every one of these reached the user as a 500.
    """
    def status_and_detail(name, text):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        r = client.post("/api/runs", json=_run_body(project, tmp_path, client, config=str(path)))
        return r.status_code, str(r.json().get("detail", ""))

    # A failed generator run leaves this behind: `> file` truncates before it starts.
    code, detail = status_and_detail("empty.yaml", "")
    assert code == 400 and "is empty" in detail
    assert "--out" in detail, "the message must name the flag that avoids the truncation"

    # agents.yaml.example copied into place, placeholders intact.
    code, detail = status_and_detail("placeholder.yaml", "guardrail_id: REPLACE_WITH_GUARDRAIL_ID\n")
    assert code == 400 and "guardrail_id" in detail

    # Valid YAML, wrong shape — a scalar answers `.get` with an AttributeError too.
    code, detail = status_and_detail("scalar.yaml", "just a string\n")
    assert code == 400 and "not a YAML mapping" in detail
    assert client.get("/api/runs/nope/events").status_code == 404


def test_second_run_while_one_is_active_is_409(client, project, tmp_path, aws):
    gate = threading.Event()
    original = aws["upgrade"].return_value.invoke.side_effect

    def slow(messages):
        gate.wait(10)
        return original(messages)

    aws["upgrade"].return_value.invoke.side_effect = slow
    first = client.post("/api/runs", json=_run_body(project, tmp_path, client)).json()["job_id"]
    second = client.post("/api/runs", json=_run_body(project, tmp_path, client))
    assert second.status_code == 409 and first in second.json()["detail"]
    assert client.get("/api/health").json()["active"]["id"] == first
    gate.set()
    assert _finish(client, first)["state"] == "done"


def test_sse_streams_file_events_then_done_and_resumes_from_last_event_id(client, project, tmp_path, aws):
    job_id = client.post("/api/runs", json=_run_body(project, tmp_path, client)).json()["job_id"]
    frames = _sse(client, job_id)
    events = [f["event"] for f in frames]
    assert events == ["start", "file", "file", "summary", "done"]
    assert [f["id"] for f in frames] == [1, 2, 3, 4, 5]
    assert frames[1]["data"]["label"] == "Other.java" and frames[-1]["data"]["state"] == "done"
    resumed = _sse(client, job_id, headers={"Last-Event-ID": "3"})
    assert [f["id"] for f in resumed] == [4, 5]


def test_cancel_stops_between_units_and_still_writes_artifacts(client, project, tmp_path, aws):
    gate, started = threading.Event(), threading.Event()
    original = aws["upgrade"].return_value.invoke.side_effect

    def slow(messages):
        if not started.is_set():
            started.set()      # unit 1 is under way; the cancel lands between units
            gate.wait(10)
        return original(messages)

    aws["upgrade"].return_value.invoke.side_effect = slow
    job_id = client.post("/api/runs", json=_run_body(project, tmp_path, client)).json()["job_id"]
    assert started.wait(10)
    assert client.post(f"/api/runs/{job_id}/cancel").json()["cancelled"] is True
    gate.set()
    job = _finish(client, job_id)
    assert job["state"] == "cancelled" and job["result"]["cancelled"] is True
    assert job["result"]["totals"]["total"] == 1
    assert (tmp_path / "out/migration-report.md").exists()
    assert client.post(f"/api/runs/{job_id}/cancel").json()["cancelled"] is False


def test_nothing_to_do_is_a_done_job_with_a_message(client, tmp_path, aws):
    empty = tmp_path / "empty"
    empty.mkdir()
    job_id = client.post("/api/runs", json=_run_body(empty, tmp_path, client)).json()["job_id"]
    job = _finish(client, job_id)
    assert job["state"] == "done" and job["result"] is None
    frames = _sse(client, job_id)
    assert [f["event"] for f in frames] == ["nothing", "done"]
    assert "No eligible files" in frames[0]["data"]["message"]


def test_failing_job_reports_failed_with_an_error_event(client, project, tmp_path):
    with patch("forge.service.run_migration", side_effect=RuntimeError("boom")):
        job_id = client.post("/api/runs", json=_run_body(project, tmp_path, client)).json()["job_id"]
        job = _finish(client, job_id)
    assert job["state"] == "failed" and job["error"] == "RuntimeError: boom"
    frames = _sse(client, job_id)
    assert frames[-1]["event"] == "error" and "boom" in frames[-1]["data"]["traceback"]


def test_risk_ceiling_in_the_body_reaches_the_hold_gate(client, tmp_path, aws):
    from tests.test_extract_web_bootstrap import make_module

    make_module(tmp_path / "proj", java=("LoggingFilter.java",))
    body = {"source_dir": str(tmp_path / "proj"), "output_dir": str(tmp_path / "out"), "config": client.cfg,
            "phase": "liberty-server-config"}
    held = _finish(client, client.post("/api/runs", json=body).json()["job_id"])
    assert held["result"]["files"][0]["status"] == "HELD", "default review-high holds a generated unit"
    auto = _finish(client, client.post("/api/runs", json={**body, "output_dir": str(tmp_path / "out2"),
                                                           "decisions": {"risk_ceiling": "auto"}}).json()["job_id"])
    assert auto["result"]["files"][0]["status"] == "DONE"
    assert list((tmp_path / "out2").rglob("server.xml")), "auto writes the generated server.xml"


# ─── review ───────────────────────────────────────────────────────────────────

def _run_with_hold(client, project, tmp_path):
    (project / "src/main/java/com/corp/WebSecurityConfig.java").write_text(HIGH, encoding="utf-8")
    job = _finish(client, client.post("/api/runs", json=_run_body(project, tmp_path, client)).json()["job_id"])
    assert job["result"]["totals"]["held"] == 1
    return job


def test_review_returns_the_fieldset_contract(client, project, tmp_path, aws):
    assert client.get("/api/review", params={"output_dir": str(tmp_path / "none")}).status_code == 404
    _run_with_hold(client, project, tmp_path)
    data = client.get("/api/review", params={"output_dir": str(tmp_path / "out")}).json()
    assert data["count"] == 1 and data["by_status"] == {"HELD": 1}
    assert data["entries"][0]["rel_path"] == "src/main/java/com/corp/WebSecurityConfig.java"
    assert 'fieldset class="decision" data-file="src/main/java/com/corp/WebSecurityConfig.java"' in data["entries_html"]
    assert 'textarea class="note"' in data["entries_html"] and "fieldset.decision" in data["css"]


def test_review_rejects_a_stale_queue_version(client, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "manual-review-queue.json").write_text(json.dumps({"version": 1, "entries": []}), encoding="utf-8")
    assert client.get("/api/review", params={"output_dir": str(out)}).status_code == 409


def test_decisions_post_applies_and_empties_the_queue(client, project, tmp_path, aws):
    _run_with_hold(client, project, tmp_path)
    body = {"source_dir": str(project), "output_dir": str(tmp_path / "out"), "config": client.cfg,
            "decisions": [{"file": "src/main/java/com/corp/WebSecurityConfig.java", "pack": "javax-to-jakarta",
                           "decision": "approve", "note": "fine"}]}
    r = client.post("/api/review/decisions", json=body)
    assert r.status_code == 202, r.text
    job = _finish(client, r.json()["job_id"])
    assert job["kind"] == "apply" and job["state"] == "done"
    assert job["result"]["all_applied"] and job["result"]["outcomes"][0]["status_after"] == "DONE"
    frames = _sse(client, job["id"])
    assert [f["event"] for f in frames] == ["apply_outcome", "apply_done", "done"]
    assert (tmp_path / "out/src/main/java/com/corp/WebSecurityConfig.java").exists()
    assert client.get("/api/review", params={"output_dir": str(tmp_path / "out")}).json()["count"] == 0
    assert (tmp_path / "out/decisions-applied.jsonl").exists()


def test_decisions_post_validates_like_the_cli(client, project, tmp_path, aws):
    _run_with_hold(client, project, tmp_path)
    base = {"source_dir": str(project), "output_dir": str(tmp_path / "out"), "config": client.cfg}
    bad = client.post("/api/review/decisions", json={**base, "decisions": [{"file": "x", "decision": "maybe"}]})
    assert bad.status_code == 400 and "needs 'file' and a 'decision'" in bad.json()["detail"]
    assert client.post("/api/review/decisions", json={**base, "decisions": []}).status_code == 400
    assert client.post("/api/review/decisions", json={**base, "output_dir": str(tmp_path / "none"),
                                                       "decisions": [{"file": "x", "decision": "approve"}]}).status_code == 404


# ─── acceptance / feedback / artifacts / files ────────────────────────────────

def test_acceptance_feedback_and_artifacts(client, project, tmp_path, aws):
    _run_with_hold(client, project, tmp_path)
    out = str(tmp_path / "out")
    acc = client.post("/api/acceptance", json={"source_dir": str(project), "output_dir": out, "config": client.cfg,
                                               "phase": "javax-to-jakarta"})
    assert acc.status_code == 200, acc.text
    assert acc.json()["verdict"] in ("PASS", "FAIL", "INCOMPLETE") and isinstance(acc.json()["results"], list)
    assert client.post("/api/acceptance", json={"source_dir": str(project), "output_dir": str(tmp_path / "none"),
                                                "config": client.cfg, "phase": "javax-to-jakarta"}).status_code == 404

    fb = client.get("/api/feedback", params={"output_dir": out}).json()
    assert fb["notes"] == 0 and fb["path"].endswith("pack-feedback.md")

    arts = client.get("/api/artifacts", params={"output_dir": out}).json()["artifacts"]
    names = {a["name"] for a in arts}
    assert {"migration-report.md", "migration-review.html", "manual-review-queue.json",
            "migration-acceptance.json", "pack-feedback.md"} <= names
    served = client.get(next(a["url"] for a in arts if a["name"] == "migration-report.md"))
    assert served.status_code == 200 and served.headers["content-type"].startswith("text/markdown")
    assert "# FORGE" in served.text or "Migration" in served.text


def test_files_refuses_anything_outside_output_dir(client, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "migration-report.md").write_text("# report", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("s3cret", encoding="utf-8")
    os.symlink(tmp_path / "secret.txt", out / "link.txt")

    ok = client.get("/api/files", params={"output_dir": str(out), "name": "migration-report.md"})
    assert ok.status_code == 200 and ok.text == "# report"
    for name in ("../secret.txt", str(tmp_path / "secret.txt"), "link.txt", "sub/../../secret.txt"):
        r = client.get("/api/files", params={"output_dir": str(out), "name": name})
        assert r.status_code == 403, name
    assert client.get("/api/files", params={"output_dir": str(out), "name": "missing.md"}).status_code == 404


# ─── test generation ──────────────────────────────────────────────────────────

def test_testgen_is_a_job_like_a_run_and_its_record_is_served(client, project, tmp_path, aws):
    from tests.conftest import mocked_testgen

    out = str(tmp_path / "out")
    started = client.post("/api/runs", json=_run_body(project, tmp_path, client))
    _finish(client, started.json()["job_id"])

    assert client.get("/api/testgen", params={"output_dir": out}).status_code == 404

    with mocked_testgen():
        r = client.post("/api/testgen", json={"source_dir": str(project), "output_dir": out, "config": client.cfg})
        assert r.status_code == 202, r.text
        job = _finish(client, r.json()["job_id"])

    assert job["state"] == "done"
    assert job["result"]["totals"]["generated"] == 2
    assert (tmp_path / "out/src/test/java/com/corp/user/UserActionTest.java").is_file()

    record = client.get("/api/testgen", params={"output_dir": out}).json()
    assert record["version"] == 1 and len(record["units"]) == 2

    names = {a["name"] for a in client.get("/api/artifacts", params={"output_dir": out}).json()["artifacts"]}
    assert {"test-generation-report.md", "generated-tests.json"} <= names


def test_testgen_needs_an_output_directory_to_have_migrated_into(client, project, tmp_path):
    r = client.post("/api/testgen", json={"source_dir": str(project), "output_dir": str(tmp_path / "nothing"),
                                          "config": client.cfg})
    assert r.status_code == 404 and "run a migration first" in r.json()["detail"]


def test_a_run_can_ask_for_tests_in_the_same_job(client, project, tmp_path, aws):
    from tests.conftest import mocked_testgen

    with mocked_testgen():
        started = client.post("/api/runs", json=_run_body(project, tmp_path, client, generate_tests=True))
        job = _finish(client, started.json()["job_id"])

    assert job["result"]["testgen"]["totals"]["generated"] == 2
    events = [e for e in _sse(client, started.json()["job_id"]) if e["event"].startswith("testgen")]
    assert [e["event"] for e in events] == ["testgen_start", "testgen_unit", "testgen_unit", "testgen_summary"]


# ─── registry ─────────────────────────────────────────────────────────────────

def test_registry_one_job_at_a_time_and_events_in_order():
    reg = JobRegistry()
    gate = threading.Event()

    def target(job, emit):
        emit({"type": "a"})
        gate.wait(5)
        emit({"type": "b"})
        return {"ok": True}

    job = reg.start("run", {}, target)
    with pytest.raises(JobBusy):
        reg.start("run", {}, target)
    gate.set()
    assert reg.wait(job) and job.state == "done" and job.result == {"ok": True}
    assert [e["type"] for e in job.events] == ["a", "b", "done"]
    assert [e["seq"] for e in job.events] == [1, 2, 3]
    assert [seq for seq, _ in reg.subscribe(job, after=1)] == [2, 3]
    assert reg.active() is None and reg.start("run", {}, target).id != job.id


# ─── the Intent step ─────────────────────────────────────────────────────────

def _intent_body(project, tmp_path, client, text="modernize this"):
    return {"source_dir": str(project), "output_dir": str(tmp_path / "out"),
            "config": client.cfg, "intent": text}


def test_intent_route_narrows_the_plan_and_reports_provenance(client, project, tmp_path):
    from tests.conftest import llm_reply

    # This fixture is two .java files and no pom, so javax-to-jakarta is the
    # only pack with evidence. Asking for spring-to-spring6 as well is the
    # case that matters: it must come back unsupported, not selected.
    proposal = {
        "include": ["javax-to-jakarta", "spring-to-spring6"],
        "decisions": {"risk_ceiling": "review-all"},
        "scope": {"exclude_globs": ["db/**"]},
    }
    with patch("forge.intent.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value.invoke.return_value = llm_reply(proposal)
        r = client.post("/api/intent", json=_intent_body(project, tmp_path, client))

    assert r.status_code == 200
    body = r.json()
    assert body["order"] == ["javax-to-jakarta"]
    assert body["intent"]["provenance"]["risk_ceiling"] == "prompt"
    assert body["intent"]["scope"]["exclude_globs"] == ["db/**"]
    assert [u["asked"] for u in body["intent"]["unsupported"]] == ["spring-to-spring6"]


def test_intent_route_rejects_an_empty_request_before_calling_anything(client, project, tmp_path):
    with patch("forge.intent.agent.ChatBedrockConverse") as MockLLM:
        r = client.post("/api/intent", json=_intent_body(project, tmp_path, client, text="   "))
        MockLLM.assert_not_called()
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]


def test_intent_route_needs_a_config(client, project, tmp_path):
    body = _intent_body(project, tmp_path, client)
    body["config"] = str(tmp_path / "nope.yaml")
    r = client.post("/api/intent", json=body)
    assert r.status_code == 400
    assert "config not found" in r.json()["detail"]


def test_the_intent_plan_is_a_listed_artifact(client, project, tmp_path):
    from tests.conftest import llm_reply

    out = tmp_path / "out"
    with patch("forge.intent.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value.invoke.return_value = llm_reply({"include": ["javax-to-jakarta"]})
        client.post("/api/intent", json=_intent_body(project, tmp_path, client))

    names = [a["name"] for a in client.get(f"/api/artifacts?output_dir={out}").json()["artifacts"]]
    assert "intent-plan.json" in names
    # The discovery detail row used to name a file the emitter never writes.
    assert "stack-profile.json" in names


# ─── the page is the chat, and nothing else ──────────────────────────────────

def test_the_page_is_chat_only_and_the_helpers_it_needs_exist():
    """The nine wizard steps are gone, and this is what stops them coming back.

    The owner opened the UI, counted nine numbered steps plus a Project setup
    form and asked "do i really need all these?" — then chose to delete them.
    There is no router left to keep in sync, so the failure mode this test used
    to guard (a nav entry with no section, silent at runtime) is replaced by a
    sharper one: chat.js reaching through ``window.FORGE`` for a helper app.js
    no longer exposes. That is ``undefined is not a function`` on a click, in a
    file with no build step to catch it and nothing raised where a test looks.
    """
    import re

    from forge.leader import CHAT_EVENT_TYPES

    static = Path(app_module.__file__).parent / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    chat_js = (static / "chat.js").read_text(encoding="utf-8")

    # One view. A `data-step` or a `#step-*` for anything but chat is a wizard
    # step that survived, and a hidden one is a step that is merely out of sight.
    assert set(re.findall(r'data-step="([a-z]+)"', html)) <= {"chat"}
    assert set(re.findall(r'id="step-([a-z]+)"', html)) == {"chat"}
    # The rail element survives — it holds the mark, New chat and the spend
    # meter — so what is pinned is the wizard itself: nine numbered markers and
    # the hash links that went with them.
    assert re.findall(r'<span class="n">', html) == [], "the numbered step markers are back"
    section = re.search(r'<[a-z]+ id="step-chat"[^>]*>', html)
    assert section and " hidden" not in section.group(0), (
        "the only view on the page cannot start hidden — nothing is left to reveal it")
    assert 'href="#/' not in html, "a hash link with no router behind it is a dead link"

    # app.js is a helpers module now: no step handlers, no router, no project form.
    assert re.findall(r'steps\.([a-z]+)\s*=\s*\{', js) == [], "app.js still carries step handlers"
    assert re.search(r'\bsteps\s*=\s*\{', js) is None, "the steps handler object is still there"
    assert re.search(r'\bfunction\s+needProject\b', js) is None

    # chat.js reaches the rest of the page through exactly one object, resolved
    # at call time. A name it calls and app.js does not expose fails on a click.
    block = re.search(r'window\.FORGE\s*=\s*\{(.*?)\};', js, re.S)
    assert block, "app.js no longer exposes window.FORGE, which is all chat.js has"
    exposed = set(re.findall(r'(?:^|[{,])\s*([A-Za-z_$][\w$]*)\s*:', block.group(1), re.M))
    wanted = {"S", "save", "api", "follow", "esc", "tag", "flash", "fileUrl", "health"}
    assert wanted <= exposed, f"window.FORGE no longer exposes {sorted(wanted - exposed)}"
    used = set(re.findall(r'FORGE\.([A-Za-z_$][\w$]*)', chat_js))
    assert used <= exposed, f"chat.js calls FORGE.{sorted(used - exposed)}, which app.js does not expose"
    assert "steps" not in used, "chat.js is the page; there is nothing left to route to"

    assert re.search(r'window\.ForgeChat\s*=', chat_js) and "render" in chat_js
    assert 'src="/static/chat.js"' in html and 'href="/static/chat.css"' in html

    # Every element chat.js looks up has to be on the page it renders into.
    for required in ("c-transcript", "c-composer"):
        assert f'id="{required}"' in html, f"index.html has no #{required} for chat.js to render into"
    ids = set(re.findall(r"(?:getElementById|\$)\(\s*'(c-[a-z0-9-]+)'\s*\)", chat_js))
    absent = sorted(i for i in ids if f'id="{i}"' not in html)
    assert not absent, f"chat.js looks up element(s) index.html does not have: {absent}"

    # EventSource only delivers the event names a listener was registered for,
    # so a chat event missing from this array is dropped in silence.
    listed = re.search(r'EVENT_TYPES\s*=\s*\[(.*?)\]', js, re.S).group(1)
    for name in CHAT_EVENT_TYPES:
        assert f"'{name}'" in listed, f"app.js never listens for the chat event {name}"


def test_every_card_the_leader_can_emit_has_a_renderer_and_no_renderer_is_orphaned():
    """The other half of the cross-file contract, and the half nobody owns.

    ``cards.py`` and ``chat.js`` were written by different hands against the
    same list of card kinds. A kind the backend emits and the browser does not
    know renders as "This page does not know that card" — in the middle of a
    paid run, on the one surface there is. A renderer for a kind nothing emits
    is the opposite tell: a deleted card whose branch stayed behind, like the
    ``setup`` card that pointed at the Project form.
    """
    import re

    from forge.leader import cards, tools

    static = Path(app_module.__file__).parent / "static"
    chat_js = (static / "chat.js").read_text(encoding="utf-8")

    emitted = set()
    for module in (cards, tools):
        emitted |= set(re.findall(r'"kind":\s*"([a-z_]+)"',
                                  Path(module.__file__).read_text(encoding="utf-8")))
    rendered = set(re.findall(r"kind === '([a-z_]+)'", chat_js))

    assert emitted, "no card kinds found — the regex, not the code, is what broke"
    assert emitted <= rendered, f"chat.js cannot draw {sorted(emitted - rendered)}"
    assert rendered <= emitted, f"chat.js draws {sorted(rendered - emitted)}, which nothing emits"
    # The three the wizard's deletion added, named so a silent removal fails here.
    assert {"evidence", "artifacts", "land"} <= emitted


def test_the_static_pages_only_read_profile_fields_the_profiler_emits(project):
    """A field name that does not exist renders as "?" and nobody notices.

    The chat's plan card prints the build system and the source level out of
    ``discover()["profile"]``, which is ``Profile.to_json()``. JavaScript reads
    it by attribute, so a wrong name is not an error — it is ``undefined``
    falling through to the ``|| '?'`` default, and "Java ?" on a Maven 8 project
    looks exactly like a repository the profiler could not read. app.js and
    chat.js once carried the same wrong name (``java_version``) and so agreed
    with each other perfectly; this pins them to the schema instead.

    app.js dropped out of the loop with increment 2: the Discover step that read
    the profile there is gone, and chat.js is the only page left that prints it.
    """
    import re

    from forge.discover import build_profile

    known = set(build_profile(str(project)).to_json())
    assert "java_level" in known and "build_system" in known, sorted(known)

    static = Path(app_module.__file__).parent / "static"
    for name in ("chat.js",):
        js = (static / name).read_text(encoding="utf-8")
        read = set(re.findall(r"\bprofile\.([a-z_]+)", js))
        assert read, f"{name} no longer reads the profile at all — is this test still pinning anything?"
        assert read <= known, f"{name} reads profile field(s) the profiler never emits: {sorted(read - known)}"
