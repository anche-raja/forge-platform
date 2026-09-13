"""Turn a reviewer's notes into pack edits.

A correction made five times is a pack rule that is wrong. This report groups
every decision note by the pack that produced the unit and, where the note
names one, by rule — with counts and the files it came from — and names the
``.pack.md`` to edit. It is the learning loop: corrections become the next
version of the pack instead of being re-typed per file.
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional

FEEDBACK_NAME = "pack-feedback.md"
_RULE = re.compile(r"\brule\s*#?\s*(\d+)\b", re.IGNORECASE)
_WS = re.compile(r"\s+")


def extract_rule(note: str, explicit: str = "") -> Optional[str]:
    """``Rule N`` from the explicit field, else the first mention in the note."""
    for text in (explicit or "", note or ""):
        m = _RULE.search(text)
        if m:
            return f"Rule {m.group(1)}"
    return None


def normalise_note(note: str) -> str:
    """The note's first non-empty line, lower-cased and squeezed — the grouping key when no rule is named."""
    for line in (note or "").splitlines():
        line = _WS.sub(" ", line).strip().rstrip(".!;:").lower()
        if line:
            return line[:120]
    return "(no note)"


def collect_notes(output_dir: str) -> List[dict]:
    """Every decision recorded under ``output_dir``, deduplicated."""
    out = Path(output_dir)
    seen = set()
    notes: List[dict] = []

    def add(d: dict, source: str) -> None:
        key = (d.get("pack", ""), d.get("file", ""), d.get("decision", ""), (d.get("note") or "").strip())
        if key in seen or not d.get("file"):
            return
        seen.add(key)
        notes.append({"pack": d.get("pack") or "(unknown pack)", "file": d.get("file"), "decision": d.get("decision", ""),
                      "note": (d.get("note") or "").strip(), "rule": (d.get("rule") or "").strip(), "source": source})

    for path in sorted(out.glob("decisions*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for d in (data.get("decisions") or []) if isinstance(data, dict) else []:
            if isinstance(d, dict):
                add(d, path.name)
    log = out / "decisions-applied.jsonl"
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                add(d, log.name)
    return notes


def group_notes(notes: List[dict]) -> Dict[str, Dict[str, dict]]:
    """pack → key → {rule, label, count, files, decisions}; rule-keyed rows first, then by count."""
    groups: Dict[str, Dict[str, dict]] = defaultdict(dict)
    for n in notes:
        rule = extract_rule(n["note"], n["rule"])
        key = rule or normalise_note(n["note"])
        row = groups[n["pack"]].setdefault(key, {"rule": rule, "label": key, "count": 0, "files": set(),
                                                 "decisions": Counter(), "examples": []})
        row["count"] += 1
        row["files"].add(n["file"])
        row["decisions"][n["decision"]] += 1
        if n["note"] and len(row["examples"]) < 3 and n["note"] not in row["examples"]:
            row["examples"].append(n["note"])
    ordered: Dict[str, Dict[str, dict]] = {}
    for pack in sorted(groups):
        rows = groups[pack]
        ordered[pack] = dict(sorted(rows.items(), key=lambda kv: (kv[1]["rule"] is None, -kv[1]["count"], kv[0])))
    return ordered


def pack_edit_path(pack_id: str) -> str:
    """Where the correction belongs."""
    try:
        from forge.phases import BUILTIN_PHASE_NAMES, get_phase
    except Exception:
        return "(unknown pack)"
    if pack_id in BUILTIN_PHASE_NAMES:
        return "forge/phases.py"
    try:
        spec = get_phase(pack_id)
    except ValueError:
        return "(unknown pack)"
    return str(getattr(spec, "source_path", "") or "(unknown pack)")


def render_feedback_report(groups: Dict[str, Dict[str, dict]], output_dir: str) -> str:
    if not groups:
        return (f"# Pack feedback\n\nNo decision notes found in {output_dir} — record decisions on the review page "
                "and apply them with --apply-decisions first.\n")
    lines = ["# Pack feedback", "",
             "A correction made repeatedly is a pack rule that is wrong. Rows are grouped by the rule the note "
             "names, or by the note itself; edit the pack file named under each heading.", ""]
    for pack, rows in groups.items():
        total = sum(r["count"] for r in rows.values())
        lines += [f"## {pack} — {total} note(s)", "", f"Edit: `{pack_edit_path(pack)}`", "",
                  "| Rule / note | Count | Decisions | Files |", "|---|---|---|---|"]
        for row in rows.values():
            decisions = ", ".join(f"{k} ×{v}" for k, v in sorted(row["decisions"].items()))
            files = ", ".join(f"`{f}`" for f in sorted(row["files"])[:6]) + (" …" if len(row["files"]) > 6 else "")
            lines.append(f"| {row['label']} | {row['count']} | {decisions} | {files} |")
        examples = [ex for row in rows.values() for ex in row["examples"] if row["rule"]]
        if examples:
            lines += ["", "<details><summary>Notes as written</summary>", ""]
            lines += [f"- {ex}" for ex in examples[:12]]
            lines += ["", "</details>"]
        lines.append("")
    return "\n".join(lines)


def write_feedback_report(output_dir: str) -> Path:
    groups = group_notes(collect_notes(output_dir))
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / FEEDBACK_NAME
    path.write_text(render_feedback_report(groups, output_dir), encoding="utf-8")
    return path
