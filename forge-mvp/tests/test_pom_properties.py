"""A pom that references a property nobody defines fails Maven's model build before javac runs.

On AMS the build pack imported ``org.junit:junit-bom`` and pinned junit-jupiter
at ``${junit-bom.version}``, a property it never defined. The XML was
well-formed, the reviewer passed it, and the project build refused the whole
reactor at POM parsing. Whether a referenced property is defined is mechanical,
so it is checked locally, and a hit goes through the same retry-with-feedback
loop a parse error does.
"""

import contextlib

import pytest

from forge.config import ForgeConfig
from forge.verify import syntax
from tests.conftest import make_state, write_config
from tests.test_syntax import _graph

CONFIG = ForgeConfig(data={"target_java_version": "21"})

PARENT = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.corp</groupId>
  <artifactId>parent</artifactId>
  <version>1.0</version>
  <packaging>pom</packaging>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
    %(properties)s
  </properties>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>org.junit</groupId><artifactId>junit-bom</artifactId>
        <version>5.10.5</version><type>pom</type><scope>import</scope>
      </dependency>
      <dependency>
        <groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>
        <version>%(junit)s</version><scope>test</scope>
      </dependency>
    </dependencies>
  </dependencyManagement>
</project>
"""

CHILD = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.corp</groupId><artifactId>parent</artifactId><version>1.0</version>%(relative)s
  </parent>
  <artifactId>child</artifactId>
  <dependencies>
    <dependency><groupId>x</groupId><artifactId>y</artifactId><version>${lib.version}</version></dependency>
  </dependencies>
</project>
"""


def parent(properties: str = "", junit: str = "5.10.5") -> str:
    return PARENT % {"properties": properties, "junit": junit}


def child(relative: str = "") -> str:
    return CHILD % {"relative": relative}


# ─── one pom, judged on its own ───────────────────────────────────────────────

def test_the_ams_defect_fails_and_names_the_property():
    verdict, errors = syntax.check_files({"ams-parent-bom/pom.xml": parent(junit="${junit-bom.version}")},
                                         CONFIG, inherited_properties=set())
    assert verdict == syntax.FAIL
    assert len(errors) == 1
    assert "${junit-bom.version}" in errors[0] and "BOM manages" in errors[0]


def test_a_defined_property_passes():
    ok = parent("<junit-bom.version>5.10.5</junit-bom.version>", "${junit-bom.version}")
    assert syntax.check_files({"pom.xml": ok}, CONFIG, inherited_properties=set()) == (syntax.PASS, [])


def test_no_version_at_all_is_what_a_bom_import_wants():
    managed = parent().replace("<version>5.10.5</version><scope>test</scope>", "<scope>test</scope>")
    assert "${" not in managed
    assert syntax.check_files({"pom.xml": managed}, CONFIG, inherited_properties=set()) == (syntax.PASS, [])


@pytest.mark.parametrize("ref", ["${project.version}", "${revision}", "${env.JUNIT}",
                                 "${maven.build.timestamp}", "${basedir}", "${settings.localRepository}"])
def test_maven_builtins_and_command_line_versions_are_not_undefined(ref):
    assert syntax.check_files({"pom.xml": parent(junit=ref)}, CONFIG, inherited_properties=set()) == (syntax.PASS, [])


def test_a_reference_inside_a_comment_is_not_a_reference():
    pom = parent("<!-- ${old.version} used to live here -->")
    assert syntax.check_files({"pom.xml": pom}, CONFIG, inherited_properties=set()) == (syntax.PASS, [])


def test_a_profile_may_define_the_property():
    pom = parent(junit="${junit-bom.version}").replace(
        "</project>",
        "<profiles><profile><id>ci</id><properties><junit-bom.version>5.10.5</junit-bom.version>"
        "</properties></profile></profiles></project>")
    assert syntax.check_files({"pom.xml": pom}, CONFIG, inherited_properties=set()) == (syntax.PASS, [])


def test_without_parent_information_nothing_is_judged():
    # The default: a caller that cannot say what the pom inherits gets no property verdict.
    assert syntax.check_files({"pom.xml": parent(junit="${junit-bom.version}")}, CONFIG) == (syntax.PASS, [])


def test_malformed_xml_is_reported_once_not_twice():
    verdict, errors = syntax.check_files({"pom.xml": "<project><properties>"}, CONFIG, inherited_properties=set())
    assert verdict == syntax.FAIL and len(errors) == 1


def test_other_xml_is_not_checked_for_properties():
    files = {"web.xml": "<web-app><param>${nothing}</param></web-app>"}
    assert syntax.check_files(files, CONFIG, inherited_properties=set()) == (syntax.PASS, [])


# ─── what a child inherits ────────────────────────────────────────────────────

