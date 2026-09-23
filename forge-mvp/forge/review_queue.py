"""The review queue and the page a human decides from.

The queue is what the pipeline could not settle on its own — plus, in a dry
run, everything it would have done, since a first trial exists precisely to
look at that. Each entry is self-contained: the original text, every
transformed file, the verdicts, the risk reasons. The page renders them side
by side with a diff and a decision widget, and produces the decisions file
that ``--apply-decisions`` consumes. No server, no external assets; it opens
from ``file://``.
"""

import difflib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from forge.state import FileStatus, make_file_status
from forge.utils.file_writer import resolve_relative

QUEUE_NAME = "manual-review-queue.json"
REVIEW_PAGE_NAME = "migration-review.html"
REVIEW_STATUSES = ("MANUAL_REVIEW", "HELD", "BLOCKED")
ORIGINAL_CAP_BYTES = 200_000

_ENTRY_FIELDS = (
    "status", "phase", "generate", "risk_score", "risk_tier", "risk_reasons",
    "review_score", "review_verdict", "review_feedback", "guardrail_pre_verdict",
    "guardrail_post_verdict", "guardrail_findings", "build_verdict", "build_output", "syntax_verdict", "syntax_errors",
    "retry_count", "context_name", "context_digest", "context_missing", "error", "hold_reason",
    "held_paths", "deleted_files", "human_decision", "human_note", "human_rule",
    "human_decided_at",
)


# ─── queue ────────────────────────────────────────────────────────────────────

def needs_review(fs: FileStatus, *, dry_run: bool) -> bool:
    if fs.get("status") in REVIEW_STATUSES:
        return True
    return bool(dry_run and _files_of(fs))


def _files_of(fs: FileStatus) -> Dict[str, str]:
    out = fs.get("transform_output")
    if isinstance(out, dict) and isinstance(out.get("files"), dict):
        return {str(k): str(v) for k, v in out["files"].items()}
    return {}


