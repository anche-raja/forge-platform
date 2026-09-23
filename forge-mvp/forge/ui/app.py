"""The HTTP surface of the local UI. Every route is a thin call into ``forge.service``.

Binds to loopback only and serves files from nowhere but the chosen
``output_dir``; there is no auth because there is no network.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from forge import service
from forge.config import ConfigError, ForgeConfig
from forge.ui.jobs import JobBusy, JobRegistry

STATIC = Path(__file__).parent / "static"
KEEPALIVE_SECONDS = 15.0        # tests shrink this

# What each decision may be, from prompts/FORGE-Platform-Requirements.md §decisions.
# Defined in forge.intent.vocabulary — it is the decision vocabulary, not a UI
# concern, and the CLI validates against the same table.
from forge.intent.vocabulary import DECISION_OPTIONS  # noqa: E402

ARTIFACTS = [
    ("migration-summary.md", "Plan summary (every pack)"),
    ("migration-report.md", "Migration report (latest run)"),
    ("migration-review.html", "Review page (static)"),
    ("manual-review-queue.json", "Review queue"),
    ("migration-context.json", "Context snapshot"),
    ("migration-acceptance.json", "Acceptance record"),
    ("test-generation-report.md", "Test generation report"),
    ("generated-tests.json", "Generated tests record"),
    ("decisions-applied.jsonl", "Applied decisions log"),
    ("pack-feedback.md", "Pack feedback report"),
    ("forge-profile.yaml", "Discovery profile"),
    # The emitter writes stack-profile.json (forge/discover/emit.py:PROFILE_JSON);
    # the old name here never matched a file, so the row never appeared.
    ("stack-profile.json", "Discovery detail"),
    ("intent-plan.json", "Intent plan"),
]
_MEDIA = {".md": "text/markdown", ".html": "text/html", ".json": "application/json",
          ".jsonl": "text/plain", ".yaml": "text/yaml", ".yml": "text/yaml"}


# ─── request bodies ───────────────────────────────────────────────────────────

class Project(BaseModel):
    source_dir: str
    output_dir: str = "./migrated"
    config: Optional[str] = None
    decisions: Optional[Dict[str, str]] = None


class IntentRequest(Project):
    intent: str


class RunRequest(Project):
    phase: str
    dry_run: bool = False
    acceptance: bool = False
    acceptance_build: bool = False
    no_metrics: bool = False
    single_file: Optional[str] = None
    resume: bool = False
    generate_tests: bool = False      # write JUnit 5 tests for what this run wrote
    run_tests: bool = False           # ...and execute them


class GenerateTestsRequest(Project):
    """Test generation over an existing output directory."""

    dry_run: bool = False
    run_tests: bool = False


class DecisionsRequest(BaseModel):
    source_dir: str
    output_dir: str = "./migrated"
    config: Optional[str] = None
    decisions: List[Dict[str, Any]]                       # the reviewer's approve/reject/retry rows
    decision_overrides: Optional[Dict[str, str]] = None   # per-run platform decisions, as on /api/runs
    run: str = ""
    dry_run: bool = False
    phase: Optional[str] = None


class AcceptanceRequest(Project):
    phase: str
    run_build: bool = False


class ChatRequest(Project):
    """One turn of the chat: a typed message, or a button the user pressed.

    Never both. An ``action`` is the only thing that may execute a gated tool —
    a spend confirmation or a review decision is a click, not a sentence the
    model can talk itself into — so a body carrying both would leave it
    ambiguous which of the two authorised the turn.

    ``source_dir`` overrides the inherited required field and is optional here,
    which is the whole of the owner's objection to the wizard: *"I requested to
    change with prompt instead of this project setup"*. The chat has to accept
    a first message before anyone has named a folder, because asking for the
    folder is the leader's job and it cannot ask from behind a 400. ``config``
    stays required — a leader with no model is not a leader.

    ``trial`` is the chat's "Trial run" box: this turn's runs transform with
    ``trial_transform_model`` instead of ``transform_model``. Per turn, from a
    click — never something the leader can set.
    """

    source_dir: Optional[str] = None
    # Unset means the repository's own `.migrated` (forge.leader.tools
    # .default_output_dir), the same default set_project applies — never the
    # server's working directory.
    output_dir: Optional[str] = None
    conversation_id: Optional[str] = None
    message: Optional[str] = None
    action: Optional[Dict[str, Any]] = None
    trial: bool = False


# ─── helpers ──────────────────────────────────────────────────────────────────

def _config(path: Optional[str], decisions: Optional[Dict[str, str]]) -> ForgeConfig:
    """Resolve agents.yaml the way the CLI does, then overlay per-run decisions."""
    path = path or os.environ.get("FORGE_AGENTS_YAML", "agents.yaml")
    if not Path(path).is_file():
        raise HTTPException(400, f"config not found: {path}")
    try:
        config = ForgeConfig(path)
    except ConfigError as e:
        # migrate.py catches these, prints them and exits 2; the browser needs
        # the same words. Uncaught, FastAPI renders a bare "Internal Server
        # Error" and the one message that says how to repair the config only
        # ever reaches the server's terminal.
        raise HTTPException(400, str(e)) from e
    if decisions:
        bad = {k: v for k, v in decisions.items() if k in DECISION_OPTIONS and v not in DECISION_OPTIONS[k]}
        if bad:
            raise HTTPException(400, f"invalid decision value(s): {bad}")
        config = config.with_overrides({"decisions": decisions})
    return config


def _source(source_dir: str) -> str:
    p = Path(source_dir).expanduser()
    if not p.is_dir():
        raise HTTPException(400, f"source_dir is not a directory: {source_dir}")
    return str(p.resolve())


def _phase(phase: str, *, runnable_only: bool) -> str:
    from forge.phases import PHASE_NAMES
    from forge.utils.file_scanner import runnable_phases

    allowed = runnable_phases() if runnable_only else list(PHASE_NAMES)
    if phase not in allowed:
        raise HTTPException(400, f"phase '{phase}' is not runnable; choose one of: {', '.join(allowed)}")
    return phase


def _queue(output_dir: str) -> dict:
    from forge.review_queue import load_queue

    try:
        return load_queue(str(Path(output_dir).expanduser()))
    except FileNotFoundError:
        raise HTTPException(404, f"no review queue in {output_dir} — run a migration first")
    except ValueError as e:
        raise HTTPException(409, str(e))


def _safe_file(output_dir: str, name: str) -> Path:
    """Only a plain file inside ``output_dir``. Absolute names, ``..`` and symlink escapes are refused."""
    if not name or Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name:
        raise HTTPException(403, "file name must be relative and inside the output directory")
    base = Path(output_dir).expanduser().resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(403 if not target.is_relative_to(base) else 404, f"not served: {name}")
    return target


def _job_or_404(registry: JobRegistry, job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, f"no job {job_id}")
    return job


# ─── the app ──────────────────────────────────────────────────────────────────

def create_app(registry: Optional[JobRegistry] = None) -> FastAPI:
    from forge.leader.convo import ConversationStore

    app = FastAPI(title="FORGE", docs_url=None, redoc_url=None)
    registry = registry or JobRegistry()
    app.state.registry = registry
    # In memory for the life of the process, like the job registry beside it.
    # A conversation that outlived the server would promise a history the rest
    # of the UI does not keep; the browser re-creates one from the 202 instead.
    conversations = ConversationStore()
    app.state.conversations = conversations
    if STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        page = STATIC / "index.html"
        if page.is_file():
            return page.read_text(encoding="utf-8")
        return "<!doctype html><title>FORGE</title><p>FORGE API is up; the page has not been built yet.</p>"

    @app.get("/api/health")
    def health():
        active = registry.active()
        return {"ok": True, "cwd": os.getcwd(), "active": active.to_json() if active else None,
                "config_default": os.environ.get("FORGE_AGENTS_YAML", "agents.yaml")}

    @app.get("/api/packs")
    def packs():
        from forge.discover.emit import DEFAULT_DECISIONS
        from forge.packs import PackError
        from forge.phases import PHASE_NAMES
        from forge.utils.file_scanner import runnable_phases

        try:
            listed = service.packs()
        except PackError as e:
            raise HTTPException(500, f"pack library failed to load: {e}")
        return {"packs": listed, "runnable": runnable_phases(), "phases": list(PHASE_NAMES),
                "decision_defaults": dict(DEFAULT_DECISIONS), "decision_options": DECISION_OPTIONS}

    @app.post("/api/discover")
    def discover(body: Project):
        from forge.packs import PackError

        source = _source(body.source_dir)
        config = _config(body.config, body.decisions) if (body.config or body.decisions) else None
        try:
            return service.discover(source, str(Path(body.output_dir).expanduser()), config)
        except PackError as e:
            raise HTTPException(500, f"pack library failed to load: {e}")

    @app.post("/api/intent")
    def intent(body: IntentRequest):
        """Discovery, narrowed by a sentence. One model call, so it needs a config.

        Synchronous like /api/discover rather than a job: one cheap call has
        nothing to stream, and making it a job would take the single job slot
        away from an actual run.
        """
        from forge.packs import PackError

        text = body.intent.strip()
        if not text:
            raise HTTPException(400, "intent is empty")
        source = _source(body.source_dir)
        config = _config(body.config, body.decisions)
        try:
            return service.discover(source, str(Path(body.output_dir).expanduser()), config, intent=text)
        except PackError as e:
            raise HTTPException(500, f"pack library failed to load: {e}")
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ─── runs ─────────────────────────────────────────────────────────────────

    @app.post("/api/runs", status_code=202)
    def start_run(body: RunRequest):
        source = _source(body.source_dir)
        phase = _phase(body.phase, runnable_only=True)
        config = _config(body.config, body.decisions)
        output_dir = str(Path(body.output_dir).expanduser())

        def target(job, emit):
            try:
                return service.run_migration(
                    source, phase, output_dir, config, dry_run=body.dry_run, single_file=body.single_file,
                    resume=body.resume, no_metrics=body.no_metrics, run_acceptance=body.acceptance,
                    acceptance_build=body.acceptance_build, with_tests=body.generate_tests,
                    run_tests=body.run_tests or None, on_event=emit, cancel=job.cancel,
                )
            except service.NoEligibleFiles as e:
                emit({"type": "nothing", "message": str(e)})
                return None
            except service.PackOverlap as e:
                # An error, not a "nothing": the user asked for work that was
                # refused, and the message names what to do instead.
                emit({"type": "error", "error": str(e)})
                return None

        try:
            job = registry.start("run", body.model_dump(), target,
                                 summarise=lambda r: r.summary() if r is not None else None)
        except JobBusy as e:
            raise HTTPException(409, str(e))
        return {"job_id": job.id, "state": job.state}

    @app.get("/api/jobs")
    def jobs():
        return {"jobs": [j.to_json() for j in registry.list()]}

    @app.get("/api/runs/{job_id}")
    def get_run(job_id: str):
        return _job_or_404(registry, job_id).to_json()

    @app.post("/api/runs/{job_id}/cancel")
    def cancel_run(job_id: str):
        job = _job_or_404(registry, job_id)
        if job.finished:
            return {"job_id": job.id, "state": job.state, "cancelled": False}
        registry.cancel(job)
        return {"job_id": job.id, "state": job.state, "cancelled": True}

    @app.get("/api/runs/{job_id}/events")
    def run_events(job_id: str, request: Request, after: int = 0):
        job = _job_or_404(registry, job_id)
        last = request.headers.get("last-event-id")
        start = int(last) if last and last.isdigit() else after

        def frames():
            for item in registry.subscribe(job, after=start, timeout=KEEPALIVE_SECONDS):
                if item is None:
                    yield ": keep-alive\n\n"
                else:
                    seq, event = item
                    yield f"id: {seq}\nevent: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ─── chat ─────────────────────────────────────────────────────────────────

    def _active_chat(conversation_id: str):
        """The job currently writing that conversation, or None."""
        active = registry.active()
        if active is None or active.kind != "chat":
            return None
        return active if active.params.get("conversation_id") == conversation_id else None

    @app.post("/api/chat", status_code=202)
    def chat(body: ChatRequest):
        """One leader turn, as a job in the same single slot a run takes.

        A turn is not a request-thread call like /api/intent, because the tools
        it may reach are runs: minutes of model calls with events to stream. It
        holds the one job slot on purpose (R7). The tools call ``service.*`` on
        this thread and relay their events into this job's stream, so a second
        job would put two threads on the graph, DynamoDB and the context cache
        — the invariant jobs.py enforces by allowing only one.
        """
        from forge.leader.agent import LeaderAgent
        from forge.leader.tools import ProjectContext, default_output_dir

        has_message, has_action = body.message is not None, body.action is not None
        if has_message == has_action:
            raise HTTPException(400, "send exactly one of message or action")
        message = body.message.strip() if has_message else None
        if has_message and not message:
            raise HTTPException(400, "message is empty")

        # Everything that can raise HTTPException runs here, on the request
        # thread. Inside the target it would be an ordinary exception that
        # fails the job — a 400 the browser would have to read out of an SSE
        # error frame.
        config = _config(body.config, body.decisions)
        # The same config without the browser's decision overlay. Handing the
        # overlay to the intent layer as "config" makes it attribute the last
        # plan's answers to agents.yaml and drop them from its assumptions —
        # the misattribution app.js already avoids by leaving `decisions` out
        # of its /api/intent body.
        base_config = _config(body.config, None)

        convo = conversations.get_or_create(body.conversation_id)
        # Three ways a turn learns which repository it is about, in order: the
        # body named one (the browser still may, and the CLI's tests do), the
        # conversation is already bound to one, or nobody has said yet — and
        # that last case runs too. A blank string is "not said", not a path:
        # the field is optional now, and an empty input box must not become a
        # 400 the leader cannot answer.
        if body.source_dir is not None and str(body.source_dir).strip():
            source = _source(body.source_dir)
            output_dir = (str(Path(body.output_dir).expanduser()) if str(body.output_dir or "").strip()
                          else default_output_dir(source))
            if not convo.bind(source, output_dir):
                raise HTTPException(400, "this conversation belongs to a different project — start a new chat")
        else:
            # set_project binds the conversation from inside a turn, so this is
            # where a chat that named its folder in prose picks it up again.
            source, output_dir = convo.source_dir, convo.output_dir
        # The trial model reaches the runs only. The leader keeps `config`: it
        # falls back to transform_model when leader.model is unset, and the
        # chat's own behaviour must not change with a box about run cost.
        run_config = config
        if body.trial:
            trial_model = str(config.get("trial_transform_model") or "").strip()
            if not trial_model:
                raise HTTPException(400, "Trial run is on, but agents.yaml has no trial_transform_model "
                                         "— add one (e.g. us.anthropic.claude-sonnet-5) or turn the box off")
            run_config = config.with_overrides({"transform_model": trial_model})
        ctx = ProjectContext(source_dir=source, output_dir=output_dir, config=run_config,
                             base_config=base_config, bound=bool(source))

        def target(job, emit):
            # Claim the turn, then build the agent — in that order, and both in
            # here. Constructing it on the request thread would turn a missing
            # region or profile into a 500 on the POST; built here it fails the
            # job the way a run does, and the claim means the failure lands in
            # the transcript stamped with this job's id, so a reload can show
            # the turn that did not happen.
            convo.begin_turn(job.id)
            try:
                agent = LeaderAgent(config)
            except Exception as e:      # noqa: BLE001 — recorded, then re-raised as the job's error
                convo.add_item({"role": "error", "message": f"{type(e).__name__}: {e}"})
                convo.end_turn()
                raise
            return agent.run_turn(convo, ctx, message=message, action=body.action,
                                  emit=emit, cancel=job.cancel, job_id=job.id)

        try:
            # `phase` is present but empty: the job bar reads params.phase for
            # whatever is active, and a chat turn has no phase to show.
            job = registry.start("chat", {"surface": "chat", "conversation_id": convo.id, "phase": ""},
                                 target, summarise=lambda r: r)
        except JobBusy as e:
            raise HTTPException(409, str(e))
        return {"job_id": job.id, "conversation_id": convo.id, "state": job.state}

    @app.get("/api/chat/{conversation_id}")
    def get_chat(conversation_id: str):
        """The conversation, and the job still writing it if there is one.

        The active job is read BEFORE the transcript, and that order is the
        whole point. Read the other way round, a turn that finishes between the
        two reads gives the browser a half-written turn and no stream to replay
        the rest from. This way the overlap is the harmless one: items the
        browser skips because they carry the active job's id, and then draws
        from the replay.

        An id this process has never seen is a fresh conversation, not a 404 —
        the store dies with the server and the browser keeps the id in
        localStorage, so every restart would otherwise open on an error.
        """
        active = _active_chat(conversation_id)
        convo = conversations.get_or_create(conversation_id)
        return {**convo.to_json(), "active_job": active.to_json() if active else None}

    @app.post("/api/chat/{conversation_id}/reset")
    def reset_chat(conversation_id: str):
        """Start again: the old conversation is dropped, not archived.

        Refused while its turn is running, because the job thread would keep
        spending into a conversation nothing can display any more — the run it
        is in the middle of would finish with its held files unreachable.
        """
        if _active_chat(conversation_id) is not None:
            raise HTTPException(409, "that chat has a turn running — stop it first")
        return {"conversation_id": conversations.reset(conversation_id).id}

    # ─── review ───────────────────────────────────────────────────────────────

    @app.get("/api/review")
    def review(output_dir: str = Query(...)):
        from forge.review_queue import REVIEW_CSS, render_entries

        queue = _queue(output_dir)
        entries = queue.get("entries", [])
        by_status: Dict[str, int] = {}
        for e in entries:
            by_status[e.get("status", "?")] = by_status.get(e.get("status", "?"), 0) + 1
        return {
            "run": queue.get("run"), "phase": queue.get("phase"), "dry_run": bool(queue.get("dry_run")),
            "source_dir": queue.get("source_dir"), "output_dir": queue.get("output_dir"),
            "count": len(entries), "by_status": by_status,
            "entries": [{"rel_path": e.get("rel_path"), "pack": e.get("pack"), "status": e.get("status"),
                         "risk_tier": e.get("risk_tier"), "risk_score": e.get("risk_score"),
                         "review_score": e.get("review_score"), "human_decision": e.get("human_decision")}
                        for e in entries],
            "entries_html": render_entries(queue), "css": REVIEW_CSS,
        }

    @app.post("/api/review/decisions", status_code=202)
    def post_decisions(body: DecisionsRequest):
        from forge.decisions import decisions_from

        source = _source(body.source_dir)
        output_dir = str(Path(body.output_dir).expanduser())
        _queue(output_dir)
        try:
            decided = decisions_from(body.decisions, "decisions")
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not decided:
            raise HTTPException(400, "no decisions to apply")
        config = _config(body.config, body.decision_overrides)

        def target(job, emit):
            return service.apply(decided, source, output_dir, config, run=body.run, dry_run=body.dry_run,
                                 phase=body.phase, on_event=emit)

        try:
            job = registry.start("apply", {**body.model_dump(exclude={"decisions"}), "count": len(decided)}, target,
                                 summarise=lambda r: r.to_json())
        except JobBusy as e:
            raise HTTPException(409, str(e))
        return {"job_id": job.id, "state": job.state, "count": len(decided)}

    # ─── acceptance / feedback / artifacts ────────────────────────────────────

    @app.post("/api/acceptance")
    def acceptance(body: AcceptanceRequest):
        source = _source(body.source_dir)
        phase = _phase(body.phase, runnable_only=False)
        config = _config(body.config, body.decisions)
        output_dir = Path(body.output_dir).expanduser()
        if not output_dir.is_dir():
            raise HTTPException(404, f"no output directory at {body.output_dir} — run a migration first")
        outcome = service.acceptance(phase, source, str(output_dir), config, run_build=body.run_build)
        return {**outcome.to_json(), "exit_code": outcome.exit_code, "phase": phase}

    # ─── test generation ──────────────────────────────────────────────────────

    @app.post("/api/testgen", status_code=202)
    def start_testgen(body: GenerateTestsRequest):
        """Generate tests for the migrated classes already in the output directory.

        A job like a run: it is minutes of model calls, and the page follows the
        same SSE stream.
        """
        source = _source(body.source_dir)
        config = _config(body.config, body.decisions)
        output_dir = Path(body.output_dir).expanduser()
        if not output_dir.is_dir():
            raise HTTPException(404, f"no output directory at {body.output_dir} — run a migration first")

        def target(job, emit):
            return service.generate_tests(source, str(output_dir), config, dry_run=body.dry_run,
                                          run_tests=body.run_tests or None, on_event=emit, cancel=job.cancel)

        try:
            job = registry.start("testgen", body.model_dump(), target, summarise=lambda r: r.to_json())
        except JobBusy as e:
            raise HTTPException(409, str(e))
        return {"job_id": job.id, "state": job.state}

    @app.get("/api/testgen")
    def testgen(output_dir: str = Query(...)):
        """The last test-generation record, for the page to render without re-running."""
        from forge.testgen import RECORD_NAME

        path = Path(output_dir).expanduser() / RECORD_NAME
        if not path.is_file():
            raise HTTPException(404, f"no {RECORD_NAME} in {output_dir} — generate tests first")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise HTTPException(409, f"{path} is not readable as JSON: {e}")

    @app.get("/api/feedback")
    def feedback(output_dir: str = Query(...)):
        out = Path(output_dir).expanduser()
        if not out.is_dir():
            raise HTTPException(404, f"no output directory at {output_dir}")
        return service.feedback(str(out))

    @app.get("/api/artifacts")
    def artifacts(output_dir: str = Query(...)):
        out = Path(output_dir).expanduser()
        found = []
        for name, label in ARTIFACTS:
            p = out / name
            if p.is_file():
                found.append({"name": name, "label": label, "size": p.stat().st_size,
                              "modified": p.stat().st_mtime,
                              "url": f"/api/files?output_dir={out}&name={name}"})
        return {"output_dir": str(out), "exists": out.is_dir(), "artifacts": found}

    @app.get("/api/files")
    def files(output_dir: str = Query(...), name: str = Query(...)):
        target = _safe_file(output_dir, name)
        media = _MEDIA.get(target.suffix.lower(), "application/octet-stream")
        return FileResponse(str(target), media_type=media)

    return app
