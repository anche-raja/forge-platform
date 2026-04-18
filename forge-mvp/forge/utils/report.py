from datetime import datetime, timezone
from pathlib import Path
from typing import List

from forge.state import FileStatus


def generate_report(
    output_path: str,
    phase: str,
    source_dir: str,
    file_statuses: List[FileStatus],
    bedrock_calls: int,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    counts = {s: 0 for s in ("DONE", "MANUAL_REVIEW", "BLOCKED", "PENDING")}
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
        f"- **Files passed (DONE):** {counts.get('DONE', 0)}",
        f"- **Files retried:** {retried}",
        f"- **Files manual review:** {counts.get('MANUAL_REVIEW', 0)}",
        f"- **Files blocked:** {counts.get('BLOCKED', 0)}",
        f"- **Total Bedrock calls:** {bedrock_calls}",
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

    Path(output_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
