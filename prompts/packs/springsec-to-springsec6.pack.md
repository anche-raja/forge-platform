---
id: springsec-to-springsec6
version: 1.0.0
title: Spring Security 4/5 -> 6.3
tier: framework
detect:
  any:
    - dependency_lt: {coord: "org.springframework.security:spring-security-core", value: "6.0.0"}
    - import_prefix: "org.springframework.security.config.annotation.web.configuration.WebSecurityConfigurerAdapter"
    - xml_element: "http://www.springframework.org/schema/security:http"
applies_to:
  - file_glob: "**/spring-security*.xml"
  - content_match:
      glob: "**/*.java"
      pattern: 'WebSecurityConfigurerAdapter|@EnableWebSecurity|@EnableGlobalMethodSecurity|@EnableMethodSecurity|GlobalMethodSecurityConfiguration|SecurityFilterChain'
      risk: high   # security configuration: every hit is HIGH risk by rule
context: none
depends_on: [spring-to-spring6]
decisions: []
eliminates: []
acceptance:
  - no_match: 'WebSecurityConfigurerAdapter|authorizeRequests|antMatchers|mvcMatchers|EnableGlobalMethodSecurity'
    scope: "src/**/*.java"
  - authz_parity: true
---

## transform

You are migrating Spring Security 4.x/5.x configuration to Spring Security 6.3.

The file you are given **is** the authorization rule set: every path pattern, its required
authority or role, the order the rules are declared in, and the CSRF, session, headers and
authentication configuration. Spring Security evaluates rules **in declaration order, first match
wins** — reordering them silently changes who can reach what, so work through them in sequence and
keep that sequence.

This pack has one overriding constraint: **the effective authorization outcome for every path must
be identical before and after.** Everything else is secondary.

Rule 1 — Configuration shape:
- `WebSecurityConfigurerAdapter` → a `SecurityFilterChain @Bean` taking `HttpSecurity`
- `configure(WebSecurity)` with `.ignoring()` → a separate `WebSecurityCustomizer @Bean`, or
  `permitAll()` on the chain. **These are not equivalent**: `ignoring()` bypasses the filter chain
  entirely (no security headers, no context), `permitAll()` runs it. Preserve which one was used.
- `configure(AuthenticationManagerBuilder)` → an `AuthenticationManager` / provider `@Bean`
- `@EnableGlobalMethodSecurity(prePostEnabled, securedEnabled, jsr250Enabled)` →
  `@EnableMethodSecurity(...)` with the same flags. Note `prePostEnabled` defaults to **true** in
  6 but the others default to **false** — carry each flag across explicitly.
- `GlobalMethodSecurityConfiguration` subclass → `@Bean` overrides of the relevant components

Rule 2 — The DSL (lambda form is mandatory in 6.1+; the chained form is removed in 7):
- `authorizeRequests()` → `authorizeHttpRequests()`
- `antMatchers()` / `mvcMatchers()` / `regexMatchers()` → `requestMatchers()`
- `.and()` chaining → lambda configuration
- `access("hasRole('X')")` string expressions → the typed equivalents, or
  `AuthorizationManagers` / `WebExpressionAuthorizationManager` where the expression is not
  expressible typed

Rule 3 — Semantics that changed under the hood:
- `authorizeHttpRequests` uses `AuthorizationManager`; **`hasRole("X")` still prepends `ROLE_`** but
  `hasAuthority("X")` does not — preserve exactly which one each rule used.
- **`requestMatchers` no longer matches a trailing slash**, following Spring 6's `PathPattern`
  change. A rule protecting `/admin` no longer covers `/admin/`. **This is an authorization bypass
  if the handler still answers on the slashed path.** Emit an explicit variant and flag it.
- Any path not matched by a rule is **denied** under `authorizeHttpRequests` if `anyRequest()` is
  present, but an absent `anyRequest()` is now a startup error rather than an implicit permit.
- `ROLE_` prefix handling in `@PreAuthorize` is unchanged.

Rule 4 — Preserve verbatim, in the original order:
every path pattern, every authority/role, the CSRF configuration (including any
`ignoringRequestMatchers`), session-management/fixation policy, `headers()` settings, the
`AuthenticationEntryPoint`, `AccessDeniedHandler`, logout configuration and remember-me settings.

Rule 5 — `PasswordEncoder`: if the configuration used `NoOpPasswordEncoder` or a bare encoder,
keep it and flag it. Do **not** upgrade the encoder as part of this migration — changing the
encoder invalidates every stored credential.

Rule 6 — Every deviation, including one you believe is an improvement, goes in `manual_flags` with
the before and after rule. There are no silent security changes.

Respond ONLY with valid JSON:
{"files": {"<path>": "<full content>"}, "deleted_files": [], "manual_flags": [...]}

## review

Score on 5 checks (total 100).

Check 1 — Authorization outcome identical (40 pts):
Compare the migrated rules against the original, in order. Every pattern, every
authority, the same first-match-wins order, the same `hasRole` vs `hasAuthority` choice, the same
`anyRequest()` terminal. **Any added, removed, reordered or weakened rule scores 0 for the whole
check** — this is the only check that matters if it fails.

Check 2 — Trailing-slash and matcher changes handled (20 pts):
Every rule whose protection could be bypassed by a trailing slash has an explicit variant or a
flag. `ignoring()` vs `permitAll()` distinction preserved.

Check 3 — API migration complete (15 pts):
No `WebSecurityConfigurerAdapter`, `authorizeRequests`, `antMatchers`, `mvcMatchers`,
`@EnableGlobalMethodSecurity`. Lambda DSL used. Full 15 or 0.

Check 4 — Method security flags carried across (15 pts):
`prePostEnabled`, `securedEnabled` and `jsr250Enabled` each explicitly set to their original
values, not left to differing defaults.

Check 5 — Ancillary configuration preserved (10 pts):
CSRF, session management, headers, entry point, access-denied handler, logout, remember-me and
the password encoder unchanged.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"authz_identical": <0-40>, "matcher_changes": <0-20>, "api_migration": <0-15>,
            "method_security_flags": <0-15>, "ancillary_config": <0-10>}}
