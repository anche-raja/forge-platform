"""The per-run migration report, and the plan-level summary across packs.

A plan runs several packs into one output directory, and each run used to
overwrite ``migration-report.md`` -- after a ten-pack plan it described only
the last pack. So every run now writes, beside the output:

- ``migration-report-<pack>.md``: that pack's latest run, kept until the same
  pack runs again. ``migration-report.md`` stays too, as "the latest run", for
  anything that already reads it.
- ``migration-acceptance-<pack>.json``: the same for the acceptance record.
- ``migration-summary.md``: one row per pack -- files, passed, manual, blocked,
  held, what is still awaiting review, cost, acceptance verdict -- plus the
  project build, regenerated after every run, build and applied decision.
  ``migration-summary.json`` is the record it is rendered from.

All of these are flat names at the root of the output directory, never a
subdirectory: a ``reports/`` folder could collide with a module of the
project being migrated, and the landing and merged-tree filters already treat
root-level files the source does not have as FORGE's own.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence

from forge.state import FileStatus

if TYPE_CHECKING:  # the scanner pulls in the pack library; a type hint does not need it
    from forge.utils.file_scanner import SkippedFile

REPORT_NAME = "migration-report.md"
SUMMARY_NAME = "migration-summary.md"
SUMMARY_RECORD = "migration-summary.json"
_PER_PACK = re.compile(r"^(migration-report-.+\.md|migration-acceptance-.+\.json)$")


def _slug(pack: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(pack or "unknown"))


def pack_report_name(pack: str) -> str:
    return f"migration-report-{_slug(pack)}.md"


def pack_acceptance_name(pack: str) -> str:
    return f"migration-acceptance-{_slug(pack)}.json"


def is_report_artifact(rel: str) -> bool:
    """True for a root-level report FORGE writes per pack or per plan."""
    rel = str(rel).replace("\\", "/")
    return "/" not in rel and (rel in (SUMMARY_NAME, SUMMARY_RECORD) or bool(_PER_PACK.match(rel)))


def generate_report(
    output_path: str,
    phase: str,
    source_dir: str,
    file_statuses: List[FileStatus],
    bedrock_calls: int,
    estimated_cost_usd: float = 0.0,
    skipped: Sequence["SkippedFile"] = (),
    passed_over: int = 0,
    damaged: Sequence[dict] = (),
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    counts = {s: 0 for s in ("DONE", "MANUAL_REVIEW", "BLOCKED", "HELD", "REJECTED", "PENDING")}
    retried = 0
    context_blind = 0
    missing_context_name = ""
    for fs in file_statuses:
        status = fs.get("status", "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
        if (fs.get("retry_count") or 0) > 0:
            retried += 1
        if fs.get("context_missing"):
            context_blind += 1
            missing_context_name = missing_context_name or (fs.get("context_name") or "")

    lines = [
        f"# FORGE Migration Report",
        f"",
        f"- **Run timestamp:** {now}",
        f"- **Phase:** {phase}",
        f"- **Source directory:** {source_dir}",
        f"- **Files scanned:** {len(file_statuses)}",
        f"- **Files skipped (out of scope):** {len(skipped)}",
        f"- **Files passed over (nothing for this pack to change, not sent):** {passed_over}",
        f"- **Files passed (DONE):** {counts.get('DONE', 0)}",
        f"- **Files retried:** {retried}",
        f"- **Files manual review:** {counts.get('MANUAL_REVIEW', 0)}",
        f"- **Files blocked:** {counts.get('BLOCKED', 0)}",
        f"- **Files held for review (HELD):** {counts.get('HELD', 0)}",
        f"- **Files rejected by reviewer:** {counts.get('REJECTED', 0)}",
        f"- **Total Bedrock calls:** {bedrock_calls}",
        f"- **Estimated Bedrock cost:** ${estimated_cost_usd:.4f}",
    ]

    # A pack whose declared extractor is not built runs without the cross-file
    # facts its author said it needs, and the reviewer loses the same block it
    # would have cross-checked against. That is a quality caveat on every row
    # below, so it is stated here rather than left to a log line.
    if context_blind:
        lines += [
            f"",
            f"> **{context_blind} file(s) were transformed without project context.** This pack "
            f"declares `context: {missing_context_name}`, and no extractor is registered for it, "
            f"so the transform saw only each file's own bytes and the reviewer had no descriptors "
            f"to cross-check against. Treat these results as lower confidence.",
        ]

    lines += [
        f"",
        f"## Per-file Results",
        f"",
        f"| File | Status | Score | Retries | Guardrail Findings |",
        f"|------|--------|-------|---------|--------------------|",
    ]

    for fs in file_statuses:
        fp = fs.get("file_path", "")
        status = fs.get("status", "")
        score = fs.get("review_score", "—")
        retries = fs.get("retry_count", 0)
        findings = "; ".join(fs.get("guardrail_findings") or [])[:80] or "—"
        lines.append(f"| `{fp}` | {status} | {score} | {retries} | {findings} |")

    if skipped:
        lines += [
            "",
            "## Files skipped — outside migration scope",
            "",
            "Not migrated because their package falls outside `scope_package_prefix`. "
            "They cost no Bedrock calls.",
            "",
            "| File | Package |",
            "|------|---------|",
        ]
        lines += [f"| `{sk.path}` | `{sk.package or '(default package)'}` |" for sk in skipped]

    superseded = sorted({d for fs in file_statuses for d in (fs.get("deleted_files") or [])})
    if superseded:
        lines += [
            "",
            "## XML configs replaced by Java configuration",
            "",
            "These were converted to `@Configuration` classes and can be deleted from the source tree:",
            "",
        ]
        lines += [f"- `{d}`" for d in superseded]

    lines += damaged_section(damaged, phase)

    Path(output_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─── the plan-level summary ───────────────────────────────────────────────────

BUILD_SECTION = "## Project build"


def build_section(record: dict) -> str:
    """The ``## Project build`` section, from a ``project-build.json`` record."""
    lines = [BUILD_SECTION, "",
             f"- **Result:** {str(record.get('outcome') or 'not_run').upper()} — {record.get('detail') or ''}",
             f"- **JDK:** {record.get('java_home') or '(default)'}",
             f"- **Built at:** {record.get('built_at')} ({record.get('seconds', 0)}s)", ""]
    lines += [f"- {s}" for s in record.get("steps") or []]
    if record.get("tail"):
        lines += ["", "```", *record["tail"], "```"]
    return "\n".join(lines) + "\n"