def _rel(path: str, source_dir: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(source_dir).resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _read_original(fs: FileStatus) -> tuple:
    if fs.get("generate"):
        return None, False
    try:
        data = Path(fs["file_path"]).read_bytes()
    except OSError:
        return None, False
    if len(data) > ORIGINAL_CAP_BYTES:
        text = data[:ORIGINAL_CAP_BYTES].decode("utf-8", errors="replace")
        return text + f"\n... [truncated: {len(data)} bytes total]", True
    return data.decode("utf-8", errors="replace"), False


def build_entry(fs: FileStatus, source_dir: str, output_dir: str) -> dict:
    entry = {"file_path": fs["file_path"], "rel_path": _rel(fs["file_path"], source_dir),
             "pack": fs.get("phase")}
    for key in _ENTRY_FIELDS:
        entry[key] = fs.get(key)

    transformed: Dict[str, str] = {}
    for path, content in _files_of(fs).items():
        transformed[str(resolve_relative(path, content, source_dir)).replace("\\", "/")] = content
    if not transformed:
        # Persisted state may carry only the truncation marker; the files
        # themselves are on disk wherever the run put them.
        for staged in list(fs.get("held_paths") or []) + list(fs.get("written_paths") or []):
            p = Path(staged)
            if p.is_file():
                try:
                    rel = str(p.relative_to(Path(output_dir).resolve() / ".forge-staging")).replace("\\", "/")
                except ValueError:
                    rel = _rel(staged, output_dir)
                transformed[rel] = p.read_text(encoding="utf-8", errors="replace")
    entry["transformed"] = transformed
    entry["original"], entry["original_truncated"] = _read_original(fs)
    return entry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_stamp(queue: Optional[dict] = None) -> str:
    """A run stamp no entry of ``queue`` already carries.

    A card is judged current by its entry's stamp, so two runs inside one
    second must not share one: the second would make the first's cards look
    current for content they never showed. Seconds normally; microseconds only
    on a collision.
    """
    taken = set()
    if queue:
        taken.add(str(queue.get("run") or ""))
        taken |= {str(e.get("run") or "") for e in queue.get("entries") or [] if isinstance(e, dict)}
    stamp = _now()
    if stamp in taken:
        stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    return stamp


def build_queue(statuses: Sequence[FileStatus], source_dir: str, output_dir: str, *, phase: str = "",
                run_id: Optional[str] = None, dry_run: bool = False) -> dict:
    run = run_id or _now()
    entries = []
    for fs in statuses:
        if needs_review(fs, dry_run=dry_run):
            # Stamped per entry: an accumulated queue holds several packs'
            # runs, and a review card is stale only when ITS entry changed.
            entries.append({**build_entry(fs, source_dir, output_dir), "run": run, "dry_run": dry_run})
    return {
        "version": 2,
        "run": run,
        "phase": phase,
        "source_dir": str(Path(source_dir).resolve()),
        "output_dir": str(Path(output_dir).resolve()),
        "dry_run": dry_run,
        "entries": entries,
    }


def entry_key(entry: dict) -> tuple:
    """What identifies an entry in an accumulated queue: the pack and the file."""
    return (str(entry.get("pack") or ""), str(entry.get("rel_path") or entry.get("file_path") or ""))


def entry_run(entry: dict, queue: dict) -> str:
    """The run that produced ``entry`` -- its own stamp, or the queue's for an older file."""
    return str(entry.get("run") or queue.get("run") or "")


def merge_queue(previous: Optional[dict], fresh: dict) -> dict:
    """``fresh`` with the entries of other packs from ``previous`` kept ahead of it.

    A plan runs many packs into one output directory, and the queue used to be
    rewritten by each: after ten packs, held files from the first nine had
    dropped off the review page. The rule is by pack:

    - another pack's entries stay until a human decides them;
    - a real run of a pack replaces all of that pack's entries;
    - a dry run replaces only that pack's *dry-run* entries -- it must not push
      a real held file (staged, waiting on a human) off the page. Where the
      two name the same file, the real entry is the one kept.
    """
    if not previous or previous.get("version") != 2:
        return fresh
    pack = str(fresh.get("phase") or "")
    dry = bool(fresh.get("dry_run"))
    # Staging mirrors the source layout, so a later pack that holds the same
    # file overwrote the earlier pack's staged copy. The earlier entry keeps
    # its own transformed text (approve writes from that) but loses the path:
    # promoting it would ship the later pack's content, and rejecting it would
    # delete the later pack's staged file.
    restaged = {str(p) for e in fresh.get("entries") or [] for p in (e.get("held_paths") or [])}
    kept = []
    for e in previous.get("entries") or []:
        if not isinstance(e, dict):
            continue
        e = {**e, "run": entry_run(e, previous), "dry_run": bool(e.get("dry_run", previous.get("dry_run")))}
        if str(e.get("pack") or "") != pack or (dry and not e["dry_run"]):
            if restaged.intersection(str(p) for p in (e.get("held_paths") or [])):
                e["held_paths"] = [p for p in e["held_paths"] if str(p) not in restaged]
            kept.append(e)
    taken = {entry_key(e) for e in kept}
    entries = kept + [e for e in fresh.get("entries") or [] if entry_key(e) not in taken]
    return {**fresh, "entries": entries}


def save_queue(output_dir: str, queue: dict) -> dict:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / QUEUE_NAME).write_text(json.dumps(queue, indent=2, default=str), encoding="utf-8")
    return queue


def write_queue(output_dir: str, statuses: Sequence[FileStatus], source_dir: str, *, phase: str = "",
                dry_run: bool = False, run_id: Optional[str] = None, accumulate: bool = False) -> dict:
    """Write the queue and return it. An empty queue is still written, so a
    reader never mistakes a stale file for this run's.

    ``accumulate`` keeps other packs' undecided entries (:func:`merge_queue`);
    that is what a pack run does. Without it the file holds only ``statuses``.
    """
    previous = None
    if accumulate:
        try:
            previous = load_queue(output_dir)
        except (OSError, ValueError):
            previous = None
    queue = build_queue(statuses, source_dir, output_dir, phase=phase,
                        run_id=run_id or new_run_stamp(previous), dry_run=dry_run)
    if accumulate:
        queue = merge_queue(previous, queue)
    return save_queue(output_dir, queue)


