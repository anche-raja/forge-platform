"""Two renderings of one result: what the model may read, and what the browser shows.

Every tool result leaves this module twice. The **observation** goes into a
``ToolMessage`` and is therefore read by a model; the **card** goes down the SSE
stream and is read by a person. The split is GUARDRAILS.md §7 — *a model can
never be the control that decides what a model may see* — applied one layer up,
and it is why the reducers here are the only way a queue entry becomes an
observation.

Three fields are the reason this file exists rather than a couple of dict
comprehensions at the call sites:

``guardrail_findings`` is ``f"{category}: {data}"`` where ``data`` is the raw
Bedrock assessment (``bedrock_guardrails.py:30-34``), and a
``sensitiveInformationPolicy`` assessment embeds the matched bytes verbatim —
the AWS key or password the guardrail exists to stop. Only the part before the
first colon ever leaves here. Local secret-scan findings ("AWS access key id at
line 2") carry no colon and survive whole, which is the rule GUARDRAILS §7
already states for them.

``review_feedback`` is overwritten with compiler output on a build failure
(``graph.py:52-59``), and javac prints the offending source line under every
error. The observation carries ``has_feedback``/``feedback_kind`` instead, so
the model knows there is feedback to point a human at without being shown it.

Acceptance ``evidence`` is ``rel:line: <80 chars of matched source>`` for
no-match checks and the tail of the build output for build checks. One reducer,
``acceptance_obs``, is used by both ``check_acceptance`` and ``run_pack`` so a
second call site cannot forget.

Cards may carry all of it — a diff is the whole point of a review card — because
a card is rendered in a browser and never sent anywhere else.
"""

import difflib
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

# A model-written `reason`, an exception message, an apply `detail`: bounded so
# one long string cannot crowd the rest of an observation out of the window.
TEXT_CAP = 200
# The diff of a 3000-line file is not a review card, it is a denial of service
# on the person reading it. The card says it was cut.
DIFF_LINE_CAP = 400
# Bedrock's guardrail policy categories. A finding's kind, never its payload.
GUARDRAIL_KIND_CAP = 40


def cap(value: Any, limit: int = TEXT_CAP) -> Optional[str]:
    """Trim a string for an observation; None stays None."""
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit]


# ─── observation reducers (model-visible; R3) ─────────────────────────────────

def guardrail_kinds(findings: Any) -> List[str]:
    """The kind of each finding and nothing after it.

    ``finding.split(":", 1)[0]`` is the secret-scan kind or the Bedrock policy
    category. The remainder of a Bedrock finding is the assessment dict, which
    quotes the matched secret.
    """
    if not isinstance(findings, (list, tuple)):
        return []
    return [str(f).split(":", 1)[0][:GUARDRAIL_KIND_CAP] for f in findings]


def guardrail_obs(findings: Any) -> Dict[str, Any]:
    kinds = guardrail_kinds(findings)
    return {"count": len(kinds), "kinds": kinds}


def entry_obs(entry: dict) -> Dict[str, Any]:
    """One review-queue entry as metadata: paths, verdicts, scores. No text."""
    if not isinstance(entry, dict):
        return {}
    build_verdict = entry.get("build_verdict")
    feedback = entry.get("review_feedback")
    obs: Dict[str, Any] = {
        "file": str(entry.get("rel_path") or entry.get("file_path") or ""),
        "pack": entry.get("pack"),
        "status": entry.get("status"),
        "risk_tier": entry.get("risk_tier"),
        # Rule-generated, from forge/risk/score.py — a closed set of sentences
        # about the file's shape, never a quote from it.
        "risk_reasons": [str(r) for r in (entry.get("risk_reasons") or [])],
        "review_score": entry.get("review_score"),
        "review_verdict": entry.get("review_verdict"),
        "build_verdict": build_verdict,
        "has_feedback": bool(feedback),
        "hold_reason": cap(entry.get("hold_reason")),
        "guardrail_findings": guardrail_obs(entry.get("guardrail_findings")),
        "error": cap(entry.get("error")),
    }
    if build_verdict == "FAIL":
        # The one case where "feedback" is compiler output, which echoes source.
        obs["feedback_kind"] = "build"
    return obs


def acceptance_obs(outcome: Any) -> Dict[str, Any]:
    """An acceptance result as counts. The evidence strings stay in the card."""
    data = outcome.to_json() if hasattr(outcome, "to_json") else outcome
    if not isinstance(data, dict):
        return {"verdict": None, "passed": 0, "failed": 0, "skipped": 0, "results": []}
    results = data.get("results")
    rows = []
    for r in results if isinstance(results, list) else []:
        if not isinstance(r, dict):
            continue
        evidence = r.get("evidence")
        rows.append({
            "pack": r.get("pack"), "kind": r.get("kind"), "scope": r.get("scope"),
            "outcome": r.get("outcome"),
            "evidence_count": len(evidence) if isinstance(evidence, (list, tuple)) else 0,
        })
    return {
        "verdict": data.get("verdict"),
        "passed": data.get("passed", 0),
        "failed": data.get("failed", 0),
        "skipped": data.get("skipped", 0),
        # A fixed platform sentence ("phase 'x' declares no acceptance checks"),
        # and the only thing that makes a null verdict readable.
        "skipped_reason": cap(data.get("skipped_reason")),
        "results": rows,
    }


