"""Parse, validate and order the stack pack library.

Packs are markdown files with YAML frontmatter, living in ``prompts/packs/``.
Override the directory with the ``FORGE_PACKS_DIR`` environment variable.

The loader is deliberately strict. A pack that loads is a pack the engine can
plan with: its dependencies exist, its rubric is internally consistent, and its
detection rules are of kinds the discovery stage knows how to evaluate. Failing
at startup is the whole point — the alternative is discovering at file 300 of a
migration run that a rubric sums to 95 and the pass threshold has meant nothing
all along.
"""

import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

import yaml

from forge.packs.spec import (
    ACCEPTANCE_KINDS,
    DETECT_KINDS,
    STATUSES,
    TIERS,
    AcceptanceCheck,
    DetectRule,
    PackError,
    PackSpec,
)

# forge/packs/loader.py -> parents[3] == forge-platform/
_DEFAULT_DIR = Path(__file__).resolve().parents[3] / "prompts" / "packs"

_FRONTMATTER = re.compile(r"\A---\n(?P<yaml>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
_SECTION = re.compile(r"^## (transform|review)[ \t]*$", re.MULTILINE)
_ID = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SEMVER = re.compile(r"\A\d+\.\d+\.\d+\Z")
_PROSE_WEIGHT = re.compile(r"\((\d+) pts\)")
_CHECKS_BLOCK = re.compile(r'"checks"\s*:\s*\{(.*?)\}', re.DOTALL)
_JSON_MAX = re.compile(r"<0-(\d+)>")

_REQUIRED = (
    "id", "version", "title", "tier",
    "detect", "applies_to", "context",
    "depends_on", "decisions", "eliminates", "acceptance",
)


def packs_dir() -> Path:
    return Path(os.environ.get("FORGE_PACKS_DIR", str(_DEFAULT_DIR)))


# ─── parsing ──────────────────────────────────────────────────────────────────

def _split(text: str, where: Path) -> Tuple[dict, str, str]:
    """Return (frontmatter, transform_prompt, review_prompt)."""
    m = _FRONTMATTER.match(text)
    if not m:
        raise PackError(f"{where}: no YAML frontmatter — a pack must open with a '---' line")
    try:
        meta = yaml.safe_load(m.group("yaml"))
    except yaml.YAMLError as e:
        raise PackError(f"{where}: frontmatter is not valid YAML: {e}") from None
    if not isinstance(meta, dict):
        raise PackError(f"{where}: frontmatter must be a mapping, got {type(meta).__name__}")

    body = m.group("body")
    parts = _SECTION.split(body)
    # split() yields [preamble, name, text, name, text, ...]
    sections: Dict[str, str] = {}
    for name, chunk in zip(parts[1::2], parts[2::2]):
        if name in sections:
            raise PackError(f"{where}: duplicate '## {name}' section")
        sections[name] = chunk.strip()

    missing = [s for s in ("transform", "review") if s not in sections]
    if missing:
        raise PackError(
            f"{where}: missing section(s) {', '.join('## ' + m for m in missing)}"
        )
    return meta, sections["transform"], sections["review"]


# ─── validation ───────────────────────────────────────────────────────────────

def _str_list(meta: dict, key: str, where: Path) -> Tuple[str, ...]:
    raw = meta.get(key)
    if raw is None:
        return ()
    if isinstance(raw, str):
        raise PackError(f"{where}: '{key}' must be a list, not a bare string")
    if not isinstance(raw, list) or any(not isinstance(v, str) for v in raw):
        raise PackError(f"{where}: '{key}' must be a list of strings")
    return tuple(raw)


def _coordinates(meta: dict, key: str, where: Path, *, parts: int) -> Tuple[str, ...]:
    """A list of Maven coordinates, checked for shape.

    A malformed coordinate does not fail loudly downstream — it simply matches
    no dependency, and the build pack silently leaves the old version in place.
    """
    values = _str_list(meta, key, where)
    shape = "group:artifact" if parts == 2 else "group:artifact:version"
    for coord in values:
        if coord.count(":") != parts - 1:
            raise PackError(f"{where}: '{key}' entry '{coord}' is not {shape}")
    return values


def _detect_rules(meta: dict, where: Path) -> Tuple[DetectRule, ...]:
    detect = meta.get("detect")
    if not isinstance(detect, dict) or "any" not in detect:
        raise PackError(f"{where}: 'detect' must be a mapping with an 'any' list")
    entries = detect["any"]
    if not isinstance(entries, list) or not entries:
        raise PackError(f"{where}: 'detect.any' must be a non-empty list")

    rules: List[DetectRule] = []
    for entry in entries:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise PackError(f"{where}: each detect rule must be a single-key mapping, got {entry!r}")
        (kind, value), = entry.items()
        if kind not in DETECT_KINDS:
            raise PackError(
                f"{where}: unknown detect kind '{kind}'. "
                f"Known: {', '.join(sorted(DETECT_KINDS))}"
            )
        expected = DETECT_KINDS[kind]
        if not isinstance(value, expected):
            raise PackError(
                f"{where}: detect rule '{kind}' expects {expected.__name__}, got {type(value).__name__}"
            )
        if kind == "content_match":
            try:
                re.compile(value)
            except re.error as e:
                raise PackError(f"{where}: detect 'content_match' is not a valid regex: {e}") from None
        rules.append(DetectRule(kind=kind, value=value))
    return tuple(rules)


def _applies_to(meta: dict, where: Path) -> Tuple[Dict[str, str], ...]:
    raw = meta.get("applies_to")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise PackError(f"{where}: 'applies_to' must be a list")
    out: List[Dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise PackError(f"{where}: each applies_to entry must be a single-key mapping, got {entry!r}")
        (key, value), = entry.items()
        if key not in ("file_glob", "selector", "content_match"):
            raise PackError(
                f"{where}: applies_to key must be 'file_glob', 'content_match' or 'selector', got '{key}'"
            )
        if key == "content_match":
            if not isinstance(value, dict) or set(value) != {"glob", "pattern"}:
                raise PackError(
                    f"{where}: applies_to 'content_match' needs exactly {{glob, pattern}}, got {value!r}"
                )
            try:
                re.compile(value["pattern"])
            except re.error as e:
                raise PackError(f"{where}: applies_to 'content_match' pattern is not a valid regex: {e}") from None
        elif not isinstance(value, str) or not value:
            raise PackError(f"{where}: applies_to '{key}' must be a non-empty string")
        out.append({key: value})
    return tuple(out)


def _acceptance(meta: dict, where: Path, declared_decisions: Tuple[str, ...]) -> Tuple[AcceptanceCheck, ...]:
    raw = meta.get("acceptance")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise PackError(f"{where}: 'acceptance' must be a list")
    out: List[AcceptanceCheck] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise PackError(f"{where}: each acceptance check must be a mapping, got {entry!r}")
        scope = entry.get("scope", "")
        when = entry.get("when") or {}
        if not isinstance(when, dict) or any(not isinstance(v, str) for v in when.values()):
            raise PackError(f"{where}: acceptance 'when' must be a mapping of decision to value")
        for key in when:
            if key not in declared_decisions:
                raise PackError(
                    f"{where}: acceptance 'when' names decision '{key}', which the pack does not "
                    f"declare in its 'decisions' list"
                )
        kinds = [k for k in entry if k not in ("scope", "when")]
        if len(kinds) != 1:
            raise PackError(
                f"{where}: each acceptance check needs exactly one kind besides 'scope', got {kinds!r}"
            )
        kind = kinds[0]
        if kind not in ACCEPTANCE_KINDS:
            raise PackError(
                f"{where}: unknown acceptance kind '{kind}'. "
                f"Known: {', '.join(sorted(ACCEPTANCE_KINDS))}"
            )
        value = entry[kind]
        expected = ACCEPTANCE_KINDS[kind]
        if not isinstance(value, expected):
            raise PackError(
                f"{where}: acceptance '{kind}' expects {expected.__name__}, got {type(value).__name__}"
            )
        if kind in ("no_match", "count_unchanged"):
            try:
                re.compile(value)
            except re.error as e:
                raise PackError(f"{where}: acceptance '{kind}' is not a valid regex: {e}") from None
        out.append(AcceptanceCheck(kind=kind, value=value, scope=scope,
                                   when=tuple(sorted(when.items()))))
    return tuple(out)


def _validate_context_contract(context: str, applies_to: Tuple[Dict[str, object], ...], where: Path) -> None:
    """A pack's selectors must be answerable by the context it declares.

    Three cases, in order of how sure we can be:

    1. ``context: none`` with a ``selector:`` is a contradiction — the pack asks
       for a file set only an extractor can name and then says it needs none.
    2. A registered extractor must actually provide every selector the pack
       uses; otherwise the pack references a fact nothing produces.
    3. An unregistered context is tolerated at load time. Several shipped packs
       name extractors that are not built yet, and refusing them here would
       take the whole library down (``phases._packs()`` degrades to none on any
       PackError). The scanner decides runnability; a ratchet test in
       ``tests/test_packs.py`` stops the pending set from growing.
    """
    selectors = [a["selector"] for a in applies_to if "selector" in a]
    if not selectors:
        return
    if context == "none":
        raise PackError(
            f"{where}: selector '{selectors[0]}' needs a context extractor to resolve it, "
            "but 'context' is none"
        )
    # Lazy: forge.extract must not be imported at module load, or the extractors'
    # own imports (forge.utils.*) would race this module during package init.
    from forge.extract import get_extractor

    extractor = get_extractor(context)
    if extractor is None:
        return
    missing = [s for s in selectors if not extractor.provides(s)]
    if missing:
        raise PackError(
            f"{where}: selector '{missing[0]}' is not provided by context '{context}' "
            f"(provides: {', '.join(sorted(extractor.selectors))})"
        )


def rubric_weights(review_prompt: str) -> Tuple[int, ...]:
    """The ``(N pts)`` weights declared in a review rubric, in order."""
    return tuple(int(n) for n in _PROSE_WEIGHT.findall(review_prompt))


def response_maxima(review_prompt: str) -> Tuple[int, ...]:
    """The per-check maxima declared in the rubric's ``"checks"`` JSON, in order."""
    block = _CHECKS_BLOCK.search(review_prompt)
    if not block:
        return ()
    return tuple(int(n) for n in _JSON_MAX.findall(block.group(1)))


def _validate_rubric(review_prompt: str, where: Path) -> None:
    weights = rubric_weights(review_prompt)
    if not weights:
        raise PackError(f"{where}: review rubric declares no '(N pts)' weights")
    total = sum(weights)
    if total != 100:
        raise PackError(
            f"{where}: review rubric sums to {total}, not 100 {list(weights)}. "
            "pass_threshold is meaningless against a rubric that does not total 100."
        )
    maxima = response_maxima(review_prompt)
    if not maxima:
        raise PackError(f"{where}: review rubric declares no 'checks' block in its response schema")
    if maxima != weights:
        raise PackError(
            f"{where}: rubric prose and response schema disagree — "
            f"prose declares {list(weights)}, the checks block declares {list(maxima)}. "
            "A reviewer cannot score against two different rubrics."
        )


def parse_pack(path: Path) -> PackSpec:
    """Read and validate one ``*.pack.md`` file."""
    meta, transform, review = _split(path.read_text(encoding="utf-8"), path)

    missing = [k for k in _REQUIRED if k not in meta]
    if missing:
        raise PackError(f"{path}: frontmatter missing required key(s): {', '.join(missing)}")

    pack_id = meta["id"]
    if not isinstance(pack_id, str) or not _ID.match(pack_id):
        raise PackError(f"{path}: 'id' must be kebab-case, got {pack_id!r}")

    stem = path.name[: -len(".pack.md")] if path.name.endswith(".pack.md") else path.stem
    if stem != pack_id:
        raise PackError(
            f"{path}: id '{pack_id}' does not match filename '{stem}'. "
            "The filename is how a profile pins a pack, so the two must agree."
        )

    version = str(meta["version"])
    if not _SEMVER.match(version):
        raise PackError(f"{path}: 'version' must be semver (e.g. 1.0.0), got {version!r}")

    tier = meta["tier"]
    if tier not in TIERS:
        raise PackError(f"{path}: unknown tier '{tier}'. Known: {', '.join(TIERS)}")

    status = meta.get("status", "complete")
    if status not in STATUSES:
        raise PackError(f"{path}: unknown status '{status}'. Known: {', '.join(STATUSES)}")

    context = meta["context"]
    if not isinstance(context, str) or not context:
        raise PackError(f"{path}: 'context' must be a non-empty string ('none' when none is needed)")

    applies_to = _applies_to(meta, path)
    _validate_context_contract(context, applies_to, path)

    if not transform:
        raise PackError(f"{path}: '## transform' section is empty")
    if not review:
        raise PackError(f"{path}: '## review' section is empty")

    # A detect-only pack names a technology it cannot yet migrate. It has no
    # rubric to be consistent with, because nothing it matches reaches a reviewer.
    if status == "complete":
        _validate_rubric(review, path)

    return PackSpec(
        id=pack_id,
        version=version,
        title=str(meta["title"]),
        tier=tier,
        status=status,
        detect=_detect_rules(meta, path),
        applies_to=applies_to,
        context=context,
        depends_on=_str_list(meta, "depends_on", path),
        decisions=_str_list(meta, "decisions", path),
        eliminates=_str_list(meta, "eliminates", path),
        upgrades=_coordinates(meta, "upgrades", path, parts=3),
        acceptance=_acceptance(meta, path, _str_list(meta, "decisions", path)),
        transform_prompt=transform,
        review_prompt=review,
        source_path=path,
    )


# ─── registry ─────────────────────────────────────────────────────────────────

class PackRegistry(Mapping[str, PackSpec]):
    """The loaded pack library, with dependencies resolved."""

    def __init__(self, packs: Iterable[PackSpec]):
        self._packs: Dict[str, PackSpec] = {}
        for pack in packs:
            if pack.id in self._packs:
                raise PackError(
                    f"duplicate pack id '{pack.id}': "
                    f"{self._packs[pack.id].source_path} and {pack.source_path}"
                )
            self._packs[pack.id] = pack
        self._check_dependencies()
        # Resolve eagerly so a cycle is a load-time failure, not a plan-time one.
        self._order = self._topological(self._packs)

    # Mapping protocol
    def __getitem__(self, key: str) -> PackSpec:
        try:
            return self._packs[key]
        except KeyError:
            raise PackError(
                f"Unknown pack '{key}'. Available: {', '.join(sorted(self._packs))}"
            ) from None

    def __contains__(self, key: object) -> bool:
        # Mapping's default __contains__ calls __getitem__ and catches KeyError.
        # Ours raises PackError, so membership has to be answered directly.
        return key in self._packs

    def __iter__(self):
        return iter(self._packs)

    def __len__(self) -> int:
        return len(self._packs)

    # ── ordering ─────────────────────────────────────────────────────────────

    def _check_dependencies(self) -> None:
        for pack in self._packs.values():
            for dep in pack.depends_on:
                if dep not in self._packs:
                    raise PackError(
                        f"{pack.source_path}: pack '{pack.id}' depends on '{dep}', which does not exist"
                    )

    @staticmethod
    def _topological(packs: Mapping[str, PackSpec]) -> Tuple[str, ...]:
        """Dependency order, with tier then id as a deterministic tie-break.

        Two packs that could run in either order must always run in the *same*
        order, or two runs of the same project produce different plans.
        """
        indegree = {pid: 0 for pid in packs}
        dependents: Dict[str, List[str]] = {pid: [] for pid in packs}
        for pid, pack in packs.items():
            for dep in pack.depends_on:
                indegree[pid] += 1
                dependents[dep].append(pid)

        def sort_key(pid: str) -> Tuple[int, str]:
            return (packs[pid].tier_index, pid)

        ready = sorted((p for p, d in indegree.items() if d == 0), key=sort_key)
        order: List[str] = []
        while ready:
            pid = ready.pop(0)
            order.append(pid)
            for child in dependents[pid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort(key=sort_key)

        if len(order) != len(packs):
            stuck = sorted(p for p in packs if p not in order)
            raise PackError(
                "pack dependencies contain a cycle among: " + ", ".join(stuck)
            )
        return tuple(order)

    @property
    def order(self) -> Tuple[str, ...]:
        """Every pack id in dependency order."""
        return self._order

    def resolve_order(self, ids: Sequence[str]) -> Tuple[str, ...]:
        """`ids` in dependency order, rejecting unknown ids.

        ``depends_on`` is an **ordering edge, not a requirement**: it constrains
        the order of two packs when both are active and says nothing when only
        one is. That distinction is load-bearing — ``jsp-jstl-modernize``
        declares both Struts packs because it must follow whichever one runs,
        but a Struts 2 project will never activate the Struts 1 pack.

        So dependencies outside `ids` are neither pulled in nor treated as an
        error. Use :meth:`missing_dependencies` to report the ones worth a human
        look.
        """
        wanted = set(ids)
        for pid in wanted:
            self[pid]  # raises PackError on unknown
        return tuple(pid for pid in self._order if pid in wanted)

    def missing_dependencies(self, ids: Sequence[str]) -> Dict[str, Tuple[str, ...]]:
        """Ordering edges from `ids` that point outside `ids`. Advisory.

        Most entries are benign — the unused half of an either/or, like the
        Struts 1 pack on a Struts 2 project. It is the *unexpected* entry that
        matters: a project activating ``struts2-to-springmvc6`` with no
        ``javax-to-jakarta`` is a profile someone hand-edited wrongly, and the
        plan should say so rather than migrate into a broken namespace.
        """
        wanted = set(ids)
        gaps = {}
        for pid in sorted(wanted):
            absent = tuple(d for d in self[pid].depends_on if d not in wanted)
            if absent:
                gaps[pid] = absent
        return gaps

    # ── selection ────────────────────────────────────────────────────────────

    def by_tier(self, tier: str) -> Tuple[PackSpec, ...]:
        return tuple(p for p in self._packs.values() if p.tier == tier)

    @property
    def complete(self) -> Tuple[PackSpec, ...]:
        return tuple(self._packs[p] for p in self._order if self._packs[p].is_complete)

    @property
    def detect_only(self) -> Tuple[PackSpec, ...]:
        return tuple(self._packs[p] for p in self._order if not self._packs[p].is_complete)


def load_packs(directory: str | Path | None = None) -> PackRegistry:
    """Load every ``*.pack.md`` under `directory` (default: ``prompts/packs/``)."""
    path = Path(directory) if directory is not None else packs_dir()
    if not path.is_dir():
        raise PackError(
            f"Pack directory not found: {path}. "
            "Set FORGE_PACKS_DIR, or add packs under prompts/packs/."
        )
    files = sorted(path.glob("*.pack.md"))
    if not files:
        raise PackError(f"No *.pack.md files in {path}")
    return PackRegistry(parse_pack(f) for f in files)
