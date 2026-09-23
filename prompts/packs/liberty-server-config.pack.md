---
id: liberty-server-config
version: 1.0.0
title: App-server config -> Liberty server.xml + IBM bindings
tier: platform
detect:
  any:
    # decision_equals is a gate, not evidence: the pack applies to a web
    # application (below) only when the target container is Liberty.
    - decision_equals: {key: "container", value: "liberty"}
    - file_glob: "**/WEB-INF/web.xml"
    - file_glob: "**/server.xml"
    - file_glob: "**/WEB-INF/ibm-web-*.xmi"
    - file_glob: "**/WEB-INF/ibm-web-*.xml"
applies_to:
  - selector: server_config
  - file_glob: "**/WEB-INF/ibm-web-*.xmi"
  - file_glob: "**/WEB-INF/ibm-web-*.xml"
context: web_bootstrap
depends_on: [webapp-bootstrap-jakarta10]
decisions: [container, liberty_edition, liberty_features]
eliminates: []
acceptance:
  - no_match: '\.xmi$'
    scope: "**/WEB-INF/ibm-web-*"
  - build: "mvn -q -DskipTests package"
---

## transform

You are producing the **Liberty server configuration** for an application being migrated to
Jakarta EE 10 on WebSphere Liberty or Open Liberty. The two are the same runtime core; the
`liberty_edition` decision only changes which features are available and the Maven plugin's
runtime artifact.

Unlike every other pack, you are mostly **generating a new file rather than transforming one**.
`server.xml` is assembled from the union of what the old descriptors declared: `web.xml`, the
vendor descriptor (`ibm-web-bnd`, `weblogic.xml`, `jboss-web.xml`), the datasource configuration,
and the shared libraries the app server provided.

**You are given** the parsed descriptors, the resolved dependency set, and the list of resources
the application expects by JNDI name.

Rule 1 — Features. Liberty loads nothing it is not told to load; a missing feature is a runtime
`ClassNotFoundException`, not a config warning. Per `liberty_features`:
- `granular` (default, preferred): enable only what the application uses. Start from the
  descriptor evidence, not from a template.
  ```
  servlet-6.0      always, for a WAR
  pages-3.1        if any .jsp exists — this also provides JSTL 3.0
  expressionLanguage-5.0  pulled in by pages, declare it if used directly
  cdi-4.0          if beans.xml or any jakarta.enterprise.* use
  persistence-3.1  if persistence.xml or jakarta.persistence.* use
  jdbc-4.3         if any dataSource is declared
  transaction-2.0  if JTA / UserTransaction is used
  appSecurity-5.0  if web.xml had security-constraint or login-config
  restfulWS-3.1    if jakarta.ws.rs is used
  jsonb-3.0 / jsonp-2.1   if Jakarta JSON is used
  mail-2.1         if a mail session is declared
  messaging-3.1    if JMS is used
  ```
- `webProfile-10.0` / `jakartaee-10.0`: the convenience umbrellas. They start slower and enable
  features the application does not use, which widens the attack surface. Use only when asked.

Do **not** enable `springBoot-3.0`. There is no Spring Boot in this target.

Rule 2 — Application declaration:
```xml
<webApplication id="app" location="app.war" contextRoot="/ctx">
  <classloader delegation="parentLast" commonLibraryRef="appLibs"/>
</webApplication>
```
The context root comes from the vendor descriptor (`ibm-web-ext` `contextRoot`, WebLogic
`context-root`, JBoss `context-root`) or from the EAR's `application.xml`. **Preserve it exactly** —
it is in every bookmark, every integration and every reverse-proxy rule.

Rule 3 — Classloading. This is the setting that most often decides whether the WAR starts:
- `prefer-web-inf-classes` (WebSphere traditional), `parent-last` (WebLogic/JBoss) →
  `<classloader delegation="parentLast"/>`
- Default Liberty delegation is `parent` (server first). A Spring 6 application bundling its own
  jars usually needs `parentLast`, because Liberty's own feature classes would otherwise win.
- Shared libraries the app server provided become a `<library>` with a `<fileset>`, referenced by
  `commonLibraryRef`. **A shared library that was provided by the old server and is not declared
  here simply is not there at runtime.** List every one you find in `manual_flags`.

