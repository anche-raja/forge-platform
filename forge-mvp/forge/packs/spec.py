"""The pack data model.

A pack is one technology transition — Struts 2 to Spring MVC, javax to jakarta,
JUnit 4 to JUnit 5. It pairs the rules a transform model is given with the rubric
that grades the result, plus enough declarative metadata for the engine to decide
*whether* it applies to a project and *when* it runs relative to everything else.

The contract lives in ``prompts/FORGE-Platform-Requirements.md``; this module is
that contract expressed as types.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

# Ordering fallback when two packs are both ready to run. Nothing compiles until
# the build file targets the right Java level, and no framework rewrite is
# verifiable until the namespace is consistent — hence build and namespace early,
# platform packaging last.
TIERS: Tuple[str, ...] = (
    "build",
    "language",
    "namespace",
    "persistence",
    "framework",
    "view",
    "test",
    "platform",
)

STATUSES: Tuple[str, ...] = ("complete", "detect-only")

# Every detection rule must be decidable without a model — coordinates, globs,
# import prefixes, descriptor elements. Detection is evidence, not opinion.
# The value is the YAML type the rule's payload must have.
DETECT_KINDS: Mapping[str, type] = {
    "dependency": str,          # "group:artifact", artifact may be "*"
    "dependency_lt": dict,      # {coord: "group:artifact", value: "6.0.0"}
    "file_glob": str,
    "import_prefix": str,
    "content_match": str,       # regex
    "property_lt": dict,        # {name: "maven.compiler.source", value: "21"}
    "gradle_property_lt": dict,
    "xml_element": str,         # "namespace:element"
    "decision_equals": dict,    # {key: "container", value: "liberty"}
}

# Acceptance checks decide whether the *project* migrated, independently of how
# individual files scored. Scalar kinds take `true`; the rest take a payload.
ACCEPTANCE_KINDS: Mapping[str, type] = {
    "no_match": str,            # regex that must not match within `scope`
    "count_unchanged": str,     # match count must equal the pre-migration count
    "routing_parity": bool,
    "test_parity": bool,
    "authz_parity": bool,
    "build": str,               # command that must succeed
}


class PackError(Exception):
    """A pack is malformed, or the library as a whole does not resolve."""


@dataclass(frozen=True)
class DetectRule:
    kind: str
    value: Any

    def __str__(self) -> str:
        return f"{self.kind}={self.value!r}"


@dataclass(frozen=True)
class AcceptanceCheck:
    kind: str
    value: Any
    scope: str = ""


@dataclass(frozen=True)
class PackSpec:
    """One technology transition.

    Exposes ``name``/``description``/``includes`` so a pack can stand in for a
    Phase 0 ``PhaseSpec`` wherever the CLI and scanner expect one.
    """

    id: str
    version: str
    title: str
    tier: str
    status: str
    detect: Tuple[DetectRule, ...]
    applies_to: Tuple[Dict[str, str], ...]
    context: str
    depends_on: Tuple[str, ...]
    decisions: Tuple[str, ...]
    eliminates: Tuple[str, ...]
    acceptance: Tuple[AcceptanceCheck, ...]
    transform_prompt: str
    review_prompt: str
    source_path: Path = field(compare=False, default=Path())

    # ── PhaseSpec compatibility ──────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self.id

    @property
    def description(self) -> str:
        return self.title

    # ── Applicability ────────────────────────────────────────────────────────

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    @property
    def globs(self) -> Tuple[str, ...]:
        return tuple(a["file_glob"] for a in self.applies_to if "file_glob" in a)

    @property
    def selectors(self) -> Tuple[str, ...]:
        """Named file sets that only a context extractor can resolve.

        ``struts_actions`` is not a glob — which files are Struts actions is
        known from the routing table, not from a path. The planner resolves
        these; :meth:`includes` cannot and does not try.
        """
        return tuple(a["selector"] for a in self.applies_to if "selector" in a)

    @property
    def needs_selectors(self) -> bool:
        return bool(self.selectors)

    def includes(self, path: str) -> bool:
        """Whether `path` is matched by this pack's **glob** selectors.

        A pack whose ``applies_to`` is entirely named selectors matches nothing
        here. That is not "no files" — it is "ask the extractor". Callers that
        scan the filesystem directly must check :attr:`needs_selectors` first,
        or they will silently migrate zero files.
        """
        from forge.packs.glob import glob_match

        rel = str(path).replace("\\", "/")
        return any(glob_match(g, rel) for g in self.globs)

    @property
    def tier_index(self) -> int:
        return TIERS.index(self.tier)
