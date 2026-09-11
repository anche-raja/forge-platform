#!/usr/bin/env python3
"""FORGE migration CLI."""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _build_initial_state(config, file_path: str, phase: str, dry_run: bool, source_dir: str, output_dir: str) -> dict:
    from forge.state import make_file_status
    return {
        "current_file": make_file_status(file_path, phase),
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
        "bedrock_calls": 0,
        "estimated_cost_usd": 0.0,
        "messages": [],
    }


def run_file(app, config, state_manager, metrics, file_path: str, index: int, total: int, phase: str, dry_run: bool, source_dir: str, output_dir: str) -> dict:
    initial = _build_initial_state(config, file_path, phase, dry_run, source_dir, output_dir)
    cfg = {"configurable": {"thread_id": file_path}}

    final = app.invoke(initial, config=cfg)
    fs = final["current_file"]
    status = fs.get("status", "UNKNOWN")
    score = fs.get("review_score")
    score_str = f", score: {score}" if score is not None else ""

    print(f"[{index}/{total}] {Path(file_path).name} → {status}{score_str}")

    if not dry_run:
        state_manager.put_file_status(fs)
        # Emitted per file, not per run: the FORGE-PipelineStalled alarm watches
        # for files_processed dropping below 1 in a 15-minute window, so a long
        # run that only reported at the end would trip it.
        _emit_file_metrics(metrics, final, fs)

    return final


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
    parser.add_argument("source_dir", help="Root of the Java project to migrate")
    from forge.phases import PHASES, PHASE_NAMES
    phase_help = " | ".join(f"{n}: {PHASES[n].description}" for n in PHASE_NAMES)
    parser.add_argument("--phase", required=True, choices=list(PHASE_NAMES),
                        help=f"Migration phase — {phase_help}")
    parser.add_argument("--dry-run", action="store_true", help="Run full pipeline without writing files or updating DynamoDB")
    parser.add_argument("--resume", action="store_true", help="Process only files with PENDING status in DynamoDB")
    parser.add_argument("--file", dest="single_file", help="Process a single file path only")
    parser.add_argument("--output-dir", default="./migrated", help="Destination root for migrated files (default: ./migrated)")
    parser.add_argument("--config", default=None, help="Path to agents.yaml (default: agents.yaml)")
    parser.add_argument("--no-metrics", action="store_true", help="Skip CloudWatch metric emission")
    parser.add_argument("--log-level", default=None, help="Logging level (default: INFO, or $FORGE_LOG_LEVEL)")
    args = parser.parse_args()

    from forge.utils.telemetry import MetricsEmitter, configure_logging
    configure_logging(args.log_level)

    from forge.config import ForgeConfig
    from forge.graph import build_graph
    from forge.state_store.dynamodb import DynamoDBStateManager
    from forge.utils.file_scanner import scan_java_files
    from forge.utils.report import generate_report

    config = ForgeConfig(args.config)
    app = build_graph(config)
    state_manager = DynamoDBStateManager(config)
    metrics = MetricsEmitter(config, enabled=not args.no_metrics and not args.dry_run)

    source_dir = str(Path(args.source_dir).resolve())

    # Determine file list
    skipped = []
    if args.single_file:
        # Naming a file explicitly beats a config default — no scope filtering.
        files = [str(Path(args.single_file).resolve())]
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
        files, skipped = scan.files, scan.skipped
        if skipped:
            print(f"Skipped {len(skipped)} file(s) outside scope prefix "
                  f"'{config.get('scope_package_prefix', '')}'")
        if not files:
            print(f"No eligible files found in {source_dir}")
            sys.exit(0)
        if not args.dry_run:
            state_manager.mark_pending(files, args.phase)

    total = len(files)
    print(f"FORGE — phase: {args.phase} | files: {total} | dry-run: {args.dry_run}")

    all_statuses = []
    total_bedrock_calls = 0
    total_cost = 0.0

    for i, file_path in enumerate(files, start=1):
        final = run_file(
            app, config, state_manager, metrics,
            file_path=file_path,
            index=i,
            total=total,
            phase=args.phase,
            dry_run=args.dry_run,
            source_dir=source_dir,
            output_dir=args.output_dir,
        )
        all_statuses.append(final["current_file"])
        total_bedrock_calls += final.get("bedrock_calls", 0)
        total_cost += final.get("estimated_cost_usd", 0.0) or 0.0

    output_root = Path(args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    # Write manual review queue alongside migrated output
    manual = [fs for fs in all_statuses if fs.get("status") == "MANUAL_REVIEW"]
    if manual and not args.dry_run:
        queue_path = output_root / "manual-review-queue.json"
        with open(queue_path, "w", encoding="utf-8") as f:
            json.dump(manual, f, indent=2, default=str)
        print(f"\nManual review queue: {queue_path} ({len(manual)} files)")

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

    passed = sum(1 for fs in all_statuses if fs.get("status") == "DONE")
    blocked = sum(1 for fs in all_statuses if fs.get("status") == "BLOCKED")
    manual_count = len(manual)

    print(f"\nSummary: {passed} passed | {manual_count} manual | {blocked} blocked | {total_bedrock_calls} Bedrock calls")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