def load_queue(output_dir: str) -> dict:
    path = Path(output_dir) / QUEUE_NAME
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found — run a migration (or a --dry-run) first to produce the queue")
    queue = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(queue, dict) or queue.get("version") != 2:
        raise ValueError(f"{path} is not a version-2 review queue; re-run the migration to regenerate it")
    return queue


def entry_to_file_status(entry: dict) -> FileStatus:
    """A FileStatus for ``put_file_status`` and re-runs, rebuilt from a queue entry."""
    fs = make_file_status(entry["file_path"], entry.get("pack") or "java21")
    for key in _ENTRY_FIELDS:
        if key in entry and entry[key] is not None:
            fs[key] = entry[key]  # type: ignore[literal-required]
    if entry.get("transformed"):
        fs["transform_output"] = {"files": dict(entry["transformed"]), "deleted_files": entry.get("deleted_files") or [],
                                  "manual_flags": []}
    return fs


# ─── page ─────────────────────────────────────────────────────────────────────

_CSS = """
body{font:14px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;color:#1a1a1a;background:#f4f4f2}
main{max-width:1400px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:0}
.meta{color:#555;font-size:13px;margin-bottom:20px}
section.entry{background:#fff;border:1px solid #d6d6d2;margin:0 0 20px;padding:16px}
.head{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:baseline;margin-bottom:8px}
.tag{font:12px ui-monospace,Menlo,monospace;padding:1px 6px;border:1px solid #bbb;border-radius:3px}
.tag.HIGH{border-color:#a3332b;color:#a3332b}.tag.MEDIUM{border-color:#9a6a1e;color:#9a6a1e}.tag.LOW{border-color:#2b6d4f;color:#2b6d4f}
.tag.HELD{background:#fff3d6}.tag.MANUAL_REVIEW{background:#fde2e0}.tag.BLOCKED{background:#e8e8e8}.tag.DONE{background:#dff0e6}
ul.reasons{margin:4px 0 10px;padding-left:18px;color:#444}
details{margin:6px 0}summary{cursor:pointer;color:#333}
pre{font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word;margin:0;padding:10px;background:#fafaf8;border:1px solid #e2e2de;max-height:520px;overflow:auto}
table.side{width:100%;border-collapse:collapse;table-layout:fixed;margin-top:8px}
table.side th{text-align:left;font-size:12px;color:#555;padding:4px 0}
table.side td{vertical-align:top;padding:0 6px 0 0;width:50%}
pre.diff .add{background:#e2f5e6;display:block}pre.diff .del{background:#fbe3e1;display:block}pre.diff .hunk{color:#6a5acd;display:block}
fieldset.decision{border:1px solid #bbb;margin-top:12px;padding:10px 12px}
fieldset.decision legend{font-weight:600;font-size:13px}
fieldset.decision label{margin-right:14px}
fieldset.decision textarea{width:100%;box-sizing:border-box;min-height:56px;margin-top:6px;font:13px inherit}
fieldset.decision input.rule{width:160px;font:13px inherit;margin-left:8px}
footer{position:sticky;bottom:0;background:#fff;border-top:2px solid #1a1a1a;padding:12px 24px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
footer button{font:13px inherit;padding:6px 12px;cursor:pointer}
#decisions-out{width:100%;min-height:80px;font:12px ui-monospace,Menlo,monospace;margin-top:8px}
.count{color:#555;font-size:13px}
"""

