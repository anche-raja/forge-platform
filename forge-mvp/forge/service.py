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

import json
import tempfile
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from forge.config import ForgeConfig
from forge.state import FileStatus, make_file_status
from forge.utils import run_manifest

OnEvent = Optional[Callable[[dict], None]]


class NoEligibleFiles(Exception):
    """The run has nothing to do. The CLI prints the message and exits 0."""


class PackOverlap(Exception):
    """This pack would overwrite another pack's output in the same directory.

    Refused before any spend, because packs read the original source and so
    cannot build on each other — the message names the two honest alternatives.
    """


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
class TestGenResult:
    """What a test-generation stage produced. Same shape of answer as a run."""

    source_dir: str
    output_dir: str
    dry_run: bool
    style: str
    units: List[dict]
    skipped: list
    totals: Dict[str, Any]
    paths: Dict[str, Optional[str]]
    record: dict
    cancelled: bool = False

    @property
    def exit_code(self) -> int:
        """Non-zero when anything needs a human — so ``--generate-tests-only`` gates CI."""
        held = self.totals.get("held", 0) + self.totals.get("blocked", 0) + self.totals.get("tests_failed", 0)
        return 1 if held else 0

    def to_json(self) -> dict:
        return {
            "source_dir": self.source_dir, "output_dir": self.output_dir, "dry_run": self.dry_run,
            "style": self.style, "cancelled": self.cancelled, "totals": self.totals, "paths": self.paths,
            "skipped": len(self.skipped),
            "dependencies": list(self.record.get("dependencies") or []),
            "units": [{"rel_path": u.get("rel_path"), "kind": u.get("kind"), "status": u.get("status"),
                       "score": u.get("review_score"), "test_rel_path": u.get("test_rel_path"),
                       "test_verdict": u.get("test_verdict"), "retry_count": u.get("retry_count"),
                       "hold_reason": u.get("hold_reason") or u.get("error")}
                      for u in self.units],
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
    testgen: Optional[TestGenResult] = None

    def summary(self) -> dict:
        """JSON-safe: what a UI shows after a run. Not the full statuses."""
        return {
            "phase": self.phase, "source_dir": self.source_dir, "output_dir": self.output_dir,
            "dry_run": self.dry_run, "cancelled": self.cancelled, "totals": self.totals, "paths": self.paths,
            "skipped": len(self.skipped),
            "queue_count": len(self.queue.get("entries", [])),
            "acceptance": self.acceptance.to_json() if self.acceptance else None,
            "testgen": self.testgen.to_json() if self.testgen else None,
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
                    "risk_tier": fs.get("risk_tier"), "retry_count": fs.get("retry_count"),
                    "cost_usd": round(final.get("estimated_cost_usd", 0.0) or 0.0, 6)})

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
        # A pack that declares a context it cannot get is the systemic case, and
        # it used to return here silently while the far rarer parse failure
        # below got an event. Report the common one too, or a whole run goes by
        # with no record that every file was transformed blind.
        if name != "none":
            emit(on_event, {"type": "context_missing", "context": name, "phase": phase,
                            "reason": f"no extractor is registered for context '{name}'; "
                                      "this pack runs without project context"})
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


