---
id: spring-to-spring6
version: 1.0.0
title: Spring Framework 4/5 -> 6.2
tier: framework
detect:
  any:
    - dependency_lt: {coord: "org.springframework:spring-core", value: "6.0.0"}
    - import_prefix: "org.springframework"
    - file_glob: "**/applicationContext*.xml"
applies_to:
  # Java files that use Spring at all; the XML contexts below by name.
  - content_match:
      glob: "**/*.java"
      pattern: '\borg\.springframework\b'
  - file_glob: "**/applicationContext*.xml"
  - file_glob: "**/spring-*.xml"
context: spring_bean_graph
depends_on: [javax-to-jakarta]
decisions: []
eliminates: []
acceptance:
  - no_match: 'WebMvcConfigurerAdapter|AsyncRestTemplate|LocalContainerEntityManagerFactoryBean\s*\(\s*\)'
    scope: "src/**/*.java"
---

## transform

You are migrating Spring Framework 4.x/5.x code to Spring Framework 6.2, deployed as a **WAR on a
Jakarta EE 10 servlet container**. There is no Spring Boot in this target: no starters, no
autoconfiguration, no `@SpringBootApplication`. Every bean the application needs is declared
explicitly, as it already is today.

Spring 6's hard requirements are Java 17+ and Jakarta EE 9+. The `javax-to-jakarta` pack has
already run — do not redo namespace work here, and do not undo it.

Rule 1 — Removed and replaced APIs (these do not compile on 6):
- `WebMvcConfigurerAdapter` → implement `WebMvcConfigurer` directly (it has default methods)
- `WebSecurityConfigurerAdapter` → handled by the security pack; leave it alone here
- `AsyncRestTemplate` → `WebClient`
- `RestTemplate` → `RestClient`. **Only where the call is synchronous and the response handling is
  straightforward.** `RestTemplate` still exists in 6 and is not deprecated; a mechanical rewrite
  of complex interceptor/error-handler configuration is riskier than leaving it. Flag rather than
  force.
- `ListenableFuture` → `CompletableFuture`
- `SimpleJdbcTemplate`, `NamedParameterJdbcTemplate` legacy ctors, `JdbcTemplate.queryForObject(String, Class, Object...)`
  ordering changes → use the current signatures
- `@Autowired` on a constructor when the class has exactly one → redundant, remove it
- `org.springframework.util.MimeType` / `StringUtils` methods removed in 6 → the surviving
  equivalents

Rule 2 — Behavioural changes that compile fine and fail at runtime. These are the ones that matter:
- **Trailing-slash matching is off by default in 6.** `/api/users` no longer also matches
  `/api/users/`. Any mapping that relied on it needs an explicit variant. Flag every mapping —
  this is the single most common Spring 6 upgrade regression.
- **`PathPattern` replaces `AntPathMatcher`** for MVC mapping. `**` is only legal at the end of a
  pattern now. A mid-pattern `**` is a startup failure.
- `@RequestMapping` without an explicit method matches all methods — unchanged, but combined with
  the loss of Struts filtering it may now expose verbs that were previously unreachable. Flag.
- Bean definition overriding is still **permitted** in plain Spring Framework (only Boot disables
  it). Two beans of the same name therefore remain a silent last-one-wins, and the winner depends
  on definition order. Do not change the behaviour — but flag every duplicate bean name you
  create or encounter, because a configuration rewrite is exactly when the order changes.
- `@Order` and `@Priority` on autowired collections are honoured differently from Spring 4 —
  verify any list injection whose order is significant.
- `ObjectProvider` and `@Nullable` injection resolution tightened; an optional dependency that
  silently resolved to null may now fail fast at startup.

Rule 3 — Configuration style:
- XML `<bean>` definitions → `@Configuration` class with `@Bean` methods, preserving bean **names**
  exactly (`@Bean(name=...)` where the XML id differs from the method name — something may look it
  up by name), scope, `init-method`/`destroy-method`, `depends-on` and lazy flags.
- `<context:component-scan base-package="..."/>` → `@ComponentScan(basePackages = ...)` with the
  identical package list. Never widen the scan.
- `<aop:*>` / `<tx:advice>` → `@EnableTransactionManagement` + `@Transactional`, preserving
  propagation, isolation, `rollbackFor` and read-only flags **exactly**. A changed propagation is a
  data-integrity bug.
- Property placeholder configurers → `@PropertySource` / `Environment`.

Rule 4 — Injection: field `@Autowired` → constructor injection, fields `final`. Where a circular
dependency prevents this (Spring 6 no longer resolves some cycles it used to), do not force it —
flag the cycle, because it is a design problem the migration has now surfaced.

Rule 5 — HTTP client, messaging and scheduling APIs that changed shape are rewritten only when the
mapping is exact. Otherwise `TODO(migration)` with the original left in place.

Rule 6 — Preserve every bean name, qualifier, profile and `@Order`. Framework code, XML, and
`getBean("name")` call sites depend on them.

Respond ONLY with valid JSON:
{"files": {"<path>": "<full content>"}, "deleted_files": ["<xml config replaced by java config>"], "manual_flags": [...]}

## review

Score on 5 checks (total 100).

Check 1 — Removed APIs eliminated (25 pts):
No `WebMvcConfigurerAdapter`, `AsyncRestTemplate`, `ListenableFuture`, or other Spring 6 removals.
Full or partial credit per API.

Check 2 — Runtime behaviour changes addressed (25 pts):
Trailing-slash reliance flagged or handled. No mid-pattern `**`. Duplicate bean names resolved.
`@ConstructorBinding` corrected. **These compile cleanly and fail in production — a migration that
ignores them is not done**, so an unflagged trailing-slash mapping scores 0 here.

Check 3 — Bean identity preserved (20 pts):
Every bean name, qualifier, scope, profile, `@Order`, init/destroy method and `depends-on` from
the original is preserved. Component-scan packages identical, never widened. A renamed bean
scores 0.

Check 4 — Transaction semantics preserved (20 pts):
Propagation, isolation, `rollbackFor`, `readOnly` and transaction boundaries byte-for-byte
equivalent to the XML or annotations they replaced. Any change scores 0.

Check 5 — Injection and no regressions (10 pts):
Constructor injection applied where safe, cycles flagged rather than forced, logic unchanged.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"removed_apis": <0-25>, "runtime_changes": <0-25>, "bean_identity": <0-20>,
            "transaction_semantics": <0-20>, "injection_no_regressions": <0-10>}}
