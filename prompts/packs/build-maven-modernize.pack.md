---
id: build-maven-modernize
version: 1.0.0
title: Maven reactor -> Java 21 / Spring 6 WAR / Jakarta EE 10
tier: build
detect:
  any:
    - file_glob: "**/pom.xml"
applies_to:
  - file_glob: "**/pom.xml"
context: reactor
depends_on: []
decisions: [runtime, container]
eliminates: []
acceptance:
  - build: "mvn -q -DskipTests package"
  - no_match: 'org\.springframework\.boot'
    scope: "**/pom.xml"
---

## transform

You are migrating one `pom.xml` in a multi-module reactor to Java 21, Spring Framework 6.2 and
Jakarta EE 10, packaged as a **WAR for a Jakarta EE 10 servlet container**.

**Spring Boot is not part of this target and must not appear in the output.** No
`org.springframework.boot` coordinate, no `spring-boot-maven-plugin`, no starter dependencies.
The application is built as a WAR and deployed to the container named by the `container`
decision. Reach for a Boot starter and you have produced something the deployment pipeline
cannot take.

**You are given the reactor context**: the module graph, this module's parent, its children, the
full property block of the parent, and the consolidated list of coordinates every activated pack
has asked to remove (`eliminates`). Transform only the file you are given — but be consistent with
where it sits in the reactor: version pinning belongs in the parent, dependencies in the module.

Rule 1 — Java level: replace `maven.compiler.source`/`target` with a single
`<maven.compiler.release>21</maven.compiler.release>`. `release` is stricter than source/target —
it prevents compiling against APIs absent from the target JDK, which is the point.

Rule 2 — Dependency management. Without Boot there is no single BOM, so import the individual
ones into the parent's `<dependencyManagement>`, each with `<type>pom</type>` and
`<scope>import</scope>`:
- `org.springframework:spring-framework-bom` 6.2.x
- `org.springframework.security:spring-security-bom` 6.3.x
- `com.fasterxml.jackson:jackson-bom` 2.17.x
- `org.junit:junit-bom` 5.10.x
- `org.slf4j:slf4j-bom` (or pin slf4j/log4j2 explicitly — these have no universal BOM)

Import order matters: the **first** declaration of a managed version wins in Maven, so declare
these in the order above and keep any project-specific overrides *before* them if an override is
genuinely intended.

**Remove every hand-pinned version a BOM now manages** — `spring.version`,
`spring-security.version`, `jackson.version`, `junit.version`, `mockito.version`. A pinned
version alongside its BOM silently overrides it and reintroduces exactly the version skew the
upgrade was meant to remove. Keep hand-pinned versions for anything no BOM manages: JDBC
drivers, internal artifacts, niche libraries.

Rule 3 — Remove every coordinate in the `eliminates` list supplied to you. Do not remove a
dependency that is not on that list, even if it looks obsolete — another pack may still be
migrating code that uses it, and removing it breaks the build gate before that pack runs.

Rule 4 — Jakarta EE 10 coordinates:
- `javax.servlet:javax.servlet-api` → `jakarta.servlet:jakarta.servlet-api` 6.0.0 (`provided`)
- `javax.servlet:jstl` → `jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api` 3.0.0 plus the
  `org.glassfish.web:jakarta.servlet.jsp.jstl` 3.0.1 implementation (the API alone does not render)
- `javax.annotation:javax.annotation-api` → `jakarta.annotation:jakarta.annotation-api` 2.1.1
- `javax.validation:validation-api` → `jakarta.validation:jakarta.validation-api` 3.0.2
- `javax.xml.bind:jaxb-api` → `jakarta.xml.bind:jakarta.xml.bind-api` 4.0.x plus
  `org.glassfish.jaxb:jaxb-runtime` — JAXB left the JDK, and the API without the runtime compiles
  and fails at startup
- `javax.persistence:persistence-api` → `jakarta.persistence:jakarta.persistence-api` 3.1.0

Rule 5 — Plugins that must move for Java 21 to build at all:
- `maven-compiler-plugin` 3.11+; `maven-surefire-plugin` 3.2+ (older surefire does not discover
  JUnit 5); `maven-failsafe-plugin` to match; `maven-war-plugin` 3.4+; `maven-ear-plugin` 3.3+;
  `jacoco` 0.8.11+ (earlier versions cannot read Java 21 class files and fail the build);
  `aspectj` 1.9.22+ if present (1.7/1.8 cannot weave Java 21 bytecode).

