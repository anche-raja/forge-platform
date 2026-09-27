---
id: build-maven-modernize
version: 1.0.0
title: Maven reactor -> Java 21 / Spring 6 WAR / Jakarta EE 10
tier: build
detect:
  any:
    - file_glob: "**/pom.xml"
applies_to:
  # By content, not by name: a pom with nothing to modernize — an aggregator
  # that only names its parent and its modules — is not sent. The transform
  # had nothing to change in it, and the reviewer, asked to score five checks
  # on a file none of them apply to, scored both AMS aggregators 0 and sent
  # them to a human. A pom that declares dependencies, management, a build,
  # properties or profiles is where the rules have something to act on.
  - content_match:
      glob: "**/pom.xml"
      pattern: '<(dependencies|dependencyManagement|build|pluginManagement|properties|profiles|reporting)\b'
context: reactor
depends_on: []
decisions: [runtime, container]
eliminates: []
acceptance:
  - build: "mvn -q -DskipTests package"
  - no_match: 'org\.springframework\.boot'
    scope: "**/pom.xml"
  # Liberty is not a target (removed 2026-09-27). It is named here only as the
  # previous application server whose Maven plugin and runtime Rule 7 retires
  # from the pom; this check is that the retirement happened.
  - no_match: 'liberty-maven-plugin|io\.openliberty|openliberty-runtime'
    scope: "**/pom.xml"
    when: {container: "tomcat"}
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
`spring-security.version`, `jackson.version`, `junit.version`. A pinned version alongside its BOM
silently overrides it and reintroduces exactly the version skew the upgrade was meant to remove.
Keep hand-pinned versions for anything no BOM manages: Mockito, JDBC drivers, internal artifacts,
niche libraries.

