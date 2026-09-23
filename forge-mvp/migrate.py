#!/usr/bin/env python3
"""FORGE migration CLI — a thin printer over ``forge.service``.

Every command here calls the same functions the web UI calls; this file only
parses arguments, turns service events into the lines it has always printed,
and maps results to exit codes.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from forge.config import ConfigError

load_dotenv()


# ─── events → the historical stdout ───────────────────────────────────────────

def _print_event(event: dict) -> None:
    """Reproduce the CLI's output line for line from service events."""
    t = event["type"]
    if t == "skipped":
        print(f"Skipped {event['count']} file(s) outside scope prefix '{event['prefix']}'")
    elif t == "start":
        gen_str = f" (+{event['generated']} generated)" if event["generated"] else ""
        print(f"FORGE — phase: {event['phase']} | files: {event['files']}{gen_str} | dry-run: {event['dry_run']}")
        if event.get("passed_over"):
            print(f"{event['passed_over']} file(s) had nothing for this pack to change and were not sent")
    elif t == "file":
        score_str = f", score: {event['score']}" if event["score"] is not None else ""
        print(f"[{event['index']}/{event['total']}] {event['label']} → {event['status']}{score_str}")
    elif t == "snapshot":
        print(f"Context snapshot: {event['path']}")
    elif t == "snapshot_skipped":
        print(f"Context snapshot skipped: {event['reason']}")
    elif t == "chained":
        print(f"Chaining: {event['reason']}")
    elif t == "context_missing":
        # Printed before the per-file lines, because it is a caveat on all of them.
        print(f"WARNING: {event['reason']}.\n"
              f"         Every file in this run is transformed without project context, and the\n"
              f"         reviewer has no descriptors to cross-check. Results are lower confidence.")
    elif t == "queue":
        print(f"\nReview queue: {event['path']} ({event['count']} files)")
        print(f"Review page:  {event['page']}")
    elif t == "acceptance_skipped":
        print(f"\nAcceptance: {event['reason']}")
    elif t == "acceptance":
        print(f"\nAcceptance: {event['verdict']} — {event['passed']} passed, {event['failed']} failed, "
              f"{event['skipped']} skipped")
        for r in event["results"]:
            print(f"  [{r['outcome'].upper():4}] {r['kind']:<16} {r['detail']}")
        print(f"Acceptance record: {event['path']}")
    elif t == "testgen_start":
        run_str = " | running them" if event["run_tests"] else ""
        print(f"\nTest generation ({event['style']}) — {event['targets']} class(es), "
              f"{event['skipped']} skipped{run_str}")
    elif t == "testgen_unit":
        score_str = f", score: {event['score']}" if event["score"] is not None else ""
        verdict = f", tests: {event['test_verdict']}" if event["test_verdict"] not in (None, "SKIPPED") else ""
        reason = f" — {event['reason']}" if event.get("reason") else ""
        print(f"[{event['index']}/{event['total']}] {event['label']} → {event['status']}"
              f"{score_str}{verdict}{reason}")
    elif t == "testgen_cancelled":
        print(f"\nTest generation cancelled after {event['done']} of {event['total']} class(es)")
    elif t == "testgen_summary":
        print(f"\nTests: {event['generated']} written | {event['held']} held | {event['blocked']} blocked "
              f"| {event['tests_passed']} passed | {event['tests_failed']} failed")
        if event["dependencies"]:
            print("Test dependencies needed: " + ", ".join(event["dependencies"]))
        print(f"Test report: {event['report']}")
    elif t == "cancelled":
        print(f"\nCancelled after {event['done']} of {event['total']} unit(s)")
    elif t == "summary":
        held_str = f" | {event['held']} held" if event["held"] else ""
        print(f"\nSummary: {event['passed']} passed | {event['manual']} manual | {event['blocked']} blocked"
              f"{held_str} | {event['bedrock_calls']} Bedrock calls")
        print(f"Report: {event['report']}")


