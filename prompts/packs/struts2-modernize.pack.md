---
id: struts2-modernize
version: 1.0.0
title: Struts 2.3/2.5/6.x -> Struts 7 (Jakarta EE 10, Java 17+)
tier: framework
detect:
  any:
    - dependency_lt: {coord: "org.apache.struts:struts2-core", value: "7.0.0"}
    - import_prefix: "com.opensymphony.xwork2"
    - file_glob: "**/struts*.xml"
applies_to:
  - file_glob: "**/*.java"
  - file_glob: "**/struts*.xml"
  - file_glob: "**/*-validation.xml"
  - file_glob: "**/validators.xml"
  - file_glob: "**/*.ftl"
context: none
depends_on: [build-maven-modernize, javax-to-jakarta]
decisions: [web_framework]
eliminates:
  - "org.apache.struts:struts2-portlet-plugin"
  - "org.apache.struts:struts2-dwr-plugin"
  - "org.apache.struts:struts2-sitemesh-plugin"
  - "org.apache.struts:struts2-pell-multipart-plugin"
upgrades:
  - "org.apache.struts:struts2-core:7.3.0"
  - "org.apache.struts:struts2-json-plugin:7.3.0"
  - "org.apache.struts:struts2-spring-plugin:7.3.0"
  - "org.apache.struts:struts2-convention-plugin:7.3.0"
  - "org.apache.struts:struts2-tiles-plugin:7.3.0"
acceptance:
  - no_match: 'com\.opensymphony'
    scope: "**/*.java"
  - no_match: 'com\.opensymphony'
    scope: "**/struts*.xml"
  - no_match: 'struts-7\.[0-9]+\.dtd'
    scope: "**/struts*.xml"
  - routing_parity: true
  - build: "mvn -q -DskipTests package"
---

## transform

You are upgrading an application **from Struts 2.3/2.5/6.x to Struts 7.3**, in place. Struts stays.
This is a framework version upgrade, not a migration to a different framework — do not introduce
Spring MVC annotations, do not convert actions to controllers, do not touch the routing model.

Struts 7 requires **Java 17+** and a **Jakarta Servlet 6** container. The `javax-to-jakarta` and
`build-maven-modernize` packs run before this one and have handled the namespace and the
coordinates; your job is the Struts API surface and the behavioural changes that come with it.

Most of this upgrade is a rename. The parts that are not a rename are the parts that break
silently, so they are Rules 2 to 4 and they matter far more than Rule 1.

Rule 1 — Package relocation. `com.opensymphony.*` is gone; everything moved under
`org.apache.struts2`:
- `com.opensymphony.xwork2.X` → `org.apache.struts2.X` for the general case
- **Three exceptions that a blanket search-and-replace gets wrong:**
  - `com.opensymphony.xwork2.Action` → `org.apache.struts2.action.Action` (note the extra
    `.action` segment — this one does **not** follow the general rule)
  - text classes — `TextProvider`, `LocalizedTextProvider`, `TextProviderFactory`,
    `ResourceBundleTextProvider` → `org.apache.struts2.text.*`
  - locale classes — `LocaleProvider`, `LocaleProviderFactory`, `DefaultLocaleProvider` →
    `org.apache.struts2.locale.*`
- `com.opensymphony.xwork2.interceptor.*`, `.validator.*`, `.conversion.*`, `.util.*`,
  `.config.*`, `.inject.*` all follow the general rule into `org.apache.struts2.*`
- `ActionSupport`, `ActionContext`, `ModelDriven`, `Preparable`, `ValidationAware` follow the
  general rule.
Apply the same rename inside **XML** (`<interceptor class="...">`, `<result-type class="...">`,
`<bean class="...">`) and inside any string literal used for reflection.

Rule 2 — `@StrutsParameter` is now mandatory, and this is the change that silently breaks
applications. Struts 7 no longer injects request parameters into action setters unless the setter
(or the getter of a nested object) is annotated. An unannotated action still compiles, still runs,
and simply receives **null or default values for every field the form submitted**.
- Annotate every setter that a request parameter is bound to: `@StrutsParameter`
- For a nested/model object populated by parameters, annotate the accessor with
  `@StrutsParameter(depth = N)` where N is how deep the binding goes (depth 1 permits
  `user.name`, depth 2 `user.address.city`).
- A `ModelDriven` action's `getModel()` needs the annotation for its model to bind.
- **Do not annotate a setter that is not bound from a request** — that re-opens the mass
  assignment hole the annotation exists to close. Decide from the JSP/form fields and the
  `<param>` entries, and when the evidence is unclear annotate nothing and emit
  `// TODO(migration): does this field bind from a request parameter?`.

Rule 3 — Routing defaults changed, which changes which URLs resolve:
- `struts.mapper.alwaysSelectFullNamespace` now defaults to **true** (was false)
- `struts.actionConfig.fallbackToEmptyNamespace` now defaults to **false** (was true)
Together these mean an action previously reachable through a partial namespace match, or found by
falling back to the empty namespace, now returns 404. **Preserve existing behaviour by setting
both constants explicitly in `struts.xml` to their old values**, and emit a `TODO(migration)`
recommending the new defaults be adopted deliberately later. Do not accept the new defaults
silently as part of a version upgrade.