def discover(source_dir: str, output_dir: str, config: Optional[ForgeConfig] = None,
             intent: Optional[str] = None) -> dict:
    """Profile a repository and say which packs apply, on what evidence.

    No model and no AWS when ``intent`` is absent — the deterministic path is
    untouched, and a caller that passes nothing gets exactly what it always did.

    With ``intent``, one model call maps the sentence onto decisions, scope and
    a subset of the packs the evidence *already* allows. It can narrow that set
    and never extend it; the order still comes from ``resolve_order``. See
    :mod:`forge.intent`.
    """
    from forge.discover import build_profile, render_summary, resolve_packs, write_outputs
    from forge.discover.emit import DEFAULT_DECISIONS
    from forge.discover.resolve import content_patterns
    from forge.packs import load_packs
    from forge.utils.file_scanner import runnable_phases

    registry = load_packs()
    config_decisions = dict(config.get("decisions") or {}) if config is not None else {}
    decisions = dict(DEFAULT_DECISIONS)
    decisions.update(config_decisions)

    source_dir = str(Path(source_dir).resolve())
    pack_list = list(registry.values())
    profile = build_profile(source_dir, content_patterns=content_patterns(pack_list), decisions=decisions)
    runnable = set(runnable_phases())

    def activate() -> list:
        acts = resolve_packs(profile, pack_list)
        for a in acts:
            a.runnable = a.pack_id in runnable
        return acts

    def as_json(acts) -> list:
        return [{"pack": a.pack_id, "complete": a.complete, "runnable": a.runnable, "evidence": a.evidence}
                for a in acts]

    activations = activate()
    plan = None

    if intent:
        from forge.intent.agent import IntentAgent
        from forge.intent.resolve import reconcile

        if config is None:
            raise ValueError("--intent needs agents.yaml: it makes one model call")

        agent = IntentAgent(config)
        proposal = agent.propose(intent, profile.to_json(), as_json(activations), decisions)

        def resolved(acts):
            return reconcile(proposal, as_json(acts), registry, defaults=DEFAULT_DECISIONS,
                             config_decisions=config_decisions, intent=intent)

        plan = resolved(activations)
        # A decision from the prompt can change which packs a `decision_equals`
        # gate admits — ask for Tomcat and the Liberty pack must stop firing.
        # resolve_packs is pure over profile.decisions, so re-deriving costs
        # nothing and needs no second model call.
        if plan.decisions != decisions:
            decisions = dict(plan.decisions)
            profile.decisions = dict(decisions)
            activations = activate()
            plan = resolved(activations)
        plan.bedrock_calls = agent.bedrock_calls
        plan.cost_usd = agent.cost_usd

    order = plan.packs if plan else registry.resolve_order([a.pack_id for a in activations])
    json_path, yaml_path = write_outputs(profile, activations, order, decisions, output_dir, plan=plan)
    result = {
        "summary": render_summary(profile, activations, order),
        "profile": profile.to_json(),
        "activations": as_json(activations),
        "order": list(order),
        "decisions": decisions,
        "paths": {"json": str(json_path), "yaml": str(yaml_path)},
    }
    if plan is not None:
        result["intent"] = plan.to_json()
        result["paths"]["intent"] = str(Path(output_dir) / "intent-plan.json")
    return result


# ─── the migration run ────────────────────────────────────────────────────────

# ─── running units, several at a time ─────────────────────────────────────────

class _ProgressRelay:
    """Forwards ``run_file`` events, renumbering ``file`` events by completion.

    With several files in flight they finish out of scan order, and the
    ``[k/total]`` a person reads is a progress counter, so ``index`` becomes
    "k-th to finish". The lock also keeps two workers' events from
    interleaving mid-emit. With one worker the numbers are the scan indices,
    exactly as before.
    """

    def __init__(self, on_event: OnEvent):
        self._on_event = on_event
        self._lock = threading.Lock()
        self._finished = 0

    def __call__(self, event: dict) -> None:
        with self._lock:
            if event.get("type") == "file":
                self._finished += 1
                event = {**event, "index": self._finished}
            emit(self._on_event, event)


def _workers_for(config) -> int:
    """``max_parallel_files``, except where parallel files would corrupt each other.

    Maven build verification compiles the whole output tree, so a compile
    running beside another file's write sees a half-migrated project and
    feeds a wrong verdict into the retry loop. That mode stays sequential.
    """
    from forge.config import parallel_files
    from forge.utils.telemetry import get_logger

    workers = parallel_files(config)
    bv = (config.get("build_verification") if config is not None else None) or {}
    if workers > 1 and bv.get("enabled") and bv.get("mode") == "maven":
        get_logger(__name__).info("build_verification mode maven: running files one at a time")
        return 1
    return workers


def _run_units(numbered, one, workers: int, cancel: Optional[threading.Event]):
    """Run ``one(i, path, generate)`` over ``numbered``; returns ``({i: final}, cancelled)``.

    ``cancel`` is honoured before each unit *starts*: a unit already running
    finishes and is kept, because its model calls are paid for and a held
    file is already staged. An exception in any unit ends the run, as it
    always has; units still in flight are allowed to finish first.
    """
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    results: Dict[int, dict] = {}
    if workers <= 1:
        for i, (path, generate) in numbered:
            if cancel is not None and cancel.is_set():
                return results, True
            results[i] = one(i, path, generate)
        return results, False

    cancelled = False
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="forge-unit") as pool:
        running: Dict[Any, int] = {}

        def collect(done) -> None:
            for fut in done:
                results[running.pop(fut)] = fut.result()

        for i, (path, generate) in numbered:
            while len(running) >= workers:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                collect(done)
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            running[pool.submit(one, i, path, generate)] = i
        while running:
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            collect(done)
    return results, cancelled


def _tree_rel(path: str, tree: str) -> str:
    """A ``deleted_files`` entry as a path relative to the tree the run read.

    The model names a retired file the way it saw its target: absolute, under
    the source (or the chained copy of it). The manifest, the merged view and
    landing all key on the relative path, so an absolute entry left as-is
    would retire nothing. An entry outside the tree, or already relative, is
    kept as given -- landing bounds it to the repository either way.
    """
    p = Path(str(path))
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.resolve().relative_to(Path(tree).resolve()).as_posix()
    except (ValueError, OSError):
        return p.as_posix()


