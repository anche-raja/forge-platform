"""Apply a human's decisions to the review queue.

The engineer records approve / reject / retry-with-note on the review page;
this module makes those decisions real: approved files leave staging and
land in the output tree, rejected ones are discarded with the reason kept,
and a retry re-runs the single unit with the note in its prompt on a fresh
budget. Every decision is recorded on the unit's status so the audit trail —
and DynamoDB — carry who decided what.
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from forge.review_queue import entry_to_file_status
from forge.state import FileStatus
from forge.utils.file_writer import discard_staged, promote_staged, write_files
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

VALID_DECISIONS = ("approve", "reject", "retry")
APPLIED_LOG = "decisions-applied.jsonl"


@dataclass
class Decision:
    file: str
    pack: str
    decision: str
    note: str = ""
    rule: str = ""


@dataclass
class Outcome:
    file: str
    decision: str
    applied: bool
    status_after: str
    detail: str


def load_decisions(path: str) -> Tuple[str, List[Decision]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        raise ValueError(f"{path}: expected {{\"run\": ..., \"decisions\": [...]}}")
    out: List[Decision] = []
    for i, d in enumerate(data["decisions"]):
        if not isinstance(d, dict) or not d.get("file") or d.get("decision") not in VALID_DECISIONS:
            raise ValueError(
                f"{path}: decisions[{i}] needs 'file' and a 'decision' in {', '.join(VALID_DECISIONS)}; got {d!r}"
            )
        out.append(Decision(file=str(d["file"]), pack=str(d.get("pack") or ""), decision=d["decision"],
                            note=str(d.get("note") or "").strip(), rule=str(d.get("rule") or "").strip()))
    return str(data.get("run") or ""), out


def find_entry(queue: dict, decision: Decision) -> Optional[dict]:
    """Match by relative path, then absolute, then a unique basename."""
    entries = queue.get("entries", [])
    for e in entries:
        if e.get("rel_path") == decision.file or e.get("file_path") == decision.file:
            return e
    by_name = [e for e in entries if Path(e.get("rel_path", "")).name == Path(decision.file).name]
    return by_name[0] if len(by_name) == 1 else None


def _stamp(fs: FileStatus, decision: Decision) -> None:
    fs["human_decision"] = decision.decision
    fs["human_note"] = decision.note or None
    fs["human_rule"] = decision.rule or None
    fs["human_decided_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def apply_decisions(
    decisions: Sequence[Decision],
    queue: dict,
    *,
    source_dir: str,
    output_dir: str,
    put_status: Callable[[FileStatus], None],
    verify_build: Callable[[List[str]], dict],
    rerun: Callable[[dict, Decision], FileStatus],
    dry_run: bool = False,
) -> Tuple[List[Outcome], List[FileStatus]]:
    """Apply every decision. Returns the outcomes and the statuses still needing review."""
    outcomes: List[Outcome] = []
    resolved: Dict[str, FileStatus] = {}   # rel_path -> status after the decision

    for d in decisions:
        entry = find_entry(queue, d)
        if entry is None:
            outcomes.append(Outcome(d.file, d.decision, False, "?", "not in the review queue"))
            continue
        rel = entry["rel_path"]
        fs = entry_to_file_status(entry)
        held = [p for p in (entry.get("held_paths") or []) if Path(p).is_file()]

        if d.decision == "approve":
            if entry.get("status") == "BLOCKED":
                outcomes.append(Outcome(rel, d.decision, False, "BLOCKED", "a blocked unit has no transform to approve"))
                continue
            if dry_run:
                written: List[str] = []
                detail = "dry run — would write " + (f"{len(held)} staged file(s)" if held else f"{len(entry.get('transformed') or {})} transformed file(s)")
            elif held:
                written = promote_staged(output_dir, held)
                detail = f"promoted {len(written)} staged file(s)"
            elif entry.get("transformed"):
                written = write_files(entry["transformed"], source_dir, Path(output_dir))
                detail = f"wrote {len(written)} file(s) from the transformed text"
            else:
                outcomes.append(Outcome(rel, d.decision, False, entry.get("status", "?"), "nothing to write"))
                continue
            fs["status"] = "DONE"
            fs["written_paths"] = written
            fs["held_paths"] = []
            _stamp(fs, d)
            if written and not dry_run:
                result = verify_build(written)
                fs["build_verdict"], fs["build_output"] = result.get("verdict"), result.get("output")
                if result.get("verdict") == "FAIL":
                    # A human approval is final; the failure is reported, not reversed.
                    detail += f"; build FAIL (approval stands)"
                elif result.get("verdict"):
                    detail += f"; build {result['verdict']}"
            resolved[rel] = fs
            outcomes.append(Outcome(rel, d.decision, True, "DONE", detail))

        elif d.decision == "reject":
            if not dry_run:
                discard_staged(output_dir, held)
            fs["status"] = "REJECTED"
            fs["held_paths"] = []
            fs["error"] = f"Rejected by reviewer: {d.note}" if d.note else "Rejected by reviewer"
            _stamp(fs, d)
            resolved[rel] = fs
            outcomes.append(Outcome(rel, d.decision, True, "REJECTED", "discarded" + (f" — {d.note}" if d.note else "")))

        else:  # retry
            if not dry_run:
                discard_staged(output_dir, held)
            if dry_run:
                fs["status"] = entry.get("status", "?")
                _stamp(fs, d)
                resolved[rel] = fs
                outcomes.append(Outcome(rel, d.decision, True, fs["status"], "dry run — would re-run with the note"))
                continue
            new_fs = rerun(entry, d)
            resolved[rel] = new_fs
            outcomes.append(Outcome(rel, d.decision, True, new_fs.get("status", "?"),
                                    f"re-ran with the note; now {new_fs.get('status')}"
                                    + (f", score {new_fs['review_score']}" if new_fs.get("review_score") is not None else "")))

        if not dry_run:
            put_status(resolved[rel])

    remaining: List[FileStatus] = []
    for entry in queue.get("entries", []):
        rel = entry["rel_path"]
        if rel in resolved:
            fs = resolved[rel]
            if fs.get("status") in ("HELD", "MANUAL_REVIEW", "BLOCKED"):
                remaining.append(fs)
        else:
            remaining.append(entry_to_file_status(entry))
    return outcomes, remaining


def write_applied_log(output_dir: str, run: str, decisions: Sequence[Decision], outcomes: Sequence[Outcome]) -> Path:
    path = Path(output_dir) / APPLIED_LOG
    by_file = {o.file: o for o in outcomes}
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as f:
        for d in decisions:
            o = by_file.get(d.file) or next((o for o in outcomes if Path(o.file).name == Path(d.file).name), None)
            f.write(json.dumps({"at": at, "run": run, **asdict(d),
                                "applied": bool(o and o.applied), "status_after": o.status_after if o else "?"}) + "\n")
    return path


def applied_section(outcomes: Sequence[Outcome]) -> str:
    lines = ["## Applied decisions", "",
             f"{sum(o.applied for o in outcomes)} applied, {sum(not o.applied for o in outcomes)} not applied", "",
             "| File | Decision | Applied | Status after | Detail |", "|---|---|---|---|---|"]
    for o in outcomes:
        lines.append(f"| `{o.file}` | {o.decision} | {'yes' if o.applied else 'no'} | {o.status_after} | {o.detail} |")
    return "\n".join(lines) + "\n"


def append_report_section(output_dir: str, text: str) -> None:
    md = Path(output_dir) / "migration-report.md"
    existing = md.read_text(encoding="utf-8").rstrip("\n") + "\n\n" if md.is_file() else ""
    md.write_text(existing + text, encoding="utf-8")
