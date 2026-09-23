---
id: webapp-bootstrap-jakarta10
version: 1.0.0
title: web.xml / app-server descriptors -> Jakarta EE 10 WAR (Spring 6, no Boot)
tier: platform
detect:
  any:
    - file_glob: "**/WEB-INF/web.xml"
    - file_glob: "**/WEB-INF/weblogic*.xml"
    - file_glob: "**/WEB-INF/jboss-web.xml"
    - file_glob: "**/WEB-INF/ibm-web-*.xmi"
    - file_glob: "**/META-INF/application.xml"
applies_to:
  - file_glob: "**/WEB-INF/web.xml"
  - file_glob: "**/WEB-INF/*web*.xml"
  - file_glob: "**/META-INF/application.xml"
  - selector: servlet_components
context: web_bootstrap
depends_on: [spring-to-spring6, springsec-to-springsec6]
decisions: [runtime, container]
eliminates: []
acceptance:
  - no_match: 'SpringBootServletInitializer|SpringBootApplication|org\.springframework\.boot'
    scope: "src/**/*.java"
  - build: "mvn -q -DskipTests package"
---

## transform

You are migrating a Java EE web bootstrap to **Jakarta EE 10, packaged as a WAR, running on a
servlet container, with Spring Framework 6.2 and no Spring Boot.**

The deployment model does not change. The artifact is still a WAR, the container still starts the
application, and the root context is still built by Spring's own bootstrap — either `web.xml` or a
`WebApplicationInitializer`. **Never emit `SpringBootServletInitializer`, `@SpringBootApplication`,
a starter dependency, or an `application.yml` that expects autoconfiguration.** Everything Boot
would have configured for you is configured explicitly here, which is the point.

**You are given the parsed descriptors**: every filter, listener, servlet, context-param,
error-page, welcome-file, security-constraint, resource-ref and env-entry, **in declaration
order**, plus any vendor descriptor (WebLogic / JBoss / WebSphere) alongside it.

Rule 1 — Order is behaviour. `web.xml` filter-mapping order defines the chain, and the chain is
security-relevant: a filter that ran before authentication now running after it changes what
unauthenticated requests can reach. Preserve the exact relative order and make it explicit —
never leave it to classpath or component-scan ordering, which is not deterministic across builds.

Rule 2 — Per the `runtime` decision:

- **`war-xml-bootstrap`** (default, lowest risk): keep `web.xml`. Update the schema to
  ```
  xmlns="https://jakarta.ee/xml/ns/jakartaee"
  xsi:schemaLocation="https://jakarta.ee/xml/ns/jakartaee
      https://jakarta.ee/xml/ns/jakartaee/web-app_6_0.xsd"
  version="6.0"
  ```
  Remove the filter and mappings of any framework being retired by another pack. Keep
  `ContextLoaderListener`, `contextClass` and `contextConfigLocation` exactly as they are —
  `AnnotationConfigWebApplicationContext` pointing at a `@Configuration` class is already the
  right shape and needs no change.

- **`war-programmatic-bootstrap`**: replace `web.xml` with a `WebApplicationInitializer` —
  normally `AbstractAnnotationConfigDispatcherServletInitializer`, declaring `getRootConfigClasses`,
  `getServletConfigClasses` and `getServletMappings`. Register each filter and listener in
  `onStartup` in the original order. `<context-param>` values become constants or a
  `@PropertySource`, **not** Boot properties. List `web.xml` in `deleted_files`.
  Note that `metadata-complete="true"` in the original disables annotation scanning — if it was
  set, a programmatic initializer will not be discovered at all until it is removed.

Rule 3 — Vendor descriptors (`weblogic.xml`, `jboss-web.xml`, `ibm-web-bnd.xmi`) carry settings
with no portable equivalent. **Do not discard them.** They are the input the
`liberty-server-config` pack consumes to produce `server.xml`, so extract every value rather than
resolving it here. Map what maps and flag the rest with its original value:
- context root → the WAR's final name, or the container's deployment descriptor
- session timeout → `<session-config>` in `web.xml`
- security-role mapping to directory groups → the Spring Security authority mapping
- resource-ref binding → the JNDI name the container exposes
- classloader policy (`prefer-web-inf-classes`, `parent-last`) → on Liberty this maps directly to
  `<classloader delegation="parentLast"/>`; carry the value through for `liberty-server-config`.
  It matters: the policy often existed to resolve a jar conflict that the dependency upgrade may
  have just removed — or may have just created.

Rule 4 — JNDI and container-provided resources. On the platform default
(`container: liberty`, a full Jakarta EE 10 profile) the container still provides JNDI, JTA,
managed connection pools and security registries, so **the application code does not change**:
- Keep every `<resource-ref>`, `<resource-env-ref>` and `<env-entry>` in `web.xml`.
- Keep `JndiDataSourceLookup` / `JndiObjectFactoryBean` and the JNDI names they resolve. Do not
  replace a container DataSource with an application-managed pool — that moves pool sizing,
  failover and monitoring out of the platform for no benefit.
