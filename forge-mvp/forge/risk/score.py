"""Score how risky it is to let a unit's migration land without a human looking.

Deterministic and additive: the same file always gets the same score and the
same reasons in the same order, so a held unit can say exactly why it was held.
The markers are the ones the packs' own rubrics call out as the places a
migration goes wrong silently — a Spring-proxied Struts action renders blank
under Struts 7, a security constraint dropped from web.xml opens a path, an
OGNL expression guessed into EL shows nothing and throws nothing.

The score decides a tier; ``risk_ceiling`` decides what the tier means.
"""

import re
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

DEFAULT_THRESHOLDS: Mapping[str, int] = {"high_at": 60, "medium_at": 30}

_LOC_BANDS = ((1000, 40), (600, 30), (300, 20), (100, 10))
_PROXY_ANNOTATIONS = re.compile(r"@(Transactional|Secured|PreAuthorize|Async|Cacheable)\b")
_SECURITY_CONFIG = re.compile(
    r"WebSecurityConfigurerAdapter|authorizeRequests|authorizeHttpRequests|SecurityFilterChain|@EnableWebSecurity"
)
_MODEL_DRIVEN = re.compile(r"\bModelDriven\b")
_UNSAFE = re.compile(r"\bsun\.misc\.Unsafe\b")
_THREAD_STOP = re.compile(r"\bThread\.stop\b|\.stop\(\)\s*;")
_OGNL = re.compile(r"%\{|#session\b|#request\b|#application\b|#attr\b")
_WEB_XML_FANOUT = re.compile(r"<(filter|servlet|listener)>")
_STRUTS_ACTIONS = re.compile(r"<action\b")
_SECURITY_CONSTRAINT = re.compile(r"<(security-constraint|login-config)[\s/>]")
_DESCRIPTOR_NAMES = re.compile(r"^(web\.xml|struts.*\.xml|struts-config.*\.xml|validation\.xml)$")


def thresholds_from(config) -> Mapping[str, int]:
    """``risk: {high_at, medium_at}`` from agents.yaml, merged over the defaults."""
    raw = (config.get("risk") if config is not None else None) or {}
    out = dict(DEFAULT_THRESHOLDS)
    for key in ("high_at", "medium_at"):
        try:
            out[key] = int(raw.get(key, out[key]))
        except (TypeError, ValueError):
            pass
    return out


def tier_for(score: int, thresholds: Mapping[str, int] = DEFAULT_THRESHOLDS) -> str:
    if score >= thresholds["high_at"]:
        return "HIGH"
    if score >= thresholds["medium_at"]:
        return "MEDIUM"
    return "LOW"


def score_unit(
    file_path: str,
    content: str,
    spec=None,
    ctx_summary: Optional[dict] = None,
    *,
    generate: bool = False,
    thresholds: Mapping[str, int] = DEFAULT_THRESHOLDS,
) -> Tuple[int, str, List[str]]:
    """``(score 0–100, tier, reasons)`` for one unit.

    ``spec`` is the pack (or phase) migrating it; its ``high_risk_matchers``
    (``content_match`` entries declared ``risk: high``) mark files the pack
    singled out as dangerous — a security config, say — and a hit there is
    HIGH by rule. A plain ``content_match`` only selects relevant files and
    adds nothing to the score. ``ctx_summary`` may carry an extracted
    ``filter_chain`` to size a descriptor's fan-out without re-parsing it.
    """
    name = Path(file_path).name
    suffix = Path(file_path).suffix.lower()
    score = 0
    reasons: List[str] = []
    force_high = False

    if generate:
        force_high = True
        reasons.append("generated unit: no source to diff against")

    loc = content.count("\n")
    for at_least, points in _LOC_BANDS:
        if loc >= at_least:
            score += points
            reasons.append(f"{loc} lines")
            break

    if _DESCRIPTOR_NAMES.match(name):
        if ctx_summary and isinstance(ctx_summary.get("filter_chain"), list):
            fanout = len(ctx_summary["filter_chain"])
        elif name == "web.xml":
            fanout = len(_WEB_XML_FANOUT.findall(content))
        else:
            fanout = len(_STRUTS_ACTIONS.findall(content))
        if fanout > 10:
            score += 30
        elif fanout > 3:
            score += 15
        elif fanout > 0:
            score += 5
        if fanout:
            reasons.append(f"descriptor fan-out: {fanout} entries")
        if name == "web.xml" and _SECURITY_CONSTRAINT.search(content):
            score += 25
            reasons.append("authorization rules in descriptor (authz_parity)")

    if suffix == ".java":
        if _PROXY_ANNOTATIONS.search(content):
            score += 25
            reasons.append("Spring-proxied class: OGNL cannot traverse proxies under Struts 7")
        if _MODEL_DRIVEN.search(content):
            score += 20
            reasons.append("ModelDriven: per-request model becomes @ModelAttribute binding")
        if _SECURITY_CONFIG.search(content):
            force_high = True
            reasons.append("security configuration: HIGH by rule")
        if _UNSAFE.search(content):
            score += 30
            reasons.append("sun.misc.Unsafe: strong encapsulation on Java 21")
        if _THREAD_STOP.search(content):
            score += 20
            reasons.append("Thread.stop: throws UnsupportedOperationException on Java 21")

    if suffix in (".jsp", ".jspf", ".tag", ".tagf"):
        ognl = len(_OGNL.findall(content))
        if ognl >= 10:
            score += 30
        elif ognl >= 3:
            score += 15
        elif ognl >= 1:
            score += 5
        if ognl:
            reasons.append(f"OGNL expressions: {ognl}")

    matchers = getattr(spec, "high_risk_matchers", ()) or ()
    if matchers and suffix == ".java":
        for _glob, pattern in matchers:
            try:
                if re.search(pattern, content):
                    force_high = True
                    reasons.append("matched the pack's content selector: HIGH by rule")
                    break
            except re.error:
                continue

    score = min(score, 100)
    if force_high:
        score = max(score, int(thresholds["high_at"]))
    return score, tier_for(score, thresholds), reasons
