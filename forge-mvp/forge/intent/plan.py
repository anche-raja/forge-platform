"""The resolved plan: what will run, what will not, and why for both."""

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Tuple


@dataclass
class IntentPlan:
    """A pack selection with every choice attributed.

    ``provenance`` is what makes a wrong answer debuggable — the objection the
    "no model-driven leader" rule actually raises. Every decision says whether
    it came from the prompt, from ``agents.yaml``, or from the platform default.
    """

    intent: str = ""
    packs: Tuple[str, ...] = ()                      # final selection, dependency order
    states: Dict[str, str] = field(default_factory=dict)    # pack -> runnable|blocked|detect-only
    excluded: List[dict] = field(default_factory=list)      # {pack, reason, evidence}
    unsupported: List[dict] = field(default_factory=list)   # {asked, reason}
    decisions: Dict[str, str] = field(default_factory=dict)
    provenance: Dict[str, str] = field(default_factory=dict)  # key -> prompt|config|default
    rejected: List[dict] = field(default_factory=list)      # {key, value, reason}
    scope: Dict[str, object] = field(default_factory=lambda: {"package_prefix": "", "exclude_globs": []})
    assumptions: List[str] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)
    gaps: Dict[str, List[str]] = field(default_factory=dict)  # pack -> ordering deps outside the set
    bedrock_calls: int = 0
    cost_usd: float = 0.0

    def to_json(self) -> dict:
        return {
            "intent": self.intent,
            "packs": list(self.packs),
            "states": self.states,
            "excluded": self.excluded,
            "unsupported": self.unsupported,
            "decisions": self.decisions,
            "provenance": self.provenance,
            "rejected": self.rejected,
            "scope": self.scope,
            "assumptions": self.assumptions,
            "questions": self.questions,
            "gaps": {k: list(v) for k, v in self.gaps.items()},
            "bedrock_calls": self.bedrock_calls,
            "cost_usd": round(self.cost_usd, 6),
        }

    def render(self) -> str:
        """What the CLI prints under the discovery summary."""
        lines = [f"Intent: {self.intent}", ""]
        lines.append(f"  {len(self.packs)} pack(s) selected:")
        for i, pid in enumerate(self.packs, 1):
            state = self.states.get(pid, "runnable")
            marker = " " if state == "runnable" else "*"
            lines.append(f"  {i:2}.{marker} {pid:<28} {state}")
        for e in self.excluded:
            lines.append(f"    -  {e['pack']:<28} excluded: {e['reason']}")
        for u in self.unsupported:
            lines.append(f"    ?  {u['asked']:<28} not available: {u['reason']}")

        lines += ["", "  decisions:"]
        for k, v in self.decisions.items():
            lines.append(f"    {k:<22} {v:<26} [{self.provenance.get(k, 'default')}]")
        for r in self.rejected:
            lines.append(f"    ! {r['key']}={r['value']} rejected: {r['reason']}")

        scope_globs = self.scope.get("exclude_globs") or []
        prefix = self.scope.get("package_prefix") or ""
        if scope_globs or prefix:
            lines += ["", "  scope:"]
            if prefix:
                lines.append(f"    package_prefix  {prefix}")
            if scope_globs:
                lines.append(f"    exclude_globs   {', '.join(scope_globs)}")

        if self.assumptions:
            lines += ["", "  assumed (not stated in the intent):"]
            lines += [f"    - {a}" for a in self.assumptions]
        if self.questions:
            lines += ["", "  worth confirming:"]
            lines += [f"    - {q}" for q in self.questions]
        if self.gaps:
            lines += ["", "  ordering gaps (a dependency of a selected pack is not selected):"]
            lines += [f"    - {p} expects {', '.join(d)}" for p, d in sorted(self.gaps.items())]
        return "\n".join(lines)


def decisions_with_provenance(
    defaults: Mapping[str, str],
    from_config: Mapping[str, str],
    from_prompt: Mapping[str, str],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Merge the three sources in precedence order, recording where each won."""
    decisions: Dict[str, str] = {}
    provenance: Dict[str, str] = {}
    for source, values in (("default", defaults), ("config", from_config), ("prompt", from_prompt)):
        for key, value in values.items():
            decisions[key] = value
            provenance[key] = source
    return decisions, provenance