- `<resource-ref>` JNDI names must match the `jndiName` the Liberty `server.xml` declares. The
  `liberty-server-config` pack produces that file; list every name you rely on so the two agree.
- The same holds for `container: wildfly`.
- Only on a bare servlet container (`tomcat`, `jetty`) does this change: there is no JTA manager
  and no managed pool, so every `<resource-ref>` must be flagged with what will now supply it.
  Never silently substitute a pool — pool sizing is an operational decision and guessing it wrong
  takes production down under load.

Rule 5 — `<security-constraint>` / `<login-config>` → Spring Security rules, preserving the exact
path patterns, HTTP methods and role names. Container-managed `BASIC` / `FORM` / `CLIENT-CERT`
authentication has a Spring Security equivalent; the **user store** usually does not — flag where
the realm came from (a container realm, an LDAP binding, a vendor descriptor) and what now supplies
it. Note that `<security-constraint>` matching and `requestMatchers` matching differ on trailing
slashes and on unlisted HTTP methods — a constraint that omitted `<http-method>` covered *all*
methods.

Rule 6 — Namespace, in every filter, listener and servlet you touch. The `javax-to-jakarta` pack
runs before this one, but the components you rewrite here are Java sources and the same rule and
the same trap apply:
- MIGRATE `javax.servlet.*` → `jakarta.servlet.*`, and likewise `javax.annotation.@Resource`,
  `@PostConstruct`, `@PreDestroy`.
- **LEAVE ALONE the JDK's own `javax.*`** — `javax.sql` (a filter or listener that looks up a
  `DataSource` uses `javax.sql.DataSource`, and rewriting it breaks the build), `javax.naming`
  (the JNDI lookup itself), `javax.crypto`, `javax.net`, `javax.security.auth`,
  `javax.xml.parsers`, `javax.xml.transform`.
- `ServletRequestAware`-style framework interfaces are gone with the framework; take
  `HttpServletRequest` / `HttpServletResponse` as method parameters instead.

Rule 7 — Servlet API level. Jakarta EE 10 is Servlet 6.0. Removed or changed since 3.1:
`HttpServletRequest.getRealPath()` (use `getServletContext().getRealPath()`),
`HttpSessionContext`, `SingleThreadModel`, `javax.servlet.http.HttpUtils`, and
`ServletContext.getServlet*`. Replace or flag each. Also: Servlet 6 rejects request paths
containing encoded path separators by default, which can break URLs that previously worked.

Rule 8 — EAR assembly. If an `application.xml` declares multiple modules with a shared library
directory, keep it. An EAR classloader isolates modules in ways a plain WAR does not, and
flattening it changes which class wins. Preserve the module list and `<library-directory>`, and
flag any module that is being retired so the assembly is updated rather than left dangling.

Respond ONLY with valid JSON:
{"files": {"<path>": "<full content>"}, "deleted_files": [], "manual_flags": [...]}

## review

Score on 5 checks (total 100).

Check 1 — Chain order preserved (25 pts):
Every filter and listener from the descriptor exists, with the same URL patterns, dispatcher types
and **relative order**, expressed explicitly. A reordered security filter scores 0 — it can place
unauthenticated requests in front of components that assumed authentication had already run.

Check 2 — Nothing from the descriptors was dropped (25 pts):
Every context-param, error-page, welcome-file, resource-ref, env-entry, security-constraint,
session-config and vendor-descriptor setting is either migrated or flagged with its original
value. A silently discarded vendor setting scores 0. Container-provided resources that a servlet
container no longer supplies are each named, with what must replace them.

Check 3 — Security constraints preserved (20 pts):
`<security-constraint>` path patterns, HTTP methods and roles carried across exactly. A constraint
with no `<http-method>` covered every method and its replacement must too. The authentication
mechanism and its user store are accounted for.

Check 4 — No Spring Boot, correct WAR shape (15 pts):
No `SpringBootServletInitializer`, `@SpringBootApplication`, starter dependency or
autoconfiguration property. Packaging is still a WAR. The output matches the declared `runtime`
option, and `web.xml` is either migrated-and-kept or deleted-and-replaced — never both, never
neither. **Any Boot artifact scores 0 for this check.**

Check 5 — Servlet 6 compliance (15 pts):
No removed Servlet APIs. Schema namespace and version updated to Jakarta EE 10. Encoded-path and
JNDI consequences flagged rather than assumed.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"chain_order": <0-25>, "nothing_dropped": <0-25>, "security_constraints": <0-20>,
            "no_boot_war_shape": <0-15>, "servlet6_compliance": <0-15>}}