Rule 4 — OGNL is materially more restricted. Each of these is a runtime failure, not a compile
error:
- static field access is disabled — any `@com.example.Foo@BAR` expression in a JSP, XML or
  annotation stops resolving. Flag every one.
- custom `Map` instantiation in OGNL is blocked; only `HashMap`, `TreeMap` and `LinkedHashMap`
  are allowed
- maximum expression length dropped from 200 to 150 characters — flag any longer expression
- access to proxied objects (Spring, Hibernate) and to classes in the default package is
  disabled via OGNL
- Struts 7 adds an allowlist: `struts.allowlist.enable`, `struts.allowlist.packageNames`,
  `struts.allowlist.classes`. Where the application relies on OGNL reaching its own model classes,
  add the application's package to `struts.allowlist.packageNames` rather than disabling the
  allowlist.

Rule 5 — Removed plugins and interceptors. These do not exist in Struts 7:
- `struts2-portlet-plugin`, `struts2-dwr-plugin`, `struts2-pell-multipart-plugin` — removed with
  no replacement. Flag the usage; do not attempt a substitute.
- `struts2-sitemesh-plugin` — removed; integrate SiteMesh 3 directly. Flag.
- **File Upload Interceptor → Action File Upload Interceptor.** Replace `<interceptor-ref
  name="fileUpload"/>` with `actionFileUpload`, and change the action's upload properties from the
  `File`/`String` triple to `UploadedFile`. This one has a direct replacement, so perform it.

Rule 6 — FreeMarker templates: the template variable `parameters` was renamed to `attributes`,
to stop it being confused with HTTP request parameters. In any custom `.ftl` template and any
`<s:component/>` tag, `${parameters.x}` → `${attributes.x}` and `%{parameters.x}` →
`%{attributes.x}`.

Rule 7 — **Do not change the `struts.xml` DOCTYPE to a 7.0 DTD. There is no `struts-7.0.dtd`.**
The newest DTD Struts ships is `struts-6.5.dtd`:
```
<!DOCTYPE struts PUBLIC "-//Apache Software Foundation//DTD Struts Configuration 6.5//EN"
    "https://struts.apache.org/dtds/struts-6.5.dtd">
```
A 2.3 or 2.5 DOCTYPE may be uplifted to 6.5. Inventing a 7.0 DTD makes the configuration
unparseable and the application fails at startup. Validation XML keeps its
`xwork-validator-1.0.3.dtd` DOCTYPE — that filename is unchanged.

Rule 8 — Leave alone: action names, namespaces, result names, the interceptor stack composition,
result types, JSP tag usage (`/struts-tags` is unchanged), and all business logic. The URL a user
hits before this upgrade must be the URL they hit after it.

Respond ONLY with valid JSON — no markdown fences:
{"files": {"<path>": "<full content>"}, "deleted_files": [], "manual_flags": [{"file":"<p>","line":<n>,"reason":"<why>"}]}

## review

Score on 5 checks (total 100).

Check 1 — Parameter binding preserved (30 pts):
Every action setter that binds a request parameter carries `@StrutsParameter`, with the correct
`depth` for nested paths, and `getModel()` is annotated on `ModelDriven` actions. **An unannotated
bound setter scores 0 for this check** — the action compiles, runs, and silently receives nulls
for everything the form submitted, which no compiler and no smoke test will catch. Equally, a
setter annotated without evidence that it binds from a request scores 0: that re-opens the mass
assignment vulnerability the annotation exists to close.

Check 2 — Package relocation correct, including the exceptions (25 pts):
No `com.opensymphony` reference remains in Java, XML or string literals. `Action` went to
`org.apache.struts2.action.Action`, the text classes to `org.apache.struts2.text.*`, and the
locale classes to `org.apache.struts2.locale.*`. A blanket rename that sent any of those three to
plain `org.apache.struts2` scores 0 — it does not compile, and it is the most likely error.

Check 3 — Routing behaviour unchanged (20 pts):
`struts.mapper.alwaysSelectFullNamespace` and `struts.actionConfig.fallbackToEmptyNamespace` are
both set explicitly to their pre-7 values, with a TODO recommending a deliberate later change.
Action names, namespaces and result names are untouched. Silently accepting the new defaults
scores 0 — actions that resolved before will 404.

Check 4 — Removed plugins and restricted OGNL handled (15 pts):
`fileUpload` replaced by `actionFileUpload` with `UploadedFile` properties. Removed plugins
flagged rather than substituted. Static-field OGNL expressions, over-length expressions and
custom map instantiation each flagged; allowlist configured by package rather than disabled.

Check 5 — DTD and scope discipline (10 pts):
The DOCTYPE is a real DTD — 6.5 or the existing one, **never an invented 7.0**. FreeMarker
`parameters` renamed to `attributes`. No Spring MVC annotation, no controller conversion, no
business-logic change: this is a version upgrade, not a framework migration.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"parameter_binding": <0-30>, "package_relocation": <0-25>, "routing_unchanged": <0-20>,
            "plugins_and_ognl": <0-15>, "dtd_and_scope": <0-10>}}