# ─── cards (browser-only) ─────────────────────────────────────────────────────

def plan_card(discovery: dict, *, intent: bool, request: Optional[str] = None) -> dict:
    """The discovery result, whole.

    Deliberately unreduced: chat.js hands this straight to the existing
    ``steps.discover.show`` / ``steps.intent.show``, which read
    ``activations[].evidence``, ``summary`` and ``paths``. A reduced plan card
    would make those views throw.
    """
    return {"kind": "plan", "intent": bool(intent), "request": request, "discovery": discovery}


def estimate_card(pack: str, units: int, generated: int, est_usd: float, unit_cost_usd: float,
                  note: str) -> dict:
    return {"kind": "estimate", "pack": pack, "units": units, "generated": generated,
            "est_usd": round(float(est_usd), 4), "unit_cost_usd": unit_cost_usd, "note": note}


def confirm_card(pending_id: str, tool: str, title: str, args: dict, est_usd: float, *,
                 units: Optional[int] = None, decisions: Optional[list] = None) -> dict:
    """What the click asserts, in full.

    ``decisions`` is verbatim — including each ``note``, which reaches the
    transform prompt as "HUMAN REVIEW FEEDBACK … takes precedence". When the
    model wrote that note, the human's click is signing it, so the human has to
    be able to read it first.
    """
    return {"kind": "confirm", "pending_id": pending_id, "tool": tool, "title": title,
            "args": dict(args or {}), "est_usd": round(float(est_usd), 4),
            "units": units, "decisions": decisions}


def unified_diff(original: str, transformed: str, rel: str) -> tuple:
    """A plain unified diff, capped. Returns (text, truncated)."""
    lines = list(difflib.unified_diff(original.splitlines(), transformed.splitlines(),
                                      fromfile=rel, tofile=rel, lineterm=""))
    truncated = len(lines) > DIFF_LINE_CAP
    if truncated:
        lines = lines[:DIFF_LINE_CAP] + [f"... [diff truncated at {DIFF_LINE_CAP} lines]"]
    return "\n".join(lines), truncated


def review_file_card(entry: dict, *, run: str = "") -> dict:
    """One held file, with its diff. The browser's copy of a queue entry.

    ``entry["original"]`` is read to build the diff and is never copied into the
    card: a BLOCKED unit carries the full original of a file the gate refused —
    the secret or PII the whole pipeline exists to keep in one place — and it
    has no transform to diff against anyway. So a BLOCKED entry gets
    ``diff: None`` whatever its guardrail verdict, and so does a generated unit
    (no original) and any unit whose transform produced nothing.
    """
    transformed = entry.get("transformed")
    transformed = transformed if isinstance(transformed, dict) else {}
    rel = str(entry.get("rel_path") or entry.get("file_path") or "")
    primary = rel if rel in transformed else (sorted(transformed)[0] if transformed else None)
    original = entry.get("original")

    diff: Optional[str] = None
    diff_truncated = False
    if primary is not None and entry.get("status") != "BLOCKED" and isinstance(original, str):
        diff, diff_truncated = unified_diff(original, str(transformed.get(primary) or ""), primary)

    return {
        "kind": "review_file",
        "run": run,
        "file": rel,
        "file_path": entry.get("file_path"),
        "pack": entry.get("pack"),
        "status": entry.get("status"),
        "risk_tier": entry.get("risk_tier"),
        "risk_score": entry.get("risk_score"),
        "risk_reasons": [str(r) for r in (entry.get("risk_reasons") or [])],
        "review_score": entry.get("review_score"),
        "review_verdict": entry.get("review_verdict"),
        "build_verdict": entry.get("build_verdict"),
        "review_feedback": entry.get("review_feedback"),
        "hold_reason": entry.get("hold_reason"),
        # Reduced here too: the card is JSON in the transcript that GET
        # /api/chat serves, and a matched secret has no business there either.
        "guardrail_findings": guardrail_kinds(entry.get("guardrail_findings")),
        "error": entry.get("error"),
        "generate": bool(entry.get("generate")),
        "diff": diff,
        "diff_truncated": diff_truncated,
        "also_transformed": [k for k in sorted(transformed) if k != primary],
        "deleted_files": [str(d) for d in (entry.get("deleted_files") or [])],
    }


def review_more_card(shown: int, total: int) -> dict:
    """The overflow card. It carries no link: there is no router to link to any
    more, and chat.js loads the remaining entries into the card itself."""
    return {"kind": "review_more", "shown": shown, "total": total}


def acceptance_card(pack: str, outcome: Any) -> dict:
    data = outcome.to_json() if hasattr(outcome, "to_json") else outcome
    return {"kind": "acceptance", "pack": pack, **(data if isinstance(data, dict) else {})}


