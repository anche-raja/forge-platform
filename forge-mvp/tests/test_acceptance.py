"""The acceptance runner: mechanical, project-level checks over the merged
post-migration view. Every outcome is pass, fail with evidence, or skip with
the reason — a check the runner cannot perform is never counted as a pass."""

import json
from pathlib import Path

import pytest

from forge.context.snapshot import write_context_snapshot
from forge.extract import clear_context_cache
from forge.packs.spec import AcceptanceCheck, PackSpec
from forge.verify.acceptance import ACCEPTANCE_NAME, run_acceptance, write_acceptance
from forge.verify.merged_tree import MergedTree
from tests.test_extract_web_bootstrap import make_module


def _pack(*checks, pid="p", decisions=()):
    return PackSpec(id=pid, version="1.0.0", title="t", tier="framework", status="complete",
                    detect=(), applies_to=(), context="none", depends_on=(), decisions=tuple(decisions),
                    eliminates=(), upgrades=(), acceptance=tuple(checks),
                    transform_prompt="t", review_prompt="r")


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def project(tmp_path):
    """A source tree and an output overlay: one file migrated, one untouched."""
    src, out = tmp_path / "src", tmp_path / "out"
    _write(src, "app/src/main/java/com/acme/A.java", "package com.acme;\nimport org.apache.struts2.X;\nimport javax.sql.DataSource;\n")
    _write(src, "app/src/main/java/com/acme/B.java", "package com.acme;\nimport com.opensymphony.xwork2.Action;\nimport javax.sql.DataSource;\n")
    _write(src, "app/src/main/resources/struts.xml", "<struts/>")
    _write(src, "app/target/classes/Stale.java", "import com.opensymphony.xwork2.Action;\n")
    _write(out, "app/src/main/java/com/acme/A.java", "package com.acme;\nimport org.apache.struts2.X;\nimport javax.sql.DataSource;\n// migrated\n")
    _write(out, "migration-report.md", "# FORGE Migration Report\n")
    return src, out


# ─── merged view ──────────────────────────────────────────────────────────────

def test_merged_view_overlays_output_over_source_and_excludes_build_dirs(project):
    src, out = project
    tree = MergedTree(str(src), str(out))
    rels = list(tree.rel_paths())
    assert "app/src/main/java/com/acme/A.java" in rels and "app/src/main/java/com/acme/B.java" in rels
    assert not any("target/" in r for r in rels)
    assert "migration-report.md" not in rels, "the pipeline's own artifacts are not project files"
    assert tree.resolve("app/src/main/java/com/acme/A.java") == out / "app/src/main/java/com/acme/A.java"
    assert tree.resolve("app/src/main/java/com/acme/B.java") == src / "app/src/main/java/com/acme/B.java"


def test_merged_view_hides_deleted_files(project):
    src, out = project
    tree = MergedTree(str(src), str(out), deleted=["app/src/main/resources/struts.xml"])
    assert "app/src/main/resources/struts.xml" not in list(tree.rel_paths())
    assert tree.resolve("app/src/main/resources/struts.xml") is None


def test_materialize_copies_the_merged_view(project, tmp_path):
    src, out = project
    dest = MergedTree(str(src), str(out)).materialize(str(tmp_path / "merged"))
    assert (dest / "app/src/main/java/com/acme/A.java").read_text(encoding="utf-8").endswith("// migrated\n")
    assert (dest / "app/src/main/java/com/acme/B.java").exists()
    assert not (dest / "app/target").exists()


# ─── no_match / count_unchanged ───────────────────────────────────────────────

def test_no_match_fails_with_file_and_line_evidence_over_the_merged_view(project):
    src, out = project
    pack = _pack(AcceptanceCheck("no_match", r"com\.opensymphony", "**/*.java"))
    report = run_acceptance([pack], str(src), str(out), {})
    (r,) = report.results
    assert r.failed and "1 match" in r.detail
    assert r.evidence == ["app/src/main/java/com/acme/B.java:2: com.opensymphony"]
    assert report.verdict == "FAIL"