def run_migration(source_dir: str, phase: str, output_dir: str, config: ForgeConfig, *, dry_run: bool = False,
                  single_file: Optional[str] = None, resume: bool = False, no_metrics: bool = False,
                  run_acceptance: bool = False, acceptance_build: bool = False, with_tests: bool = False,
                  run_tests: Optional[bool] = None, chain: bool = False, in_place: bool = False,
                  on_event: OnEvent = None, cancel: Optional[threading.Event] = None) -> RunResult:
    """One phase over one project — what ``migrate.py --phase`` does.

    Raises ``NoEligibleFiles`` when there is nothing to do. Units run
    ``max_parallel_files`` at a time (agents.yaml; 1 when unset). ``cancel``
    stops new units from starting, and units already running finish; on
    cancel the artifacts are still written, because held files are already
    staged and must not be orphaned.

    ``with_tests`` runs test generation afterwards, over the files this run
    actually wrote — after acceptance, because a project that did not migrate
    is not a project to write tests for.

    ``chain`` builds on what an earlier pack already wrote instead of refusing
    to overwrite it. The units are read from the merged view of the source tree
    with ``output_dir`` laid over it, so this pack transforms the *previous*
    pack's result rather than the original file. Without it a second pack over
    the same files is refused (``PackOverlap``), because it would read the
    original and replace the first pack's work.

    ``in_place`` is the chat's ``leader.migrate_on_branch``: every earlier pack's
    output has already been committed into ``source_dir`` itself, so the units
    are read from there -- no merged view, and no overlap refusal, since the
    input already is the previous pack's result. Each unit gets its own
    checkpoint thread per pack, because the path no longer differs between packs.
    """
    from forge.extract import clear_context_cache
    from forge.extract.selectors import is_generated_target
    from forge.graph import build_graph
    from forge.phases import get_phase
    from forge.review_queue import QUEUE_NAME, write_queue, write_review_page
    from forge.state_store.dynamodb import DynamoDBStateManager
    from forge.utils.file_scanner import scan_java_files
    from forge.utils.report import REPORT_NAME, clear_reverted, generate_report, pack_report_name, record_pack_run
    from forge.utils.telemetry import MetricsEmitter

    clear_context_cache()
    app = build_graph(config)
    state_manager = DynamoDBStateManager(config)
    metrics = MetricsEmitter(config, enabled=not no_metrics and not dry_run)
    source_dir = str(Path(source_dir).resolve())

    # Chaining: read this pack's units from source ⊕ output rather than from
    # source alone, so it transforms what the last pack produced. The merged
    # view is materialised because the whole pipeline — scanner, transform,
    # extractors, writer — works on real paths, and mirroring the source layout
    # means `write_output`'s relative paths still land correctly in output_dir.
    _chain_dir: Optional[tempfile.TemporaryDirectory] = None
    damaged: List[dict] = []
    if chain and not resume and not in_place:
        from forge.verify import syntax
        from forge.verify.merged_tree import MergedTree

        # Before the view is built: a file an earlier run damaged would
        # otherwise be read as this pack's input and carried forward. A dry
        # run only reports it, because a dry run changes nothing on disk.
        if syntax.enabled(config) and Path(output_dir).is_dir():
            damaged = check_output(source_dir, output_dir, config, repair=not dry_run, found_by=phase,
                                   on_event=on_event)["damaged"]
        _chain_dir = tempfile.TemporaryDirectory(prefix="forge-chain-")
        merged = MergedTree(source_dir, output_dir, deleted=run_manifest.deleted_paths(output_dir))
        source_dir = str(merged.materialize(_chain_dir.name).resolve())
        emit(on_event, {"type": "chained", "phase": phase,
                        "reason": "reading from the previous pack's output, not the original source"})

    skipped: list = []
    generated: Sequence[str] = ()
    passed_over = 0
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
        exclude_globs = config.get("scope_exclude_globs") or []
        scan = scan_java_files(source_dir, phase, prefix, exclude_globs)
        files, skipped, generated = scan.files, scan.skipped, scan.generated
        passed_over = scan.passed_over
        if skipped:
            emit(on_event, {"type": "skipped", "count": len(skipped), "prefix": prefix})
        if not files and not generated:
            raise NoEligibleFiles(f"No eligible files found in {source_dir}")
        if not dry_run and files:
            state_manager.mark_pending(files, phase)

    # Generated targets run after the real files so the descriptors they are
    # built from have already been migrated in this run.
    units = [(f, False) for f in files] + [(g, True) for g in generated]
    if damaged:
        chosen = {run_manifest._rel(source_dir, u) for u, _ in units}
        for d in damaged:
            d["selected"] = d["file"] in chosen

    # Before anything is spent: would this pack overwrite a different pack's
    # work? Packs read the original source, so it would replace rather than
    # build on it. Refused rather than merged — see run_manifest's docstring.
    # Chaining is the sanctioned answer, so it is exempt: overwriting is the
    # point when the input was that output.
    if not dry_run and not chain and not in_place:
        clashes = run_manifest.conflicts(output_dir, phase, source_dir, [u for u, _ in units])
        if clashes:
            raise PackOverlap(run_manifest.refusal(phase, clashes))

    total = len(units)
    emit(on_event, {"type": "start", "phase": phase, "files": len(files), "generated": len(generated),
                    "dry_run": dry_run, "total": total, "passed_over": passed_over})

    workers = _workers_for(config)

    def one(i: int, file_path: str, generate: bool) -> dict:
        return run_file(app, config, state_manager, metrics, file_path=file_path, index=i, total=total,
                        phase=phase, dry_run=dry_run, source_dir=source_dir, output_dir=output_dir,
                        generate=generate, thread_id=f"{file_path}#{phase}" if in_place else None,
                        on_event=progress)

    progress = _ProgressRelay(on_event)
    numbered = list(enumerate(units, start=1))
    try:
        # Real files first, then generated targets: those are built from the
        # descriptors the real files migrate, so they must see the finished ones.
        results, cancelled = _run_units([u for u in numbered if not u[1][1]], one, workers, cancel)
        if not cancelled:
            more, cancelled = _run_units([u for u in numbered if u[1][1]], one, workers, cancel)
            results.update(more)
    except BaseException:
        if _chain_dir is not None:
            _chain_dir.cleanup()
        raise
    if cancelled:
        emit(on_event, {"type": "cancelled", "done": len(results), "total": total})

    # Scan order, whatever order the files finished in, so the report, the
    # review queue and the manifest read the same way run after run.
    finals = [results[i] for i in sorted(results)]
    all_statuses: List[FileStatus] = [f["current_file"] for f in finals]
    total_bedrock_calls = sum(f.get("bedrock_calls", 0) for f in finals)
    total_cost = sum(f.get("estimated_cost_usd", 0.0) or 0.0 for f in finals)

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    # Note what this pack owns, so the next one is refused rather than allowed
    # to overwrite it. Recorded from the paths actually written, not the planned
    # ones, so a held or blocked unit claims nothing.
    if not dry_run:
        run_manifest.record(str(output_root), phase,
                            [p for fs in all_statuses for p in (fs.get("written_paths") or [])],
                            deleted=[_tree_rel(d, source_dir) for fs in all_statuses
                                     for d in (fs.get("deleted_files") or [])])

    # The full extracted context, for the reviewer of last resort and for the
    # acceptance checks that diff pre- against post-migration facts. Written in
    # dry-run too: it is an audit artifact, like the report.
    snapshot_path = _write_snapshot(phase, source_dir, str(output_root), [u for u, _ in units], on_event)

    # The review queue: what the pipeline could not settle — and, in a dry run,
    # everything it would have done, since a first trial exists to look at that.
    # It accumulates across the packs of a plan: another pack's held files stay
    # on the page until a human decides them (review_queue.merge_queue).
    queue = write_queue(str(output_root), all_statuses, source_dir, phase=phase, dry_run=dry_run,
                        accumulate=True)
    page_path: Optional[str] = None
    if queue["entries"]:
        page_path = str(write_review_page(str(output_root), queue))
        emit(on_event, {"type": "queue", "path": str(output_root / QUEUE_NAME), "page": page_path,
                        "count": len(queue["entries"])})

    # This pack's report, kept until the pack runs again, and the same text as
    # the latest-run report every earlier reader knows by name.
    pack_report_path = output_root / pack_report_name(phase)
    generate_report(output_path=str(pack_report_path), phase=phase, source_dir=source_dir,
                    file_statuses=all_statuses, bedrock_calls=total_bedrock_calls, estimated_cost_usd=total_cost,
                    skipped=skipped, passed_over=passed_over, damaged=damaged)
    report_path = output_root / REPORT_NAME
    report_path.write_text(pack_report_path.read_text(encoding="utf-8"), encoding="utf-8")

    deleted = [d for fs in all_statuses for d in (fs.get("deleted_files") or [])]

    acceptance_outcome: Optional[AcceptanceOutcome] = None
    if run_acceptance:
        acceptance_outcome = acceptance(phase, source_dir, str(output_root), config, deleted=deleted,
                                        run_build=acceptance_build, dry_run=dry_run)
        _emit_acceptance(on_event, acceptance_outcome)

    testgen_result: Optional[TestGenResult] = None
    if with_tests:
        # Only what this run wrote. A file the migration left alone already has
        # whatever tests it always had, and generating for it would be a
        # different job at a different price.
        written = [p for fs in all_statuses for p in (fs.get("written_paths") or [])]
        # An empty list is not "everything": a run that wrote nothing has
        # nothing to test, and the output directory may hold an earlier run's work.
        testgen_result = generate_tests(source_dir, str(output_root), config, dry_run=dry_run,
                                        only=written, run_tests=run_tests, deleted=deleted,
                                        on_event=on_event, cancel=cancel)

    totals = {
        "total": len(all_statuses),
        "passed": sum(1 for fs in all_statuses if fs.get("status") == "DONE"),
        "manual": sum(1 for fs in all_statuses if fs.get("status") == "MANUAL_REVIEW"),
        "blocked": sum(1 for fs in all_statuses if fs.get("status") == "BLOCKED"),
        "held": sum(1 for fs in all_statuses if fs.get("status") == "HELD"),
        "bedrock_calls": total_bedrock_calls,
        "cost_usd": round(total_cost, 6),
        "passed_over": passed_over,
    }
    if not dry_run and not cancelled:
        # This pack has now run over the reverted view, so whatever it had to
        # redo on a file an earlier check reverted is redone.
        clear_reverted(str(output_root), phase)
    record_pack_run(str(output_root), phase, {
        "run": queue["run"], "dry_run": dry_run, "cancelled": cancelled, "totals": totals,
        "acceptance": acceptance_outcome.report.verdict if acceptance_outcome and acceptance_outcome.report else None,
        "report": pack_report_path.name,
    })
    summary_path = refresh_summary(str(output_root))
    emit(on_event, {"type": "summary", **totals, "report": str(report_path), "plan_summary": str(summary_path)})

    # The chained copy has done its job; everything downstream reads output_dir.
    if _chain_dir is not None:
        _chain_dir.cleanup()

    return RunResult(
        phase=phase, source_dir=source_dir, output_dir=str(output_root), dry_run=dry_run,
        statuses=all_statuses, totals=totals, skipped=list(skipped), queue=queue,
        paths={"report": str(report_path), "pack_report": str(pack_report_path), "summary": str(summary_path),
               "queue": str(output_root / QUEUE_NAME), "page": page_path,
               "snapshot": snapshot_path,
               "acceptance": str(acceptance_outcome.path) if acceptance_outcome and acceptance_outcome.path else None,
               "testgen": testgen_result.paths["report"] if testgen_result else None,
               "generated_tests": testgen_result.paths["record"] if testgen_result else None},
        acceptance=acceptance_outcome, cancelled=cancelled, testgen=testgen_result,
    )


