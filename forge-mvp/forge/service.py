"""The service layer: everything the CLI and the UI both do, as plain functions.

No argparse, no ``print``, no ``sys.exit``. Progress is reported through an
optional ``on_event(dict)`` callback whose events carry a ``"type"`` key; the
CLI passes a printer that reproduces its historical output line for line, and
the UI streams the same events to a browser. There is exactly one
implementation of a migration run, and both front ends call it.

Modules that touch AWS (the graph, the state store, metrics, the build
verifier) are imported inside the functions that need them, so a test can
patch them before they are first bound.
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from forge.config import ForgeConfig
from forge.state import FileStatus, make_file_status

OnEvent = Optional[Callable[[dict], None]]


class NoEligibleFiles(Exception):
    """The run has nothing to do. The CLI prints the message and exits 0."""


def emit(on_event: OnEvent, event: dict) -> None:
    if on_event is not None:
        on_event(event)


# ─── results ──────────────────────────────────────────────────────────────────

@dataclass
class AcceptanceOutcome:
    report: Any                      # forge.verify.acceptance.AcceptanceReport, or None when skipped
    path: Optional[Path]
    skipped_reason: Optional[str]
    exit_code: int

    def to_json(self) -> dict:
        if self.report is None:
            return {"verdict": None, "skipped_reason": self.skipped_reason, "path": None, "results": []}
        return {
            "verdict": self.report.verdict,
            "passed": sum(r.passed for r in self.report.results),
            "failed": len(self.report.failed),
            "skipped": len(self.report.skipped),
            "path": str(self.path) if self.path else None,
            "results": [{"pack": r.pack, "kind": r.kind, "value": str(r.value), "scope": r.scope,
                         "outcome": r.outcome, "detail": r.detail, "evidence": list(r.evidence)}
                        for r in self.report.results],
        }


@dataclass
class RunResult:
    phase: str
    source_dir: str
    output_dir: str
    dry_run: bool
    statuses: List[FileStatus]
    totals: Dict[str, Any]
    skipped: list
    queue: dict
    paths: Dict[str, Optional[str]]
    acceptance: Optional[AcceptanceOutcome] = None
    cancelled: bool = False

    def summary(self) -> dict:
        """JSON-safe: what a UI shows after a run. Not the full statuses."""
        return {
            "phase": self.phase, "source_dir": self.source_dir, "output_dir": self.output_dir,
            "dry_run": self.dry_run, "cancelled": self.cancelled, "totals": self.totals, "paths": self.paths,
            "skipped": len(self.skipped),
            "queue_count": len(self.queue.get("entries", [])),
            "acceptance": self.acceptance.to_json() if self.acceptance else None,
            "files": [{
                "file_path": fs.get("file_path"), "status": fs.get("status"), "score": fs.get("review_score"),
                "risk_tier": fs.get("risk_tier"), "retry_count": fs.get("retry_count"),
                "generated": bool(fs.get("generate")), "build_verdict": fs.get("build_verdict"),
            } for fs in self.statuses],
        }


@dataclass
class ApplyResult:
    outcomes: list
    remaining: List[FileStatus]
    queue_after: Optional[dict]
    log_path: Optional[Path]
    all_applied: bool

    def to_json(self) -> dict:
        return {
            "all_applied": self.all_applied,
            "remaining": len(self.remaining),
            "log_path": str(self.log_path) if self.log_path else None,
            "outcomes": [{"file": o.file, "decision": o.decision, "applied": o.applied,
                          "status_after": o.status_after, "detail": o.detail} for o in self.outcomes],
        }


# ─── one file through the graph ───────────────────────────────────────────────

def _build_initial_state(config, file_path: str, phase: str, dry_run: bool, source_dir: str, output_dir: str,
                         generate: bool = False, file_status_overrides: Optional[dict] = None) -> dict:
    file_status = make_file_status(file_path, phase)
    file_status["generate"] = generate
    if file_status_overrides:
        file_status.update(file_status_overrides)
    return {
        "current_file": file_status,
        "phase": phase,
        "dry_run": dry_run,
        "source_dir": str(Path(source_dir).resolve()),
        "output_dir": output_dir,
        "target_java_version": config.get("target_java_version", "21"),
        "target_spring_version": "3",
        "files_processed": 0,
        "files_passed": 0,
        "files_retried": 0,
        "files_manual": 0,
        "files_blocked": 0,
        "files_held": 0,
        "bedrock_calls": 0,
        "estimated_cost_usd": 0.0,
        "messages": [],
    }


def _emit_file_metrics(metrics, final: dict, fs: dict) -> None:
    status = fs.get("status")
    payload = {
        "files_processed": 1,
        "files_passed": 1 if status == "DONE" else 0,
        "files_manual": 1 if status == "MANUAL_REVIEW" else 0,
        "files_blocked": 1 if status == "BLOCKED" else 0,
        "files_retried": 1 if (fs.get("retry_count") or 0) > 0 else 0,
        "bedrock_calls": final.get("bedrock_calls", 0),
        "estimated_cost_usd": final.get("estimated_cost_usd", 0.0),
    }
    if fs.get("review_score") is not None:
        payload["review_score"] = fs["review_score"]
    metrics.emit(payload)


def run_file(app, config, state_manager, metrics, file_path: str, index: int, total: int, phase: str, dry_run: bool,
             source_dir: str, output_dir: str, generate: bool = False, file_status_overrides: Optional[dict] = None,
             thread_id: Optional[str] = None, on_event: OnEvent = None) -> dict:
    initial = _build_initial_state(config, file_path, phase, dry_run, source_dir, output_dir,
                                   generate=generate, file_status_overrides=file_status_overrides)
    cfg = {"configurable": {"thread_id": thread_id or file_path}}

    final = app.invoke(initial, config=cfg)
    fs = final["current_file"]
    label = f"{Path(file_path).name} (generated)" if generate else Path(file_path).name
    emit(on_event, {"type": "file", "index": index, "total": total, "file": file_path, "label": label,
                    "status": fs.get("status", "UNKNOWN"), "score": fs.get("review_score"), "generated": generate,
                    "risk_tier": fs.get("risk_tier"), "retry_count": fs.get("retry_count")})

    if not dry_run:
        state_manager.put_file_status(fs)
        # Emitted per file, not per run: the FORGE-PipelineStalled alarm watches
        # for files_processed dropping below 1 in a 15-minute window, so a long
        # run that only reported at the end would trip it.
        _emit_file_metrics(metrics, final, fs)

    return final


def _write_snapshot(phase: str, source_dir: str, output_dir: str, unit_paths: Sequence[str],
                    on_event: OnEvent) -> Optional[str]:
    from forge.context.snapshot import write_context_snapshot
    from forge.extract import get_extractor
    from forge.phases import get_phase

    name = getattr(get_phase(phase), "context", "none")
    extractor = get_extractor(name) if name != "none" else None
    if extractor is None:
        return None
    modules = sorted({extractor.module_for(p, source_dir) for p in unit_paths})
    try:
        path = write_context_snapshot(output_dir, name, source_dir, modules)
    except ValueError as e:
        emit(on_event, {"type": "snapshot_skipped", "reason": str(e)})
        return None
    emit(on_event, {"type": "snapshot", "path": str(path)})
    return str(path)


# ─── packs and discovery ──────────────────────────────────────────────────────

def packs() -> List[dict]:
    """The pack library in dependency order. Raises PackError if it is broken."""
    from forge.packs import load_packs
    from forge.utils.file_scanner import runnable_phases

    registry = load_packs()
    runnable = set(runnable_phases())
    return [{
        "id": p.id, "title": p.title, "tier": p.tier, "status": p.status, "complete": p.is_complete,
        "runnable": p.id in runnable, "context": p.context, "depends_on": list(p.depends_on),
        "decisions": list(p.decisions),
    } for p in (registry[i] for i in registry.order)]


def discover(source_dir: str, output_dir: str, config: Optional[ForgeConfig] = None) -> dict:
    """Profile a repository and say which packs apply, on what evidence. No model, no AWS."""
    from forge.discover import build_profile, render_summary, resolve_packs, write_outputs
    from forge.discover.emit import DEFAULT_DECISIONS
    from forge.discover.resolve import content_patterns
    from forge.packs import load_packs
    from forge.utils.file_scanner import runnable_phases

    registry = load_packs()
    decisions = dict(DEFAULT_DECISIONS)
    if config is not None:
        decisions.update(config.get("decisions") or {})

    source_dir = str(Path(source_dir).resolve())
    pack_list = list(registry.values())
    profile = build_profile(source_dir, content_patterns=content_patterns(pack_list), decisions=decisions)
    activations = resolve_packs(profile, pack_list)
    runnable = set(runnable_phases())
    for a in activations:
        a.runnable = a.pack_id in runnable
    order = registry.resolve_order([a.pack_id for a in activations])
    json_path, yaml_path = write_outputs(profile, activations, order, decisions, output_dir)
    return {
        "summary": render_summary(profile, activations, order),
        "profile": profile.to_json(),
        "activations": [{"pack": a.pack_id, "complete": a.complete, "runnable": a.runnable, "evidence": a.evidence}
                        for a in activations],
        "order": list(order),
        "decisions": decisions,
        "paths": {"json": str(json_path), "yaml": str(yaml_path)},
    }


# ─── the migration run ────────────────────────────────────────────────────────

def run_migration(source_dir: str, phase: str, output_dir: str, config: ForgeConfig, *, dry_run: bool = False,
                  single_file: Optional[str] = None, resume: bool = False, no_metrics: bool = False,
                  run_acceptance: bool = False, acceptance_build: bool = False,
                  on_event: OnEvent = None, cancel: Optional[threading.Event] = None) -> RunResult:
    """One phase over one project — what ``migrate.py --phase`` does.

    Raises ``NoEligibleFiles`` when there is nothing to do. ``cancel`` is
    checked between units; on cancel the artifacts are still written, because
    held files are already staged and must not be orphaned.
    """
    from forge.extract import clear_context_cache
    from forge.extract.selectors import is_generated_target
    from forge.graph import build_graph
    from forge.phases import get_phase
    from forge.review_queue import QUEUE_NAME, write_queue, write_review_page
    from forge.state_store.dynamodb import DynamoDBStateManager
    from forge.utils.file_scanner import scan_java_files
    from forge.utils.report import generate_report
    from forge.utils.telemetry import MetricsEmitter

    clear_context_cache()
    app = build_graph(config)
    state_manager = DynamoDBStateManager(config)
    metrics = MetricsEmitter(config, enabled=not no_metrics and not dry_run)
    source_dir = str(Path(source_dir).resolve())

    skipped: list = []
    generated: Sequence[str] = ()
    if single_file:
        # Naming a file explicitly beats a config default — no scope filtering.
        files = [str(Path(single_file).resolve())]
        if is_generated_target(get_phase(phase), files[0]):
            files, generated = [], (files[0],)
    elif resume:
        pending = state_manager.get_files_by_status("PENDING")
        files = [fs["file_path"] for fs in pending]
        if not files:
            raise NoEligibleFiles("No PENDING files found in DynamoDB. Nothing to resume.")
    else:
        # Scope filtering happens here, before any model call, so an out-of-scope
        # file costs nothing rather than being discovered mid-pipeline.
        prefix = config.get("scope_package_prefix", "")
        scan = scan_java_files(source_dir, phase, prefix)
        files, skipped, generated = scan.files, scan.skipped, scan.generated
        if skipped:
            emit(on_event, {"type": "skipped", "count": len(skipped), "prefix": prefix})
        if not files and not generated:
            raise NoEligibleFiles(f"No eligible files found in {source_dir}")
        if not dry_run and files:
            state_manager.mark_pending(files, phase)

    # Generated targets run after the real files so the descriptors they are
    # built from have already been migrated in this run.
    units = [(f, False) for f in files] + [(g, True) for g in generated]
    total = len(units)
    emit(on_event, {"type": "start", "phase": phase, "files": len(files), "generated": len(generated),
                    "dry_run": dry_run, "total": total})

    all_statuses: List[FileStatus] = []
    total_bedrock_calls = 0
    total_cost = 0.0
    cancelled = False
    done_units: List[str] = []

    for i, (file_path, generate) in enumerate(units, start=1):
        if cancel is not None and cancel.is_set():
            cancelled = True
            emit(on_event, {"type": "cancelled", "done": i - 1, "total": total})
            break
        final = run_file(app, config, state_manager, metrics, file_path=file_path, index=i, total=total,
                         phase=phase, dry_run=dry_run, source_dir=source_dir, output_dir=output_dir,
                         generate=generate, on_event=on_event)
        all_statuses.append(final["current_file"])
        total_bedrock_calls += final.get("bedrock_calls", 0)
        total_cost += final.get("estimated_cost_usd", 0.0) or 0.0
        done_units.append(file_path)

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    # The full extracted context, for the reviewer of last resort and for the
    # acceptance checks that diff pre- against post-migration facts. Written in
    # dry-run too: it is an audit artifact, like the report.
    snapshot_path = _write_snapshot(phase, source_dir, str(output_root), [u for u, _ in units], on_event)

    # The review queue: what the pipeline could not settle — and, in a dry run,
    # everything it would have done, since a first trial exists to look at that.
    queue = write_queue(str(output_root), all_statuses, source_dir, phase=phase, dry_run=dry_run)
    page_path: Optional[str] = None
    if queue["entries"]:
        page_path = str(write_review_page(str(output_root), queue))
        emit(on_event, {"type": "queue", "path": str(output_root / QUEUE_NAME), "page": page_path,
                        "count": len(queue["entries"])})

    report_path = output_root / "migration-report.md"
    generate_report(output_path=str(report_path), phase=phase, source_dir=source_dir, file_statuses=all_statuses,
                    bedrock_calls=total_bedrock_calls, estimated_cost_usd=total_cost, skipped=skipped)

    acceptance_outcome: Optional[AcceptanceOutcome] = None
    if run_acceptance:
        deleted = [d for fs in all_statuses for d in (fs.get("deleted_files") or [])]
        acceptance_outcome = acceptance(phase, source_dir, str(output_root), config, deleted=deleted,
                                        run_build=acceptance_build, dry_run=dry_run)
        _emit_acceptance(on_event, acceptance_outcome)

    totals = {
        "total": len(all_statuses),
        "passed": sum(1 for fs in all_statuses if fs.get("status") == "DONE"),
        "manual": sum(1 for fs in all_statuses if fs.get("status") == "MANUAL_REVIEW"),
        "blocked": sum(1 for fs in all_statuses if fs.get("status") == "BLOCKED"),
        "held": sum(1 for fs in all_statuses if fs.get("status") == "HELD"),
        "bedrock_calls": total_bedrock_calls,
        "cost_usd": round(total_cost, 6),
    }
    emit(on_event, {"type": "summary", **totals, "report": str(report_path)})

    return RunResult(
        phase=phase, source_dir=source_dir, output_dir=str(output_root), dry_run=dry_run,
        statuses=all_statuses, totals=totals, skipped=list(skipped), queue=queue,
        paths={"report": str(report_path), "queue": str(output_root / QUEUE_NAME), "page": page_path,
               "snapshot": snapshot_path,
               "acceptance": str(acceptance_outcome.path) if acceptance_outcome and acceptance_outcome.path else None},
        acceptance=acceptance_outcome, cancelled=cancelled,
    )


# ─── acceptance ───────────────────────────────────────────────────────────────

def acceptance(phase: str, source_dir: str, output_dir: str, config: ForgeConfig, *, deleted: Sequence[str] = (),
               run_build: bool = False, dry_run: bool = False) -> AcceptanceOutcome:
    """A pack's checks over the merged tree. Gates the project, not a file."""
    from forge.phases import get_phase
    from forge.verify.acceptance import run_acceptance as _run, write_acceptance

    spec = get_phase(phase)
    checks = getattr(spec, "acceptance", ())
    if not checks:
        return AcceptanceOutcome(None, None, f"phase '{phase}' declares no acceptance checks", 0)
    if dry_run:
        return AcceptanceOutcome(None, None, "skipped — a dry run writes nothing, so there is no post-migration tree to check", 0)

    decisions = config.get("decisions") or {}
    report = _run([spec], source_dir, output_dir, decisions, deleted=deleted, run_build=run_build)
    path = write_acceptance(report, output_dir)
    return AcceptanceOutcome(report, path, None, 0 if report.verdict == "PASS" else 1)


