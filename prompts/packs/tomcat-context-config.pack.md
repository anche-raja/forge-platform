---
id: tomcat-context-config
version: 1.0.0
title: App-server config -> Tomcat 10.1 META-INF/context.xml
tier: platform
detect:
  any:
    # decision_equals is a gate, not evidence: the pack applies to a web
    # application (below) only when the target container is Tomcat.
    - decision_equals: {key: "container", value: "tomcat"}
    - file_glob: "**/WEB-INF/web.xml"
applies_to:
  - selector: tomcat_context
  - content_match:
      glob: "**/Dockerfile*"
      pattern: '(?i)liberty|websphere|/opt/ol/|/config/server\.xml'
context: web_bootstrap
depends_on: [webapp-bootstrap-jakarta10, build-maven-modernize]
decisions: [container]
eliminates: []
acceptance:
  - no_match: 'liberty-maven-plugin|io\.openliberty|openliberty-runtime'
    scope: "**/pom.xml"
    when: {container: "tomcat"}
  - no_match: '(?i)websphere-liberty|open-liberty|openliberty'
    scope: "**/Dockerfile*"
    when: {container: "tomcat"}
  - build: "mvn -q -DskipTests package"
---

## transform

You are moving a web application's **server configuration from Liberty (or another full Jakarta
EE server) to Apache Tomcat 10.1**, the Servlet 6 / Jakarta EE 10 line. Tomcat is a servlet
container: it provides JNDI, and a connection pool for each `<Resource>` it is given, but no JTA,
no CDI and no Liberty features. Everything the old server supplied must now come from the WAR's
own `META-INF/context.xml`, from Tomcat's `lib/`, or be flagged.

You are given the parsed descriptors: `web.xml` (every `resource-ref`, `resource-env-ref`,
`env-entry`, `security-constraint`), any vendor descriptor, and the existing Liberty `server.xml`
when there is one (its `dataSource`, `connectionManager`, `library`, `httpEndpoint` and
`webApplication` settings). The unit is one of two things.

**A. The generated `META-INF/context.xml`** (the target path ends in it and there is no file to
read). Produce it from the descriptors:

Rule 1 — One `<Resource>` per DataSource the application looks up. The `name` is the
`res-ref-name` **byte for byte** (`jdbc/AppDS`, not `java:comp/env/jdbc/AppDS`) — the code keeps its
JNDI lookup and nothing in Java changes:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Context>
  <Resource name="jdbc/AppDS" auth="Container" type="javax.sql.DataSource"
            driverClassName="org.h2.Driver" url="${app.db.url}"
            username="${app.db.user}" password="${app.db.password}"
            maxTotal="50" minIdle="5" maxIdle="10" maxWaitMillis="30000"
            defaultTransactionIsolation="READ_COMMITTED"/>
</Context>
```
`type="javax.sql.DataSource"` is the JDK's `javax.sql`, which stays `javax` — do not change it.

Rule 2 — Carry the pool across; never invent it. Map Liberty `connectionManager` onto Tomcat's
pool: `maxPoolSize` -> `maxTotal`, `minPoolSize` -> `minIdle`, `connectionTimeout` ->
`maxWaitMillis` (in milliseconds), `isolationLevel` -> `defaultTransactionIsolation`. Where the old
server's sizing cannot be found, write `<!-- TODO(migration): pool size for jdbc/AppDS -->` and
leave Tomcat's defaults rather than guessing a number: a pool sized wrong queues under load and
looks like an application hang.

Rule 3 — No credential or environment value inline. URLs, users and passwords are `${...}`
properties Tomcat resolves from `catalina.properties` or `-D` system properties. Name every
property the operator must set in `manual_flags`. Never copy a password, even a development one,
out of the old configuration.

Rule 4 — The driver is Tomcat's, not the WAR's: it must be in `$CATALINA_BASE/lib` (Tomcat's pool
loads it from there). Flag that, with the driver's Maven coordinate.

Rule 5 — What `context.xml` cannot hold goes in `manual_flags`, with its original value and
where it came from: the context root (Tomcat takes it from the WAR's file name — flag the file
name that preserves the old context root exactly), HTTP/HTTPS ports and TLS (Tomcat's own
`conf/server.xml`), JVM options and Liberty `bootstrap.properties` variables (`bin/setenv`),
logging, container-managed security registries, and any JTA, JMS or mail resource Tomcat does not
provide. A setting dropped in silence is discovered in production.

Rule 6 — Retire the Liberty configuration this file replaces. In `deleted_files`, list the
module's Liberty config directory files — `server.xml`, `bootstrap.properties`, `jvm.options` and
`server.env` under `src/main/liberty/config/` of **the same module as the target path** — written
the same way the target path is written (a file that does not exist is ignored). Everything they
set that matters is now either in this `context.xml` or in `manual_flags`.

**B. A Dockerfile that builds a Liberty or WebSphere image.** Rewrite it for Tomcat 10.1:
- Base image `tomcat:10.1-jdk21-temurin` (or the `jdk17` variant if the build targets 17).
- Deploy the WAR to `/usr/local/tomcat/webapps/<name>.war`, where `<name>` keeps the old context
  root; remove every Liberty step (`configure.sh`, `features.sh`, `COPY ... /config/`,
  `/opt/ol/`, `/opt/ibm/wlp/`).
- Copy the JDBC driver jar into `/usr/local/tomcat/lib/`.
- Carry `ENV`, `EXPOSE` (Tomcat listens on 8080) and health checks across; flag a port change.
- Keep every other build stage as it is.

Respond ONLY with valid JSON:
{"files": {"<path>": "<full content>"}, "deleted_files": ["<retired Liberty config files>"], "manual_flags": [...]}

## review

Score on 5 checks (total 100).

Check 1 — Every JNDI resource provided (30 pts):
Each DataSource the application looks up (every `resource-ref` of a DataSource type) has a
`<Resource>` whose `name` matches the `res-ref-name` exactly, `auth="Container"`,
`type="javax.sql.DataSource"`, and a driver class. A missing or misnamed resource scores 0: the
lookup fails at startup. For a Dockerfile, this check is about the driver jar reaching
`/usr/local/tomcat/lib/`.

Check 2 — Pool carried across, never invented (25 pts):
Pool sizes, wait time and isolation come from the old configuration, or carry a
`TODO(migration)` naming the resource. A number with no source scores 0.

Check 3 — No secret inline (20 pts):
URLs, users and passwords are `${...}` properties and every one is listed in `manual_flags`. An
inline password — any value, development or not — scores 0.

Check 4 — Nothing dropped silently (15 pts):
Context root, ports, TLS, JVM options, bootstrap variables, security registries and any resource
Tomcat does not provide are each flagged with their original value and source.

Check 5 — Liberty retired (10 pts):
The generated file lists the module's Liberty config files in `deleted_files`; a Dockerfile keeps
no Liberty or WebSphere base image or step, and deploys to Tomcat's `webapps/`.

Checks that do not apply: a check that does not apply to this file earns its full points. A check
applies when the file contains what it is about, or when this file is where the transform had to
introduce it; it does not apply when there is nothing here for it to judge. Name the checks that
did not apply in `feedback`. Never score a check 0 for having nothing to examine: 0 is for a
subject that is present and wrong, or missing where this file had to supply it.

The unit is judged against the PROJECT DECISIONS given with it: `container: tomcat` means Tomcat
10.1, and a Liberty or full-server construct in the output is the error.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"jndi_resources_provided": <0-30>, "pool_carried_across": <0-25>,
            "no_inline_secrets": <0-20>, "nothing_dropped": <0-15>, "liberty_retired": <0-10>}}
