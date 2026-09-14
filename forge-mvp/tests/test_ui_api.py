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
