# FORGE — Enterprise J2EE Migration Platform

FORGE migrates any J2EE/Jakarta application in the enterprise, from a single-module WAR to a
multi-module EAR, using the **same engine**. What differs between projects is never code — it is
which **stack packs** discovery activates.

**The target is a WAR on WebSphere/Open Liberty at Jakarta EE 10 — Spring Framework 6.2 without
Spring Boot.** This is a deliberate platform constraint, not a per-project decision: no embedded
server, no executable jar, no Boot autoconfiguration, no `spring-boot-*` coordinate anywhere in
the output. Dependency versions are aligned by the Spring Framework, Spring Security, Jackson and
JUnit BOMs instead.

Liberty being a **full Jakarta EE 10 profile** is what makes this the low-risk target. JNDI,
JTA, managed connection pools, security registries and shared-library classloading all remain
container responsibilities, exactly as they are on the app server the application runs on today.
Nothing that the container used to provide has to be reimplemented inside the application — which
is the failure mode of migrating a JEE app onto a bare servlet container.

Three things are kept strictly separate:

| Layer | What it is | Lives in | Changes per project? |
|---|---|---|---|
| **Engine** | Discovery, planning, transform/review loop, build gate, reconcile | `forge/` (Python) | Never |
| **Stack packs** | One technology transition each — detection, rules, rubric, acceptance | `prompts/packs/*.pack.md` | Never (enterprise-wide, versioned) |
| **Project profile** | What this app actually is, and the decisions taken for it | `forge-profile.yaml` (generated, then edited) | Always |

A new technology to support is a **new pack file**, not a code change. A new project to migrate is
a **discovery run**, not a configuration exercise.

---

## 1. The pack contract

Every file in `prompts/packs/` is a stack pack: YAML frontmatter plus a `## transform` and a
`## review` section. `forge/packs/loader.py` reads them at startup.

```yaml
---
id: struts2-to-springmvc6        # unique, kebab-case
version: 1.0.0                   # semver; pinned per project in the profile
title: Struts 2 -> Spring MVC 6
tier: framework                  # language | build | namespace | framework | view | persistence | test | platform

detect:                          # ANY match activates the pack
  any:
    - dependency: "org.apache.struts:struts2-core"
    - file_glob: "**/struts*.xml"
    - import_prefix: "com.opensymphony.xwork2"

applies_to:                      # which files this pack transforms
  - file_glob: "**/struts*.xml"
  - content_match:               # decidable from one file's bytes — no extractor
      glob: "**/*.java"
      pattern: 'WebSecurityConfigurerAdapter|@EnableWebSecurity'
  - selector: struts_actions     # named selector resolved by the context extractor

context: struts_routing_table    # deterministic extractor that must run before transform
depends_on: [javax-to-jakarta, spring-to-spring6]
decisions: [url_compat]          # profile keys this pack reads
eliminates:                      # coordinates the build pack must remove
  - "org.apache.struts:*"
upgrades:                        # coordinates the build pack must bump, group:artifact:version
  - "org.apache.struts:struts2-core:7.3.0"

acceptance:                      # mechanical, post-migration; no model involved
  - no_match: "org\\.apache\\.struts|com\\.opensymphony"
    scope: "src/**/*.java"
  - routing_parity: true
---
```

### Field rules

- **`detect.any` fires on repository evidence; `decision_equals` rules are gates.** A decision
  describes the target, not the repository, so it can never activate a pack by itself — every
  decision rule a pack declares must hold *and* at least one other rule must fire. Otherwise an
  empty directory would "need" the Liberty pack because the config names Liberty.
- **`detect`** must be decidable **without an LLM** — dependency coordinates, file globs, import
  prefixes, XML doctypes, descriptor element names. Detection is evidence, not opinion.
- **`applies_to`** takes three kinds. A `file_glob` names files by path. A `content_match`
  (`{glob, pattern}`) names them by what is in them — "the class that extends
  `WebSecurityConfigurerAdapter`" is a real file set with no filename pattern, and it is decidable
  from one file's own bytes, so a pack using it needs no extractor and stays runnable. A
  `selector` names a set only a context extractor can resolve, and blocks the pack until that
  extractor exists. **Prefer `content_match` to a `selector` wherever the question is answerable
  from a single file** — the difference is whether the pack runs today.
- **`acceptance`** checks may carry `when: {decision: value}`, so a check that is right on one
  route is not applied on the other. "No Struts tag remains" is correct when the framework is
  being replaced and wrong when it is being upgraded.
- **`context`** names a deterministic extractor. A pack that needs cross-file facts (a routing
  table, an EJB bean table, a faces navigation graph) declares it here and the engine guarantees it
  is populated before any model call. **A pack whose rules reference facts it did not declare is
  a bug** — the loader rejects it.
- **`depends_on`** is an **ordering edge, not a requirement**. It fixes the order of two packs when
  both are active and says nothing when only one is — `jsp-jstl-modernize` lists both Struts packs
  because it must follow whichever is active, and a Struts 2 project never activates the Struts 1
  one. The planner topologically sorts activated packs; a cycle is a startup error, and an edge
  pointing outside the activated set is reported, not enforced.
