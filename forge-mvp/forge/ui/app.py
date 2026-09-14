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
from forge.config import ForgeConfig
from forge.ui.jobs import JobBusy, JobRegistry

STATIC = Path(__file__).parent / "static"
KEEPALIVE_SECONDS = 15.0        # tests shrink this

# What each decision may be, from prompts/FORGE-Platform-Requirements.md §decisions.
DECISION_OPTIONS: Dict[str, List[str]] = {
    "web_framework": ["modernize-in-place", "migrate-to-spring"],
    "runtime": ["war-xml-bootstrap", "war-programmatic-bootstrap"],
    "container": ["liberty", "wildfly", "tomcat", "jetty"],
    "liberty_edition": ["open", "websphere"],
    "liberty_features": ["jakartaee-10.0", "webProfile-10.0", "granular"],
    "views": ["in-place", "thymeleaf", "defer"],
    "url_compat": ["preserve-with-redirect", "preserve-exact", "clean-only"],
    "persistence": ["keep-orm", "to-spring-data"],
    "idiom_aggressiveness": ["conservative", "moderate"],
    "risk_ceiling": ["auto", "review-high", "review-all"],
}

ARTIFACTS = [
    ("migration-report.md", "Migration report"),
    ("migration-review.html", "Review page (static)"),
    ("manual-review-queue.json", "Review queue"),
    ("migration-context.json", "Context snapshot"),
    ("migration-acceptance.json", "Acceptance record"),
    ("decisions-applied.jsonl", "Applied decisions log"),
    ("pack-feedback.md", "Pack feedback report"),
    ("forge-profile.yaml", "Discovery profile"),
    ("forge-profile.json", "Discovery detail"),
]
_MEDIA = {".md": "text/markdown", ".html": "text/html", ".json": "application/json",
          ".jsonl": "text/plain", ".yaml": "text/yaml", ".yml": "text/yaml"}


# ─── request bodies ───────────────────────────────────────────────────────────

class Project(BaseModel):
    source_dir: str
    output_dir: str = "./migrated"
    config: Optional[str] = None
    decisions: Optional[Dict[str, str]] = None


class RunRequest(Project):
    phase: str
    dry_run: bool = False
    acceptance: bool = False
    acceptance_build: bool = False
    no_metrics: bool = False
    single_file: Optional[str] = None
    resume: bool = False


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


# ─── helpers ──────────────────────────────────────────────────────────────────

def _config(path: Optional[str], decisions: Optional[Dict[str, str]]) -> ForgeConfig:
    """Resolve agents.yaml the way the CLI does, then overlay per-run decisions."""
    path = path or os.environ.get("FORGE_AGENTS_YAML", "agents.yaml")
    if not Path(path).is_file():
        raise HTTPException(400, f"config not found: {path}")
    config = ForgeConfig(path)
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
    app = FastAPI(title="FORGE", docs_url=None, redoc_url=None)
    registry = registry or JobRegistry()
    app.state.registry = registry
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
                    acceptance_build=body.acceptance_build, on_event=emit, cancel=job.cancel,
                )
            except service.NoEligibleFiles as e:
                emit({"type": "nothing", "message": str(e)})
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
