"""Which packs apply to a profile, and on what evidence.

Every ``detect`` rule kind the pack contract allows is evaluated here. A pack
fires when any rule matches; the evidence names the matching item — the
coordinate and version, the file, the import, the property — so a reader can
see *why* a pack was selected, not just that it was.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from forge.discover.profile import Profile, normalize_java_level
from forge.packs.glob import glob_match
from forge.packs.spec import DetectRule, PackSpec

_EVIDENCE_CAP = 6


@dataclass
class Activation:
    pack_id: str
    complete: bool
    evidence: List[str] = field(default_factory=list)
    runnable: Optional[bool] = None   # filled by the caller from the scanner


def version_lt(a: str, b: str) -> Optional[bool]:
    """``a < b`` for dotted versions; None when ``a`` is not comparable (unresolved)."""
    if not a:
        return None
    def key(v: str) -> List[Tuple[int, object]]:
        out: List[Tuple[int, object]] = []
        for tok in re.split(r"[.\-_+]", v):
            m = re.match(r"^(\d+)(.*)$", tok)
            if m:
                out.append((0, int(m.group(1))))
                if m.group(2):
                    out.append((1, m.group(2).lower()))
            elif tok:
                out.append((1, tok.lower()))
        return out
    ka, kb = key(a), key(b)
    # Pad so 6.8 and 6.8.0 compare equal.
    while len(ka) < len(kb):
        ka.append((0, 0))
    while len(kb) < len(ka):
        kb.append((0, 0))
    for x, y in zip(ka, kb):
        if x == y:
            continue
        if x[0] != y[0]:
            return x[0] == 1   # a qualifier (RC, SNAPSHOT) sorts before the bare number
        return x[1] < y[1]
    return False


def _match_rule(rule: DetectRule, profile: Profile) -> List[str]:
    kind, value = rule.kind, rule.value
    if kind == "dependency":
        group, _, artifact = str(value).partition(":")
        return [f"dependency {d.coord}:{d.version or d.raw_version or '?'} ({d.pom})"
                for d in profile.has_dependency(group, artifact)][:_EVIDENCE_CAP]

    if kind == "dependency_lt":
        group, _, artifact = str(value["coord"]).partition(":")
        out = []
        for d in profile.has_dependency(group, artifact):
            lt = version_lt(d.version, str(value["value"]))
            if lt:
                out.append(f"dependency {d.coord}:{d.version} < {value['value']} ({d.pom})")
            elif lt is None and d.raw_version:
                out.append(f"dependency {d.coord}:{d.raw_version} (unresolved version; assumed older than {value['value']}) ({d.pom})")
        return out[:_EVIDENCE_CAP]

    if kind == "file_glob":
        return [f"file {p}" for p in profile.files if glob_match(str(value), p)][:_EVIDENCE_CAP]

    if kind == "import_prefix":
        prefix = str(value)
        return [f"import {i}" for i in profile.imports
                if i == prefix or i.startswith(prefix + ".") or i.startswith(prefix)][:_EVIDENCE_CAP]

    if kind == "content_match":
        return [f"content {p}" for p in profile.content_hits.get(str(value), [])][:_EVIDENCE_CAP]

    if kind in ("property_lt", "gradle_property_lt"):
        name, threshold = str(value["name"]), str(value["value"])
        source = profile.gradle_properties if kind == "gradle_property_lt" else profile.properties
        raw = source.get(name)
        if raw is None:
            return []
        have, want = normalize_java_level(raw), normalize_java_level(threshold)
        if have is not None and want is not None:
            return [f"property {name}={raw} (Java {have} < {want})"] if have < want else []
        return [f"property {name}={raw} < {threshold}"] if version_lt(raw, threshold) else []

    if kind == "xml_element":
        wanted = str(value)
        if ":" in wanted:
            return [f"xml element {wanted}"] if wanted in profile.xml_elements else []
        return [f"xml element {e}" for e in profile.xml_elements if e.endswith(":" + wanted)][:_EVIDENCE_CAP]

    if kind == "decision_equals":
        key, expected = str(value["key"]), str(value["value"])
        actual = profile.decisions.get(key)
        return [f"decision {key}={actual}"] if actual == expected else []

    return []


def resolve_packs(profile: Profile, packs: Sequence[PackSpec]) -> List[Activation]:
    """Activations for every pack whose ``detect.any`` fires, in the order given.

    ``decision_equals`` rules are gates, not evidence: a decision says what the
    target is, not what the repository contains, so it can never activate a
    pack on its own. Every decision rule a pack declares must hold, and at least
    one repository rule must fire. Otherwise an empty directory would "need"
    the Liberty pack because the config says the container is Liberty.
    """
    out: List[Activation] = []
    for pack in packs:
        gates = [r for r in pack.detect if r.kind == "decision_equals"]
        if gates and not all(_match_rule(r, profile) for r in gates):
            continue
        evidence: List[str] = []
        for rule in pack.detect:
            if rule.kind != "decision_equals":
                evidence.extend(_match_rule(rule, profile))
        if evidence:
            evidence.extend(e for r in gates for e in _match_rule(r, profile))
            seen: Dict[str, None] = {}
            for e in evidence:
                seen.setdefault(e, None)
            out.append(Activation(pack_id=pack.id, complete=pack.is_complete, evidence=list(seen)[:_EVIDENCE_CAP * 2]))
    return out


def content_patterns(packs: Sequence[PackSpec]) -> List[str]:
    """Every ``content_match`` pattern the library asks for, so the walk evaluates them once."""
    return sorted({str(r.value) for p in packs for r in p.detect if r.kind == "content_match"})
