"""The ``reactor`` extractor hands a pom's transform and review the parent chain it inherits from.

On AMS a sound services pom was held at 45 with every deduction reading
"parent not provided, cannot verify": the build pack declared a ``reactor``
context that no extractor supplied. The context is read from the source tree
with no model, one run per module, and the summary says the chain is the
reactor before the pack ran.
"""

from pathlib import Path

import pytest

from forge.context.inject import context_available
from forge.context.render import render_context, section_priority
from forge.extract import clear_context_cache, get_context, get_extractor
from forge.extract.reactor import find_modules, module_for, run
from forge.utils.file_scanner import runnable_phases

ROOT_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.corp</groupId><artifactId>parent</artifactId><version>1.0</version>
  <packaging>pom</packaging>
  <modules><module>internal</module></modules>
  <properties>
    <maven.compiler.source>1.8</maven.compiler.source>
    <spring.version>5.3.39</spring.version>
    <ams.version>1.0</ams.version>
  </properties>
  <dependencyManagement>
    <dependencies>
      <dependency><groupId>org.springframework</groupId><artifactId>spring-framework-bom</artifactId>
        <version>${spring.version}</version><type>pom</type><scope>import</scope></dependency>
      <dependency><groupId>com.h2database</groupId><artifactId>h2</artifactId><version>1.3.176</version></dependency>
    </dependencies>
  </dependencyManagement>
  <build>
    <pluginManagement><plugins>
      <plugin><artifactId>maven-surefire-plugin</artifactId><version>2.22.2</version></plugin>
    </plugins></pluginManagement>
  </build>
  <profiles><profile><id>ci</id><properties><ci.only>1</ci.only></properties></profile></profiles>
</project>
"""

AGGREGATOR = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>com.corp</groupId><artifactId>parent</artifactId><version>1.0</version></parent>
  <artifactId>internal</artifactId>
  <packaging>pom</packaging>
  <properties><spring.version>5.3.40</spring.version></properties>
  <modules><module>services</module></modules>
</project>
"""

LEAF = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>com.corp</groupId><artifactId>internal</artifactId><version>1.0</version></parent>
  <artifactId>services</artifactId>
  <dependencies>
    <dependency><groupId>org.springframework</groupId><artifactId>spring-jdbc</artifactId></dependency>
    <dependency><groupId>com.corp</groupId><artifactId>shared</artifactId><version>${ams.version}</version></dependency>
  </dependencies>
