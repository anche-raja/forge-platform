"""Reconcile a model's proposal against what the repository actually contains.

The model proposes; this disposes. Everything here is pure and deterministic —
same proposal plus same activations gives the same plan, every time — which is
what keeps an intent-driven run reproducible.

The eight rules below are the safety contract. Each one has a test.
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence

from forge.intent.plan import IntentPlan, decisions_with_provenance
from forge.intent.vocabulary import (
    DECISION_OPTIONS,
    WEB_FRAMEWORK_ROUTES,
    route_governed_packs,
)
from forge.packs.glob import glob_match
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

_GLOB_PROBE = "probe/Example.java"


def _str_list(value: Any) -> List[str]:
    """Defensive coercion, house style: a model's list may be anything."""
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _reason_map(value: Any, key: str) -> Dict[str, str]:
    """``[{pack, reason}]`` -> ``{pack: reason}``, skipping malformed entries."""
    out: Dict[str, str] = {}
    if not isinstance(value, list):
        return out
    for entry in value:
        if isinstance(entry, dict) and isinstance(entry.get(key), str):
            out[entry[key].strip()] = str(entry.get("reason") or "").strip()
        elif isinstance(entry, str) and entry.strip():
            out[entry.strip()] = ""
    return out


def _validate_decisions(raw: Any) -> tuple:
    """Rule 3 — a decision must be a known key with a value from its enum."""
    accepted: Dict[str, str] = {}
    rejected: List[dict] = []
    if not isinstance(raw, dict):
        return accepted, rejected
    for key, value in raw.items():
        key, value = str(key), str(value)
        options = DECISION_OPTIONS.get(key)
        if options is None:
            rejected.append({"key": key, "value": value, "reason": "unknown decision key"})
        elif value not in options:
            rejected.append({"key": key, "value": value,
                             "reason": f"not one of: {', '.join(options)}"})
        else:
            accepted[key] = value
    return accepted, rejected


def _validate_scope(raw: Any, rejected: List[dict]) -> Dict[str, object]:
    """Scope may only ever shrink the unit set, so it needs no ceiling —
    only a syntax check, against the same matcher ``file_glob`` rules use."""
    scope: Dict[str, object] = {"package_prefix": "", "exclude_globs": []}
    if not isinstance(raw, dict):
        return scope

    prefix = raw.get("package_prefix")
    if isinstance(prefix, str):
        scope["package_prefix"] = prefix.strip()

    globs: List[str] = []
    for pattern in _str_list(raw.get("exclude_globs")):
        try:
            glob_match(pattern, _GLOB_PROBE)
        except Exception as e:  # noqa: BLE001 — a bad pattern is the model's error, not ours
            rejected.append({"key": "scope.exclude_globs", "value": pattern,
                             "reason": f"not a valid glob: {e}"})
            continue
        globs.append(pattern)
    scope["exclude_globs"] = globs
    return scope


def reconcile(
    proposal: Optional[Mapping[str, Any]],
    activations: Sequence[Mapping[str, Any]],
    registry,
    *,
    defaults: Mapping[str, str],
    config_decisions: Optional[Mapping[str, str]] = None,
    intent: str = "",
) -> IntentPlan:
    """A checked plan from an unchecked proposal.

    ``activations`` is discovery's output — the evidence-based candidate set.
    A proposal of ``None`` (no intent, or a response that would not parse) gives
    the plan discovery would have produced on its own, so every caller can treat
    the result uniformly.
    """
    proposal = proposal or {}
    by_id = {str(a["pack"]): a for a in activations}
    activated = set(by_id)

    # ── rule 3: closed vocabulary ────────────────────────────────────────────
    from_prompt, rejected = _validate_decisions(proposal.get("decisions"))
    decisions, provenance = decisions_with_provenance(defaults, config_decisions or {}, from_prompt)

    scope = _validate_scope(proposal.get("scope"), rejected)

    # ── rules 1 & 2: intent narrows; nothing is silently dropped ─────────────
    include = set(_str_list(proposal.get("include")))
    exclude_reasons = _reason_map(proposal.get("exclude"), "pack")

    unsupported = [
        {"asked": str(u.get("asked") or ""), "reason": str(u.get("reason") or "")}
        for u in (proposal.get("unsupported") or [])
        if isinstance(u, dict) and str(u.get("asked") or "").strip()
    ]
    # Rule 1 — a pack the repository shows no evidence for can never be selected.
    for pid in sorted(include - activated):
        unsupported.append({"asked": pid, "reason": "no detection evidence in this repository"})
        _log.debug("intent asked for %s, which discovery did not activate", pid)
    include &= activated

    selected = (include if include else activated) - set(exclude_reasons)

    # ── rule 4: mutually exclusive routes ────────────────────────────────────
    route = decisions.get("web_framework")
    route_dropped = set()
    if route in WEB_FRAMEWORK_ROUTES:
        allowed = WEB_FRAMEWORK_ROUTES[route]
        for pid in sorted((selected & route_governed_packs()) - allowed):
            selected.discard(pid)
            route_dropped.add(pid)
            exclude_reasons.setdefault(pid, f"web_framework={route} takes the other route")

    # ── rule 2 again: every dropped candidate keeps its evidence ─────────────
    excluded = [
        {
            "pack": pid,
            "reason": exclude_reasons.get(pid) or "not selected by intent",
            "evidence": list(by_id[pid].get("evidence") or []),
        }
        for pid in sorted(activated - selected)
    ]

    # ── rule 7: the order is never the model's ───────────────────────────────
    order = registry.resolve_order(sorted(selected))

    # ── rule 6: coherence, via the existing (previously unused) helper ───────
    # Narrowed to dependencies that *were* available and got dropped anyway. An
    # edge pointing at a pack the repository never had is the benign either/or
    # case `missing_dependencies` warns about, and so is the losing half of the
    # route decision — `jsp-jstl-modernize` names both Struts packs precisely
    # because it must follow whichever one runs.
    gaps = {
        pid: [d for d in deps if d in activated and d not in route_dropped]
        for pid, deps in registry.missing_dependencies(sorted(selected)).items()
    }
    gaps = {pid: deps for pid, deps in gaps.items() if deps}

    # ── rule 5: state labels survive selection ───────────────────────────────
    states = {}
    for pid in order:
        a = by_id[pid]
        states[pid] = "runnable" if a.get("runnable") else ("blocked" if a.get("complete") else "detect-only")

    # Which unstated decisions actually matter for the packs about to run.
    assumptions = _str_list(proposal.get("assumptions"))
    read_by_selected = set()
    for pid in order:
        read_by_selected |= set(registry[pid].decisions)
    for key in sorted(read_by_selected):
        if key not in decisions:
            # DEFAULT_DECISIONS is missing two keys the spec table lists
            # (liberty_edition, liberty_features). A pack reading a decision
            # that has no value anywhere is worth more noise than an assumed
            # default, not less — it is the one a reader cannot even see.
            assumptions.append(
                f"{key} is read by a selected pack but has no value set anywhere "
                f"(options: {', '.join(DECISION_OPTIONS.get(key, ['?']))})")
        elif provenance.get(key) == "default":
            assumptions.append(f"{key} not stated; using platform default '{decisions[key]}'")

    return IntentPlan(
        intent=intent,
        packs=order,
        states=states,
        excluded=excluded,
        unsupported=unsupported,
        decisions=decisions,
        provenance=provenance,
        rejected=rejected,
        scope=scope,
        assumptions=assumptions,
        questions=_str_list(proposal.get("questions")),
        gaps=gaps,
    )
