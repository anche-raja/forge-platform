---
id: struts2-to-springmvc6
version: 1.0.0
title: Struts 2 -> Spring MVC 6
tier: framework
detect:
  any:
    - dependency: "org.apache.struts:struts2-core"
    - file_glob: "**/struts*.xml"
    - import_prefix: "com.opensymphony.xwork2"
    - import_prefix: "org.apache.struts2"
applies_to:
  - selector: struts_actions
  - selector: struts_interceptors
  - file_glob: "**/struts*.xml"
context: struts_routing_table
depends_on: [javax-to-jakarta, spring-to-spring6]
decisions: [url_compat, web_framework]
eliminates:
  - "org.apache.struts:*"
  - "ognl:ognl"
acceptance:
  - no_match: 'org\.apache\.struts|com\.opensymphony\.xwork2'
    scope: "src/**/*.java"
  - routing_parity: true
---

## transform

You are migrating one Struts 2 action class to a Spring MVC 6 controller.

**You are given the routing table for this class, extracted from the Struts XML.** It is the
authoritative source for URLs, HTTP methods, result views and interceptor stacks — the Java file
alone does not contain them, and you must not infer them from the class name. For each `<action>`
entry you are given: name, namespace, class, method, every `<result>` (name, type, target), and
the interceptor stack resolved through the package `extends` chain.

**Do not invent a mapping that is not in the table. Do not drop one that is.**

Rule 1 — URL mapping:
- namespace + action name → the path. Apply the `url_compat` decision for the extension:
  - `preserve-with-redirect`: clean path on the handler, plus one `LegacyUrlRedirectFilter` entry
  - `preserve-exact`: `@PostMapping({"/ns/Name.action", "/ns/Name"})`
  - `clean-only`: clean path only
- Wildcard actions (`<action name="*Asset" method="{1}">`) → one handler per concrete method the
  table resolved, not a wildcard mapping. If the table could not resolve them, flag and stop.

Rule 2 — Results:
- a `<result>` with no `name` is `"success"`
- `<result>/WEB-INF/foo/bar.jsp</result>` → `return "foo/bar";`, with prefix/suffix configured on
  the view resolver centrally. Never hardcode `/WEB-INF` in the return value.
- `<result name="input">` → the validation-failure path: `if (bindingResult.hasErrors()) return ...`
- `<result type="redirect">` / `redirectAction` → `return "redirect:/...";`
- `<result type="json">` → return the payload with `@ResponseBody`. If **every** result on the
  class is json, make it `@RestController` instead.
- `<result type="stream">` → `ResponseEntity<Resource>` / `StreamingResponseBody`, preserving the
  content type, charset and `Content-Disposition` the Struts result declared.

Rule 3 — HTTP method. Struts does not declare one; infer conservatively:
- renders a form, or reads only → `@GetMapping`
- name begins Submit/Save/Update/Delete/Create/Add/Remove/Cancel, or the body mutates state
  → `@PostMapping`
- genuinely ambiguous → `@RequestMapping` plus `TODO(migration)` naming the ambiguity.
Never silently choose POST for something the UI reaches by a link.

Rule 4 — `ModelDriven<T>` / `getModel()` → the model type becomes an `@ModelAttribute` handler
parameter. Under Struts the params interceptor populated a long-lived field; under Spring it is
bound per request. Preserve any initialisation the Struts version relied on, and check for code
that read the model **before** binding.

Rule 5 — Scope is a real semantic change, and the most dangerous part of this migration.
Struts actions are per-request (`@Scope("prototype")` or created by the framework). Spring
controllers are **singletons**. Every mutable instance field is now shared across all concurrent
requests. Each one must become a method local, a handler parameter, or an explicitly
request-scoped bean. **List every field you relocate in `manual_flags`.** A surviving mutable
field is a data-corruption bug that will not show up in testing.

Rule 6 — Struts interceptors, per the stack in the routing table:
- sets response headers / runs for every request → `OncePerRequestFilter` with explicit `@Order`
- runs per action, needs the handler or its annotations → `HandlerInterceptor`
- populates or validates the model → `@Valid` + `HandlerMethodArgumentResolver`
A Struts interceptor is action-scoped; a servlet filter is request-scoped. Choosing a filter for
something that was action-scoped widens when it runs — justify the choice in a comment.
`ExceptionMappingInterceptor` / `<global-exception-mappings>` → `@ControllerAdvice`.

Rule 7 — Struts API surface to remove:
- `ActionSupport.SUCCESS/INPUT/ERROR/NONE/LOGIN` → view names or `ResponseEntity`
- `ServletRequestAware` / `ServletResponseAware` / `SessionAware` → handler method parameters
  (`HttpServletRequest`, `HttpSession`) — note `SessionAware` gave a `Map`, not an `HttpSession`
- `ActionContext.getContext()` → the corresponding Spring/servlet API
- `addActionError` / `addFieldError` → `BindingResult.reject` / `rejectValue`
- `ValidationAware` / `validate()` → jakarta.validation constraints on the model, with anything
  not expressible as a constraint moving into the handler and flagged

Rule 8 — Dependency injection: `@Autowired` fields → constructor injection with `final` fields.
`@Component("Name") @Scope("prototype")` → `@Controller`.

Rule 9 — Every security check is preserved verbatim and evaluated at the same point in the flow.
Role gates, environment gates and property gates that the original re-evaluated per call stay per
call. Do not lift a per-method check to a class-level annotation unless the original gated every
method identically.

Respond ONLY with valid JSON:
{"files": {...}, "deleted_files": ["<struts xml superseded>"], "manual_flags": [...]}

## review

Score the migrated controller on 5 checks (total 100). You are given the same routing table the
transformer saw — check the output against it, entry by entry.

Check 1 — Routing fidelity (30 pts):
Every `<action>` entry has exactly one handler at the correct path with the correct result views,
including `input` and any error/unauthorized results. `type="json"` results return a body, not a
view name. No invented endpoints. **Score 0 if any table entry has no handler, or any handler has
no table entry** — a lost endpoint is a broken feature and an invented one is an attack surface.

Check 2 — Singleton-safety (25 pts):
Every field that was per-request under Struts is now a local, a parameter, or request-scoped, and
each relocation is listed in `manual_flags`. A surviving mutable instance field scores 0.

Check 3 — Security preserved (20 pts):
Every role, environment and property gate is present, at the same point in the flow, with the same
failure view. Interceptor-enforced controls (CSRF/AJAX tokens, input allowlists) have equivalents
with the correct scope. Any weakening scores 0 regardless of the rest of the file.

Check 4 — No Struts residue (15 pts):
No `com.opensymphony.xwork2.*`, `org.apache.struts2.*`, `ActionSupport`, `ModelDriven`,
`*Aware` interfaces, or `SUCCESS`/`INPUT` constants. Full 15 or 0.

Check 5 — Behaviour preserved (10 pts):
Conditionals, early returns, exception handling and ordering equivalent. HTTP methods are
defensible. Ambiguity marked `TODO(migration)` rather than guessed.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"routing_fidelity": <0-30>, "singleton_safety": <0-25>, "security_preserved": <0-20>,
            "no_struts_residue": <0-15>, "behaviour_preserved": <0-10>}}
