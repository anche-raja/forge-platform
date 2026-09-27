"""The build pack takes poms by content: an aggregator with nothing to modernize is not sent.

On AMS the two aggregator poms — a parent reference and a module list, 25
lines — went to the model, came back trivially changed, and were scored 0 by a
reviewer asked to grade five checks none of which applied. A pom that declares
dependencies, dependency management, a build, properties or profiles is where
the pack's rules have something to act on; one that declares none of them is
passed over before any model call.
"""

from pathlib import Path

from forge.utils.file_scanner import scan_java_files

BUILD = "build-maven-modernize"

AGGREGATOR = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.corp</groupId><artifactId>parent</artifactId><version>1.0</version>
  </parent>
  <artifactId>internal</artifactId>
  <packaging>pom</packaging>
  <name>Internal</name>
  <description>Only names its parent and its modules.</description>
  <modules>
    <module>web</module>
  </modules>
</project>
"""

PARENT = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.corp</groupId><artifactId>parent</artifactId><version>1.0</version>
  <packaging>pom</packaging>
  <properties>
    <maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
  </properties>
  <dependencyManagement>
    <dependencies>
      <dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.12</version></dependency>
    </dependencies>
  </dependencyManagement>
</project>
"""

MODULE = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.corp</groupId><artifactId>internal</artifactId><version>1.0</version>
  </parent>
  <artifactId>web</artifactId>
  <packaging>war</packaging>
  <dependencies>
    <dependency><groupId>javax.servlet</groupId><artifactId>javax.servlet-api</artifactId><scope>provided</scope></dependency>
  </dependencies>
</project>
"""


def _reactor(tmp_path, aggregator=AGGREGATOR):
    (tmp_path / "pom.xml").write_text(PARENT, encoding="utf-8")
    (tmp_path / "internal" / "web").mkdir(parents=True)
    (tmp_path / "internal" / "pom.xml").write_text(aggregator, encoding="utf-8")
    (tmp_path / "internal" / "web" / "pom.xml").write_text(MODULE, encoding="utf-8")
    scan = scan_java_files(str(tmp_path), BUILD)
    return sorted(Path(f).resolve().relative_to(tmp_path.resolve()).as_posix() for f in scan.files)


def test_an_aggregator_is_passed_over_and_poms_with_something_to_modernize_are_sent(tmp_path):
    assert _reactor(tmp_path) == ["internal/web/pom.xml", "pom.xml"]


def test_an_aggregator_that_carries_a_profile_is_sent_because_rule_7_may_strip_it(tmp_path):
    with_profile = AGGREGATOR.replace(
        "</project>", "<profiles><profile><id>server</id></profile></profiles></project>")
    assert _reactor(tmp_path, with_profile) == ["internal/pom.xml", "internal/web/pom.xml", "pom.xml"]


def test_the_pack_still_detects_by_file_name_so_discovery_activates_it(tmp_path):
    from forge.phases import get_phase

    spec = get_phase(BUILD)
    assert not spec.globs, "selection is by content now, not by name"
    (glob, _pattern), = spec.content_matchers
    assert glob == "**/pom.xml"
    assert any(rule.kind == "file_glob" and rule.value == "**/pom.xml" for rule in spec.detect), \
        "discovery still activates the pack on the file's existence"
