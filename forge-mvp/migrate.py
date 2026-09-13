#!/usr/bin/env python3
"""FORGE migration CLI."""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _build_initial_state(config, file_path: str, phase: str, dry_run: bool, source_dir: str, output_dir: str,
                         generate: bool = False) -> dict:
    from forge.state import make_file_status
    file_status = make_file_status(file_path, phase)
    file_status["generate"] = generate
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


def run_file(app, config, state_manager, metrics, file_path: str, index: int, total: int, phase: str, dry_run: bool,
             source_dir: str, output_dir: str, generate: bool = False) -> dict:
    initial = _build_initial_state(config, file_path, phase, dry_run, source_dir, output_dir, generate=generate)
    cfg = {"configurable": {"thread_id": file_path}}

    final = app.invoke(initial, config=cfg)
    fs = final["current_file"]
    status = fs.get("status", "UNKNOWN")
    score = fs.get("review_score")
    score_str = f", score: {score}" if score is not None else ""

    label = f"{Path(file_path).name} (generated)" if generate else Path(file_path).name
    print(f"[{index}/{total}] {label} → {status}{score_str}")

    if not dry_run:
        state_manager.put_file_status(fs)
        # Emitted per file, not per run: the FORGE-PipelineStalled alarm watches
        # for files_processed dropping below 1 in a 15-minute window, so a long
        # run that only reported at the end would trip it.
        _emit_file_metrics(metrics, final, fs)

    return final


def _discover(args) -> int:
    """Profile a repository and say which packs apply, on what evidence.

    No model, no AWS. The profile it writes is the editable input the rest of
    the platform consumes; a pack it names as blocked or detect-only is a gap
    made visible, not a technology silently ignored.
    """
    from forge.discover import build_profile, render_summary, resolve_packs, write_outputs
    from forge.discover.emit import DEFAULT_DECISIONS
    from forge.discover.resolve import content_patterns
    from forge.packs import PackError, load_packs
    from forge.utils.file_scanner import runnable_phases

    try:
        registry = load_packs()
    except PackError as e:
        print(f"Pack library failed to load:\n  {e}")
        return 1
    decisions = dict(DEFAULT_DECISIONS)
    if args.config and Path(args.config).is_file():
        from forge.config import ForgeConfig
        decisions.update(ForgeConfig(args.config).get("decisions") or {})

    source_dir = str(Path(args.source_dir).resolve())
    packs = list(registry.values())
    profile = build_profile(source_dir, content_patterns=content_patterns(packs), decisions=decisions)
    activations = resolve_packs(profile, packs)
    runnable = set(runnable_phases())
    for a in activations:
        a.runnable = a.pack_id in runnable
    order = registry.resolve_order([a.pack_id for a in activations])

    print(render_summary(profile, activations, order))
    json_path, yaml_path = write_outputs(profile, activations, order, decisions, args.output_dir)
    print(f"\nProfile: {yaml_path}\nDetail:  {json_path}")
    return 0


def _acceptance_only(args) -> int:
    from forge.config import ForgeConfig

    config = ForgeConfig(args.config)
    source_dir = str(Path(args.source_dir).resolve())
    return _run_acceptance(args.phase, source_dir, args.output_dir, config,
                           run_build=args.acceptance_build, dry_run=False)


def _run_acceptance(phase: str, source_dir: str, output_dir: str, config, *, deleted=(), run_build: bool,
                    dry_run: bool) -> int:
    """Execute the phase's acceptance checks and append the verdict to the report.

    A pack's checks gate the *project*, independently of how files scored.
    In a dry run nothing was written, so there is no post-migration tree to
    check — say so rather than report the pre-migration state as a result.
    """
    from forge.phases import get_phase
    from forge.verify.acceptance import run_acceptance, write_acceptance

    spec = get_phase(phase)
    checks = getattr(spec, "acceptance", ())
    if not checks:
        print(f"\nAcceptance: phase '{phase}' declares no acceptance checks")
        return 0
    if dry_run:
        print("\nAcceptance: skipped — a dry run writes nothing, so there is no post-migration tree to check")
        return 0

    decisions = config.get("decisions") or {}
    report = run_acceptance([spec], source_dir, output_dir, decisions, deleted=deleted, run_build=run_build)
    path = write_acceptance(report, output_dir)
    print(f"\nAcceptance: {report.verdict} — {sum(r.passed for r in report.results)} passed, "
          f"{len(report.failed)} failed, {len(report.skipped)} skipped")
    for r in report.results:
        print(f"  [{r.outcome.upper():4}] {r.kind:<16} {r.detail}")
    print(f"Acceptance record: {path}")
    return 0 if report.verdict == "PASS" else 1


