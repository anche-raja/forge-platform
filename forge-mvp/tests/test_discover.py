"""Discovery: profile a repository without a model, resolve which packs apply,
and show the evidence."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from forge.discover import build_profile, render_summary, resolve_packs, write_outputs
from forge.discover.emit import DEFAULT_DECISIONS, PROFILE_JSON, PROFILE_YAML
from forge.discover.profile import normalize_java_level
from forge.discover.resolve import content_patterns, version_lt
from forge.packs import load_packs
from tests.test_extract_web_bootstrap import make_module

PARENT_POM = """<project>
  <groupId>com.acme</groupId><artifactId>acme-parent</artifactId><version>1.0</version><packaging>pom</packaging>
  <modules><module>orders</module></modules>
  <properties>
    <maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
    <struts2.version>6.8.0</struts2.version>
    <spring.version>5.3.39</spring.version>
  </properties>
  <dependencyManagement><dependencies>
    <dependency><groupId>org.hibernate</groupId><artifactId>hibernate-core</artifactId><version>5.6.15.Final</version></dependency>
    <dependency><groupId>org.mockito</groupId><artifactId>mockito-core</artifactId><version>1.9.5</version></dependency>
  </dependencies></dependencyManagement>
</project>"""

MODULE_POM = """<project>
  <parent><groupId>com.acme</groupId><artifactId>acme-parent</artifactId><version>1.0</version></parent>
  <artifactId>orders</artifactId><packaging>war</packaging>
  <dependencies>
    <dependency><groupId>org.apache.struts</groupId><artifactId>struts2-core</artifactId><version>${struts2.version}</version></dependency>
    <dependency><groupId>org.springframework</groupId><artifactId>spring-core</artifactId><version>${spring.version}</version></dependency>
    <dependency><groupId>org.hibernate</groupId><artifactId>hibernate-core</artifactId></dependency>
    <dependency><groupId>org.mockito</groupId><artifactId>mockito-core</artifactId><scope>test</scope></dependency>
    <dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.12</version><scope>test</scope></dependency>
    <dependency><groupId>javax.servlet</groupId><artifactId>javax.servlet-api</artifactId><version>3.1.0</version><scope>provided</scope></dependency>
  </dependencies>
