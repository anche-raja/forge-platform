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


def run_file(app, config, state_manager, file_path: str, index: int, total: int, phase: str, dry_run: bool, source_dir: str, output_dir: str) -> dict:
    from forge.state import make_file_status

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

    return final


def main():
    parser = argparse.ArgumentParser(description="FORGE Java migration pipeline")
    parser.add_argument("source_dir", help="Root of the Java project to migrate")
    parser.add_argument("--phase", required=True, choices=["java21"], help="Migration phase")
    parser.add_argument("--dry-run", action="store_true", help="Run full pipeline without writing files or updating DynamoDB")
    parser.add_argument("--resume", action="store_true", help="Process only files with PENDING status in DynamoDB")
    parser.add_argument("--file", dest="single_file", help="Process a single file path only")
    parser.add_argument("--output-dir", default="./migrated", help="Destination root for migrated files (default: ./migrated)")
    parser.add_argument("--config", default=None, help="Path to agents.yaml (default: agents.yaml)")
    args = parser.parse_args()

    from forge.config import ForgeConfig
    from forge.graph import build_graph
    from forge.state_store.dynamodb import DynamoDBStateManager
    from forge.utils.file_scanner import scan_java_files
    from forge.utils.report import generate_report

    config = ForgeConfig(args.config)
    app = build_graph(config)
    state_manager = DynamoDBStateManager(config)

    source_dir = str(Path(args.source_dir).resolve())

    # Determine file list
    if args.single_file:
        files = [str(Path(args.single_file).resolve())]
    elif args.resume:
        pending = state_manager.get_files_by_status("PENDING")
        files = [fs["file_path"] for fs in pending]
        if not files:
            print("No PENDING files found in DynamoDB. Nothing to resume.")
            sys.exit(0)
    else:
        files = scan_java_files(source_dir, args.phase)
        if not files:
            print(f"No eligible .java files found in {source_dir}")
            sys.exit(0)
        if not args.dry_run:
            state_manager.mark_pending(files, args.phase)

    total = len(files)
    print(f"FORGE — phase: {args.phase} | files: {total} | dry-run: {args.dry_run}")

    all_statuses = []
    total_bedrock_calls = 0

    for i, file_path in enumerate(files, start=1):
        final = run_file(
            app, config, state_manager,
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

    # Write manual review queue
    manual = [fs for fs in all_statuses if fs.get("status") == "MANUAL_REVIEW"]
    if manual and not args.dry_run:
        queue_path = "manual-review-queue.json"
        with open(queue_path, "w") as f:
            json.dump(manual, f, indent=2, default=str)
        print(f"\nManual review queue: {queue_path} ({len(manual)} files)")

    # Generate report
    report_path = "migration-report.md"
    generate_report(
        output_path=report_path,
        phase=args.phase,
        source_dir=source_dir,
        file_statuses=all_statuses,
        bedrock_calls=total_bedrock_calls,
    )

    passed = sum(1 for fs in all_statuses if fs.get("status") == "DONE")
    blocked = sum(1 for fs in all_statuses if fs.get("status") == "BLOCKED")
    manual_count = len(manual)

    print(f"\nSummary: {passed} passed | {manual_count} manual | {blocked} blocked | {total_bedrock_calls} Bedrock calls")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