def _write_snapshot(phase: str, source_dir: str, output_dir: str, unit_paths, get_extractor, write_context_snapshot) -> None:
    from forge.phases import get_phase

    name = getattr(get_phase(phase), "context", "none")
    extractor = get_extractor(name) if name != "none" else None
    if extractor is None:
        return
    modules = sorted({extractor.module_for(p, source_dir) for p in unit_paths})
    try:
        path = write_context_snapshot(output_dir, name, source_dir, modules)
    except ValueError as e:
        print(f"Context snapshot skipped: {e}")
        return
    print(f"Context snapshot: {path}")


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


def _list_packs() -> int:
    """Print the pack library in dependency order. Exits non-zero if it is broken.

    This is the one place a malformed pack reports in full — everywhere else the
    library degrades to the built-in phases with a warning so that Phase 0 keeps
    running.
    """
    from forge.packs import PackError, load_packs

    try:
        registry = load_packs()
    except PackError as e:
        print(f"Pack library failed to load:\n  {e}")
        return 1

    print(f"{len(registry)} packs — {len(registry.complete)} complete, "
          f"{len(registry.detect_only)} detect-only\n")
    for i, pack_id in enumerate(registry.order, 1):
        pack = registry[pack_id]
        mark = " " if pack.is_complete else "*"
        deps = f"  after: {', '.join(pack.depends_on)}" if pack.depends_on else ""
        print(f"{i:3}.{mark} [{pack.tier:<11}] {pack.id:<28} {pack.title}")
        if deps:
            print(f"     {deps}")
    print("\n* detect-only — recognised, reported, but not yet migrated")
    return 0


