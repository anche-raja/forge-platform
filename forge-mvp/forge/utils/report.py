from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence

from forge.state import FileStatus
from forge.utils.file_scanner import SkippedFile


def generate_report(
    output_path: str,
    phase: str,
    source_dir: str,
    file_statuses: List[FileStatus],
    bedrock_calls: int,
    estimated_cost_usd: float = 0.0,
    skipped: Sequence[SkippedFile] = (),
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    counts = {s: 0 for s in ("DONE", "MANUAL_REVIEW", "BLOCKED", "HELD", "REJECTED", "PENDING")}
    retried = 0
    for fs in file_statuses:
        status = fs.get("status", "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
        if (fs.get("retry_count") or 0) > 0:
            retried += 1

    lines = [
        f"# FORGE Migration Report",
        f"",
        f"- **Run timestamp:** {now}",
        f"- **Phase:** {phase}",
        f"- **Source directory:** {source_dir}",
        f"- **Files scanned:** {len(file_statuses)}",
        f"- **Files skipped (out of scope):** {len(skipped)}",
        f"- **Files passed (DONE):** {counts.get('DONE', 0)}",
        f"- **Files retried:** {retried}",
        f"- **Files manual review:** {counts.get('MANUAL_REVIEW', 0)}",
        f"- **Files blocked:** {counts.get('BLOCKED', 0)}",
        f"- **Files held for review (HELD):** {counts.get('HELD', 0)}",
        f"- **Files rejected by reviewer:** {counts.get('REJECTED', 0)}",
        f"- **Total Bedrock calls:** {bedrock_calls}",
        f"- **Estimated Bedrock cost:** ${estimated_cost_usd:.4f}",
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

    Path(output_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