# ─── commands ─────────────────────────────────────────────────────────────────

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

    from forge.utils.file_scanner import degraded_phases, runnable_phases

    runnable = set(runnable_phases())
    degraded = degraded_phases()

    print(f"{len(registry)} packs — {len(registry.complete)} complete, "
          f"{len(registry.detect_only)} detect-only\n")
    for i, pack_id in enumerate(registry.order, 1):
        pack = registry[pack_id]
        mark = " " if pack.is_complete else "*"
        deps = f"  after: {', '.join(pack.depends_on)}" if pack.depends_on else ""
        # A complete pack that is not runnable is blocked on a selector's
        # extractor; a runnable one may still be missing a declared context.
        if pack.is_complete and pack_id not in runnable:
            note = "  ← BLOCKED: needs the '%s' extractor" % pack.context
        elif pack_id in degraded:
            note = "  ← runs WITHOUT context (needs the '%s' extractor)" % degraded[pack_id]
        else:
            note = ""
        print(f"{i:3}.{mark} [{pack.tier:<11}] {pack.id:<28} {pack.title}{note}")
        if deps:
            print(f"     {deps}")
    print("\n* detect-only — recognised, reported, but not yet migrated")
    if degraded:
        print("\nA pack marked \"runs WITHOUT context\" transforms each file from its own bytes\n"
              "alone: the cross-file facts its author declared are unavailable, and the reviewer\n"
              "loses the descriptors it would have cross-checked. It works; it is less reliable.")
    return 0


def _discover(args) -> int:
    import os

    from forge import service
    from forge.config import ForgeConfig
    from forge.packs import PackError

    config_path = args.config or os.environ.get("FORGE_AGENTS_YAML", "agents.yaml")
    config = ForgeConfig(config_path) if Path(config_path).is_file() else None
    if args.intent and config is None:
        # Discovery alone needs no config; one model call does.
        print(f"--intent needs {config_path}: it makes one model call to map the request "
              f"onto decisions.\nRun without --intent for evidence-only discovery.")
        return 1
    try:
        result = service.discover(args.source_dir, args.output_dir, config, intent=args.intent)
    except PackError as e:
        print(f"Pack library failed to load:\n  {e}")
        return 1
    print(result["summary"])
    if "intent" in result:
        from forge.intent.plan import IntentPlan

        # to_json's keys are IntentPlan's fields, so the round trip is exact.
        print()
        print(IntentPlan(**result["intent"]).render())
    print(f"\nProfile: {result['paths']['yaml']}\nDetail:  {result['paths']['json']}")
    if "intent" in result["paths"]:
        print(f"Plan:    {result['paths']['intent']}")
    return 0


def _feedback_report(args) -> int:
    from forge import service

    result = service.feedback(args.output_dir)
    packs = result["packs"]
    print(f"{result['notes']} decision note(s) across {len(packs)} pack(s)" + (f": {', '.join(packs)}" if packs else ""))
    print(f"Feedback report: {result['path']}")
    return 0


def _apply_decisions(args) -> int:
    """Exit 0 only when every decision applied, so a CI step can gate on it."""
    from forge import service
    from forge.config import ForgeConfig
    from forge.decisions import load_decisions

    config = ForgeConfig(args.config)
    try:
        run, decisions = load_decisions(args.apply_decisions)
        if not decisions:
            print("No decisions in the file — nothing to apply.")
            return 0
        result = service.apply(decisions, args.source_dir, args.output_dir, config, run=run,
                               dry_run=args.dry_run, phase=args.phase, on_event=_print_event)
    except (FileNotFoundError, ValueError) as e:
        print(f"Cannot apply decisions: {e}")
        return 1

    print(f"{'FILE':<48} {'DECISION':<8} {'APPLIED':<8} {'STATUS':<14} DETAIL")
    for o in result.outcomes:
        print(f"{o.file[-48:]:<48} {o.decision:<8} {'yes' if o.applied else 'no':<8} {o.status_after:<14} {o.detail}")
    if not args.dry_run:
        print(f"\n{len(result.queue_after['entries'])} file(s) still awaiting review · decisions logged to {result.log_path}")
    else:
        print("\nDry run — nothing was written, moved or re-run.")
    return 0 if result.all_applied else 1