- **`eliminates`** and **`upgrades`** are how a pack tells the build pack what its technology
  needs — remove this coordinate, or move that one to this version. A pack never edits a build
  file itself, so two packs can never fight over the same `pom.xml`.
- **`acceptance`** checks run after the migration and are how "done" is decided. Model scores gate
  individual files; acceptance checks gate the **project**.
- **`review` weights must total 100.** The loader asserts it, because `pass_threshold` is
  meaningless otherwise.

### Tiers and default ordering

Packs are ordered by `depends_on`, with tier as the tie-break:

```
build → language → namespace → persistence → framework → view → test → platform
```

Rationale: nothing compiles until the build file targets the right Java level, and no framework
rewrite is verifiable until the namespace is consistent.

---

## 2. Pack library

Status is honest: **complete** packs are written and reviewable; **detect-only** packs are
registered so discovery reports them and the plan shows the gap, rather than silently ignoring the
technology.

| Pack | Tier | Status |
|---|---|---|
| `build-maven-modernize` | build | complete |
| `java8-to-java21` | language | complete |
| `javax-to-jakarta` | namespace | complete |
| `spring-to-spring6` | framework | complete |
| `springsec-to-springsec6` | framework | complete |
| `struts2-modernize` | framework | complete |
| `struts2-to-springmvc6` | framework | complete |
| `struts1-to-springmvc6` | framework | complete |
| `jsp-jstl-modernize` | view | complete |
| `junit4-to-junit5` | test | complete |
| `webapp-bootstrap-jakarta10` | platform | complete |
| `liberty-server-config` | platform | complete |
| `ejb2-to-spring` | framework | detect-only |
| `ejb3-to-spring` | framework | detect-only |
| `jsf-to-faces4` | view | detect-only |
| `hibernate-to-hibernate6` | persistence | detect-only |
| `ibatis-to-mybatis` | persistence | detect-only |
| `jaxrs-to-jakarta-rs` | framework | detect-only |
| `jms-to-spring-jms` | framework | detect-only |
| `ant-to-maven` | build | detect-only |

---

## 3. Global invariants

Enforced for every pack, every project. A violation is MANUAL_REVIEW regardless of review score.
Packs may add invariants; they may never relax these.

1. **Business logic is preserved verbatim.** Conditionals, ordering, transaction boundaries, error
   handling, null checks and data access are behaviourally identical. Where the original is
   ambiguous, emit `// TODO(migration): <question>` — never guess.
2. **No package is ever renamed.** Renaming breaks every import, component-scan base package and
   reflective lookup. The declared package is read, never rewritten.
3. **Zero Jakarta-EE `javax.*` in output — and the JDK's own `javax.*` is never touched.**
   Migrate: `javax.servlet`, `persistence`, `validation`, `annotation` (EE subset), `transaction`,
   `ejb`, `enterprise`, `faces`, `el`, `jms`, `mail`, `ws.rs`, `websocket`, `interceptor`,
   `xml.bind`, `xml.soap`, `xml.ws`.
   **Leave alone:** `javax.sql`, `javax.crypto`, `javax.net`, `javax.naming`, `javax.security.auth`,
   `javax.xml.parsers`, `javax.xml.transform`, `javax.xml.stream`, `javax.xml.xpath`,
   `javax.imageio`, `javax.swing`, `javax.management`, `javax.script`, `javax.tools`,
   `javax.annotation.processing`, `javax.lang.model`.
   A blanket `javax.xml` carve-out is wrong — `javax.xml.bind` **is** Jakarta.
4. **Security posture may only tighten, never loosen.** Any change to an authorization rule, a
   permitted-path set, a role check or a CSRF/token control is a security change and must be
   reported explicitly, never absorbed into a refactor.
5. **A file the model creates must be reachable.** A transform that emits a class nothing wires up
   is incomplete, not a partial success.
6. **No Spring Boot.** The output is a WAR built against Spring Framework 6.2 and deployed to a
   Jakarta EE 10 servlet container. No `org.springframework.boot` coordinate, no
   `spring-boot-maven-plugin`, no `SpringBootServletInitializer`, no `@SpringBootApplication`,
   no `application.yml` autoconfiguration. A pack that reaches for Boot to solve a problem has
   chosen the wrong solution — the non-Boot equivalent always exists.
7. **Nothing is silently dropped.** Deleted endpoints, removed tests, skipped files and retired
   descriptors are all enumerated in `migration-report.md`.

---

## 4. Decisions

Decisions are the parameters a pack reads from the project profile. They are the only project-
specific input, and they live in `forge-profile.yaml` — never in a pack, never in code.

