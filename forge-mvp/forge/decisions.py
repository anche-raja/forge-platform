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

from forge.review_queue import REVIEW_STATUSES, build_entry, entry_key, entry_to_file_status
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


def decisions_from(data, where: str = "<decisions>") -> List[Decision]:
    """Validate a list of decision dicts — from a file or an HTTP body — the same way."""
    if not isinstance(data, list):
        raise ValueError(f"{where}: 'decisions' must be a list")
    out: List[Decision] = []
    for i, d in enumerate(data):
        if not isinstance(d, dict) or not d.get("file") or d.get("decision") not in VALID_DECISIONS:
            raise ValueError(
                f"{where}: decisions[{i}] needs 'file' and a 'decision' in {', '.join(VALID_DECISIONS)}; got {d!r}"
            )
        out.append(Decision(file=str(d["file"]), pack=str(d.get("pack") or ""), decision=d["decision"],
                            note=str(d.get("note") or "").strip(), rule=str(d.get("rule") or "").strip()))
    return out


def load_decisions(path: str) -> Tuple[str, List[Decision]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        raise ValueError(f"{path}: expected {{\"run\": ..., \"decisions\": [...]}}")
    return str(data.get("run") or ""), decisions_from(data["decisions"], str(path))


def find_entry(queue: dict, decision: Decision) -> Optional[dict]:
    """Match by relative path, then absolute, then a unique basename.

    The queue accumulates across packs, so two packs can hold the same file;
    a decision that names its pack (the review page and the chat always do)
    only ever matches that pack's entry.
    """
    entries = queue.get("entries", [])
    if decision.pack:
        entries = [e for e in entries if str(e.get("pack") or decision.pack) == decision.pack]
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
    resolved_out: Optional[Dict[tuple, FileStatus]] = None,
) -> Tuple[List[Outcome], List[FileStatus]]:
    """Apply every decision. Returns the outcomes and the statuses still needing review.

    ``resolved_out``, when given, is filled with ``{entry_key: status after}``
    for every entry a decision touched -- what :func:`remaining_entries` needs
    to rewrite the queue without rebuilding the entries nobody decided.
    """
    outcomes: List[Outcome] = []
    resolved: Dict[tuple, FileStatus] = {} if resolved_out is None else resolved_out

    for d in decisions:
        entry = find_entry(queue, d)
        if entry is None:
            outcomes.append(Outcome(d.file, d.decision, False, "?", "not in the review queue"))
            continue
        rel = entry["rel_path"]
        key = entry_key(entry)
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
            resolved[key] = fs
            outcomes.append(Outcome(rel, d.decision, True, "DONE", detail))

        elif d.decision == "reject":
            if not dry_run:
                discard_staged(output_dir, held)
            fs["status"] = "REJECTED"
            fs["held_paths"] = []
            fs["error"] = f"Rejected by reviewer: {d.note}" if d.note else "Rejected by reviewer"
            _stamp(fs, d)
            resolved[key] = fs
            outcomes.append(Outcome(rel, d.decision, True, "REJECTED", "discarded" + (f" — {d.note}" if d.note else "")))

        else:  # retry
            if not dry_run:
                discard_staged(output_dir, held)
            if dry_run:
                fs["status"] = entry.get("status", "?")
                _stamp(fs, d)
                resolved[key] = fs
                outcomes.append(Outcome(rel, d.decision, True, fs["status"], "dry run — would re-run with the note"))
                continue
            new_fs = rerun(entry, d)
            resolved[key] = new_fs
            outcomes.append(Outcome(rel, d.decision, True, new_fs.get("status", "?"),
                                    f"re-ran with the note; now {new_fs.get('status')}"
                                    + (f", score {new_fs['review_score']}" if new_fs.get("review_score") is not None else "")))

        if not dry_run:
            put_status(resolved[key])

    remaining: List[FileStatus] = []
    for entry in queue.get("entries", []):
        key = entry_key(entry)
        if key in resolved:
            fs = resolved[key]
            if fs.get("status") in REVIEW_STATUSES:
                remaining.append(fs)
        else:
            remaining.append(entry_to_file_status(entry))
    return outcomes, remaining


def remaining_entries(queue: dict, resolved: Dict[tuple, FileStatus], *, source_dir: str,
                      output_dir: str, run: str) -> List[dict]:
    """The queue's entries after the decisions: undecided ones verbatim, re-held ones rebuilt.

    Verbatim matters. An entry from a chained run points at a temporary copy
    of the source that is gone by now, so rebuilding it would lose its
    relative path and its original text. A retried unit that is back in the
    queue has new content, so it gets a new ``run`` stamp and any card that
    showed the old transform goes stale.
    """
    out: List[dict] = []
    for entry in queue.get("entries", []):
        key = entry_key(entry)
        if key not in resolved:
            out.append(entry)
            continue
        fs = resolved[key]
        if fs.get("status") not in REVIEW_STATUSES:
            continue
        rebuilt = build_entry(fs, source_dir, output_dir)
        rebuilt["rel_path"] = entry.get("rel_path") or rebuilt["rel_path"]
        if rebuilt.get("original") is None:
            rebuilt["original"], rebuilt["original_truncated"] = entry.get("original"), entry.get("original_truncated")
        rebuilt["run"] = run
        rebuilt["dry_run"] = bool(entry.get("dry_run"))
        out.append(rebuilt)
    return out


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


def append_report_section(output_dir: str, text: str, packs: Sequence[str] = ()) -> None:
    """Append to the latest-run report, and to the per-pack report of each pack decided."""
    from forge.utils.report import REPORT_NAME, pack_report_name

    targets = [Path(output_dir) / REPORT_NAME]
    for pack in dict.fromkeys(p for p in packs if p):
        per_pack = Path(output_dir) / pack_report_name(pack)
        if per_pack.is_file():
            targets.append(per_pack)
    for md in targets:
        existing = md.read_text(encoding="utf-8").rstrip("\n") + "\n\n" if md.is_file() else ""
        md.write_text(existing + text, encoding="utf-8")
