"""FORGE never reads its own output as source.

The chat writes a migration into the repository it migrates (``<repo>/.migrated``),
so every walk of a source tree — the scanner, discovery, the context extractor,
the merged view, the reactor search, test generation — has to step over it. A
walk that did not would scan the first run's output as source on the second
run: every unit twice, copied into the chained view, profiled, and built.

Each test plants a full copy of the project inside the project, twice: once as
``.migrated/`` (caught by name, even with no marker yet) and once under a name
the user chose (caught by the marker FORGE leaves behind). The assertion is
always "the same answer as the tree without the copies", so a new pack or a
new kind of file cannot slip through on a technicality.
"""

import shutil
from pathlib import Path

import pytest

from forge.utils.fs import (EXCLUDED_DIRS, FORGE_OUTPUT_MARKERS, is_forge_output_dir, keep_dir,
                            prune_dirs)

FIXTURES = Path(__file__).parent / "fixtures" / "web_bootstrap"

ROOT_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.corp</groupId><artifactId>corp-parent</artifactId><version>1.0</version>
  <packaging>pom</packaging>
  <modules><module>web</module></modules>
</project>
"""

WEB_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>com.corp</groupId><artifactId>corp-parent</artifactId><version>1.0</version></parent>
  <artifactId>corp-web</artifactId><packaging>war</packaging>
  <properties><maven.compiler.source>1.8</maven.compiler.source></properties>
  <dependencies>
    <dependency><groupId>javax.servlet</groupId><artifactId>javax.servlet-api</artifactId><version>3.1.0</version></dependency>
    <dependency><groupId>org.apache.struts</groupId><artifactId>struts2-core</artifactId><version>2.5.30</version></dependency>
    <dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version></dependency>
  </dependencies>
</project>
"""

ACTION = """package com.corp.web;
import javax.persistence.Entity;
import javax.servlet.http.HttpServletRequest;
import com.opensymphony.xwork2.ActionSupport;
public class UserAction extends ActionSupport {
    public String execute() { return SUCCESS; }
}
"""

TEST = """package com.corp.web;
import org.junit.Test;
import static org.junit.Assert.assertTrue;
public class UserActionTest {
    @Test public void runs() { assertTrue(true); }
}
"""

STRUTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE struts PUBLIC "-//Apache Software Foundation//DTD Struts Configuration 2.5//EN"
  "http://struts.apache.org/dtds/struts-2.5.dtd">
<struts><package name="default" extends="struts-default">
  <action name="user" class="com.corp.web.UserAction"><result>/index.jsp</result></action>