def _force_utf8_console() -> None:
    """Windows consoles default to cp1252, which cannot encode the arrows and
    box characters this CLI prints. Degrade gracefully rather than crash."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    _force_utf8_console()
    parser = argparse.ArgumentParser(description="FORGE Java migration pipeline")
    parser.add_argument("source_dir", nargs="?", help="Root of the Java project to migrate")
    parser.add_argument("--list-packs", action="store_true",
                        help="List the stack pack library and exit")
    parser.add_argument("--discover", action="store_true",
                        help="Profile source_dir and report which packs apply; writes forge-profile.yaml")
    from forge.phases import PHASE_NAMES, get_phase
    phase_help = " | ".join(f"{n}: {get_phase(n).description}" for n in PHASE_NAMES)
    parser.add_argument("--phase", choices=list(PHASE_NAMES),
                        help=f"Migration phase — {phase_help}")
    parser.add_argument("--dry-run", action="store_true", help="Run full pipeline without writing files or updating DynamoDB")
    parser.add_argument("--resume", action="store_true", help="Process only files with PENDING status in DynamoDB")
    parser.add_argument("--file", dest="single_file", help="Process a single file path only")
    parser.add_argument("--output-dir", default="./migrated", help="Destination root for migrated files (default: ./migrated)")
    parser.add_argument("--config", default=None, help="Path to agents.yaml (default: agents.yaml)")
    parser.add_argument("--no-metrics", action="store_true", help="Skip CloudWatch metric emission")
    parser.add_argument("--acceptance", action="store_true",
                        help="After the run, execute the phase's acceptance checks over the merged tree")
    parser.add_argument("--acceptance-only", action="store_true",
                        help="Skip migration; run acceptance checks against an existing --output-dir")
    parser.add_argument("--acceptance-build", action="store_true",
                        help="Also run `build` acceptance checks (needs the toolchain; slow)")
    parser.add_argument("--log-level", default=None, help="Logging level (default: INFO, or $FORGE_LOG_LEVEL)")
    args = parser.parse_args()

    if args.list_packs:
        return _list_packs()
    if not args.source_dir:
        parser.error("source_dir is required (or use --list-packs)")
    if args.discover:
        return _discover(args)
    if not args.phase:
        parser.error("--phase is required")
    if args.acceptance_only:
        return _acceptance_only(args)

    from forge.utils.telemetry import MetricsEmitter, configure_logging
    configure_logging(args.log_level)

    from forge.config import ForgeConfig
    from forge.graph import build_graph
    from forge.state_store.dynamodb import DynamoDBStateManager
    from forge.utils.file_scanner import scan_java_files
    from forge.utils.report import generate_report

    from forge.context.snapshot import write_context_snapshot
    from forge.extract import clear_context_cache, get_extractor
    from forge.extract.selectors import is_generated_target
    from forge.phases import get_phase

    clear_context_cache()
    config = ForgeConfig(args.config)
    app = build_graph(config)
    state_manager = DynamoDBStateManager(config)
    metrics = MetricsEmitter(config, enabled=not args.no_metrics and not args.dry_run)

    source_dir = str(Path(args.source_dir).resolve())

    # Determine file list
    skipped = []
    generated = ()
    if args.single_file:
        # Naming a file explicitly beats a config default — no scope filtering.
        files = [str(Path(args.single_file).resolve())]
        if is_generated_target(get_phase(args.phase), files[0]):
            files, generated = [], (files[0],)
    elif args.resume:
        pending = state_manager.get_files_by_status("PENDING")
        files = [fs["file_path"] for fs in pending]
        if not files:
            print("No PENDING files found in DynamoDB. Nothing to resume.")
            sys.exit(0)
    else:
        # Scope filtering happens here, before any model call, so an out-of-scope
        # file costs nothing rather than being discovered mid-pipeline.
        scan = scan_java_files(source_dir, args.phase, config.get("scope_package_prefix", ""))
        files, skipped, generated = scan.files, scan.skipped, scan.generated
        if skipped:
            print(f"Skipped {len(skipped)} file(s) outside scope prefix "
                  f"'{config.get('scope_package_prefix', '')}'")
        if not files and not generated:
            print(f"No eligible files found in {source_dir}")
            sys.exit(0)
        if not args.dry_run and files:
            state_manager.mark_pending(files, args.phase)

    # Generated targets run after the real files so the descriptors they are
    # built from have already been migrated in this run.
    units = [(f, False) for f in files] + [(g, True) for g in generated]
    total = len(units)
    gen_str = f" (+{len(generated)} generated)" if generated else ""
    print(f"FORGE — phase: {args.phase} | files: {len(files)}{gen_str} | dry-run: {args.dry_run}")

    all_statuses = []
    total_bedrock_calls = 0
    total_cost = 0.0

    for i, (file_path, generate) in enumerate(units, start=1):
        final = run_file(
            app, config, state_manager, metrics,
            file_path=file_path,
            index=i,
            total=total,
            phase=args.phase,
            dry_run=args.dry_run,
            source_dir=source_dir,
            output_dir=args.output_dir,
            generate=generate,
        )
        all_statuses.append(final["current_file"])
        total_bedrock_calls += final.get("bedrock_calls", 0)
        total_cost += final.get("estimated_cost_usd", 0.0) or 0.0

    output_root = Path(args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    # The full extracted context, for the reviewer of last resort and for the
    # acceptance checks that diff pre- against post-migration facts. Written in
    # dry-run too: it is an audit artifact, like the report.
    _write_snapshot(args.phase, source_dir, str(output_root), [u for u, _ in units], get_extractor, write_context_snapshot)

    # The review queue: what the pipeline could not settle — and, in a dry run,
    # everything it would have done, since a first trial exists to look at that.
    from forge.review_queue import QUEUE_NAME, write_queue, write_review_page

    manual = [fs for fs in all_statuses if fs.get("status") == "MANUAL_REVIEW"]
    queue = write_queue(str(output_root), all_statuses, source_dir, phase=args.phase, dry_run=args.dry_run)
    if queue["entries"]:
        page = write_review_page(str(output_root), queue)
        print(f"\nReview queue: {output_root / QUEUE_NAME} ({len(queue['entries'])} files)")
        print(f"Review page:  {page}")

    report_path = output_root / "migration-report.md"
    generate_report(
        output_path=str(report_path),
        phase=args.phase,
        source_dir=source_dir,
        file_statuses=all_statuses,
        bedrock_calls=total_bedrock_calls,
        estimated_cost_usd=total_cost,
        skipped=skipped,
    )

    if args.acceptance:
        deleted = [d for fs in all_statuses for d in (fs.get("deleted_files") or [])]
        _run_acceptance(args.phase, source_dir, str(output_root), config, deleted=deleted,
                        run_build=args.acceptance_build, dry_run=args.dry_run)

    passed = sum(1 for fs in all_statuses if fs.get("status") == "DONE")
    blocked = sum(1 for fs in all_statuses if fs.get("status") == "BLOCKED")
    manual_count = len(manual)

    held = sum(1 for fs in all_statuses if fs.get("status") == "HELD")
    held_str = f" | {held} held" if held else ""
    print(f"\nSummary: {passed} passed | {manual_count} manual | {blocked} blocked{held_str} | {total_bedrock_calls} Bedrock calls")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    # main() returns an exit code on the acceptance paths; a CI gate needs it.
    sys.exit(main() or 0)