def _emit_acceptance(on_event: OnEvent, outcome: AcceptanceOutcome) -> None:
    if outcome.report is None:
        emit(on_event, {"type": "acceptance_skipped", "reason": outcome.skipped_reason})
    else:
        emit(on_event, {"type": "acceptance", **outcome.to_json()})


# ─── decisions ────────────────────────────────────────────────────────────────

def apply(decisions: Sequence[Any], source_dir: str, output_dir: str, config: ForgeConfig, *, run: str = "",
          dry_run: bool = False, phase: Optional[str] = None, on_event: OnEvent = None) -> ApplyResult:
    """Make a reviewer's decisions real. ``decisions`` may be dicts or ``Decision`` objects.

    Raises ``FileNotFoundError``/``ValueError`` when the queue or the decisions
    are unusable — the caller decides how to report that.
    """
    from forge.decisions import (Decision, append_report_section, applied_section, apply_decisions,
                                 decisions_from, write_applied_log)
    from forge.review_queue import load_queue, write_queue, write_review_page
    from forge.state_store.dynamodb import DynamoDBStateManager
    from forge.utils.telemetry import MetricsEmitter
    from forge.verify.build_verifier import BuildVerifier

    source_dir = str(Path(source_dir).resolve())
    output_dir = str(Path(output_dir).resolve())
    queue = load_queue(output_dir)
    decided = [d if isinstance(d, Decision) else None for d in decisions]
    if any(d is None for d in decided):
        decided = decisions_from([d.__dict__ if isinstance(d, Decision) else d for d in decisions], "<decisions>")
    if not decided:
        return ApplyResult([], [], None, None, True)

    state_manager = DynamoDBStateManager(config)
    metrics = MetricsEmitter(config, enabled=False)
    verifier = BuildVerifier(config)
    app = None
    if any(d.decision == "retry" for d in decided):
        from forge.graph import build_graph
        app = build_graph(config)
    counter = {"n": 0}

    def rerun(entry: dict, decision) -> dict:
        counter["n"] += 1
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        overrides = {"human_note": decision.note or None, "human_rule": decision.rule or None,
                     "human_decision": "retry", "human_decided_at": stamp}
        final = run_file(
            app, config, state_manager, metrics,
            file_path=entry["file_path"], index=counter["n"], total=len(decided),
            phase=entry.get("pack") or phase or "java21", dry_run=dry_run,
            source_dir=source_dir, output_dir=output_dir, generate=bool(entry.get("generate")),
            file_status_overrides=overrides,
            # A fresh budget on a fresh thread: the checkpointer must not resume the exhausted run.
            thread_id=f"{entry['file_path']}#human-{stamp}",
            on_event=on_event,
        )
        return final["current_file"]

    def verify_build(written):
        return verifier.verify({"dry_run": False, "output_dir": output_dir, "current_file": {"written_paths": written}})

    outcomes, remaining = apply_decisions(
        decided, queue, source_dir=source_dir, output_dir=output_dir,
        put_status=state_manager.put_file_status, verify_build=verify_build, rerun=rerun, dry_run=dry_run,
    )
    for o in outcomes:
        emit(on_event, {"type": "apply_outcome", "file": o.file, "decision": o.decision, "applied": o.applied,
                        "status_after": o.status_after, "detail": o.detail})

    queue_after = None
    log_path = None
    if not dry_run:
        queue_after = write_queue(output_dir, remaining, source_dir, phase=queue.get("phase", ""), run_id=queue.get("run"))
        if queue_after["entries"]:
            write_review_page(output_dir, queue_after)
        append_report_section(output_dir, applied_section(outcomes))
        log_path = write_applied_log(output_dir, run or queue.get("run", ""), decided, outcomes)
        emit(on_event, {"type": "apply_done", "remaining": len(queue_after["entries"]), "log": str(log_path)})
    return ApplyResult(outcomes, remaining, queue_after, log_path, all(o.applied for o in outcomes))


# ─── feedback ─────────────────────────────────────────────────────────────────

def feedback(output_dir: str) -> dict:
    """Reviewers' notes grouped by pack and rule; writes pack-feedback.md."""
    from forge.feedback_report import collect_notes, group_notes, pack_edit_path, write_feedback_report

    notes = collect_notes(output_dir)
    groups = group_notes(notes)
    path = write_feedback_report(output_dir)
    return {
        "path": str(path),
        "notes": len(notes),
        "packs": sorted({n["pack"] for n in notes}),
        "groups": {
            pack: {
                "edit": pack_edit_path(pack),
                "rows": [{"label": r["label"], "rule": r["rule"], "count": r["count"],
                          "files": sorted(r["files"]), "decisions": dict(r["decisions"]), "examples": list(r["examples"])}
                         for r in rows.values()],
            } for pack, rows in groups.items()
        },
    }