# ─── damage an earlier run left behind ────────────────────────────────────────

# Where a damaged output file is moved, under the staging tree: every walker
# (merged view, landing, project build, test generation) already skips it.
DAMAGED_DIR = ".damaged"


def check_output(source_dir: str, output_dir: str, config: ForgeConfig, *, repair: bool = False,
                 found_by: str = "", on_event: OnEvent = None) -> dict:
    """Parse every Java and XML file FORGE wrote into ``output_dir``; revert the ones that fail.

    Chaining reads the output as the next pack's input, and the per-file syntax
    check only ever sees fresh model output — so a file an earlier run damaged
    (``}ßßß`` after a brace, a doubled ``}``) was carried forward by every pack
    after it, the content filter passing over it because nothing was left to
    modernise. This checks only what ``.forge-writes.json`` says FORGE wrote,
    never the rest of the source.

    A file whose original does not parse either is reported and left where
    it is: there is nothing better to revert to, and it is the source that
    needs fixing.

    With ``repair`` the damaged copy is *moved*, never deleted, to
    ``.forge-staging/.damaged/<path>`` — a human-approved file included, and
    flagged as such — and its manifest entry is dropped, so the merged view
    falls back to the original source. Every file is named in a
    ``damaged_output`` event and in ``migration-summary.md`` with the pack that
    wrote it, which has to run again to redo its changes. Costs no model call.
    """
    from forge.decisions import approved_files
    from forge.utils.file_writer import staging_root
    from forge.utils.report import record_reverted
    from forge.verify import syntax

    out = Path(output_dir)
    owners = run_manifest.load(output_dir) if out.is_dir() else {}
    rels = sorted(r for r in owners if r.lower().endswith((".java", ".xml", ".xmi", ".tld")))
    if not rels:
        return {"verdict": syntax.PASS, "checked": 0, "damaged": [], "unparseable_in_source": []}
    verdict, found = syntax.check_tree(str(out), rels, config)
    if verdict == syntax.SKIPPED:
        emit(on_event, {"type": "output_check_skipped",
                        "reason": "no javac to parse the earlier output with; damage from earlier runs is not checked"})
    as_source: Dict[str, List[str]] = {}
    if found:
        _, as_source = syntax.check_tree(str(Path(source_dir).resolve()), sorted(found), config)

    approved = approved_files(output_dir)
    damaged: List[dict] = []
    for rel in sorted(found):
        # Reverting to an original that is broken too would change nothing,
        # so that file stays where it is -- and is still reported, because it
        # still fails the build. (On AMS it was: a half-finished landing had
        # copied the damaged output back over the source.)
        source_broken = rel in as_source
        entry = {"file": rel, "pack": owners.get(rel), "errors": list(found[rel][:5]),
                 "approved": rel in approved, "found_by": found_by, "moved_to": None,
                 "source_broken": source_broken, "dry_run": not repair}
        if repair and not source_broken:
            target = staging_root(output_dir) / DAMAGED_DIR / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            (out / rel).replace(target)
            entry["moved_to"] = f"{target.relative_to(out.resolve()).as_posix()}"
        damaged.append(entry)
        emit(on_event, {"type": "damaged_output", **entry})
    reverted = [d for d in damaged if d["moved_to"]]
    if reverted:
        run_manifest.forget(output_dir, [d["file"] for d in reverted])
        record_reverted(output_dir, reverted)
        refresh_summary(output_dir)
    return {"verdict": verdict, "checked": len(rels), "damaged": damaged, "reverted": reverted}