_JS = """
function collect(){
  var out=[];var fs=document.querySelectorAll('fieldset.decision');
  for(var i=0;i<fs.length;i++){
    var f=fs[i];var sel=f.querySelector('input[type=radio]:checked');
    if(!sel||sel.value==='skip')continue;
    var d={file:f.getAttribute('data-file'),pack:f.getAttribute('data-pack'),decision:sel.value,
           note:f.querySelector('textarea.note').value.trim()};
    var rule=f.querySelector('input.rule').value.trim();if(rule)d.rule=rule;
    out.push(d);}
  return {run:document.body.getAttribute('data-run'),decisions:out};
}
function render(){var t=JSON.stringify(collect(),null,2);document.getElementById('decisions-out').value=t;
  document.getElementById('count').textContent=collect().decisions.length+' decision(s)';return t;}
function copyOut(){var t=render();if(navigator.clipboard){navigator.clipboard.writeText(t);}else{document.getElementById('decisions-out').select();document.execCommand('copy');}}
function download(){var t=render();var b=new Blob([t],{type:'application/json'});var a=document.createElement('a');
  a.href=URL.createObjectURL(b);a.download='decisions.json';document.body.appendChild(a);a.click();a.remove();}
document.addEventListener('change',function(e){if(e.target&&e.target.closest('fieldset.decision'))render();});
"""


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _diff_html(original: Optional[str], transformed: Optional[str], rel: str) -> str:
    if original is None or transformed is None:
        return "<pre class=\"diff\">(no diff: " + ("generated unit" if original is None else "no transformed content") + ")</pre>"
    lines = difflib.unified_diff(original.splitlines(), transformed.splitlines(), fromfile=rel, tofile=rel, lineterm="")
    out = []
    for line in lines:
        cls = "hunk" if line.startswith("@@") else "add" if line.startswith("+") else "del" if line.startswith("-") else ""
        out.append(f"<span class=\"{cls}\">{_esc(line)}</span>" if cls else _esc(line))
    return "<pre class=\"diff\">" + ("\n".join(out) if out else "(identical)") + "</pre>"


def _entry_html(i: int, e: dict) -> str:
    rel = e.get("rel_path", "")
    transformed: Dict[str, str] = e.get("transformed") or {}
    primary_key = rel if rel in transformed else (next(iter(transformed)) if transformed else None)
    primary = transformed.get(primary_key) if primary_key else None
    others = {k: v for k, v in transformed.items() if k != primary_key}

    parts = [f"<section class=\"entry\" id=\"e-{i}\">",
             "<div class=\"head\">",
             f"<h2>{_esc(rel)}</h2>",
             f"<span class=\"tag\">{_esc(e.get('pack'))}</span>",
             f"<span class=\"tag {_esc(e.get('status'))}\">{_esc(e.get('status'))}</span>",
             f"<span class=\"tag {_esc(e.get('risk_tier'))}\">risk {_esc(e.get('risk_tier'))} · {_esc(e.get('risk_score'))}</span>"]
    if e.get("review_score") is not None:
        parts.append(f"<span class=\"tag\">review {_esc(e.get('review_score'))} · {_esc(e.get('review_verdict'))}</span>")
    if e.get("build_verdict"):
        parts.append(f"<span class=\"tag\">build {_esc(e.get('build_verdict'))}</span>")
    if e.get("retry_count"):
        parts.append(f"<span class=\"tag\">retries {_esc(e.get('retry_count'))}</span>")
    if e.get("dry_run"):
        parts.append("<span class=\"tag\">dry run — not written</span>")
    parts.append("</div>")

    reasons = e.get("risk_reasons") or []
    if e.get("hold_reason"):
        reasons = [e["hold_reason"]] + list(reasons)
    if reasons:
        parts.append("<ul class=\"reasons\">" + "".join(f"<li>{_esc(r)}</li>" for r in reasons) + "</ul>")
    for label, key in (("Reviewer feedback", "review_feedback"), ("Guardrail findings", "guardrail_findings"),
                       ("Build output", "build_output"), ("Error", "error")):
        val = e.get(key)
        if val:
            body = "\n".join(str(v) for v in val) if isinstance(val, list) else str(val)
            parts.append(f"<details><summary>{label}</summary><pre>{_esc(body)}</pre></details>")

    parts.append("<table class=\"side\"><tr><th>Original</th><th>Transformed"
                 + (f" — {_esc(primary_key)}" if primary_key and primary_key != rel else "") + "</th></tr><tr>")
    parts.append("<td><pre>" + (_esc(e.get("original")) if e.get("original") is not None else "(no source — generated unit)") + "</pre></td>")
    parts.append("<td><pre>" + (_esc(primary) if primary is not None else "(no transformed content)") + "</pre></td>")
    parts.append("</tr></table>")
    parts.append(f"<details open><summary>Diff</summary>{_diff_html(e.get('original'), primary, rel)}</details>")
    for k, v in others.items():
        parts.append(f"<details><summary>Also transformed: {_esc(k)}</summary><pre>{_esc(v)}</pre></details>")
    if e.get("deleted_files"):
        parts.append("<details><summary>Superseded files</summary><pre>" + _esc("\n".join(e["deleted_files"])) + "</pre></details>")

    name = f"d-{i}"
    prior = e.get("human_decision")
    parts.append(f"<fieldset class=\"decision\" data-file=\"{_esc(rel)}\" data-pack=\"{_esc(e.get('pack'))}\">")
    parts.append("<legend>Decision" + (f" (previously: {_esc(prior)})" if prior else "") + "</legend>")
    for value, label in (("skip", "skip"), ("approve", "approve"), ("reject", "reject"), ("retry", "retry with note")):
        checked = " checked" if value == "skip" else ""
        parts.append(f"<label><input type=\"radio\" name=\"{name}\" value=\"{value}\"{checked}> {label}</label>")
    parts.append(f"<label>rule <input class=\"rule\" placeholder=\"e.g. Rule 2\" value=\"{_esc(e.get('human_rule'))}\"></label>")
    parts.append(f"<textarea class=\"note\" placeholder=\"What should change, or why this is rejected\">{_esc(e.get('human_note'))}</textarea>")
    parts.append("</fieldset></section>")
    return "\n".join(parts)


