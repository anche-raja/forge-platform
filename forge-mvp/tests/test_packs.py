"""Pack loader: parsing, validation, dependency ordering, and library invariants.

Two kinds of test live here. The first exercises the loader against synthetic
packs written to tmp_path — every rejection the loader makes should have a test
proving it rejects. The second asserts invariants across the *real* library in
``prompts/packs/``, so a pack added later cannot quietly break a rule the engine
depends on.
"""

import re
from pathlib import Path

import pytest

from forge.packs import PackError, load_packs, parse_pack, response_maxima, rubric_weights
from forge.packs.glob import glob_match
from forge.packs.loader import PackRegistry

# ─── helpers ──────────────────────────────────────────────────────────────────

GOOD_REVIEW = """\
Score on 2 checks (total 100).

Check 1 — Something (60 pts):
It did the thing.

Check 2 — Something else (40 pts):
It did the other thing.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"something": <0-60>, "something_else": <0-40>}}
"""

GOOD_TRANSFORM = """\
Do the migration.

Respond ONLY with valid JSON:
{"files": {...}, "deleted_files": [], "manual_flags": []}
"""


def write_pack(directory: Path, pack_id: str, *, frontmatter: str | None = None,
               transform: str = GOOD_TRANSFORM, review: str = GOOD_REVIEW,
               body: str | None = None) -> Path:
    """Write a synthetic pack and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    if frontmatter is None:
        frontmatter = f"""\
id: {pack_id}
version: 1.0.0
title: Test pack {pack_id}
tier: framework
detect:
  any:
    - import_prefix: "com.example"
applies_to:
  - file_glob: "**/*.java"
