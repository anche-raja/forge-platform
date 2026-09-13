---
id: struts1-to-springmvc6
version: 1.0.0
title: Struts 1 -> Spring MVC 6
tier: framework
detect:
  any:
    - dependency: "struts:struts"
    - dependency: "org.apache.struts:struts-core"
    - file_glob: "**/struts-config*.xml"
    - import_prefix: "org.apache.struts.action"
applies_to:
  - selector: struts_actions
  - file_glob: "**/struts-config*.xml"
  - file_glob: "**/validation.xml"
  - file_glob: "**/validator-rules.xml"
context: struts_routing_table
depends_on: [javax-to-jakarta, spring-to-spring6]
decisions: [url_compat]
eliminates:
  - "struts:struts"
  - "org.apache.struts:struts-core"
  - "org.apache.struts:struts-taglib"
acceptance:
  - no_match: "org\\.apache\\.struts\\.action"
    scope: "src/**/*.java"
  - routing_parity: true
---

## transform

You are migrating one Struts 1 action to a Spring MVC 6 controller. Struts 1 has been end-of-life
since 2013; assume nothing about it is still idiomatic.

**You are given the routing table extracted from `struts-config.xml`**: for each `<action>` — path,
type, name (the form bean), scope, validate, input, parameter, and every `<forward>` (name → path,
redirect flag). Plus the `<form-bean>` definitions and any `validation.xml` rules attached to them.
The Java file does not contain this. Use the table; do not infer.

Rule 1 — `Action.execute(mapping, form, request, response)` → a handler method with only the
parameters it actually uses. The four-argument signature is framework plumbing, not intent:
- `form` → an `@ModelAttribute` parameter of the form-bean's type
- `request`/`response` → parameters, only if genuinely used
- `mapping` → gone; its forwards became return values

Rule 2 — Forwards:
- `mapping.findForward("success")` → `return "viewName";` resolved through the table
- `<forward ... redirect="true"/>` → `return "redirect:/path";`
- `<global-forwards>` entries → shared constants or a `@ControllerAdvice`
- the `input` attribute → the `BindingResult.hasErrors()` return path

Rule 3 — `DispatchAction` / `LookupDispatchAction` / `MappingDispatchAction`:
the `parameter` attribute named a request parameter whose **value** selected the method. Each
reachable method becomes its own handler with its own path. `LookupDispatchAction`'s
`getKeyMethodMap()` resolved a localised button label to a method — that indirection disappears;
map each key to an explicit endpoint and flag the label-to-endpoint pairs for review.

Rule 4 — `ActionForm` / `DynaActionForm`:
- `ActionForm` subclass → a POJO or record. Drop `reset()` and `validate()`.
- `DynaActionForm` (fields declared in XML, accessed by string) → a real typed class generated
  from the `<form-property>` declarations in the table. Every `form.get("x")` becomes `form.getX()`.
- `scope="session"` forms → do **not** silently make them request-scoped. Either keep session
  scope explicitly (`@SessionAttributes`) or flag it: a session-scoped form carried state between
  requests and something probably depends on that.

Rule 5 — Validation:
- `validation.xml` rules (`required`, `maxlength`, `mask`, `email`, `integer`, `date`, `range`)
  → jakarta.validation constraints on the form class (`@NotBlank`, `@Size`, `@Pattern`, `@Email`,
  `@Min`/`@Max`, `@Past`). Preserve the message keys.
- `validate()` method bodies → a `@Valid` + custom `ConstraintValidator`, or handler-level checks
  where the rule needs services. Never drop a rule.
- `validator-rules.xml` is framework-supplied — do not migrate it, delete it.

Rule 6 — Struts 1 API removal: `ActionErrors`/`ActionMessages` → `BindingResult`;
`ActionMessage` keys → message codes; `RequestProcessor` subclasses → `HandlerInterceptor`;
`PlugIn` implementations → `@Configuration` beans or `ApplicationListener`.

Rule 7 — Struts 1 actions were **singletons with no instance state by contract** (the framework
reused one instance). Spring controllers are also singletons, so scope does not change here — but
any instance field that existed was already a latent bug. Flag every one you find.

Rule 8 — `url_compat` governs the `.do` extension exactly as it governs `.action` elsewhere.

Respond ONLY with valid JSON:
{"files": {...}, "deleted_files": ["<struts-config/validation xml superseded>"], "manual_flags": [...]}

## review

Score the migrated controller on 5 checks (total 100). You have the routing table.

Check 1 — Routing and forward fidelity (30 pts):
Every `<action>` path has a handler; every `<forward>` maps to the right return value with the
right redirect semantics; `input` is wired to the validation-failure path. Dispatch methods each
got an endpoint. Score 0 on any unmatched entry in either direction.

Check 2 — Form and validation fidelity (25 pts):
The form bean is a typed class (no `DynaActionForm` string access left). **Every** rule from
`validation.xml` and `validate()` has a constraint or an explicit check, with message keys
preserved. A dropped validation rule scores 0 — it is a silent data-integrity regression.

Check 3 — Scope preserved (15 pts):
A `scope="session"` form is still session-scoped or explicitly flagged. No form silently narrowed
to request scope.

Check 4 — No Struts 1 residue (20 pts):
No `org.apache.struts.action.*`, `ActionForm`, `ActionErrors`, `ActionForward`, `ActionMapping`,
`RequestProcessor`. Full 20 or 0.

Check 5 — Behaviour preserved (10 pts):
Logic, ordering, exception handling and early returns equivalent. Ambiguity flagged.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"routing_fidelity": <0-30>, "form_validation_fidelity": <0-25>, "scope_preserved": <0-15>,
            "no_struts1_residue": <0-20>, "behaviour_preserved": <0-10>}}