</project>"""


@pytest.fixture
def repo(tmp_path):
    """A hybrid Struts 2 + Spring 5 web module under a parent BOM."""
    make_module(tmp_path, name="orders", java=("LoggingFilter.java",), pom=False)
    (tmp_path / "pom.xml").write_text(PARENT_POM, encoding="utf-8")
    (tmp_path / "orders/pom.xml").write_text(MODULE_POM, encoding="utf-8")
    src = tmp_path / "orders/src/main/java/com/acme/orders"
    (src / "web/UserAction.java").parent.mkdir(parents=True, exist_ok=True)
    (src / "web/UserAction.java").write_text(
        "package com.acme.orders.web;\nimport com.opensymphony.xwork2.ActionSupport;\n"
        "import javax.servlet.http.HttpServletRequest;\npublic class UserAction extends ActionSupport {}\n", encoding="utf-8")
    (src / "config/WebSecurityConfig.java").parent.mkdir(parents=True, exist_ok=True)
    (src / "config/WebSecurityConfig.java").write_text(
        "package com.acme.orders.config;\n"
        "import org.springframework.security.config.annotation.web.configuration.WebSecurityConfigurerAdapter;\n"
        "public class WebSecurityConfig extends WebSecurityConfigurerAdapter {}\n", encoding="utf-8")
    (tmp_path / "orders/src/main/resources/struts.xml").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "orders/src/main/resources/struts.xml").write_text("<struts/>", encoding="utf-8")
    jsp = tmp_path / "orders/src/main/webapp/WEB-INF/jsp/list.jsp"
    jsp.parent.mkdir(parents=True, exist_ok=True)
    jsp.write_text('<%@ taglib uri="http://java.sun.com/jsp/jstl/core" prefix="c" %>\n', encoding="utf-8")
    test = tmp_path / "orders/src/test/java/com/acme/orders/UserActionTest.java"
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text("package com.acme.orders;\nimport org.junit.Test;\npublic class UserActionTest {}\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def packs():
    return list(load_packs().values())


# ─── profile ──────────────────────────────────────────────────────────────────

def test_profile_resolves_versions_through_properties_and_dependency_management(repo, packs):
    p = build_profile(str(repo), content_patterns=content_patterns(packs))
    assert p.build_system == "maven"
    assert {m.artifact_id for m in p.modules} == {"acme-parent", "orders"}
    versions = {d.coord: d.version for d in p.dependencies if not d.managed}
    assert versions["org.apache.struts:struts2-core"] == "6.8.0", "${struts2.version} resolved through the parent"
    assert versions["org.springframework:spring-core"] == "5.3.39"
    assert versions["org.hibernate:hibernate-core"] == "5.6.15.Final", "version-less dependency resolved via dependencyManagement"
    assert versions["org.mockito:mockito-core"] == "1.9.5"
    assert p.java_level == "8"
    assert p.properties["maven.compiler.source"] == "1.8"


def test_profile_collects_imports_descriptors_xml_elements_and_content_hits(repo, packs):
    p = build_profile(str(repo), content_patterns=content_patterns(packs))
    assert "com.opensymphony.xwork2.ActionSupport" in p.imports
    assert "javax.servlet.http.HttpServletRequest" in p.imports
    assert not any("org.junit.Test" == i for i in p.imports), "test sources do not contribute imports"
    names = {Path(d).name for d in p.descriptors}
    assert {"web.xml", "struts.xml", "pom.xml"} - names == {"pom.xml"}
    assert any(e.endswith(":web-app") for e in p.xml_elements)
    assert p.content_hits[r"java\.sun\.com/jsp/jstl"] == ["orders/src/main/webapp/WEB-INF/jsp/list.jsp"]
    assert p.counts[".java"] == 4 and p.counts["test_java"] == 1 and p.counts[".jsp"] == 1


def test_profile_json_is_serialisable_and_summarises_rather_than_dumps(repo):
    p = build_profile(str(repo))
    data = json.loads(json.dumps(p.to_json()))
    assert "files" not in data, "every path is not persisted; counts and coordinates are"
    assert "org.apache.struts:struts2-core:6.8.0" in data["dependencies"]
    assert data["import_prefixes"]["javax.servlet.http"] >= 1


def test_version_less_dependency_resolves_through_an_imported_bom(tmp_path):
    """The common enterprise shape: the parent imports spring-framework-bom and
    modules declare spring-core with no version. Maven resolves it from the BOM;
    so must discovery, or every such reactor reads as 'Spring ?'."""
    (tmp_path / "pom.xml").write_text("""<project>
      <groupId>com.acme</groupId><artifactId>parent</artifactId><version>1</version><packaging>pom</packaging>
      <properties><spring.version>5.3.39</spring.version></properties>
      <dependencyManagement><dependencies>
        <dependency><groupId>org.springframework</groupId><artifactId>spring-framework-bom</artifactId>
          <version>${spring.version}</version><type>pom</type><scope>import</scope></dependency>
      </dependencies></dependencyManagement></project>""", encoding="utf-8")
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc/pom.xml").write_text("""<project>
      <parent><groupId>com.acme</groupId><artifactId>parent</artifactId><version>1</version></parent>
      <artifactId>svc</artifactId>
      <dependencies><dependency><groupId>org.springframework</groupId><artifactId>spring-core</artifactId></dependency>
      <dependency><groupId>org.springframework</groupId><artifactId>spring-boot</artifactId></dependency></dependencies>
    </project>""", encoding="utf-8")
    p = build_profile(str(tmp_path))
    core = next(d for d in p.dependencies if d.artifact == "spring-core")
    assert core.version == "5.3.39" and core.pom.endswith("via spring-framework-bom")
    packs = list(load_packs().values())
    act = next(a for a in resolve_packs(p, packs) if a.pack_id == "spring-to-spring6")
    assert any("spring-core:5.3.39 < 6.0.0" in e for e in act.evidence)


def test_gradle_module_levels_and_dependencies(tmp_path):
    (tmp_path / "build.gradle").write_text(
        "java { sourceCompatibility = JavaVersion.VERSION_1_8 }\n"
        "dependencies {\n  implementation 'org.springframework:spring-core:5.3.39'\n"
        "  testImplementation('junit:junit:4.12')\n}\n", encoding="utf-8")
    p = build_profile(str(tmp_path))
    assert p.build_system == "gradle" and p.java_level == "8"
    assert {d.coord: d.version for d in p.dependencies} == {"org.springframework:spring-core": "5.3.39", "junit:junit": "4.12"}


@pytest.mark.parametrize("raw,expected", [("1.8", 8), ("8", 8), ("11", 11), ("17", 17), ("21", 21),
                                          ("JavaVersion.VERSION_1_8", 8), ("VERSION_17", 17), ("", None), ("x", None)])
def test_java_level_normalisation(raw, expected):
    assert normalize_java_level(raw) == expected


@pytest.mark.parametrize("a,b,expected", [
    ("6.8.0", "7.0.0", True), ("7.0.0", "7.0.0", False), ("7.3.0", "7.0.0", False),
    ("5.3.39", "6.0.0", True), ("6.8", "6.8.0", False), ("5.6.15.Final", "6.0.0", True),
    ("6.0.0-RC1", "6.0.0", True), ("1.9.5", "5.0.0", True), ("", "1.0", None),
])
def test_version_lt(a, b, expected):
    assert version_lt(a, b) is expected


# ─── resolution ───────────────────────────────────────────────────────────────

def test_resolution_fires_the_right_packs_with_evidence(repo, packs):
    p = build_profile(str(repo), content_patterns=content_patterns(packs), decisions=DEFAULT_DECISIONS)
    acts = {a.pack_id: a for a in resolve_packs(p, packs)}

    expected = {"build-maven-modernize", "java8-to-java21", "javax-to-jakarta", "spring-to-spring6",
                "springsec-to-springsec6", "struts2-modernize",
                "jsp-jstl-modernize", "junit4-to-junit5", "webapp-bootstrap-jakarta10",
                "liberty-server-config", "hibernate-to-hibernate6"}
    assert expected <= set(acts), sorted(set(acts))
    for absent in ("ejb2-to-spring", "ejb3-to-spring", "jsf-to-faces4", "ibatis-to-mybatis", "ant-to-maven",
                   # Removed with the Struts -> Spring MVC route.
                   "struts1-to-springmvc6", "struts2-to-springmvc6"):
        assert absent not in acts, absent

    assert any("struts2-core:6.8.0 < 7.0.0" in e for e in acts["struts2-modernize"].evidence)
    assert any("maven.compiler.source=1.8 (Java 8 < 21)" in e for e in acts["java8-to-java21"].evidence)
    assert any("spring-core:5.3.39 < 6.0.0" in e for e in acts["spring-to-spring6"].evidence)
    assert any("import javax.servlet" in e for e in acts["javax-to-jakarta"].evidence)
    assert any("content orders/src/main/webapp/WEB-INF/jsp/list.jsp" in e for e in acts["jsp-jstl-modernize"].evidence)
    assert any("decision container=liberty" in e for e in acts["liberty-server-config"].evidence)
    assert any("hibernate-core:5.6.15.Final < 6.0.0" in e for e in acts["hibernate-to-hibernate6"].evidence)
    assert acts["hibernate-to-hibernate6"].complete is False


def test_a_project_already_on_the_target_stack_fires_nothing_version_gated(tmp_path):
    (tmp_path / "pom.xml").write_text("""<project><artifactId>modern</artifactId>
      <properties><maven.compiler.release>21</maven.compiler.release></properties>
      <dependencies>
        <dependency><groupId>org.springframework</groupId><artifactId>spring-core</artifactId><version>6.2.1</version></dependency>
        <dependency><groupId>org.apache.struts</groupId><artifactId>struts2-core</artifactId><version>7.3.0</version></dependency>
      </dependencies></project>""", encoding="utf-8")
    packs = list(load_packs().values())
    acts = {a.pack_id for a in resolve_packs(build_profile(str(tmp_path)), packs)}
    assert "java8-to-java21" not in acts and "spring-to-spring6" not in acts and "struts2-modernize" not in acts
    assert "build-maven-modernize" in acts, "the build pack always applies to a Maven reactor"


def test_unresolved_version_is_assumed_old_and_says_so(tmp_path):
    (tmp_path / "pom.xml").write_text("""<project><artifactId>x</artifactId><dependencies>
      <dependency><groupId>org.springframework</groupId><artifactId>spring-core</artifactId><version>${elsewhere}</version></dependency>
      </dependencies></project>""", encoding="utf-8")
    packs = list(load_packs().values())
    act = next(a for a in resolve_packs(build_profile(str(tmp_path)), packs) if a.pack_id == "spring-to-spring6")
    assert any("unresolved version" in e and "${elsewhere}" in e for e in act.evidence)


def test_a_decision_alone_never_activates_a_pack(tmp_path, packs):
    """Config says the container is Liberty; the directory is empty. Nothing to migrate."""
    (tmp_path / "nothing").mkdir()
    acts = resolve_packs(build_profile(str(tmp_path / "nothing"), decisions=DEFAULT_DECISIONS), packs)
    assert acts == []


def test_a_web_app_on_the_liberty_target_activates_the_liberty_pack_with_both_kinds_of_evidence(repo, packs):
    p = build_profile(str(repo), decisions=DEFAULT_DECISIONS)
    act = next(a for a in resolve_packs(p, packs) if a.pack_id == "liberty-server-config")
    assert any(e.startswith("file ") and e.endswith("WEB-INF/web.xml") for e in act.evidence)
    assert "decision container=liberty" in act.evidence


def test_decision_equals_does_not_fire_when_the_decision_differs(repo, packs):
    p = build_profile(str(repo), decisions={"container": "wildfly"})
    acts = {a.pack_id for a in resolve_packs(p, packs)}
    assert "liberty-server-config" not in acts


# ─── outputs and CLI ──────────────────────────────────────────────────────────

def test_write_outputs_produces_profile_yaml_with_packs_evidence_and_decisions(repo, packs, tmp_path):
    p = build_profile(str(repo), content_patterns=content_patterns(packs), decisions=DEFAULT_DECISIONS)
    acts = resolve_packs(p, packs)
    for a in acts:
        a.runnable = a.pack_id != "hibernate-to-hibernate6"
    order = load_packs().resolve_order([a.pack_id for a in acts])
    out = tmp_path / "out"
    json_path, yaml_path = write_outputs(p, acts, order, DEFAULT_DECISIONS, str(out))
    assert json_path.name == PROFILE_JSON and yaml_path.name == PROFILE_YAML
    yaml_text = yaml_path.read_text(encoding="utf-8")
    assert "  - build-maven-modernize" in yaml_text
    assert yaml_text.index("- build-maven-modernize") < yaml_text.index("- struts2-modernize") < yaml_text.index("- jsp-jstl-modernize")
    assert "- hibernate-to-hibernate6   # detect-only" in yaml_text
    assert "# dependency org.apache.struts:struts2-core:6.8.0 < 7.0.0" in yaml_text
    assert "web_framework: modernize-in-place" in yaml_text and "container: liberty" in yaml_text
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["order"] == list(order) and any(a["pack"] == "struts2-modernize" for a in data["activations"])


def test_summary_names_frameworks_and_marks_non_runnable_packs(repo, packs):
    p = build_profile(str(repo), content_patterns=content_patterns(packs), decisions=DEFAULT_DECISIONS)
    acts = resolve_packs(p, packs)
    for a in acts:
        # No shipped pack is blocked any more, so one is forced here to keep the
        # "blocked" label in the summary under test.
        a.runnable = a.complete and a.pack_id != "spring-to-spring6"
    order = load_packs().resolve_order([a.pack_id for a in acts])
    text = render_summary(p, acts, order)
    assert "build system   maven  (2 module(s))" in text and "java level     8" in text
    assert "Struts 2 6.8.0" in text and "Spring 5.3.39" in text and "JUnit 4 4.12" in text
    assert "spring-to-spring6            blocked" in text
    assert "hibernate-to-hibernate6      detect-only" in text
    assert "struts2-modernize            runnable" in text


def test_cli_discover_needs_no_phase_and_no_aws(repo, tmp_path, capsys):
    import migrate

    out = tmp_path / "out"
    with patch.object(sys, "argv", ["migrate.py", str(repo), "--discover", "--output-dir", str(out)]):
        code = migrate.main()
    assert code == 0
    stdout = capsys.readouterr().out
    assert "Discovery —" in stdout and "pack(s) apply" in stdout and "Profile:" in stdout
    assert (out / PROFILE_YAML).exists() and (out / PROFILE_JSON).exists()


def test_empty_directory_reports_nothing_to_migrate(tmp_path, capsys):
    import migrate

    (tmp_path / "empty").mkdir()
    with patch.object(sys, "argv", ["migrate.py", str(tmp_path / "empty"), "--discover", "--output-dir", str(tmp_path / "out")]):
        assert migrate.main() == 0
    assert "No pack's detection rules fired" in capsys.readouterr().out