def test_no_match_passes_when_the_output_removed_the_last_occurrence(project):
    src, out = project
    _write(out, "app/src/main/java/com/acme/B.java", "package com.acme;\nimport org.apache.struts2.action.Action;\n")
    pack = _pack(AcceptanceCheck("no_match", r"com\.opensymphony", "**/*.java"))
    report = run_acceptance([pack], str(src), str(out), {})
    assert report.results[0].passed and report.verdict == "PASS"


def test_count_unchanged_passes_when_jdk_imports_survive_and_fails_when_one_is_rewritten(project):
    src, out = project
    pack = _pack(AcceptanceCheck("count_unchanged", r"^import javax\.sql", "**/*.java"))
    assert run_acceptance([pack], str(src), str(out), {}).results[0].passed
    _write(out, "app/src/main/java/com/acme/A.java", "package com.acme;\nimport jakarta.sql.DataSource;\n")
    (r,) = run_acceptance([pack], str(src), str(out), {}).results
    assert r.failed and "2 before, 1 after" in r.detail and "JDK package" in r.detail


# ─── test_parity ──────────────────────────────────────────────────────────────

def _tests(root: Path, body: str):
    _write(root, "app/src/test/java/com/acme/ATest.java", body)


def test_test_parity_passes_when_counts_match_and_when_reduction_is_disabled(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _tests(src, "class ATest {\n@Test void a(){}\n@Test void b(){}\n}")
    _tests(out, "class ATest {\n@org.junit.jupiter.api.Test void a(){}\n@Test void b(){}\n}")
    pack = _pack(AcceptanceCheck("test_parity", True))
    assert run_acceptance([pack], str(src), str(out), {}).results[0].passed
    _tests(out, "class ATest {\n@Test void a(){}\n@Disabled(\"TODO(migration)\") void b(){}\n}")
    r = run_acceptance([pack], str(src), str(out), {}).results[0]
    assert r.passed, "a removed test that is @Disabled is accounted for"


def test_test_parity_fails_when_a_test_vanishes_silently(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _tests(src, "class ATest {\n@Test void a(){}\n@Test void b(){}\n}")
    _tests(out, "class ATest {\n@Test void a(){}\n}")
    r = run_acceptance([_pack(AcceptanceCheck("test_parity", True))], str(src), str(out), {}).results[0]
    assert r.failed and "lost without @Disabled" in r.detail
    assert r.evidence == ["app/src/test/java/com/acme/ATest.java: 2 tests before, 1 after, 0 @Disabled"]


def test_test_parity_skips_when_there_are_no_tests(tmp_path):
    (tmp_path / "src").mkdir()
    r = run_acceptance([_pack(AcceptanceCheck("test_parity", True))], str(tmp_path / "src"), None, {}).results[0]
    assert r.outcome == "skip" and "no test sources" in r.detail


# ─── authz_parity ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _cache():
    clear_context_cache()
    yield
    clear_context_cache()


@pytest.fixture
def webapp(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    module = make_module(src, java=("LoggingFilter.java",))
    out = tmp_path / "out"
    out.mkdir()
    write_context_snapshot(str(out), "web_bootstrap", str(src), [str(module)])
    return src, out, module


def test_authz_parity_passes_when_the_migrated_web_xml_keeps_rules_and_chain(webapp):
    src, out, module = webapp
    web_xml = module / "src/main/webapp/WEB-INF/web.xml"
    # A namespace-only migration: same filters, same constraints, Jakarta schema.
    migrated = web_xml.read_text(encoding="utf-8").replace(
        "http://xmlns.jcp.org/xml/ns/javaee", "https://jakarta.ee/xml/ns/jakartaee").replace('version="3.1"', 'version="6.0"')
    _write(out, str(web_xml.relative_to(src)), migrated)
    r = run_acceptance([_pack(AcceptanceCheck("authz_parity", True))], str(src), str(out), {}).results[0]
    assert r.passed, r.evidence


def test_authz_parity_fails_when_a_filter_or_constraint_is_dropped(webapp):
    src, out, module = webapp
    web_xml = module / "src/main/webapp/WEB-INF/web.xml"
    text = web_xml.read_text(encoding="utf-8")
    start = text.index("<security-constraint>")
    end = text.index("</security-constraint>") + len("</security-constraint>")
    _write(out, str(web_xml.relative_to(src)), text[:start] + text[end:])
    r = run_acceptance([_pack(AcceptanceCheck("authz_parity", True))], str(src), str(out), {}).results[0]
    assert r.failed
    assert any("authorization rules changed" in e for e in r.evidence)
    assert any("/admin/*" in e and "removed" in e for e in r.evidence)


def test_authz_parity_skips_without_a_snapshot(project):
    src, out = project
    r = run_acceptance([_pack(AcceptanceCheck("authz_parity", True))], str(src), str(out), {}).results[0]
    assert r.outcome == "skip" and "migration-context.json" in r.detail


# ─── routing_parity, build, when ──────────────────────────────────────────────

def test_routing_parity_is_skipped_and_names_the_missing_extractor(project):
    src, out = project
    r = run_acceptance([_pack(AcceptanceCheck("routing_parity", True))], str(src), str(out), {}).results[0]
    assert r.outcome == "skip" and "struts_routing_table" in r.detail


def test_build_is_skipped_unless_requested_then_runs_in_the_merged_tree(project):
    src, out = project
    pack = _pack(AcceptanceCheck("build", "sh -c ls"))
    r = run_acceptance([pack], str(src), str(out), {}).results[0]
    assert r.outcome == "skip" and "--acceptance-build" in r.detail
    r = run_acceptance([pack], str(src), str(out), {}, run_build=True).results[0]
    assert r.passed
    r = run_acceptance([_pack(AcceptanceCheck("build", "sh -c 'exit 3'"))], str(src), str(out), {}, run_build=True).results[0]
    assert r.failed and "exited 3" in r.detail


def test_build_with_a_missing_tool_is_skipped_not_failed(project):
    src, out = project
    r = run_acceptance([_pack(AcceptanceCheck("build", "definitely-not-a-tool compile"))],
                       str(src), str(out), {}, run_build=True).results[0]
    assert r.outcome == "skip" and "not found on PATH" in r.detail


def test_when_guard_skips_on_unset_or_non_matching_decision_and_runs_when_matching(project):
    src, out = project
    check = AcceptanceCheck("no_match", r"com\.opensymphony", "**/*.java", when=(("views", "thymeleaf"),))
    pack = _pack(check, decisions=("views",))
    unset = run_acceptance([pack], str(src), str(out), {}).results[0]
    assert unset.outcome == "skip" and "is not set" in unset.detail
    other = run_acceptance([pack], str(src), str(out), {"views": "in-place"}).results[0]
    assert other.outcome == "skip" and "not applicable" in other.detail
    match = run_acceptance([pack], str(src), str(out), {"views": "thymeleaf"}).results[0]
    assert match.failed


# ─── verdict and persistence ──────────────────────────────────────────────────

def test_verdict_is_incomplete_when_anything_was_skipped(project):
    src, out = project
    pack = _pack(AcceptanceCheck("no_match", r"NEVER", "**/*.java"), AcceptanceCheck("routing_parity", True))
    report = run_acceptance([pack], str(src), str(out), {})
    assert [r.outcome for r in report.results] == ["pass", "skip"]
    assert report.verdict == "INCOMPLETE", "a skipped check is not a pass"


def test_write_acceptance_persists_json_and_appends_to_the_report(project):
    src, out = project
    pack = _pack(AcceptanceCheck("no_match", r"com\.opensymphony", "**/*.java"))
    report = run_acceptance([pack], str(src), str(out), {})
    path = write_acceptance(report, str(out))
    assert path == out / ACCEPTANCE_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"] == "FAIL" and data["results"][0]["evidence"]
    md = (out / "migration-report.md").read_text(encoding="utf-8")
    assert md.startswith("# FORGE Migration Report")
    assert "## Acceptance" in md and "**Verdict: FAIL**" in md and "B.java:2" in md
    assert "A skipped check is not a pass" in md


def test_the_shipped_packs_checks_all_run_or_skip_with_a_reason(project):
    """Every acceptance kind the library uses is handled; none falls through."""
    from forge.packs import load_packs

    src, out = project
    report = run_acceptance(list(load_packs().complete), str(src), str(out), {"web_framework": "modernize-in-place"})
    assert report.results
    for r in report.results:
        assert r.outcome in ("pass", "fail", "skip")
        assert r.detail
        assert "unknown check kind" not in r.detail