| Key | Values | Read by | Default |
|---|---|---|---|
| `web_framework` | `modernize-in-place` · `migrate-to-spring` | `struts*-modernize`, `struts*-to-springmvc6` | `modernize-in-place` |
| `runtime` | `war-xml-bootstrap` · `war-programmatic-bootstrap` | `webapp-bootstrap-jakarta10`, `build-maven-modernize` | `war-xml-bootstrap` |
| `container` | `liberty` *(platform standard)* · `wildfly` · `tomcat` · `jetty` | `webapp-bootstrap-jakarta10`, `liberty-server-config`, `build-maven-modernize` | `liberty` |
| `liberty_edition` | `open` · `websphere` | `liberty-server-config` | `websphere` |
| `liberty_features` | `jakartaee-10.0` · `webProfile-10.0` · `granular` | `liberty-server-config` | `granular` |
| `views` | `in-place` · `thymeleaf` · `defer` | `jsp-jstl-modernize`, `jsf-to-faces4` | `in-place` |
| `url_compat` | `preserve-with-redirect` · `preserve-exact` · `clean-only` | `struts*-to-springmvc6` | `preserve-with-redirect` |
| `persistence` | `keep-orm` · `to-spring-data` | `hibernate-to-hibernate6`, `ejb*-to-spring` | `keep-orm` |
| `idiom_aggressiveness` | `conservative` · `moderate` | `java8-to-java21` | `conservative` |
| `risk_ceiling` | `auto` · `review-high` · `review-all` | engine | `review-high` |

`web_framework` picks between two mutually exclusive routes through the same portfolio, and the
order matters more than it looks. `modernize-in-place` upgrades the framework to its current
release — a per-file change with no cross-file context, so it runs on today's engine and gets the
application onto Java 21 and Jakarta EE 10 without changing a single URL. `migrate-to-spring`
replaces the framework, which needs the routing table and therefore the extract stage.

Doing them in that order is not a compromise, it is the cheaper path: once an application is on
Struts 7, it is already on Java 21, Jakarta EE 10 and Liberty, and the eventual Spring migration
is a framework change **alone** rather than four changes at once. Activating both for one project
is a configuration error — they edit the same files toward different targets.

`risk_ceiling` is what makes the platform work at both ends of the size range: `auto` for a
20-file WAR where a failed build is the only gate that matters, `review-all` for a payments
system where every diff needs a human.

---

## 5. Project profile

Discovery generates this; a human edits the decisions and commits it next to the project.

```yaml
# forge-profile.yaml — generated by `forge discover`, then edited
project: acme-orders
source_root: /src/acme-orders
target:
  java: "21"
  spring: "6.2"
  jakarta: "10"

# Detected, with the evidence that triggered each pack. Remove a line to skip that pack.
packs:
  - build-maven-modernize@1.0.0    # evidence: 4x pom.xml, maven.compiler.source=1.8
  - java8-to-java21@1.0.0          # evidence: maven.compiler.source=1.8
  - javax-to-jakarta@1.0.0         # evidence: 31 javax.servlet imports
  - spring-to-spring6@1.0.0        # evidence: org.springframework:spring-core:5.3.39
  - springsec-to-springsec6@1.0.0  # evidence: WebSecurityConfigurerAdapter
  - struts2-to-springmvc6@1.0.0    # evidence: struts2-core:6.8.0, 15x struts*.xml
  - jsp-jstl-modernize@1.0.0       # evidence: 116 JSP, 57 java.sun.com JSTL URIs
  - junit4-to-junit5@1.0.0         # evidence: junit:junit:4.12, mockito 1.9.5
  - webapp-bootstrap-jakarta10@1.0.0 # evidence: WEB-INF/web.xml v3.1
  - liberty-server-config@1.0.0    # evidence: container decision = liberty

decisions:
  runtime: war-xml-bootstrap
  container: liberty
  liberty_edition: websphere
  liberty_features: granular
  views: in-place
  url_compat: preserve-with-redirect
  idiom_aggressiveness: conservative
  risk_ceiling: review-high

scope:
  package_prefix: "com.acme"       # "is this ours to migrate?" — never a rename
  exclude_globs: ["**/generated/**", "**/vendor/**"]
```

---

## 6. Acceptance

A project is migrated when every activated pack's `acceptance` checks pass **and** the reactor
builds. These are mechanical — `forge verify` runs them with no model involved.

| Check kind | Meaning |
|---|---|
| `no_match: <regex>` | The regex must not match anywhere in `scope`. Retired frameworks, dead imports. |
| `count_unchanged: <regex>` | The match count must equal the pre-migration count. Guards the JDK `javax.*` carve-out — a drop means a JDK package was wrongly rewritten. |
| `routing_parity` | Every pre-migration endpoint maps to exactly one post-migration handler, and vice versa. Unmatched entries in either direction fail. |
| `test_parity` | Same number of test methods; any removed test is `@Disabled` with a reason. |
| `authz_parity` | The authorization rule set is byte-identical, or every difference is acknowledged in the report. |
| `build: <command>` | The reactor compiles and tests under the target JDK. |

Acceptance failure is a **project** failure, independent of how well individual files scored. A
run where every file scored 95 and `routing_parity` fails has lost endpoints, and the report says
so in those words.