**Never leave a `<dependencyManagement>` entry without a `<version>`.** An explicit entry there
replaces the imported BOM's entry for that artifact outright — its version too — wherever it sits
in the list, so a versionless one leaves the artifact with no version at all and Maven refuses the
pom ("'dependencies.dependency.version' ... is missing"). For each explicit entry of an artifact a
BOM now manages: delete the whole entry when it carries nothing but coordinates, or, when it carries
a `<scope>`, `<exclusions>` or a `<classifier>` worth keeping, give it the version the BOM pins —
as a literal, or through a property this pom's own `<properties>` block defines for that BOM
import — the same number the BOM would give, so no skew. In `<dependencies>` a versionless
dependency is correct; the rule is about the managed list. **A `<dependencyManagement>` entry with
no `<version>` is a model error**: Maven takes the first declaration, so an entry declared before
the BOM import manages the artifact with nothing, and every module that then uses it fails at POM
parsing (on AMS: `junit-jupiter` in the parent's managed list). Delete such an entry or give it
the BOM's version — never leave it versionless.

A dependency a BOM manages carries no `<version>` at all — not a literal, not a property. Never reference a `${property}` this pom does not define in its own `<properties>` (or, for a child module, that the parent's property block you were given does not define): an undefined property fails Maven's model build before anything compiles. Do not invent property names such as `${junit-bom.version}`; if you import a BOM, the artifacts it manages get no version element.

Rule 3 — Remove every coordinate in the `eliminates` list supplied to you. Do not remove a
dependency that is not on that list, even if it looks obsolete — another pack may still be
migrating code that uses it, and removing it breaks the build gate before that pack runs.

**The test stack is replaced, never just removed.** The test pack has already rewritten every
test to JUnit 5 and Mockito 5, so wherever this pom declares one of these — in
`<dependencyManagement>` and in `<dependencies>` alike — put the replacement in the same place,
at `test` scope:
- `junit:junit` → `org.junit.jupiter:junit-jupiter`, with no `<version>` — the imported
  `junit-bom` manages it. Importing `junit-bom` supplies a version, not a dependency: without this
  entry no module has JUnit 5 on its test classpath and every test fails to compile.
- `org.mockito:mockito-all`, or `org.mockito:mockito-core` below 5 → `org.mockito:mockito-core`
  **and** `org.mockito:mockito-junit-jupiter` (which carries `MockitoExtension`), both at Mockito
  5.x through a `mockito.version` property this pom (or the parent) defines — Mockito has no BOM.

A dependency you add or rename in a module must be managed somewhere the reactor can see — the
parent's `<dependencyManagement>`, an imported BOM that covers its group — or carry a `<version>`
here. A versionless dependency nothing manages fails Maven's model build (on AMS: the web pom's
`log4j-slf4j-impl` renamed to `log4j-slf4j2-impl` while the parent still managed only the old
artifact). If you rename an artifact in a child, say so in `manual_flags`: the parent's unit
manages the old coordinates and has to manage the new ones.

Rule 4 — Apply every coordinate in the `upgrades` list supplied to you, as
`group:artifact:version`. Set that exact version — in `<dependencyManagement>` if the artifact is
managed there, otherwise on the dependency itself. If the artifact is absent from this pom, do
nothing: it belongs to another module. An upgrade whose artifact you cannot find is reported in
`manual_flags`, never invented.

Rule 5 — Jakarta EE 10 coordinates:
- `javax.servlet:javax.servlet-api` → `jakarta.servlet:jakarta.servlet-api` 6.0.0 (`provided`)
- `javax.servlet:jstl` → `jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api` 3.0.0 plus the
  `org.glassfish.web:jakarta.servlet.jsp.jstl` 3.0.1 implementation (the API alone does not render)
- `javax.annotation:javax.annotation-api` → `jakarta.annotation:jakarta.annotation-api` 2.1.1
- `javax.validation:validation-api` → `jakarta.validation:jakarta.validation-api` 3.0.2
- `javax.xml.bind:jaxb-api` → `jakarta.xml.bind:jakarta.xml.bind-api` 4.0.x plus
  `org.glassfish.jaxb:jaxb-runtime` — JAXB left the JDK, and the API without the runtime compiles
  and fails at startup
- `javax.persistence:persistence-api` → `jakarta.persistence:jakarta.persistence-api` 3.1.0

Rule 6 — Plugins that must move for Java 21 to build at all:
- `maven-compiler-plugin` 3.11+; `maven-surefire-plugin` 3.2+ (older surefire does not discover
  JUnit 5); `maven-failsafe-plugin` to match; `maven-war-plugin` 3.4+; `maven-ear-plugin` 3.3+;
  `jacoco` 0.8.11+ (earlier versions cannot read Java 21 class files and fail the build);
  `aspectj` 1.9.22+ if present (1.7/1.8 cannot weave Java 21 bytecode).

Rule 7 — Packaging. **Packaging does not change.** A `war` module stays `war`; an `ear` module
stays `ear`; the module list is preserved. What changes is the container the WAR targets, per the
`container` decision. The platform default is `tomcat`:

- **`tomcat`** — Apache Tomcat **10.1**, the Servlet 6 / Jakarta EE 10 line (Tomcat 9 and earlier
  are `javax.*` and cannot run this WAR). A servlet container only, so:
  - **Remove everything that exists only to run the previous application server:** its Maven
    plugin — `liberty-maven-plugin`, `was-maven-plugin`, `weblogic-maven-plugin`,
    `wildfly-maven-plugin`, in `plugins` and in `pluginManagement` — its runtime artifacts
    (`io.openliberty:*`, `com.ibm.websphere.appserver.runtime:*`), the properties only they read
    (`liberty.*`, `openliberty.*`), and any profile whose only job is running that server. A
    profile that also carries other settings keeps them; only its server-runtime parts go. This
    overrides Rule 8's "preserve profiles" for those parts, and every removal is named in
    `manual_flags`. Do not add a Tomcat Maven plugin in their place.
  - Servlet, JSP and EL APIs stay `provided`: Tomcat supplies those.
  - **Tomcat does not ship JSTL.** In the WAR module, bundle both
    `jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api` 3.0.0 and the
    `org.glassfish.web:jakarta.servlet.jsp.jstl` 3.0.1 implementation at compile scope — the API
    alone does not render, and a `provided` JSTL fails at the first JSP.
  - **The JDBC driver is `provided` only when the container owns the DataSource** — a JNDI
    `<Resource>` in the WAR's `META-INF/context.xml` (the `tomcat-context-config` pack writes it),
    in which case Tomcat loads the driver from `$CATALINA_BASE/lib` and you flag that it must be
    installed there. When the application builds its own pool, the driver stays at compile scope.
  - Tomcat has no JTA manager and no CDI container: if the build carries either, flag it.
  - A comment that says the application deploys to the previous application server now says
    Tomcat 10.1.
- **`jetty`** — servlet container only; the same scopes as `tomcat`: servlet, JSP and EL
  `provided`, JSTL API and implementation bundled, the JDBC driver `provided` only when the
  container owns the DataSource. Anything the old app server provided (JNDI DataSource, JTA
  manager, connection pool, mail session) becomes the application's responsibility. Flag each
  `resource-ref`; do not invent a replacement in the pom.
- **`wildfly`** — uplift `wildfly-maven-plugin` to an EE 10 capable version; keep the deployment
  configuration as-is.

Scope discipline, which decides whether the WAR starts at all: every API the container supplies
stays `<scope>provided</scope>` — servlet, JSP, EL and annotation on `tomcat` and `jetty`; those
plus JSTL, CDI, persistence and transaction on `wildfly`, a full Jakarta EE 10 server — and the
**JDBC driver becomes `provided` too** when the container owns the DataSource (a JNDI resource in
`META-INF/context.xml` on Tomcat, a datasource subsystem entry on WildFly). Bundling any of these
into `WEB-INF/lib` produces a classloader conflict that surfaces as `LinkageError` or
`ClassCastException` at deploy time, not at build time. The converse holds as well: what the
container does not supply — JSTL on Tomcat and Jetty — must be bundled, or the JSPs fail at first
render.

Rule 8 — Preserve, exactly: `groupId`, `artifactId`, `version`, `<modules>` order, `<profiles>`,
`<repositories>`, `<distributionManagement>`, every existing explanatory comment, and the version
of any dependency not covered by the rules above. A pinned version with a comment explaining the
pin is a decision someone already made — keep both.

Respond ONLY with valid JSON:
{"files": {"<path>": "<full content>"}, "deleted_files": [], "manual_flags": [...]}

## review

Score on 5 checks (total 100).

Check 1 — Java 21 and BOM coherence (25 pts):
`maven.compiler.release` is 21 and old source/target properties are gone. The Spring Framework,
Spring Security, Jackson and JUnit BOMs are imported, and **no hand-pinned version remains for
anything they manage**. A surviving pinned Spring or Jackson version scores 0 — it silently
overrides the BOM and reintroduces version skew. **A `<dependencyManagement>` entry with no
`<version>` scores 0** — it shadows the BOM's version with none and Maven will not read the pom.
A `<version>` that references a property this pom does not define (and that is not a Maven built-in like `${project.version}`) scores 0 for this check — the model build fails before compilation.
**Any `org.springframework.boot` coordinate or
`spring-boot-maven-plugin` scores 0 for this check** — Boot is not part of this target.

Check 2 — Jakarta coordinates complete (20 pts):
Every EE coordinate moved to its `jakarta.*` groupId at a Jakarta EE 10 version, and every API
that needs a separate runtime (JSTL, JAXB) has it. Partial credit per dependency.

Check 3 — Plugins can build Java 21 (20 pts):
compiler, surefire, failsafe, jacoco, war/ear and aspectj at versions that function on 21.
Surefire below 3.0 or jacoco below 0.8.11 scores 0 — the build cannot run.

Check 4 — Eliminations and upgrades exact (20 pts):
Every coordinate on the supplied `eliminates` list is gone, every coordinate on `upgrades` sits at
exactly the requested version, and **nothing outside those lists changed**. Removing an extra
dependency scores 0 — it breaks the build gate for a pack that has not run yet. An `upgrades`
entry left at its old version also scores 0: the pack that requested it is about to migrate code
against an API that is not on the classpath. So does removing `junit:junit` or `mockito-all`
without `junit-jupiter`, `mockito-core` 5.x and `mockito-junit-jupiter` in their place — the
migrated tests cannot compile.

Check 5 — Packaging and scopes preserved (15 pts):
Packaging type is unchanged (`war` stays `war`, `ear` stays `ear`), the module list and its order
are intact, and every container-supplied API remains `provided`. "Container-supplied" follows the
`container` decision: on `tomcat` and `jetty` it is servlet, JSP, EL and annotation, plus the JDBC
driver when a JNDI resource in `META-INF/context.xml` owns the DataSource — and JSTL is **not**
supplied there, so a bundled JSTL API + implementation is correct and a `provided` one is the
error; on `wildfly` JSTL, CDI, persistence, transaction and the driver are container-supplied too.
A bundled container API scores 0: it deploys and then fails with `LinkageError`. Coordinates,
profiles, repositories, unrelated dependency versions and existing comments preserved — except the
previous application server's runtime parts Rule 7 removes on `tomcat`, whose removal is correct
when each is named in `manual_flags`.

Checks that do not apply: a check that does not apply to this file earns its full points. A check
applies when the file contains what it is about, or when this file is where the transform had to
introduce it; it does not apply when there is nothing here for it to judge (a check about Java
code, on a descriptor that holds none). Name the checks that did not apply in `feedback`. Never
score a check 0 for having nothing to examine: 0 is for a subject that is present and wrong, or
missing where this file had to supply it. **Report a check that does not apply as the string
`"n/a"` in `checks`** — not 0, not a guessed number; it is scored at its full points for you, and
`score` is recomputed from `checks`. A pom that declares no dependency of a kind, no plugin, or
nothing on the `eliminates`/`upgrades` lists has nothing for that check to examine.

Parent and child modules: score only what this pom itself declares. A `=== CONTEXT: reactor ===`
block, when given, shows the parent chain **as it was before this pack ran** — each parent pom is
migrated by its own unit in the same run — with its properties, BOM imports and managed versions.
Read it for what this pom inherits (a property it references, a version a parent BOM manages, a
plugin the parent pins) and never deduct for a parent's pre-migration values: those are the
parent's unit to fix, not this file's. A child module that inherits `maven.compiler.release`, the BOM imports or plugin
versions from its `<parent>` satisfies Checks 1 and 3 for those settings: award the points and say
they are inherited, because the transform puts them in the parent on purpose. What the child does
declare is still judged — its own leftover `maven.compiler.source`/`target`, a version it pins for
something a BOM manages, or a plugin it configures at an old version scores as the check says.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"java_and_bom": <0-25>, "jakarta_coords": <0-20>, "plugins_java21": <0-20>,
            "eliminations_exact": <0-20>, "nothing_else": <0-15>}}