</project>
"""


@pytest.fixture(autouse=True)
def _fresh():
    clear_context_cache()
    yield
    clear_context_cache()


def _reactor(tmp_path: Path) -> Path:
    (tmp_path / "pom.xml").write_text(ROOT_POM, encoding="utf-8")
    (tmp_path / "internal" / "services").mkdir(parents=True)
    (tmp_path / "internal" / "pom.xml").write_text(AGGREGATOR, encoding="utf-8")
    (tmp_path / "internal" / "services" / "pom.xml").write_text(LEAF, encoding="utf-8")
    (tmp_path / "internal" / "services" / "target").mkdir()
    (tmp_path / "internal" / "services" / "target" / "pom.xml").write_text("<project/>", encoding="utf-8")
    return tmp_path / "internal" / "services"


def test_the_extractor_is_registered_and_the_build_pack_is_no_longer_context_blind():
    assert get_extractor("reactor") is not None
    assert context_available("reactor")
    assert "build-maven-modernize" in set(runnable_phases())


def test_a_pom_belongs_to_its_own_directory_and_a_source_file_to_the_nearest_pom(tmp_path):
    leaf = _reactor(tmp_path)
    assert module_for(str(leaf / "pom.xml"), str(tmp_path)) == str(leaf.resolve())
    java = leaf / "src/main/java/com/corp/A.java"
    java.parent.mkdir(parents=True)
    java.write_text("class A {}", encoding="utf-8")
    assert module_for(str(java), str(tmp_path)) == str(leaf.resolve())
    assert sorted(Path(m).relative_to(tmp_path.resolve()).as_posix() for m in find_modules(str(tmp_path))) == \
        [".", "internal", "internal/services"], "build output is not a module"


def test_a_leaf_sees_its_whole_parent_chain_nearest_first(tmp_path):
    leaf = _reactor(tmp_path)
    ctx = run(str(tmp_path), str(leaf))
    assert ctx["module"]["coordinates"] == "com.corp:services:1.0"
    assert ctx["module"]["parent"] == "com.corp:internal:1.0"
    assert "properties" not in ctx["module"], "the module's own pre-migration values are not restated"
    assert [p["coordinates"] for p in ctx["parents"]] == ["com.corp:internal:1.0", "com.corp:parent:1.0"]
    assert [p["rewritten"] for p in ctx["parents"]] == [False, False]
    root = ctx["parents"][1]
    assert root["bom_imports"] == ["org.springframework:spring-framework-bom:${spring.version}"]
    assert root["managed_versions"] == {"com.h2database:h2": "1.3.176"}
    assert root["plugin_versions"] == {"org.apache.maven.plugins:maven-surefire-plugin": "2.22.2"}
    assert root["profile_properties"] == {"ci": {"ci.only": "1"}}
    assert ctx["reactor"]["root"] == "pom.xml"
    assert ctx["reactor"]["modules"] == ["internal/pom.xml", "internal/services/pom.xml", "pom.xml"]


def test_the_nearest_parent_wins_when_two_define_a_property(tmp_path):
    leaf = _reactor(tmp_path)
    inherited = run(str(tmp_path), str(leaf))["inherited_properties"]
    assert inherited["spring.version"] == "5.3.40", "the aggregator overrides the root"
    assert inherited["ams.version"] == "1.0" and inherited["maven.compiler.source"] == "1.8"


def test_the_root_and_an_external_parent_are_named_in_the_notes(tmp_path):
    _reactor(tmp_path)
    root = run(str(tmp_path), str(tmp_path))
    assert root["parents"] == [] and root["inherited_properties"] == {}
    assert any("reactor root" in n for n in root["summary"]["notes"])

    orphan = tmp_path / "orphan"
    orphan.mkdir()
    (orphan / "pom.xml").write_text(LEAF.replace("<artifactId>internal</artifactId>", "<artifactId>elsewhere</artifactId>"),
                                    encoding="utf-8")
    ctx = run(str(tmp_path), str(orphan))
    assert ctx["parents"] == [] and any("not on disk" in n for n in ctx["summary"]["notes"])


def test_the_summary_says_the_chain_is_the_reactor_before_the_pack_ran(tmp_path):
    leaf = _reactor(tmp_path)
    ctx = get_context("reactor", str(tmp_path), str(leaf)).data
    assert any("BEFORE this pack ran" in n for n in ctx["summary"]["notes"])
    assert ctx["summary"]["parent_chain"] == ["com.corp:internal:1.0", "com.corp:parent:1.0"]
    assert ctx["summary"]["inherits"] == {"properties": 3, "bom_imports": 1, "managed_versions": 1}


def test_a_pom_target_renders_the_module_and_parents_first_and_keeps_the_summary(tmp_path):
    leaf = _reactor(tmp_path)
    ctx = run(str(tmp_path), str(leaf))
    assert section_priority("internal/services/pom.xml")[:2] == ("module", "parents")
    block = render_context("reactor", ctx, for_target=str(leaf / "pom.xml"))
    assert block.index("## module") < block.index("## parents") < block.index("## inherited_properties")
    assert "spring-framework-bom" in block and "## summary" in block
    small = render_context("reactor", ctx, for_target=str(leaf / "pom.xml"), max_chars=900)
    assert "## summary" in small and "BEFORE this pack ran" in small
    assert "OMITTED" in small


def test_a_missing_or_broken_pom_yields_a_note_not_an_error(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "pom.xml").write_text("<project><properties>", encoding="utf-8")
    ctx = run(str(tmp_path), str(tmp_path / "broken"))
    assert ctx["summary"]["module"] is None and any("does not parse" in n for n in ctx["summary"]["notes"])