REVIEW_CSS = _CSS


def render_entries(queue: dict) -> str:
    """The entry sections alone — what the live UI embeds. The static page wraps the same HTML."""
    entries = queue.get("entries", [])
    return "\n".join(_entry_html(i, e) for i, e in enumerate(entries)) or "<p>Nothing to review.</p>"


def render_review_page(queue: dict) -> str:
    entries = queue.get("entries", [])
    by_status: Dict[str, int] = {}
    for e in entries:
        by_status[e.get("status", "?")] = by_status.get(e.get("status", "?"), 0) + 1
    counts = " · ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
    # An accumulated queue spans packs; name them all, in the order they appear.
    packs = list(dict.fromkeys(str(e.get("pack")) for e in entries if e.get("pack"))) or [queue.get("phase")]
    title = ", ".join(str(p) for p in packs if p) or ""
    head = (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>FORGE review — {_esc(title)}</title><style>{_CSS}</style></head>"
            f"<body data-run=\"{_esc(queue.get('run'))}\"><main>")
    intro = (f"<h1>FORGE review — {_esc(title)}</h1>"
             f"<div class=\"meta\">run {_esc(queue.get('run'))}"
             + (" · <strong>dry run</strong> — nothing was written; approve to write from the transformed text" if queue.get("dry_run") else "")
             + f" · {len(entries)} file(s): {_esc(counts) or 'none'}<br>source {_esc(queue.get('source_dir'))} · output {_esc(queue.get('output_dir'))}</div>")
    body = render_entries(queue)
    foot = ("</main><footer><button type=\"button\" onclick=\"copyOut()\">Copy decisions JSON</button>"
            "<button type=\"button\" onclick=\"download()\">Download decisions.json</button>"
            "<span class=\"count\" id=\"count\">0 decision(s)</span>"
            "<textarea id=\"decisions-out\" readonly placeholder=\"Decisions appear here; apply with: migrate.py &lt;source&gt; --apply-decisions decisions.json --output-dir &lt;output&gt;\"></textarea>"
            f"</footer><script>{_JS}</script></body></html>")
    return head + intro + body + foot


def write_review_page(output_dir: str, queue: dict) -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / REVIEW_PAGE_NAME
    path.write_text(render_review_page(queue), encoding="utf-8")
    return path