</package></struts>
"""

JSP = """<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<html><body><c:out value="hello"/></body></html>
"""


def _project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "pom.xml").write_text(ROOT_POM, encoding="utf-8")
    web = root / "web"
    java = web / "src/main/java/com/corp/web"
    java.mkdir(parents=True)
    (web / "pom.xml").write_text(WEB_POM, encoding="utf-8")
    (java / "UserAction.java").write_text(ACTION, encoding="utf-8")
    shutil.copy(FIXTURES / "StartupListener.java", java / "StartupListener.java")
    shutil.copy(FIXTURES / "LoggingFilter.java", java / "LoggingFilter.java")
    tests = web / "src/test/java/com/corp/web"
    tests.mkdir(parents=True)
    (tests / "UserActionTest.java").write_text(TEST, encoding="utf-8")
    resources = web / "src/main/resources"
    resources.mkdir(parents=True)
    (resources / "struts.xml").write_text(STRUTS_XML, encoding="utf-8")
    webinf = web / "src/main/webapp/WEB-INF"
    webinf.mkdir(parents=True)
    shutil.copy(FIXTURES / "servlet24-web.xml", webinf / "web.xml")
    (web / "src/main/webapp/index.jsp").write_text(JSP, encoding="utf-8")
    return root


def _copy_into(project: Path, name: str, *, marker: str = "") -> Path:
    """A migrated copy of the whole project inside it, as a run would leave one."""
    out = project / name
    shutil.copytree(project, out, ignore=shutil.ignore_patterns(".migrated", "custom-out"))
    if marker:
        if marker == ".forge-staging":
            (out / marker).mkdir()
        else:
            (out / marker).write_text("{}", encoding="utf-8")
    return out


@pytest.fixture
def clean(tmp_path):
    return _project(tmp_path / "clean")


@pytest.fixture
def polluted(tmp_path):
    """The same project with two FORGE outputs inside it: the chat's default,
    brand new with no marker yet, and one the user named, which a run has
    written its manifest into."""
    root = _project(tmp_path / "polluted")
    _copy_into(root, ".migrated")
    _copy_into(root, "custom-out", marker=".forge-writes.json")
    return root


def _rels(paths, root: Path):
    return sorted(str(Path(p).resolve().relative_to(root.resolve())).replace("\\", "/") for p in paths)


# ─── the rule ─────────────────────────────────────────────────────────────────

def test_a_forge_output_dir_is_known_by_its_name_or_by_a_file_only_forge_writes(tmp_path):
    assert ".migrated" in EXCLUDED_DIRS, "a walk that only knows EXCLUDED_DIRS must still skip it"

    fresh = tmp_path / ".migrated"
    fresh.mkdir()
    assert is_forge_output_dir(fresh), "a brand-new output dir has no marker yet; its name is enough"

    for marker in FORGE_OUTPUT_MARKERS:
        named = tmp_path / f"out-{marker.strip('.')}"
        named.mkdir()
        if marker == ".forge-staging":
            (named / marker).mkdir()
        else:
            (named / marker).write_text("{}", encoding="utf-8")
        assert is_forge_output_dir(named), marker
        assert not keep_dir(tmp_path, named.name)

    ordinary = tmp_path / "src"
    ordinary.mkdir()
    (ordinary / "migration-report.md").write_text("a project's own notes\n", encoding="utf-8")
    assert not is_forge_output_dir(ordinary), "only FORGE-specific names mark a directory"
    assert not is_forge_output_dir(tmp_path / "migrated"), "`migrated` can be a package name"


def test_prune_dirs_drops_excluded_and_forge_output_and_keeps_the_order(tmp_path):
    for name in ("zeta", ".migrated", "target", "alpha", "mine"):
        (tmp_path / name).mkdir()
    (tmp_path / "mine" / "manual-review-queue.json").write_text("{}", encoding="utf-8")

    dirs = ["zeta", ".migrated", "target", "alpha", "mine", "extra"]
    prune_dirs(str(tmp_path), dirs, also=("extra",))
    assert dirs == ["zeta", "alpha"]


# ─── every walk of a source tree ─────────────────────────────────────────────

def test_no_pack_scans_a_unit_out_of_forge_output_inside_the_repository(clean, polluted):
    from forge.utils.file_scanner import runnable_phases, scan_java_files

    phases = runnable_phases()
    assert len(phases) >= 8, phases
    touched = 0
    for phase in phases:
        want = scan_java_files(str(clean), phase)
        got = scan_java_files(str(polluted), phase)
        got_files = _rels(got.files, polluted)
        assert not any(p.startswith((".migrated/", "custom-out/")) for p in got_files), (phase, got_files)
        assert got_files == _rels(want.files, clean), phase
        assert _rels(got.generated, polluted) == _rels(want.generated, clean), phase
        assert got.passed_over == want.passed_over, phase
        assert [s.reason for s in got.skipped] == [s.reason for s in want.skipped], phase
        touched += len(got_files)
    assert touched, "the fixture has to give at least one pack something to scan"


def test_discovery_counts_and_modules_leave_forge_output_out(clean, polluted):
    from forge.discover import build_profile

    want = build_profile(str(clean)).to_json()
    got = build_profile(str(polluted)).to_json()

    assert got["counts"] == want["counts"]
    assert [m.get("path") for m in got.get("modules") or []] == [m.get("path") for m in want.get("modules") or []]
    assert got["build_system"] == want["build_system"] and got["java_level"] == want["java_level"]


def test_the_context_extractor_finds_no_module_inside_forge_output(clean, polluted):
    from forge.extract.web_bootstrap import find_modules

    assert _rels(find_modules(str(polluted)), polluted) == _rels(find_modules(str(clean)), clean) == ["web"]


def test_the_merged_view_never_overlays_forge_output_onto_itself(polluted):
    """With the output inside the source, the source walk must not see it: the
    chained view and the project build would otherwise carry a second copy of
    the project under `.migrated/`."""
    from forge.verify.merged_tree import MergedTree

    out = polluted / ".migrated"
    (out / "web/src/main/java/com/corp/web/UserAction.java").write_text("// migrated\n", encoding="utf-8")
    (out / "manual-review-queue.json").write_text("{}", encoding="utf-8")

    tree = MergedTree(str(polluted), str(out))
    rels = list(tree.rel_paths())
    assert not any(r.startswith((".migrated/", "custom-out/")) for r in rels), rels
    assert "web/src/main/java/com/corp/web/UserAction.java" in rels
    assert tree.resolve("web/src/main/java/com/corp/web/UserAction.java") == \
        (out / "web/src/main/java/com/corp/web/UserAction.java").resolve()


def test_the_merged_view_skips_its_own_output_even_before_it_has_a_name_or_marker(tmp_path):
    """An output directory the user named, that no run has written to yet."""
    from forge.verify.merged_tree import MergedTree

    root = _project(tmp_path / "proj")
    out = _copy_into(root, "fresh-out")          # no marker, not a FORGE name

    rels = list(MergedTree(str(root), str(out)).rel_paths())
    assert not any(r.startswith("fresh-out/") for r in rels), rels


def test_find_reactors_ignores_poms_under_forge_output(clean, polluted):
    from forge.verify.project_build import find_reactors

    got = _rels(find_reactors(str(polluted)), polluted)
    assert got == _rels(find_reactors(str(clean)), clean) == ["pom.xml"]


def test_test_generation_indexes_no_class_out_of_forge_output(clean, polluted):
    from forge.testgen.context import build_source_index
    from forge.testgen.targets import existing_test_index, walk_java

    assert _rels(walk_java(polluted), polluted) == _rels(walk_java(clean), clean)
    index = build_source_index("", str(polluted))
    assert all("/.migrated/" not in p and "/custom-out/" not in p for p in index.values()), index
    tests = existing_test_index(str(polluted))
    assert all("/.migrated/" not in p and "/custom-out/" not in p for p in tests.values()), tests
