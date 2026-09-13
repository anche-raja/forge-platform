---
id: javax-to-jakarta
version: 1.0.0
title: javax.* -> jakarta.* (Jakarta EE 10)
tier: namespace
detect:
  any:
    - import_prefix: "javax.servlet"
    - import_prefix: "javax.persistence"
    - import_prefix: "javax.validation"
    - import_prefix: "javax.ejb"
    - import_prefix: "javax.jms"
    - import_prefix: "javax.ws.rs"
    - import_prefix: "javax.xml.bind"
    - dependency: "javax.servlet:javax.servlet-api"
applies_to:
  - file_glob: "**/*.java"
context: none
depends_on: []
decisions: []
eliminates:
  - "javax.servlet:javax.servlet-api"
  - "javax.annotation:javax.annotation-api"
  - "javax.validation:validation-api"
acceptance:
  - no_match: "^import javax\\.(servlet|persistence|validation|transaction|ejb|enterprise|faces|el|jms|mail|ws\\.rs|websocket|interceptor|annotation\\.(Resource|PostConstruct|PreDestroy)|xml\\.(bind|soap|ws))"
    scope: "**/*.java"
  - count_unchanged: "^import javax\\.(sql|crypto|net|naming|security\\.auth|xml\\.(parsers|transform|stream|xpath)|imageio|swing|management|script|tools|lang\\.model|annotation\\.processing)"
    scope: "**/*.java"
---

## transform

You are migrating Java source from the `javax.*` namespace to Jakarta EE 10's `jakarta.*`.

This is a mechanical rename with **one trap**, and the trap is the whole difficulty: the JDK also
ships packages under `javax.*`, and rewriting one of those does not fail loudly — it fails at
compile time in a way that looks like a missing dependency, or worse, it silently resolves to
nothing and the build error points at an unrelated file.

Rule 1 — MIGRATE these to `jakarta.*`:
`javax.servlet`, `javax.persistence`, `javax.validation`, `javax.transaction`, `javax.ejb`,
`javax.enterprise`, `javax.inject`, `javax.faces`, `javax.el`, `javax.jms`, `javax.mail`,
`javax.ws.rs`, `javax.websocket`, `javax.interceptor`, `javax.batch`, `javax.json`,
`javax.security.enterprise`, `javax.xml.bind`, `javax.xml.soap`, `javax.xml.ws`,
`javax.resource`, `javax.jws`.

Rule 2 — `javax.annotation` is SPLIT. Only the EE subset moves:
- MIGRATE: `@Resource`, `@PostConstruct`, `@PreDestroy`, `@Generated`, `@Priority`
- LEAVE: `javax.annotation.processing.*` (JDK annotation processing API)

Rule 3 — `javax.xml` is SPLIT, and this is the most common mistake:
- MIGRATE: `javax.xml.bind` (JAXB), `javax.xml.soap`, `javax.xml.ws`
- LEAVE: `javax.xml.parsers`, `javax.xml.transform`, `javax.xml.stream`, `javax.xml.xpath`,
  `javax.xml.validation`, `javax.xml.namespace`, `javax.xml.datatype`, `javax.xml.XMLConstants`
A blanket `javax.xml` rule is always wrong in one direction or the other.

Rule 4 — NEVER TOUCH. These are JDK packages with no Jakarta equivalent:
`javax.sql`, `javax.crypto`, `javax.net`, `javax.naming`, `javax.security.auth`, `javax.imageio`,
`javax.swing`, `javax.management`, `javax.script`, `javax.tools`, `javax.lang.model`,
`javax.print`, `javax.sound`, `javax.accessibility`, `javax.rmi`, `javax.smartcardio`,
`javax.transaction.xa` (JDBC XA — note this differs from `javax.transaction`).

Rule 5 — Rename every occurrence, not just imports: fully-qualified names in code, string
literals used for reflection (`Class.forName("javax.servlet...")`), XML descriptor references,
`persistence.xml` / `beans.xml` schema namespaces, and `web.xml` schema URIs.

Rule 6 — API changes that ride along with the rename, which a pure find-and-replace misses:
- `HttpServletRequest.getRealPath()` was removed before Servlet 6 — use
  `getServletContext().getRealPath()`
- `javax.servlet.http.HttpSessionContext` no longer exists
- JAXB moved out of the JDK: the rename must be accompanied by a `jakarta.xml.bind-api` +
  `org.glassfish.jaxb:jaxb-runtime` dependency, or the code compiles and fails at runtime
- `@WebServlet`/`@WebFilter` attribute semantics are unchanged; the annotations are not

Respond ONLY with valid JSON — no markdown fences:
{"files": {"<path>": "<full content>"}, "deleted_files": [], "manual_flags": [{"file":"<p>","line":<n>,"reason":"<why>"}]}

## review

Score the migrated file on 5 checks (total 100).

Check 1 — Jakarta EE namespace complete (30 pts):
No Jakarta-EE `javax.*` import, fully-qualified reference, or string literal remains. Full 30 or 0.

Check 2 — JDK packages untouched (30 pts):
`javax.sql`, `javax.crypto`, `javax.naming`, `javax.xml.parsers`, `javax.xml.transform`,
`javax.annotation.processing` and every other JDK `javax.*` is still `javax.*`. **Rewriting any
one of these scores 0 for the whole check** — it breaks the build, which is worse than an
incomplete migration.

Check 3 — Split packages handled correctly (20 pts):
`javax.annotation.@PostConstruct` moved but `javax.annotation.processing` did not.
`javax.xml.bind` moved but `javax.xml.parsers` did not. Partial credit per package.

Check 4 — Ride-along API changes (10 pts):
Removed Servlet APIs replaced, JAXB runtime dependency flagged where `jakarta.xml.bind` is used.

Check 5 — No collateral change (10 pts):
Nothing but the namespace changed. No reformatting, no logic edits, no import reordering beyond
what the rename requires.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"jakarta_complete": <0-30>, "jdk_untouched": <0-30>, "split_packages": <0-20>,
            "ride_along_apis": <0-10>, "no_collateral": <0-10>}}