context: none
depends_on: []
decisions: []
eliminates: []
acceptance: []"""
    if body is None:
        body = f"## transform\n\n{transform}\n\n## review\n\n{review}\n"
    path = directory / f"{pack_id}.pack.md"
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def packs(tmp_path):
    return tmp_path / "packs"


def load_error(directory: Path) -> str:
    with pytest.raises(PackError) as exc:
        load_packs(directory)
    return str(exc.value)


# ─── parsing ──────────────────────────────────────────────────────────────────

def test_a_well_formed_pack_round_trips(packs):
    path = write_pack(packs, "demo-pack")
    spec = parse_pack(path)
    assert spec.id == "demo-pack"
    assert spec.version == "1.0.0"
    assert spec.tier == "framework"
    assert spec.is_complete
    assert spec.transform_prompt.startswith("Do the migration.")
    assert "Check 1" in spec.review_prompt
    assert spec.source_path == path


def test_missing_frontmatter_is_rejected(packs):
    packs.mkdir(parents=True)
    (packs / "x.pack.md").write_text("## transform\nhi\n## review\nho\n", encoding="utf-8")
    assert "no YAML frontmatter" in load_error(packs)


def test_unparseable_frontmatter_names_the_file(packs):
    write_pack(packs, "broken", frontmatter='id: "unterminated\nversion: 1.0.0')
    msg = load_error(packs)
    assert "broken.pack.md" in msg and "not valid YAML" in msg


def test_missing_sections_are_rejected(packs):
    write_pack(packs, "nosections", body="just prose, no sections\n")
    msg = load_error(packs)
    assert "## transform" in msg and "## review" in msg


def test_empty_transform_section_is_rejected(packs):
    write_pack(packs, "hollow", body=f"## transform\n\n## review\n\n{GOOD_REVIEW}\n")
    assert "'## transform' section is empty" in load_error(packs)


def test_duplicate_section_is_rejected(packs):
    write_pack(
        packs, "dupe",
        body=f"## transform\n\n{GOOD_TRANSFORM}\n\n## transform\n\nagain\n\n## review\n\n{GOOD_REVIEW}\n",
    )
    assert "duplicate" in load_error(packs)


# ─── frontmatter validation ───────────────────────────────────────────────────

def _fm(pack_id="p", **overrides):
    base = {
        "id": pack_id, "version": "1.0.0", "title": "T", "tier": "framework",
        "detect": '\n  any:\n    - import_prefix: "com.example"',
        "applies_to": '\n  - file_glob: "**/*.java"',
        "context": "none", "depends_on": "[]", "decisions": "[]",
        "eliminates": "[]", "acceptance": "[]",
    }
    base.update(overrides)
    return "\n".join(f"{k}: {v}" for k, v in base.items())


def test_id_must_match_filename(packs):
    write_pack(packs, "on-disk", frontmatter=_fm("in-frontmatter"))
    msg = load_error(packs)
    assert "does not match filename" in msg


def test_id_must_be_kebab_case(packs):
    write_pack(packs, "Bad_Id", frontmatter=_fm("Bad_Id"))
    assert "kebab-case" in load_error(packs)


def test_version_must_be_semver(packs):
    write_pack(packs, "p", frontmatter=_fm(version="v1"))
    assert "semver" in load_error(packs)


def test_unknown_tier_is_rejected_and_lists_the_valid_ones(packs):
    write_pack(packs, "p", frontmatter=_fm(tier="middleware"))
    msg = load_error(packs)
    assert "unknown tier 'middleware'" in msg and "namespace" in msg


def test_unknown_status_is_rejected(packs):
    write_pack(packs, "p", frontmatter=_fm(status="wip"))
    assert "unknown status 'wip'" in load_error(packs)


def test_required_key_is_named_when_absent(packs):
    fm = "\n".join(l for l in _fm().splitlines() if not l.startswith("context:"))
    write_pack(packs, "p", frontmatter=fm)
    assert "missing required key(s): context" in load_error(packs)


def test_unknown_detect_kind_is_rejected(packs):
    write_pack(packs, "p", frontmatter=_fm(detect='\n  any:\n    - vibes: "legacy"'))
    msg = load_error(packs)
    assert "unknown detect kind 'vibes'" in msg
    assert "import_prefix" in msg, "the error should list what is valid"


def test_detect_rule_payload_type_is_checked(packs):
    write_pack(packs, "p", frontmatter=_fm(detect='\n  any:\n    - dependency:\n        a: b'))
    assert "expects str" in load_error(packs)


def test_empty_detect_is_rejected(packs):
    write_pack(packs, "p", frontmatter=_fm(detect="\n  any: []"))
    assert "non-empty list" in load_error(packs)


def test_invalid_detect_regex_is_rejected(packs):
    write_pack(packs, "p", frontmatter=_fm(detect="\n  any:\n    - content_match: '[unclosed'"))
    assert "not a valid regex" in load_error(packs)


def test_unknown_applies_to_key_is_rejected(packs):
    write_pack(packs, "p", frontmatter=_fm(applies_to='\n  - everything: "yes"'))
    assert "'file_glob' or 'selector'" in load_error(packs)


def test_unknown_acceptance_kind_is_rejected(packs):
    write_pack(packs, "p", frontmatter=_fm(acceptance="\n  - vibe_check: true"))
    assert "unknown acceptance kind 'vibe_check'" in load_error(packs)


def test_invalid_acceptance_regex_is_rejected(packs):
    write_pack(packs, "p", frontmatter=_fm(acceptance="\n  - no_match: '(unclosed'"))
    assert "not a valid regex" in load_error(packs)


def test_acceptance_scope_is_not_mistaken_for_a_kind(packs):
    write_pack(packs, "p", frontmatter=_fm(acceptance="\n  - no_match: 'x'\n    scope: '**/*.java'"))
    spec = parse_pack(packs / "p.pack.md")
    assert spec.acceptance[0].kind == "no_match"
    assert spec.acceptance[0].scope == "**/*.java"


def test_depends_on_as_bare_string_is_rejected(packs):
    """`depends_on: javax-to-jakarta` silently means a list of characters in YAML."""
    write_pack(packs, "p", frontmatter=_fm(depends_on="some-other-pack"))
    assert "must be a list, not a bare string" in load_error(packs)


# ─── rubric consistency ───────────────────────────────────────────────────────

def test_rubric_that_does_not_total_100_is_rejected(packs):
    review = GOOD_REVIEW.replace("(40 pts)", "(35 pts)").replace("<0-40>", "<0-35>")
    write_pack(packs, "p", review=review)
    msg = load_error(packs)
    assert "sums to 95, not 100" in msg
    assert "pass_threshold is meaningless" in msg


def test_rubric_with_no_weights_is_rejected(packs):
    write_pack(packs, "p", review='No weights here.\n{"score": <0-100>, "checks": {"a": <0-100>}}')
    assert "declares no '(N pts)' weights" in load_error(packs)


def test_rubric_with_no_checks_block_is_rejected(packs):
    review = "Check 1 — Only (100 pts):\nyes.\n\n{\"score\": <0-100>, \"verdict\": \"PASS\"}"
    write_pack(packs, "p", review=review)
    assert "no 'checks' block" in load_error(packs)


def test_prose_and_response_schema_must_agree(packs):
    """A check added to the prose but not the JSON makes the reviewer emit a
    different shape than the rubric describes."""
    review = GOOD_REVIEW.replace('"checks": {"something": <0-60>, "something_else": <0-40>}',
                                 '"checks": {"something": <0-50>, "something_else": <0-50>}')
    write_pack(packs, "p", review=review)
    msg = load_error(packs)
    assert "prose and response schema disagree" in msg
    assert "[60, 40]" in msg and "[50, 50]" in msg


def test_detect_only_pack_skips_rubric_validation(packs):
    """A detect-only pack names a technology it cannot migrate; nothing it
    matches ever reaches a reviewer, so it has no rubric to be consistent with."""
    write_pack(packs, "p", frontmatter=_fm(status="detect-only"),
               review="No rubric yet. This pack does not transform anything.")
    registry = load_packs(packs)
    assert not registry["p"].is_complete


# ─── registry: dependencies and ordering ──────────────────────────────────────

def test_unknown_dependency_is_rejected(packs):
    write_pack(packs, "a", frontmatter=_fm("a", depends_on="[ghost]"))
    assert "depends on 'ghost', which does not exist" in load_error(packs)


def test_dependency_cycle_is_rejected_at_load_time(packs):
    write_pack(packs, "a", frontmatter=_fm("a", depends_on="[b]"))
    write_pack(packs, "b", frontmatter=_fm("b", depends_on="[c]"))
    write_pack(packs, "c", frontmatter=_fm("c", depends_on="[a]"))
    msg = load_error(packs)
    assert "cycle" in msg
    assert "a, b, c" in msg


def test_duplicate_ids_are_rejected():
    from forge.packs.spec import PackSpec

    def spec(pid):
        return PackSpec(id=pid, version="1.0.0", title="t", tier="build", status="complete",
                        detect=(), applies_to=(), context="none", depends_on=(), decisions=(),
                        eliminates=(), acceptance=(), transform_prompt="t", review_prompt="r")

    with pytest.raises(PackError, match="duplicate pack id 'same'"):
        PackRegistry([spec("same"), spec("same")])


def test_order_places_every_pack_after_its_dependencies(packs):
    write_pack(packs, "base", frontmatter=_fm("base", tier="build", depends_on="[]"))
    write_pack(packs, "mid", frontmatter=_fm("mid", tier="namespace", depends_on="[base]"))
    write_pack(packs, "leaf", frontmatter=_fm("leaf", tier="view", depends_on="[mid, base]"))
    order = load_packs(packs).order
    assert order.index("base") < order.index("mid") < order.index("leaf")


def test_tier_breaks_ties_deterministically(packs):
    """Two packs that could run in either order must always run in the same one,
    or two runs of the same project produce different plans."""
    write_pack(packs, "zzz-early", frontmatter=_fm("zzz-early", tier="build"))
    write_pack(packs, "aaa-late", frontmatter=_fm("aaa-late", tier="platform"))
    order = load_packs(packs).order
    assert order == ("zzz-early", "aaa-late")


def test_resolve_order_returns_the_subset_in_dependency_order(packs):
    write_pack(packs, "base", frontmatter=_fm("base", tier="build"))
    write_pack(packs, "mid", frontmatter=_fm("mid", tier="namespace", depends_on="[base]"))
    write_pack(packs, "leaf", frontmatter=_fm("leaf", tier="view", depends_on="[mid]"))
    registry = load_packs(packs)
    assert registry.resolve_order(["leaf", "base"]) == ("base", "leaf")


def test_resolve_order_rejects_an_unknown_pack(packs):
    write_pack(packs, "base", frontmatter=_fm("base"))
    registry = load_packs(packs)
    with pytest.raises(PackError, match="Unknown pack 'nope'"):
        registry.resolve_order(["base", "nope"])


def test_depends_on_is_an_ordering_edge_not_a_requirement(packs):
    """The alternatives case: a view pack follows whichever framework pack ran,
    and a project only ever activates one of them."""
    write_pack(packs, "struts-one", frontmatter=_fm("struts-one", tier="framework"))
    write_pack(packs, "struts-two", frontmatter=_fm("struts-two", tier="framework"))
    write_pack(packs, "views", frontmatter=_fm("views", tier="view", depends_on="[struts-one, struts-two]"))
    registry = load_packs(packs)

    # Activating one Struts pack with the view pack is legitimate, not an error.
    assert registry.resolve_order(["views", "struts-two"]) == ("struts-two", "views")
    # ...but the unused edge is still reported for a human to eyeball.
    assert registry.missing_dependencies(["views", "struts-two"]) == {"views": ("struts-one",)}


def test_unknown_pack_lookup_lists_what_exists(packs):
    write_pack(packs, "real-pack", frontmatter=_fm("real-pack"))
    registry = load_packs(packs)
    with pytest.raises(PackError, match="real-pack"):
        registry["imaginary"]


def test_empty_directory_is_an_error(tmp_path):
    (tmp_path / "empty").mkdir()
    assert "No *.pack.md files" in load_error(tmp_path / "empty")


def test_missing_directory_names_the_env_var(tmp_path):
    msg = load_error(tmp_path / "absent")
    assert "FORGE_PACKS_DIR" in msg


# ─── glob semantics ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("pattern,path,expected", [
    ("**/*.java", "Foo.java", True),
    ("**/*.java", "src/main/java/com/corp/Foo.java", True),
    ("**/*.java", "src/main/Foo.kt", False),
    ("*.java", "Foo.java", True),
    ("*.java", "src/Foo.java", False),          # * must not cross a separator
    ("**/WEB-INF/web.xml", "app/src/WEB-INF/web.xml", True),
    ("**/WEB-INF/web.xml", "app/WEB-INF/other.xml", False),
    ("**/struts*.xml", "res/struts-config.xml", True),
    ("**/struts*.xml", "res/sub/struts.xml", True),
    ("**/src/test/java/**/*.java", "mod/src/test/java/a/b/FooTest.java", True),
    ("**/src/test/java/**/*.java", "mod/src/main/java/a/Foo.java", False),
    ("**/*.pack.md", "a.pack.md", True),
    ("?.java", "A.java", True),
    ("?.java", "AB.java", False),
])
def test_glob_semantics(pattern, path, expected):
    assert glob_match(pattern, path) is expected


def test_windows_separators_are_normalised():
    assert glob_match("**/*.java", r"src\main\Foo.java")


# ─── applicability ────────────────────────────────────────────────────────────

def test_includes_matches_glob_selectors(packs):
    spec = parse_pack(write_pack(packs, "p"))
    assert spec.includes("src/main/java/Foo.java")
    assert not spec.includes("src/main/webapp/index.jsp")


def test_named_selectors_are_not_resolvable_by_path(packs):
    """`selector: struts_actions` is answered by the routing table, not by a
    path. A caller that scans the filesystem and trusts includes() would migrate
    zero files, so needs_selectors exists to make that visible."""
    spec = parse_pack(write_pack(packs, "p", frontmatter=_fm(applies_to="\n  - selector: struts_actions")))
    assert spec.selectors == ("struts_actions",)
    assert spec.needs_selectors
    assert spec.globs == ()
    assert not spec.includes("src/main/java/LoginAction.java")


def test_pack_stands_in_for_a_phase_spec(packs):
    """The CLI and file scanner read .name and .description off a PhaseSpec."""
    spec = parse_pack(write_pack(packs, "demo-pack"))
    assert spec.name == "demo-pack"
    assert spec.description == "Test pack demo-pack"


# ─── the real library ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def library():
    return load_packs()


def test_the_shipped_library_loads(library):
    assert len(library) >= 19
    assert library.complete, "the library should ship at least one complete pack"


def test_shipped_library_order_respects_every_dependency(library):
    order = library.order
    for pack in library.values():
        for dep in pack.depends_on:
            assert order.index(dep) < order.index(pack.id), (
                f"{pack.id} is ordered before its dependency {dep}"
            )


def test_shipped_library_order_is_stable_across_loads(library):
    assert load_packs().order == library.order


def test_build_and_namespace_packs_come_before_framework_packs(library):
    """Nothing compiles until the build file targets the right Java level, and
    no framework rewrite is verifiable until the namespace is consistent."""
    order = library.order
    first_framework = min(
        (order.index(p.id) for p in library.values() if p.tier == "framework"), default=None
    )
    assert first_framework is not None
    for pack in library.values():
        if pack.tier in ("build", "namespace"):
            assert order.index(pack.id) < first_framework, f"{pack.id} runs too late"


@pytest.mark.parametrize("pack_id", sorted(load_packs()))
def test_every_shipped_pack_declares_a_context(pack_id, library):
    """A pack needing cross-file facts must say which extractor produces them.
    'none' is a valid answer; silence is not."""
    assert library[pack_id].context


@pytest.mark.parametrize("pack_id", [p.id for p in load_packs().complete])
def test_complete_pack_transform_demands_the_response_contract(pack_id, library):
    transform = library[pack_id].transform_prompt
    assert "valid JSON" in transform
    assert '"files"' in transform
    assert '"manual_flags"' in transform


@pytest.mark.parametrize("pack_id", [p.id for p in load_packs().complete])
def test_complete_pack_rubric_sums_to_100(pack_id, library):
    weights = rubric_weights(library[pack_id].review_prompt)
    assert sum(weights) == 100, f"{pack_id}: {weights}"
    assert response_maxima(library[pack_id].review_prompt) == weights


@pytest.mark.parametrize("pack_id", [p.id for p in load_packs().complete])
def test_packs_that_rewrite_java_state_the_jdk_javax_carve_out(pack_id, library):
    """Telling a model 'zero javax.* allowed' without the JDK carve-out invites
    it to rewrite javax.sql to jakarta.sql and break the build.

    Scoped to packs that rewrite Java sources and name a javax package. A build
    pack migrating the *coordinate* `javax.servlet:javax.servlet-api` is not at
    risk — it never touches an import — so it is deliberately exempt.
    """
    pack = library[pack_id]
    rewrites_java = any(g.endswith(".java") for g in pack.globs) or pack.needs_selectors
    if not rewrites_java:
        return
    if not re.search(r"javax\.[a-z]", pack.transform_prompt):
        return
    assert "javax.sql" in pack.transform_prompt, (
        f"{pack_id} rewrites Java and names javax.* packages, but never states the JDK carve-out"
    )


def test_eliminated_coordinates_are_group_artifact_pairs(library):
    """The build pack removes exactly what other packs list here, so a malformed
    coordinate silently removes nothing."""
    for pack in library.values():
        for coord in pack.eliminates:
            assert coord.count(":") == 1, f"{pack.id}: '{coord}' is not group:artifact"


def test_detect_only_packs_are_registered_rather_than_omitted(library):
    """Silently ignoring a technology that is present produces a migration that
    looks complete and is not."""
    stubs = {p.id for p in library.detect_only}
    assert {"ejb2-to-spring", "jsf-to-faces4"} <= stubs
    for pack in library.detect_only:
        assert pack.detect, f"{pack.id} is detect-only but detects nothing"


# ─── phases.py integration ────────────────────────────────────────────────────

@pytest.fixture
def fresh_registry(monkeypatch):
    """Point FORGE_PACKS_DIR somewhere and drop the module-level cache."""
    from forge import phases

    def use(directory):
        monkeypatch.setenv("FORGE_PACKS_DIR", str(directory))
        phases._packs.cache_clear()
        return phases

    yield use
    phases._packs.cache_clear()


def test_get_phase_resolves_a_pack_by_id():
    from forge.phases import get_phase

    spec = get_phase("javax-to-jakarta")
    assert spec.name == "javax-to-jakarta"
    assert "jakarta" in spec.transform_prompt


def test_get_phase_still_returns_the_builtin_phases():
    from forge.phases import BUILTIN_PHASE_NAMES, get_phase

    assert BUILTIN_PHASE_NAMES == ("java21", "struts-spring6")
    assert "Rule 1 — Namespace migration" in get_phase("java21").transform_prompt


def test_phase_names_covers_builtins_and_packs():
    from forge.phases import PHASE_NAMES

    assert "java21" in PHASE_NAMES
    assert "javax-to-jakarta" in PHASE_NAMES


def test_unknown_phase_error_lists_packs_as_well_as_phases():
    from forge.phases import get_phase

    with pytest.raises(ValueError) as exc:
        get_phase("java99")
    msg = str(exc.value)
    assert "Unknown phase" in msg
    assert "java21" in msg and "javax-to-jakarta" in msg


def test_detect_only_packs_are_not_offered_on_the_command_line():
    """Naming one would start a run that cannot transform anything it matches."""
    from forge.phases import PHASE_NAMES

    assert "ejb2-to-spring" not in PHASE_NAMES


def test_a_broken_pack_library_does_not_break_the_builtin_phases(fresh_registry, tmp_path, caplog):
    """Phase 0 is the deployed pipeline. It must keep running while the pack
    library is mid-edit."""
    broken = tmp_path / "broken"
    write_pack(broken, "bad", frontmatter=_fm("bad", tier="not-a-tier"))
    phases = fresh_registry(broken)

    assert phases.pack_names() == ()
    assert phases.get_phase("java21").name == "java21"
    assert "Pack library did not load" in caplog.text


# ─── scanner integration ──────────────────────────────────────────────────────

def _tree(root: Path, *rel_paths: str) -> Path:
    for rel in rel_paths:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("package com.corp;\n", encoding="utf-8")
    return root


def test_scanner_matches_pack_globs_against_the_full_relative_path(fresh_registry, tmp_path):
    """A PhaseSpec matches on the basename; a pack glob like '**/WEB-INF/web.xml'
    needs the path. Both have to work through one call."""
    packs_dir = tmp_path / "packs"
    write_pack(packs_dir, "webxml-only",
               frontmatter=_fm("webxml-only", applies_to='\n  - file_glob: "**/WEB-INF/web.xml"'))
    fresh_registry(packs_dir)

    from forge.utils.file_scanner import scan_java_files

    src = _tree(tmp_path / "src", "app/WEB-INF/web.xml", "app/WEB-INF/other.xml", "Foo.java")
    found = [Path(f).name for f in scan_java_files(str(src), "webxml-only").files]
    assert found == ["web.xml"]


def test_scanner_refuses_a_pack_that_also_has_globs(fresh_registry, tmp_path):
    """A pack with both globs and selectors is the dangerous case: it would
    migrate the configuration and skip the classes the configuration refers to,
    reporting success over a half-migrated codebase."""
    packs_dir = tmp_path / "packs"
    write_pack(packs_dir, "half-runnable", frontmatter=_fm(
        "half-runnable",
        applies_to='\n  - file_glob: "**/struts*.xml"\n  - selector: struts_actions'))
    fresh_registry(packs_dir)

    from forge.utils.file_scanner import scan_java_files

    src = _tree(tmp_path / "src", "res/struts.xml", "com/corp/LoginAction.java")
    with pytest.raises(ValueError) as exc:
        scan_java_files(str(src), "half-runnable")
    assert "worse than not running at all" in str(exc.value)


def test_scanner_refuses_a_selector_only_pack_instead_of_finding_nothing(fresh_registry, tmp_path):
    """Silently scanning zero files would report a clean run over an untouched
    codebase — the worst available outcome."""
    packs_dir = tmp_path / "packs"
    write_pack(packs_dir, "actions-only",
               frontmatter=_fm("actions-only", applies_to="\n  - selector: struts_actions"))
    fresh_registry(packs_dir)

    from forge.utils.file_scanner import scan_java_files

    src = _tree(tmp_path / "src", "com/corp/LoginAction.java")
    with pytest.raises(ValueError) as exc:
        scan_java_files(str(src), "actions-only")
    assert "struts_actions" in str(exc.value)
    assert "context extractor" in str(exc.value)


def test_builtin_phase_scanning_is_unchanged(tmp_path):
    from forge.utils.file_scanner import scan_java_files

    src = _tree(tmp_path / "src", "com/corp/Foo.java", "res/struts-config.xml", "pom.xml")
    java21 = [Path(f).name for f in scan_java_files(str(src), "java21").files]
    struts = [Path(f).name for f in scan_java_files(str(src), "struts-spring6").files]
    assert java21 == ["Foo.java"]
    assert sorted(struts) == ["Foo.java", "struts-config.xml"]


def test_list_packs_prints_the_library_in_order(capsys):
    import migrate

    assert migrate._list_packs() == 0
    out = capsys.readouterr().out
    assert "19 packs" in out
    assert "javax-to-jakarta" in out
    assert "detect-only" in out
    # Dependency order, not alphabetical.
    assert out.index("javax-to-jakarta") < out.index("struts2-to-springmvc6")


def test_list_packs_reports_a_broken_library_in_full(fresh_registry, tmp_path, capsys):
    broken = tmp_path / "broken"
    write_pack(broken, "bad", frontmatter=_fm("bad", tier="not-a-tier"))
    fresh_registry(broken)

    import migrate

    assert migrate._list_packs() == 1
    assert "unknown tier 'not-a-tier'" in capsys.readouterr().out