def file_href(output_dir: str, name: str) -> str:
    """The same ``/api/files`` link the wizard builds, built server-side."""
    return "/api/files?" + urlencode({"output_dir": output_dir or "./migrated", "name": name})


def tests_card(totals: dict, dependencies: list, report_href: str) -> dict:
    return {"kind": "tests", "totals": dict(totals or {}),
            "dependencies": [str(d) for d in (dependencies or [])], "report_href": report_href}


def feedback_card(notes: int, packs: list, path: str) -> dict:
    return {"kind": "feedback", "notes": notes, "packs": [str(p) for p in (packs or [])], "path": path}


def evidence_card(discovery: dict) -> dict:
    """The activation table, from the discovery already on the conversation.

    This is what the deleted Discover step drew: every pack the detect rules
    fired on, the evidence line by line, the run order, and each decision with
    the provenance that says whether it came from the prompt, agents.yaml or
    the platform default. It needs no tool of its own — a plan card and an
    evidence card are two readings of one ``service.discover()`` result, and
    re-profiling to draw a table would be a second walk of the repository for
    nothing.

    ``state`` is computed from ``activations`` rather than taken from the
    plan's ``states``, because the table's job is to show packs the plan set
    aside as well as the ones it kept — and for those the plan says nothing.
    """
    discovery = discovery if isinstance(discovery, dict) else {}
    plan = discovery.get("intent") if isinstance(discovery.get("intent"), dict) else None
    selected = {str(p) for p in (plan.get("packs") or [])} if plan else None

    rows: List[Dict[str, Any]] = []
    states: Dict[str, str] = {}
    for a in discovery.get("activations") or []:
        if not isinstance(a, dict):
            continue
        pack = str(a.get("pack") or "")
        state = "runnable" if a.get("runnable") else ("blocked" if a.get("complete") else "detect-only")
        states[pack] = state
        rows.append({
            "pack": pack,
            "state": state,
            # No plan means discovery selected everything it activated.
            "selected": True if selected is None else pack in selected,
            "evidence": [str(e) for e in (a.get("evidence") or [])],
        })

    provenance = dict(plan.get("provenance") or {}) if plan else {}
    decisions = [{"key": str(k), "value": str(v), "from": str(provenance.get(k, "default"))}
                 for k, v in sorted((discovery.get("decisions") or {}).items())]

    order = [{"pack": str(p), "state": states.get(str(p), "unknown"),
              "selected": True if selected is None else str(p) in selected}
             for p in (discovery.get("order") or [])]

    return {
        "kind": "evidence",
        "summary": str(discovery.get("summary") or ""),
        "packs": rows,
        "order": order,
        "decisions": decisions,
        "excluded": [{"pack": x.get("pack"), "reason": str(x.get("reason") or "")}
                     for x in (plan.get("excluded") or []) if isinstance(x, dict)] if plan else [],
        "unsupported": [{"asked": u.get("asked"), "reason": str(u.get("reason") or "")}
                        for u in (plan.get("unsupported") or []) if isinstance(u, dict)] if plan else [],
        "scope": dict(plan.get("scope") or {}) if plan else {},
    }


def artifacts_card(output_dir: str, rows: List[Dict[str, Any]]) -> dict:
    """The files a run left behind, with a link each.

    Names, sizes and links only — the contents stay on disk and reach the
    browser through ``/api/files``, which serves nothing outside the chosen
    output directory. ``manual-review-queue.json`` is downloadable here and is
    still never committed by ``land_on_branch``; a download the user asked for
    and a file copied into their git history are not the same act.
    """
    return {"kind": "artifacts", "output_dir": output_dir,
            "artifacts": [dict(r) for r in rows]}


# A landed commit is the one place a card exists to be read twice: once now,
# and once by whoever finds the branch later.
LAND_FILE_CAP = 50


def land_card(result: dict) -> dict:
    """What was committed, where, and the push command the user runs themselves."""
    result = result if isinstance(result, dict) else {}
    files = [str(f) for f in (result.get("files") or [])]
    return {
        "kind": "land",
        "branch": result.get("branch"),
        "source_dir": result.get("source_dir"),
        "files_changed": result.get("files_changed", 0),
        "deleted": result.get("deleted", 0),
        "commit": result.get("commit"),
        "packs": [str(p) for p in (result.get("packs") or [])],
        "push_command": result.get("push_command"),
        "files": files[:LAND_FILE_CAP],
        "files_truncated": max(0, len(files) - LAND_FILE_CAP),
        "deleted_files": [str(f) for f in (result.get("deleted_files") or [])][:LAND_FILE_CAP],
        "note": "nothing was pushed — the branch is local until you push it",
    }


# `setup_card` was here. It said "go and fill in the Project form", and the form
# is gone — the leader asks for the folder in a sentence now (INCREMENT 2 §A),
# so the card that pointed at `#/project` had nowhere left to point.