def _acceptance_only(args) -> int:
    from forge import service
    from forge.config import ForgeConfig

    outcome = service.acceptance(args.phase, str(Path(args.source_dir).resolve()), args.output_dir,
                                 ForgeConfig(args.config), run_build=args.acceptance_build, dry_run=False)
    service._emit_acceptance(_print_event, outcome)
    return outcome.exit_code


def _generate_tests_only(args) -> int:
    """Write tests for an existing --output-dir. Exit 0 only when nothing needs a human."""
    from forge import service
    from forge.config import ForgeConfig
    from forge.utils.telemetry import configure_logging

    configure_logging(args.log_level)
    result = service.generate_tests(
        args.source_dir, args.output_dir, ForgeConfig(args.config), dry_run=args.dry_run,
        run_tests=args.run_tests or None, on_event=_print_event,
    )
    return result.exit_code


def _migrate(args) -> int:
    from forge import service
    from forge.config import ForgeConfig
    from forge.utils.telemetry import configure_logging

    configure_logging(args.log_level)
    config = ForgeConfig(args.config)
    try:
        service.run_migration(
            args.source_dir, args.phase, args.output_dir, config,
            dry_run=args.dry_run, single_file=args.single_file, resume=args.resume, no_metrics=args.no_metrics,
            run_acceptance=args.acceptance, acceptance_build=args.acceptance_build,
            with_tests=args.generate_tests, run_tests=args.run_tests or None, on_event=_print_event,
        )
    except service.NoEligibleFiles as e:
        print(str(e))
        sys.exit(0)
    except service.PackOverlap as e:
        # Non-zero: nothing ran, and a script chaining packs must not continue
        # as though the migration happened.
        print(f"Refused: {e}")
        sys.exit(2)
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
    parser.add_argument("--intent", metavar="TEXT",
                        help="With --discover: describe in plain English what you want migrated, and "
                             "FORGE narrows the detected packs and sets the decisions to match "
                             "(one model call; it can never add a pack the evidence does not support)")
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
    parser.add_argument("--generate-tests", action="store_true",
                        help="After the run, generate JUnit 5 tests for the files it wrote")
    parser.add_argument("--generate-tests-only", action="store_true",
                        help="Skip migration; generate tests for the migrated classes already in --output-dir")
    parser.add_argument("--run-tests", action="store_true",
                        help="Execute each generated test and hold the ones that fail (needs the toolchain; slow)")
    parser.add_argument("--apply-decisions", metavar="DECISIONS_JSON",
                        help="Apply a reviewer's approve/reject/retry decisions to the review queue in --output-dir")
    parser.add_argument("--feedback-report", action="store_true",
                        help="Group reviewers' notes by pack and rule into pack-feedback.md in --output-dir")
    parser.add_argument("--log-level", default=None, help="Logging level (default: INFO, or $FORGE_LOG_LEVEL)")
    parser.add_argument("--ui", action="store_true",
                        help="Start the local web UI (loopback only) and open it in a browser; no other arguments needed")
    parser.add_argument("--port", type=int, default=None, help="Port for --ui (default: 8765, or the next free one)")
    parser.add_argument("--no-browser", action="store_true", help="With --ui: print the URL instead of opening a browser")
    args = parser.parse_args()

    if args.ui:
        from forge.ui.server import DEFAULT_PORT, serve
        return serve(port=args.port or DEFAULT_PORT, open_browser=not args.no_browser, strict_port=args.port is not None)
    if args.list_packs:
        return _list_packs()
    if args.feedback_report:
        return _feedback_report(args)
    if not args.source_dir:
        parser.error("source_dir is required (or use --list-packs)")
    if args.discover:
        return _discover(args)
    if args.apply_decisions:
        return _apply_decisions(args)
    if args.generate_tests_only:
        return _generate_tests_only(args)
    if not args.phase:
        parser.error("--phase is required")
    if args.acceptance_only:
        return _acceptance_only(args)
    return _migrate(args)


if __name__ == "__main__":
    # main() returns an exit code on the acceptance paths; a CI gate needs it.
    try:
        sys.exit(main() or 0)
    except ConfigError as e:
        # A config the user can fix, so it gets its message and not a traceback.
        print(f"Configuration error: {e}")
        sys.exit(2)