def test_a_root_pom_inherits_nothing_and_is_judged_exactly(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text(parent(), encoding="utf-8")
    assert syntax.pom_inherited_properties(str(pom)) == set()


def test_a_parent_on_disk_supplies_what_a_child_inherits(tmp_path):
    (tmp_path / "pom.xml").write_text(parent("<lib.version>2</lib.version>"), encoding="utf-8")
    leaf = tmp_path / "child" / "pom.xml"
    leaf.parent.mkdir()
    leaf.write_text(child(), encoding="utf-8")
    inherited = syntax.pom_inherited_properties(str(leaf))
    assert "lib.version" in inherited
    assert syntax.check_files({"child/pom.xml": child()}, CONFIG, inherited_properties=inherited) == (syntax.PASS, [])
    assert syntax.check_files({"child/pom.xml": child()}, CONFIG, inherited_properties=set())[0] == syntax.FAIL


def test_a_grandparent_counts_too(tmp_path):
    (tmp_path / "pom.xml").write_text(parent("<lib.version>2</lib.version>"), encoding="utf-8")
    mid = tmp_path / "mid" / "pom.xml"
    mid.parent.mkdir()
    mid.write_text(child().replace("<artifactId>child</artifactId>",
                                   "<artifactId>mid</artifactId><packaging>pom</packaging>"), encoding="utf-8")
    leaf = tmp_path / "mid" / "leaf" / "pom.xml"
    leaf.parent.mkdir()
    # The leaf's parent is `mid`, and the pom at its relativePath must say so.
    leaf.write_text(child("<relativePath>../pom.xml</relativePath>").replace(
        "<artifactId>parent</artifactId>", "<artifactId>mid</artifactId>"), encoding="utf-8")
    assert "lib.version" in syntax.pom_inherited_properties(str(leaf))


def test_this_runs_rewritten_parent_counts_with_its_new_properties(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    (src / "child").mkdir(parents=True)
    out.mkdir()
    (src / "pom.xml").write_text(parent("<old.version>1</old.version>"), encoding="utf-8")
    (out / "pom.xml").write_text(parent("<lib.version>2</lib.version>"), encoding="utf-8")
    (src / "child" / "pom.xml").write_text(child(), encoding="utf-8")
    inherited = syntax.pom_inherited_properties(str(src / "child" / "pom.xml"), str(src), str(out))
    assert {"old.version", "lib.version"} <= inherited


def test_a_pom_one_directory_up_with_other_coordinates_is_not_the_parent(tmp_path):
    other = parent("<lib.version>2</lib.version>").replace("<artifactId>parent</artifactId>",
                                                          "<artifactId>sibling</artifactId>")
    (tmp_path / "pom.xml").write_text(other, encoding="utf-8")
    leaf = tmp_path / "child" / "pom.xml"
    leaf.parent.mkdir()
    leaf.write_text(child(), encoding="utf-8")
    assert syntax.pom_inherited_properties(str(leaf)) is None, "com.corp:parent is not on disk; sibling is"


def test_a_parent_not_on_disk_is_unknowable_so_nothing_is_judged(tmp_path):
    leaf = tmp_path / "child" / "pom.xml"
    leaf.parent.mkdir()
    leaf.write_text(child(), encoding="utf-8")
    assert syntax.pom_inherited_properties(str(leaf)) is None
    (tmp_path / "pom.xml").write_text(parent(), encoding="utf-8")
    leaf.write_text(child("<relativePath/>"), encoding="utf-8")
    assert syntax.pom_inherited_properties(str(leaf)) is None, "an empty relativePath means the repository, not the disk"


# ─── the graph ────────────────────────────────────────────────────────────────

def _run_pom(tmp_path, answers):
    from forge.graph import build_graph

    pom = tmp_path / "pom.xml"
    pom.write_text(parent(), encoding="utf-8")
    cfg = write_config(tmp_path, syntax_check=True)
    with contextlib.ExitStack() as stack:
        up, rev = _graph(stack, str(pom), answers)
        result = build_graph(cfg).invoke(make_state(str(pom), tmp_path, phase="build-maven-modernize"),
                                         config={"configurable": {"thread_id": str(pom)}})
    return result, up, rev


def test_the_graph_sends_an_undefined_property_back_with_the_error_and_never_reviews_it(tmp_path):
    broken = parent(junit="${junit-bom.version}")
    fixed = parent("<x.version>1</x.version>")        # differs from the original, so it is not "unchanged"
    result, up, rev = _run_pom(tmp_path, [broken, fixed])
    fs = result["current_file"]
    assert fs["status"] == "DONE" and fs["retry_count"] == 1
    assert fs["syntax_verdict"] == syntax.PASS and not fs.get("error")
    assert rev.return_value.invoke.call_count == 1, "the broken answer cost no review"
    retry_prompt = up.return_value.invoke.call_args_list[1][0][0][1].content
    assert "${junit-bom.version}" in retry_prompt and "Maven model" in retry_prompt