# ─── project build ────────────────────────────────────────────────────────────

PROJECT_BUILD_NAME = "project-build.json"
_BUILD_SECTION = "## Project build"


def build_project(source_dir: str, output_dir: str, config: ForgeConfig, *, overlay: bool = True,
                  on_event: OnEvent = None) -> dict:
    """Build the migrated project -- source with the output laid over it -- with its own build.

    The last check before landing, and the one that catches every file a pack
    missed: see ``forge/verify/project_build.py``. Writes ``project-build.json``
    beside the report (``land_on_branch`` reads it), adds a section to the
    report, and emits one ``build`` event. Costs no model call. Never raises on
    a failed build; the result says what failed.

    ``overlay=False`` builds ``source_dir`` as it stands: under
    ``leader.migrate_on_branch`` the migration is already committed there, and
    a fix the user made on the branch must be what gets built, not the output
    copy FORGE wrote before it.
    """
    from forge.verify import project_build
    from forge.verify.merged_tree import MergedTree

    source_dir = str(Path(source_dir).resolve())
    out = Path(output_dir)
    emit(on_event, {"type": "build_start"})
    with tempfile.TemporaryDirectory(prefix="forge-build-") as tmp:
        layered = overlay and out.is_dir()
        merged = MergedTree(source_dir, str(out) if layered else None,
                            deleted=run_manifest.deleted_paths(str(out)) if layered else ())
        result = project_build.run(str(merged.materialize(tmp)), config)

    record = {
        "outcome": result.outcome, "detail": result.detail, "failed_step": result.failed_step,
        "tail": result.tail, "java_home": result.java_home, "seconds": result.seconds,
        "steps": project_build.summarize(result.steps),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_dir": source_dir,
        "fingerprint": project_build.output_fingerprint(source_dir, str(out)),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / PROJECT_BUILD_NAME).write_text(json.dumps(record, indent=2), encoding="utf-8")
    _write_build_section(out / "migration-report.md", record)
    # The build is about the whole plan, not the last pack: the summary carries it.
    refresh_summary(str(out))
    emit(on_event, {"type": "build", **{k: record[k] for k in
                    ("outcome", "detail", "failed_step", "tail", "java_home", "seconds", "steps")}})
    return record