def _load_summary(output_dir: str) -> dict:
    try:
        data = json.loads((Path(output_dir) / SUMMARY_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    if not isinstance(data.get("packs"), dict):
        data["packs"] = {}
    if not isinstance(data.get("reverted"), dict):
        data["reverted"] = {}
    return data


def _save_summary(output_dir: str, data: dict) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / SUMMARY_RECORD).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def record_pack_run(output_dir: str, pack: str, row: dict) -> None:
    """Replace ``pack``'s row with this run's. Earlier packs' rows are kept, in the order they first ran."""
    data = _load_summary(output_dir)
    data["packs"][pack] = dict(row)
    _save_summary(output_dir, data)


def update_pack_row(output_dir: str, pack: str, **fields) -> None:
    """Merge ``fields`` into ``pack``'s row, creating it if this pack never ran here."""
    data = _load_summary(output_dir)
    data["packs"].setdefault(pack, {}).update(fields)
    _save_summary(output_dir, data)


def _money(value) -> str:
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "—"


def render_summary(output_dir: str, *, queue: Optional[dict] = None, build: Optional[dict] = None) -> str:
    """``migration-summary.md``: one row per pack, the review queue as it is now, the last build."""
    data = _load_summary(output_dir)
    rows: Dict[str, dict] = data["packs"]
    awaiting: Dict[str, int] = {}
    for e in (queue or {}).get("entries") or []:
        if isinstance(e, dict) and e.get("status") in ("MANUAL_REVIEW", "HELD", "BLOCKED"):
            pack = str(e.get("pack") or "?")
            awaiting[pack] = awaiting.get(pack, 0) + 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = ["# FORGE Migration Summary", "",
             f"- **Updated:** {now}",
             f"- **Output directory:** {Path(output_dir).resolve()}",
             f"- **Packs run here:** {len(rows)}",
             "",
             "One row per pack, from that pack's latest run; its full report is the file named in the "
             "last column. *Awaiting review* is the review queue as it is now, after any decisions "
             "applied since.",
             "",
             "| Pack | Last run | Files | Passed | Manual | Blocked | Held | Awaiting review | Cost | Acceptance | Report |",
             "|------|----------|-------|--------|--------|---------|------|-----------------|------|------------|--------|"]
    sums = {k: 0 for k in ("total", "passed", "manual", "blocked", "held")}
    cost = 0.0
    for pack, row in rows.items():
        t = row.get("totals") or {}
        for k in sums:
            sums[k] += int(t.get(k) or 0)
        cost += float(t.get("cost_usd") or 0.0)
        when = str(row.get("run") or "?") + (" (dry run)" if row.get("dry_run") else "")
        lines.append(f"| {pack} | {when} | {t.get('total', 0)} | {t.get('passed', 0)} | {t.get('manual', 0)} "
                     f"| {t.get('blocked', 0)} | {t.get('held', 0)} | {awaiting.get(pack, 0)} "
                     f"| {_money(t.get('cost_usd'))} | {row.get('acceptance') or 'not run'} "
                     f"| `{row.get('report') or pack_report_name(pack)}` |")
    for pack, n in sorted(awaiting.items()):
        if pack not in rows:
            lines.append(f"| {pack} | (no report) | — | — | — | — | — | {n} | — | — | — |")
    lines.append(f"| **Total** | | {sums['total']} | {sums['passed']} | {sums['manual']} | {sums['blocked']} "
                 f"| {sums['held']} | {sum(awaiting.values())} | {_money(cost)} | | |")

    if data["reverted"]:
        lines += ["", "## Damaged output reverted to the original", ""] + _reverted_intro() + [
            "",
            "| File | Written by | Re-run | Human-approved | Found by | Damaged copy |",
            "|------|------------|--------|----------------|----------|--------------|"]
        for rel, r in sorted(data["reverted"].items()):
            lines.append(f"| `{rel}` | {r.get('pack') or '?'} | **{r.get('pack') or '?'}** "
                         f"| {'yes' if r.get('approved') else 'no'} | {r.get('found_by') or '?'} "
                         f"| `{r.get('moved_to') or ''}` |")

    if build:
        section = build_section(build).rstrip("\n")
        if build.get("stale"):
            section += ("\n\n> **Stale:** the migrated files changed after this build; build again "
                        "before trusting the result.")
        lines += ["", section]
    return "\n".join(lines) + "\n"


def _reverted_intro() -> List[str]:
    return ["These files were written by an earlier run and no longer parse. The damaged copy was moved "
            "under `.forge-staging/.damaged/`, so the file reads as its original source again; the pack "
            "that wrote it has to run again (chained) to redo its changes. A row stays here until it does."]


def record_reverted(output_dir: str, entries: Sequence[dict]) -> None:
    """Note files reverted for damage, until the pack that wrote them runs again."""
    if not entries:
        return
    data = _load_summary(output_dir)
    for e in entries:
        data["reverted"][e["file"]] = {k: e.get(k) for k in ("pack", "approved", "found_by", "moved_to")}
    _save_summary(output_dir, data)


def clear_reverted(output_dir: str, pack: str) -> None:
    """``pack`` ran again over the reverted view, so whatever it had to redo is redone."""
    data = _load_summary(output_dir)
    kept = {rel: r for rel, r in data["reverted"].items() if r.get("pack") != pack}
    if len(kept) != len(data["reverted"]):
        data["reverted"] = kept
        _save_summary(output_dir, data)


def damaged_section(damaged: Sequence[dict], phase: str) -> List[str]:
    """The per-run report's account of what the pre-run check found and did."""
    if not damaged:
        return []
    lines = ["", "## Damaged output from earlier runs", ""]
    if any(d.get("moved_to") for d in damaged):
        lines += _reverted_intro()
    else:
        lines += ["These files, written by an earlier run, do not parse."]
    lines += ["", "| File | Written by | Human-approved | This run | What to do |",
              "|------|------------|----------------|----------|------------|"]
    for d in damaged:
        owner = d.get("pack") or "?"
        if d.get("source_broken"):
            this_run, todo = "left it: the original does not parse either", "fix the file in the source"
        elif not d.get("moved_to"):
            this_run, todo = "dry run: changed nothing", f"a real run reverts it; then re-run **{owner}**"
        else:
            this_run = "re-migrated it from the original" if d.get("selected") else "does not select it"
            todo = ("nothing — this pack wrote it" if owner == phase and d.get("selected")
                    else f"re-run **{owner}**")
        lines.append(f"| `{d['file']}` | {owner} | {'yes' if d.get('approved') else 'no'} | {this_run} | {todo} |")
    first = [e for d in damaged for e in (d.get("errors") or [])[:1]]
    if first:
        lines += ["", "```", *first, "```"]
    return lines


def write_summary(output_dir: str, *, queue: Optional[dict] = None, build: Optional[dict] = None) -> Path:
    path = Path(output_dir) / SUMMARY_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_summary(output_dir, queue=queue, build=build), encoding="utf-8")
    return path