Rule 4 — Data sources. Liberty supplies the connection pool, so the application keeps looking the
DataSource up by JNDI and nothing in the Java code changes:
```xml
<library id="OracleLib"><fileset dir="${shared.resource.dir}" includes="ojdbc*.jar"/></library>
<dataSource jndiName="jdbc/AppDS" transactional="true">
  <jdbcDriver libraryRef="OracleLib"/>
  <properties.oracle URL="${db.url}" user="${db.user}" password="${db.password}"/>
  <connectionManager maxPoolSize="50" minPoolSize="5"/>
</dataSource>
```
- **Carry the pool sizes across from the old server's configuration.** Do not invent them and do
  not accept Liberty's defaults silently — a pool sized for the old server and now defaulted to
  Liberty's smaller default will queue under production load and look like an application hang.
  Where the old sizing cannot be found, emit `TODO(migration)` with the JNDI name rather than
  guessing a number.
- Credentials go in `server.env` or a `<variable>`, never inline in a committed `server.xml`.
- The JDBC driver jar is a Liberty `<library>`, not a `WEB-INF/lib` dependency. Set the Maven
  dependency to `provided`.

Rule 5 — Security registry. `<basicRegistry>` is for development only — say so in a comment.
Production uses `<ldapRegistry>` or the federated registry, and the bind credentials belong in
`server.env`. Map role-to-group bindings from `ibm-application-bnd` / `ibm-web-bnd`
`<security-role><group name="..."/></security-role>` onto the equivalent, and preserve every
role name verbatim: the names are referenced by Spring Security and by `@RolesAllowed`.

Rule 6 — IBM binding and extension files. If the source is traditional WebSphere, these arrive as
`.xmi` and Liberty reads the `.xml` form:
- `ibm-web-ext.xmi` → `ibm-web-ext.xml` (context root, `reloadingEnabled`, `fileServingEnabled`,
  `directoryBrowsingEnabled`, `serveServletsByClassnameEnabled`)
- `ibm-web-bnd.xmi` → `ibm-web-bnd.xml` (virtual host, resource-ref bindings, role bindings)
Emit the `.xml` form and list the `.xmi` in `deleted_files`. **`serveServletsByClassnameEnabled`
must not be carried across as `true`** — it lets any servlet be invoked by class name and is a
known exposure; if it was on, flag it rather than reproducing it.

Rule 7 — Ancillary files, each only if the descriptors justify it:
- `jvm.options` — heap, GC and `--add-opens` flags. Java 21 strong encapsulation may require
  `--add-opens` for reflective libraries; carry across any flag the old server set.
- `server.env` — credentials and environment-specific values
- `bootstrap.properties` — Liberty variables resolved before `server.xml`
- `http` endpoint and `keyStore` config where the old server terminated TLS

Rule 8 — Preserve every value you cannot map. A setting with no Liberty equivalent goes in
`manual_flags` with its original name, its value and where it came from. An operational setting
dropped in silence is discovered in production.

Respond ONLY with valid JSON:
{"files": {"<path>": "<full content>"}, "deleted_files": ["<.xmi replaced by .xml>"], "manual_flags": [...]}

## review

Score on 5 checks (total 100).

Check 1 — Features match actual usage (25 pts):
Every Jakarta technology the application uses has its feature enabled, and nothing beyond that is
enabled under `granular`. A missing `pages-3.1` on an application with JSPs, or a missing
`appSecurity-5.0` where `web.xml` had a `security-constraint`, scores 0 — the application will not
serve. `springBoot-3.0` present scores 0.

Check 2 — Resources and pool sizing carried across (25 pts):
Every JNDI name the application looks up has a matching `<dataSource>` or resource, with the
driver supplied as a Liberty `<library>`. **Connection-pool sizes are carried from the source
configuration or explicitly flagged, never silently defaulted** — a silently defaulted pool scores
0, because it fails only under load.

Check 3 — Classloading and shared libraries (20 pts):
The delegation mode reflects what the old server used. Every shared library the old server
provided is declared or flagged. A dropped shared library scores 0.

Check 4 — Context root and security identity preserved (20 pts):
Context root byte-identical to the source. Every role name preserved, every role-to-group binding
mapped. `serveServletsByClassnameEnabled` not carried across as `true`. Credentials not inline in
`server.xml`.

Check 5 — Bindings converted and nothing dropped (10 pts):
`.xmi` bindings emitted in `.xml` form and the originals listed for deletion. Every unmappable
setting flagged with its original value and source.

Checks that do not apply: a check that does not apply to this file earns its full points. A check
applies when the file contains what it is about, or when this file is where the transform had to
introduce it; it does not apply when there is nothing here for it to judge (a check about Java
code, on a descriptor that holds none). Name the checks that did not apply in `feedback`. Never
score a check 0 for having nothing to examine: 0 is for a subject that is present and wrong, or
missing where this file had to supply it.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"features_match_usage": <0-25>, "resources_and_pooling": <0-25>,
            "classloading_libraries": <0-20>, "context_root_security": <0-20>,
            "bindings_nothing_dropped": <0-10>}}
