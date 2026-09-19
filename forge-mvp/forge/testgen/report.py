"""What a test-generation stage leaves behind.

Two files, for two readers. ``generated-tests.json`` is the machine record —
every unit, its verdicts, the cases it claims to cover and the members it could
not reach; a held unit carries its generated source inline, because that file
is not in the tree and the JSON is the only place to read it. ``test-generation-
report.md`` is the same thing for a person, ordered by what needs doing:
the dependencies the build is missing, then the held units, then everything
that was skipped and why.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence

from forge.testgen.targets import SkippedTarget

REPORT_NAME = "test-generation-report.md"
RECORD_NAME = "generated-tests.json"

_UNIT_FIELDS = (
    "rel_path", "package", "type_name", "kind", "test_rel_path", "test_class", "status",
    "review_score", "review_verdict", "review_feedback", "check_failures", "scan_findings",
    "retry_count", "written_paths", "held_paths", "hold_reason", "test_verdict", "test_output_log",
    "cases", "untested", "dependencies", "notes", "generate_model", "review_model", "error",
)


def totals_of(units: Sequence[dict]) -> Dict[str, int]:
    def count(status: str) -> int:
        return sum(1 for u in units if u.get("status") == status)

    return {
        "total": len(units),
        "generated": count("GENERATED"),
        "held": count("HELD"),
        "blocked": count("BLOCKED"),
        "tests_passed": sum(1 for u in units if u.get("test_verdict") == "PASS"),
        "tests_failed": sum(1 for u in units if u.get("test_verdict") == "FAIL"),
        "retried": sum(1 for u in units if (u.get("retry_count") or 0) > 0),
    }


def collect_dependencies(units: Sequence[dict]) -> List[str]:
    return sorted({str(d) for u in units for d in (u.get("dependencies") or []) if str(d).strip()})


def build_record(units: Sequence[dict], skipped: Sequence[SkippedTarget], *, source_dir: str, output_dir: str,
                 style: str, dry_run: bool, bedrock_calls: int = 0, cost_usd: float = 0.0) -> dict:
    entries = []
    for unit in units:
        entry = {key: unit.get(key) for key in _UNIT_FIELDS}
        # A held test is not on disk anywhere a reader would look; carry it.
        if unit.get("status") != "GENERATED" or dry_run:
            entry["files"] = dict(((unit.get("test_output") or {}).get("files") or {}))
        entries.append(entry)
    return {
        "version": 1,
        "run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "style": style,
        "dry_run": dry_run,
        "source_dir": str(Path(source_dir).resolve()),
        "output_dir": str(Path(output_dir).resolve()),
        "totals": {**totals_of(units), "bedrock_calls": bedrock_calls, "cost_usd": round(cost_usd, 6)},
        "dependencies": collect_dependencies(units),
        "units": entries,
        "skipped": [{"rel_path": s.rel_path, "reason": s.reason} for s in skipped],
    }


def write_record(output_dir: str, record: dict) -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / RECORD_NAME
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return path


def write_report(output_dir: str, record: dict) -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / REPORT_NAME
    path.write_text(render_report(record), encoding="utf-8")
    return path


# ─── markdown ─────────────────────────────────────────────────────────────────

def render_report(record: dict) -> str:
    totals = record.get("totals", {})
    units = record.get("units", [])
    lines = [
        "# FORGE Test Generation Report",
        "",
        f"- **Run timestamp:** {record.get('run', '')}",
        f"- **Style:** {record.get('style', '')}",
        f"- **Source directory:** {record.get('source_dir', '')}",
        f"- **Output directory:** {record.get('output_dir', '')}",
        f"- **Dry run:** {bool(record.get('dry_run'))}",
        f"- **Classes considered:** {totals.get('total', 0)}",
        f"- **Tests written:** {totals.get('generated', 0)}",
        f"- **Held for review:** {totals.get('held', 0)}",
        f"- **Blocked before any model call:** {totals.get('blocked', 0)}",
        f"- **Generated tests executed:** {totals.get('tests_passed', 0)} passed, "
        f"{totals.get('tests_failed', 0)} failed",
        f"- **Units retried:** {totals.get('retried', 0)}",
        f"- **Bedrock calls:** {totals.get('bedrock_calls', 0)}",
        f"- **Estimated Bedrock cost:** ${float(totals.get('cost_usd', 0.0)):.4f}",
    ]

    deps = record.get("dependencies") or []
    if deps:
        lines += [
            "",
            "## Test dependencies the generated tests need",
            "",
            "Add these at test scope before the tests will compile. FORGE never edits the build file.",
            "",
        ]
        lines += [f"- `{d}`" for d in deps]

    lines += [
        "",
        "## Per-class results",
        "",
        "| Class | Kind | Status | Score | Retries | Test | Test file |",
        "|-------|------|--------|-------|---------|------|-----------|",
    ]
    for unit in units:
        # "SKIPPED" in the Test column would read as a verdict on the test; it
        # means the run gate was off or the toolchain was absent.
        ran = {None: "—", "SKIPPED": "not run"}.get(unit.get("test_verdict"), unit.get("test_verdict"))
        lines.append(
            f"| `{unit.get('rel_path', '')}` | {unit.get('kind', '')} | {unit.get('status', '')} "
            f"| {unit.get('review_score', '—')} | {unit.get('retry_count', 0)} "
            f"| {ran} | `{unit.get('test_rel_path', '')}` |"
        )

    held = [u for u in units if u.get("status") in ("HELD", "BLOCKED")]
    if held:
        lines += [
            "",
            "## Not written — a human decides",
            "",
            "These tests are **not** in the build. A held unit's source is staged under "
            "`.forge-staging/` and carried inline in `generated-tests.json`.",
            "",
        ]
        for unit in held:
            reason = unit.get("hold_reason") or unit.get("error") or "—"
            lines.append(f"### `{unit.get('rel_path', '')}` — {unit.get('status')}")
            lines.append("")
            lines.append(f"- **Why:** {reason}")
            if unit.get("check_failures"):
                lines.append("- **Mechanical failures:** " + "; ".join(unit["check_failures"]))
            if unit.get("review_feedback"):
                lines.append(f"- **Reviewer:** {unit['review_feedback']}")
            if unit.get("test_verdict") == "FAIL" and unit.get("test_output_log"):
                lines += ["", "```", str(unit["test_output_log"])[:2000], "```"]
            lines.append("")

    untested = [(u, m) for u in units for m in (u.get("untested") or [])]
    if untested:
        lines += ["", "## Members left untested", "",
                  "What the generator could not reach without guessing. Each is a gap a human can close.",
                  "", "| Class | Member | Reason |", "|-------|--------|--------|"]
        for unit, member in untested:
            lines.append(f"| `{unit.get('rel_path', '')}` | `{member.get('member', '')}` "
                         f"| {member.get('reason', '')} |")

    notes = [(u, n) for u in units for n in (u.get("notes") or [])]
    if notes:
        lines += ["", "## Notes from the generator", ""]
        lines += [f"- `{u.get('rel_path', '')}`: {n}" for u, n in notes]

    skipped = record.get("skipped") or []
    if skipped:
        lines += ["", "## Classes skipped", "",
                  "Not sent to a model, and not a gap in the run — each reason is mechanical.",
                  "", "| Class | Reason |", "|-------|--------|"]
        lines += [f"| `{s.get('rel_path', '')}` | {s.get('reason', '')} |" for s in skipped]

    return "\n".join(lines) + "\n"