def build_status(source_dir: str, output_dir: str) -> dict:
    """The last build of this output: ``pass``/``fail``/``skip``/``not_run``, and whether it is stale.

    Stale means the migrated files changed after the build (another pack ran,
    a held file was approved), so its verdict is about a tree that no longer
    exists.
    """
    from forge.verify import project_build

    path = Path(output_dir) / PROJECT_BUILD_NAME
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"outcome": "not_run", "stale": False}
    current = project_build.output_fingerprint(str(Path(source_dir).resolve()), output_dir)
    return {**record, "stale": record.get("fingerprint") != current}


def refresh_summary(output_dir: str) -> Path:
    """Regenerate ``migration-summary.md``: every pack's row, the queue now, the last build.

    Called after every run, build and applied decision, so the plan-level view
    never describes a state that has since moved. Costs no model call.
    """
    from forge.review_queue import load_queue
    from forge.utils.report import write_summary

    try:
        queue = load_queue(output_dir)
    except (OSError, ValueError):
        queue = None
    build = None
    try:
        record = json.loads((Path(output_dir) / PROJECT_BUILD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = None
    if isinstance(record, dict):
        build = build_status(str(record["source_dir"]), output_dir) if record.get("source_dir") else record
    return write_summary(output_dir, queue=queue, build=build)


def _write_build_section(report: Path, record: dict) -> None:
    from forge.utils.report import build_section

    section = build_section(record)
    text = report.read_text(encoding="utf-8") if report.is_file() else "# FORGE Migration Report\n"
    if _BUILD_SECTION in text:
        head, rest = text.split(_BUILD_SECTION, 1)
        nxt = rest.find("\n## ")
        text = head + section + (rest[nxt + 1:] if nxt >= 0 else "")
    else:
        text = text.rstrip("\n") + "\n\n" + section
    report.write_text(text, encoding="utf-8")


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

    from forge.utils.report import pack_acceptance_name, pack_report_name, update_pack_row

    decisions = config.get("decisions") or {}
    report = _run([spec], source_dir, output_dir, decisions, deleted=deleted, run_build=run_build)
    path = write_acceptance(report, output_dir)
    # migration-acceptance.json is the latest check; this pack's copy survives the next pack's.
    out = Path(output_dir)
    (out / pack_acceptance_name(phase)).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    per_pack = out / pack_report_name(phase)
    if per_pack.is_file():
        per_pack.write_text(per_pack.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + report.to_markdown(),
                            encoding="utf-8")
    update_pack_row(output_dir, phase, acceptance=report.verdict)
    refresh_summary(output_dir)
    return AcceptanceOutcome(report, path, None, 0 if report.verdict == "PASS" else 1)


# ─── test generation ──────────────────────────────────────────────────────────

def _testgen_state(target, *, source_dir: str, output_dir: str, dry_run: bool, style: str) -> dict:
    from forge.testgen import make_test_unit

    return {
        "current_unit": make_test_unit(
            file_path=target.path, rel_path=target.rel_path, package=target.package,
            type_name=target.type_name, kind=target.kind, test_rel_path=target.test_rel_path, style=style,
        ),
        "source_dir": str(Path(source_dir).resolve()),
        "output_dir": str(Path(output_dir).resolve()),
        "dry_run": dry_run,
        "units_processed": 0,
        "units_generated": 0,
        "units_held": 0,
        "units_blocked": 0,
        "bedrock_calls": 0,
        "estimated_cost_usd": 0.0,
        "messages": [],
    }


def generate_tests(source_dir: str, output_dir: str, config: ForgeConfig, *, dry_run: bool = False,
                   only: Optional[Sequence[str]] = None, run_tests: Optional[bool] = None,
                   deleted: Sequence[str] = (), on_event: OnEvent = None,
                   cancel: Optional[threading.Event] = None) -> TestGenResult:
    """Write JUnit 5 tests for the migrated classes in ``output_dir``.

    Runs over the output tree, because that is what "the new code" is: a
    migration writes only the files it changed. ``only`` narrows it further to
    specific paths — the files one run wrote — so chaining this onto a
    migration costs nothing for classes that migration never touched.

    Never raises on an empty scan. A run with nothing to generate still writes
    the report, because *why* each class was skipped is the answer the person
    asking for tests actually needs.
    """
    from forge.testgen import TestGenSettings, build_record, scan_test_targets, write_record, write_report
    from forge.testgen.graph import build_testgen_graph
    from forge.testgen.runner import TestRunner
    from forge.utils.telemetry import MetricsEmitter

    settings = TestGenSettings.from_config(config)
    if run_tests is not None:
        settings = replace(settings, run=replace(settings.run, enabled=bool(run_tests)))

    source_dir = str(Path(source_dir).resolve())
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    scan = scan_test_targets(str(output_root), source_dir, only=only, overwrite=settings.overwrite,
                             kinds=settings.kinds)
    emit(on_event, {"type": "testgen_start", "targets": len(scan.targets), "skipped": len(scan.skipped),
                    "dry_run": dry_run, "style": settings.style,
                    "run_tests": bool(settings.run.enabled and not dry_run)})

    units: List[dict] = []
    bedrock_calls = 0
    cost = 0.0
    cancelled = False

    if scan.targets:
        # Metrics are the two the Terraform alarms already know. Test units are
        # deliberately absent from files_processed: that metric is the migration's,
        # and the PipelineStalled alarm counts it.
        metrics = MetricsEmitter(config, enabled=not dry_run)
        # The runner is a no-op returning SKIPPED unless run_tests is enabled; it
        # is always wired in so the graph has one shape.
        runner = TestRunner(config, settings, source_dir, str(output_root), deleted=deleted)
        graph = build_testgen_graph(config, settings, runner)
        try:
            for i, target in enumerate(scan.targets, start=1):
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    emit(on_event, {"type": "testgen_cancelled", "done": i - 1, "total": len(scan.targets)})
                    break
                initial = _testgen_state(target, source_dir=source_dir, output_dir=str(output_root),
                                         dry_run=dry_run, style=settings.style)
                final = graph.invoke(initial, config={"configurable": {"thread_id": f"testgen::{target.rel_path}"}})
                unit = final["current_unit"]
                units.append(unit)
                bedrock_calls += final.get("bedrock_calls", 0)
                cost += final.get("estimated_cost_usd", 0.0) or 0.0
                emit(on_event, {
                    "type": "testgen_unit", "index": i, "total": len(scan.targets), "file": target.rel_path,
                    "label": f"{target.type_name} ({target.kind})", "test": unit.get("test_rel_path"),
                    "status": unit.get("status"), "score": unit.get("review_score"),
                    "test_verdict": unit.get("test_verdict"), "retry_count": unit.get("retry_count"),
                    "reason": unit.get("hold_reason") or unit.get("error"),
                    "cost_usd": round(final.get("estimated_cost_usd", 0.0) or 0.0, 6),
                })
                if not dry_run:
                    metrics.emit({"bedrock_calls": final.get("bedrock_calls", 0),
                                  "estimated_cost_usd": final.get("estimated_cost_usd", 0.0)})
        finally:
            runner.close()

    record = build_record(units, scan.skipped, source_dir=source_dir, output_dir=str(output_root),
                          style=settings.style, dry_run=dry_run, bedrock_calls=bedrock_calls, cost_usd=cost)
    record_path = write_record(str(output_root), record)
    report_path = write_report(str(output_root), record)
    totals = dict(record["totals"])
    emit(on_event, {"type": "testgen_summary", **totals, "report": str(report_path), "record": str(record_path),
                    "dependencies": list(record.get("dependencies") or [])})

    return TestGenResult(
        source_dir=source_dir, output_dir=str(output_root), dry_run=dry_run, style=settings.style,
        units=units, skipped=list(scan.skipped), totals=totals,
        paths={"report": str(report_path), "record": str(record_path)}, record=record, cancelled=cancelled,
    )


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
                                 decisions_from, remaining_entries, write_applied_log)
    from forge.review_queue import REVIEW_STATUSES, load_queue, new_run_stamp, save_queue, write_review_page
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

    def put_status(fs) -> None:
        state_manager.put_file_status(fs)
        # An approved or retried-to-DONE unit is now in the output tree, so it
        # is recorded like any run's write: landing takes only recorded files,
        # and the next pack's overlap guard needs to know who owns it.
        if fs.get("status") == "DONE" and fs.get("written_paths"):
            run_manifest.record(output_dir, fs.get("phase") or phase or "java21", fs["written_paths"],
                                deleted=fs.get("deleted_files") or ())

    resolved: Dict[tuple, Any] = {}
    outcomes, remaining = apply_decisions(
        decided, queue, source_dir=source_dir, output_dir=output_dir,
        put_status=put_status, verify_build=verify_build, rerun=rerun, dry_run=dry_run,
        resolved_out=resolved,
    )
    for o in outcomes:
        emit(on_event, {"type": "apply_outcome", "file": o.file, "decision": o.decision, "applied": o.applied,
                        "status_after": o.status_after, "detail": o.detail})

    queue_after = None
    log_path = None
    if not dry_run:
        # Undecided entries are kept verbatim -- other packs' included. An entry
        # a retry put back in the queue has new content, so it and the queue get
        # a new stamp; approving or rejecting only removes, and leaves both alone
        # so every other card on screen stays valid.
        reheld = any(fs.get("status") in REVIEW_STATUSES for fs in resolved.values())
        run_after = new_run_stamp(queue) if reheld else queue.get("run")
        entries = remaining_entries(queue, resolved, source_dir=source_dir, output_dir=output_dir, run=run_after)
        queue_after = save_queue(output_dir, {**queue, "run": run_after, "entries": entries})
        if queue_after["entries"]:
            write_review_page(output_dir, queue_after)
        append_report_section(output_dir, applied_section(outcomes),
                              packs=[str(fs.get("phase") or "") for fs in resolved.values()])
        log_path = write_applied_log(output_dir, run or queue.get("run", ""), decided, outcomes)
        refresh_summary(output_dir)
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