Rule 6 — Packaging. **Packaging does not change.** A `war` module stays `war`; an `ear` module
stays `ear`; the module list is preserved. What changes is the container the WAR targets, per the
`container` decision. The platform default is `liberty`:

- **`liberty`** — replace any previous app-server plugin (`wildfly-maven-plugin`,
  `weblogic-maven-plugin`, `was-maven-plugin`) with:
  ```xml
  <plugin>
    <groupId>io.openliberty.tools</groupId>
    <artifactId>liberty-maven-plugin</artifactId>
    <version>3.11.x</version>
    <configuration>
      <serverName>defaultServer</serverName>
      <!-- liberty_edition: open -> io.openliberty:openliberty-runtime -->
      <!-- liberty_edition: websphere -> com.ibm.websphere.appserver.runtime:wlp-* -->
    </configuration>
  </plugin>
  ```
  The runtime artifact differs by `liberty_edition`; do not hardcode one without reading the
  decision. Liberty's `pages-3.1` feature supplies **both JSP and JSTL 3.0**, so the JSTL
  implementation must not be bundled.
- **`wildfly`** — uplift `wildfly-maven-plugin` to an EE 10 capable version; keep the deployment
  configuration as-is.
- **`tomcat` / `jetty`** — servlet container only. Anything the old app server provided (JNDI
  DataSource, JTA manager, connection pool, mail session) becomes the application's
  responsibility. Flag each `resource-ref`; do not invent a replacement in the pom.

Scope discipline, which decides whether the WAR starts at all: servlet, JSP, JSTL, annotation,
CDI, persistence and transaction APIs stay `<scope>provided</scope>`, and the **JDBC driver
becomes `provided` too** when the container owns the DataSource — on Liberty it is declared as a
`<library>` in `server.xml`. Bundling any of these into `WEB-INF/lib` produces a classloader
conflict that surfaces as `LinkageError` or `ClassCastException` at deploy time, not at build
time.

Rule 7 — Preserve, exactly: `groupId`, `artifactId`, `version`, `<modules>` order, `<profiles>`,
`<repositories>`, `<distributionManagement>`, every existing explanatory comment, and the version
of any dependency not covered by the rules above. A pinned version with a comment explaining the
pin is a decision someone already made — keep both.

Respond ONLY with valid JSON:
{"files": {...}, "deleted_files": [], "manual_flags": [...]}

## review

Score on 5 checks (total 100).

Check 1 — Java 21 and BOM coherence (25 pts):
`maven.compiler.release` is 21 and old source/target properties are gone. The Spring Framework,
Spring Security, Jackson and JUnit BOMs are imported, and **no hand-pinned version remains for
anything they manage**. A surviving pinned Spring or Jackson version scores 0 — it silently
overrides the BOM and reintroduces version skew. **Any `org.springframework.boot` coordinate or
`spring-boot-maven-plugin` scores 0 for this check** — Boot is not part of this target.

Check 2 — Jakarta coordinates complete (20 pts):
Every EE coordinate moved to its `jakarta.*` groupId at a Jakarta EE 10 version, and every API
that needs a separate runtime (JSTL, JAXB) has it. Partial credit per dependency.

Check 3 — Plugins can build Java 21 (20 pts):
compiler, surefire, failsafe, jacoco, war/ear and aspectj at versions that function on 21.
Surefire below 3.0 or jacoco below 0.8.11 scores 0 — the build cannot run.

Check 4 — Eliminations exact (20 pts):
Every coordinate on the supplied `eliminates` list is gone, and **nothing outside that list was
removed**. Removing an extra dependency scores 0 — it breaks the build gate for a pack that has
not run yet.

Check 5 — Packaging and scopes preserved (15 pts):
Packaging type is unchanged (`war` stays `war`, `ear` stays `ear`), the module list and its order
are intact, and every container-supplied API — servlet, JSP, JSTL, annotation, CDI, persistence,
transaction, plus the JDBC driver where the container owns the DataSource — remains `provided`.
A bundled container API scores 0: it deploys and then fails with `LinkageError`. Coordinates, profiles, repositories,
unrelated dependency versions and existing comments preserved.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"java_and_bom": <0-25>, "jakarta_coords": <0-20>, "plugins_java21": <0-20>,
            "eliminations_exact": <0-20>, "nothing_else": <0-15>}}
