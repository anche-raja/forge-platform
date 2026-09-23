# Adding a technology transition

A new migration — Hibernate 5→6, Log4j→SLF4J, WebLogic→Liberty — is **one markdown file and no
Python**. This is the platform's extension point, and it is the thing most worth knowing about
FORGE's design.

`prompts/FORGE-Platform-Requirements.md` §1 is the reference. This is the walkthrough.

---

## Why it is only a file

`forge/agents/java_upgrade.py` and `forge/review/java_reviewer.py` contain no Java logic and no
pack-specific logic. Both are generic prompt runners:

```python
spec = get_phase(state["phase"])
messages = [SystemMessage(content=spec.transform_prompt), HumanMessage(content=user_content)]
```

They look up whichever pack is running and send *that pack's* prompt. Your new pack rides the same
pipeline — secret scan, risk score, guardrails, transform, cross-model review, retry loop, hold
gate, compile check — without touching any of it.

---

## The file

`prompts/packs/<id>.pack.md`, at the **repository root**, not under `forge-mvp/`. (Override the
location with `FORGE_PACKS_DIR`.)

```yaml
---
id: log4j1-to-slf4j              # kebab-case, and it MUST equal the filename stem
version: 1.0.0                   # strict semver
title: Log4j 1.x -> SLF4J + Logback
tier: platform                   # build|language|namespace|persistence|framework|view|test|platform

detect:                          # ANY match activates the pack
  any:
    - dependency: "log4j:log4j"
    - import_prefix: "org.apache.log4j"
    - file_glob: "**/log4j.properties"

applies_to:                      # which files this pack transforms
  - file_glob: "**/*.java"
  - file_glob: "**/log4j.properties"

context: none                    # or a named extractor — see "the selector trap"
depends_on: []                   # an ORDERING edge, not a requirement
decisions: []                    # profile keys this pack reads
eliminates: ["log4j:log4j"]      # coordinates the build pack must remove
acceptance:
  - no_match: '^import org\.apache\.log4j'
    scope: "**/*.java"
---

## transform
Your rules, sent verbatim as the system prompt. End with the JSON response contract.

## review
Your rubric. The weights must total exactly 100.
```

---

## `detect` — what activates the pack

Detection must be decidable **without a model**. It is evidence, not opinion: this is what stops a
prompt from activating a pack your repository shows no sign of needing.

| Kind | Syntax | Matches against |
|---|---|---|
| `dependency` | `"group:artifact"` (artifact may be `*`) | Resolved Maven/Gradle coordinates |
| `dependency_lt` | `{coord: "g:a", value: "6.0.0"}` | The same, but only below that version |
| `file_glob` | `"**/WEB-INF/web.xml"` | Any relative path |
| `import_prefix` | `"com.opensymphony.xwork2"` | Java import statements |
| `content_match` | `'java\.sun\.com/jsp/jstl'` | File text (regex, compile-checked at load) |
| `property_lt` | `{name: "maven.compiler.source", value: "21"}` | Maven properties |
| `gradle_property_lt` | the same shape | Gradle properties |
| `xml_element` | `"namespace:element"` | Descriptor element names |
| `decision_equals` | `{key: "container", value: "liberty"}` | **A gate, never evidence** |

Versions are resolved through `${properties}`, `dependencyManagement` and imported BOMs, so
`dependency_lt` compares against the real resolved version.

**`decision_equals` can never activate a pack alone.** Every decision rule must hold *and* at least
one non-decision rule must fire — otherwise an empty directory would "need" the Liberty pack
because the config happens to name Liberty.

---

## `applies_to` — and the trap that costs packs their life

Three kinds:

| Kind | Needs an extractor? |
|---|---|
| `file_glob: "<glob>"` | No |
| `content_match: {glob, pattern}` | No — decidable from one file's own bytes |
| `selector: <name>` | **Yes, and the pack cannot run until it exists** |

> ### Prefer `content_match` to a `selector`, always
>
> A `selector` names a file set only a context extractor can resolve. Write one and your pack is
> **refused** — not run on a subset — until somebody builds a deterministic parser for it.
>
> This is not style advice. Two shipped packs were removed for exactly this: they declared
> `selector: struts_actions`, the `struts_routing_table` extractor was never written, and they could
> be selected by planning and never executed. Eight more are still stuck behind unbuilt extractors.
>
> "The class that extends `WebSecurityConfigurerAdapter`" is a real file set with no filename
> pattern — and it is decidable from one file's own bytes, so `content_match` handles it and the
> pack runs today.

Globs use FORGE's own matcher, not `fnmatch`: `*` stops at `/`, `**/` crosses directories, and
`**/*.java` also matches a bare `Foo.java`.

> ### Select only the files your pack can change
>
> Every unit costs three model calls, whether or not the pack finds anything to do in it. So do not
> write `file_glob: "**/*.java"` unless the pack really rewrites every Java file. Describe what the
> pack changes instead: javax-to-jakarta takes files matching the Jakarta EE `javax.*` packages,
> struts2-modernize takes files that reference XWork or Struts. On AMS this took the library from
> about 1,480 units to 420.
>
> - `file_glob` and `content_match` are **OR'd**. Adding a `content_match` beside `**/*.java`
>   narrows nothing; replace the glob.
> - Derive the pattern from your `## transform` rules. A rule with no trigger in the pattern never
>   runs, because no file carrying only that rule's target is ever sent.
> - Over-matching is cheap (one wasted file), under-matching silently skips work. When unsure,
>   widen the pattern.
> - Don't anchor on `^import` if a fully qualified use also needs changing.
> - For a pack that **must** change every occurrence, write the complement of what stays rather than
>   a list of what moves. javax-to-jakarta selects "any `javax.` that is not JDK or another spec that
>   stays javax" — a list of Jakarta packages would miss one, and the pack's leftover check, using the
>   same pattern, would miss it too. The complement fails the cheap way: an unknown package costs one
>   call and a check failure that names it.
>
> A `content_match` only *selects*. Add `risk: high` when a hit means the file is dangerous, not just
> relevant — springsec's matcher finds security configuration, so every hit is HIGH risk by rule:
>
> ```yaml
>   - content_match:
>       glob: "**/*.java"
>       pattern: 'WebSecurityConfigurerAdapter|@EnableWebSecurity'
>       risk: high
> ```

---

## The two sections

**`## transform`** is sent verbatim as the system prompt. The engine supplies the file, the path
header, the context block and any reviewer feedback on a retry — your section is purely the rules.
End it with the response contract:

```
Respond ONLY with valid JSON — no markdown fences:
{"files": {"<path>": "<full content>"}, "deleted_files": [], "manual_flags": [{"file":"<p>","line":<n>,"reason":"<why>"}]}
```

**`## review`** is the rubric, scored by a *different* model. The loader enforces the arithmetic:
`(N pts)` weights in the prose must sum to **exactly 100** and match the `<0-N>` maxima in the
`checks` block, in order.

```
Check 1 — No Log4j imports remain (60 pts): ...
Check 2 — Logger semantics preserved (40 pts): ...

{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"no_log4j": <0-60>, "semantics": <0-40>}}
```

> **Make the rubric grade what the transform was told to do.** This is the failure that is easiest
> to miss and hardest to see. `jsp-jstl-modernize` used to instruct "keep every Struts tag" while
> its rubric scored 0 unless every Struts tag had been *removed* — so a perfectly correct migration
> capped at 80 against a pass threshold of 80, one point from manual review, on every JSP in the
> project. Read the two sections against each other before you ship.

---

## The other fields

- **`depends_on` is an ordering edge, not a requirement.** It fixes the order when both packs are
  active and says nothing when only one is. A dependency outside the activated set is reported as
  advisory, not enforced.
- **`eliminates` / `upgrades` are how you talk to the build pack.** A pack never edits a build file
  itself, so two packs can never fight over the same `pom.xml`. Both use `group:artifact` (and
  `:version` for upgrades) — **Maven coordinate shape is enforced at load**, so an npm scope like
  `@angular/core` fails.
- **`acceptance`** runs after the migration, mechanically, with no model. `no_match`,
  `count_unchanged`, `routing_parity`, `test_parity`, `authz_parity`, `build`. A check may carry
  `when: {decision: value}`, and a check whose decision is unset is *skipped, never passed* — which
  forces the project verdict to `INCOMPLETE`.
- **`context`** names a deterministic extractor that must run before the transform. A pack whose
  rules reference facts it did not declare is rejected by the loader.

---

## Two traps that cost real time

- **Never write a regex in a double-quoted YAML scalar.** `"\."` is an invalid escape and YAML
  rejects it. Single-quote every pattern: `'^import javax\.'`.
- **Never use `a or b` on an ElementTree element.** An element with no children is falsy, so this
  silently takes the wrong branch. Compare with `is None`.

---

## The loop

```bash
python migrate.py --list-packs                       # lints the library; exits 1 on a bad pack
python migrate.py /path/to/fixture --discover        # does it activate on the right evidence?
python migrate.py /path/to/fixture --phase <id> --dry-run --file <one file>
```

> **Run `--list-packs` after every edit.** It is the only command that reports a malformed pack.
> Everywhere else the failure is caught and logged as *"Pack library did not load, continuing with
> built-in phases only"* — **one bad file silently disables all eighteen packs**.

Note that `--dry-run` still calls Bedrock. It skips the write, not the spend.

---

## Targeting a non-JVM stack

The honest answer: **not today, and FORGE will not tell you so.**

**Already works, no code:** the whole pipeline, retry and score routing, the hold gate, staging, the
review queue, guardrails, the secret scan, the extractor protocol, and both model agents — they are
generic despite their `java_*` filenames. Build verification is general too:

```yaml
build_verification: {enabled: true, mode: "command", command: "dotnet build {output_dir}"}
```

**Breaks loudly:** pack coordinates are Maven-shaped, so `@angular/core` or a NuGet id fails at
load — and takes the whole library down with it via the degradation above.

**Breaks silently — the dangerous part.** Point FORGE at a Python or .NET repository and it runs,
spends money, and produces plausible output with four controls quietly disabled:

1. `scope_package_prefix` matches the Java `package x.y;` declaration, so it never filters anything.
2. Every HIGH-risk rule is gated on a `.java` suffix, so the risk ceiling holds nothing.
3. The test-path check is the literal string `src/test`, so `tests/`, `*.Tests/` and `*.spec.ts`
   migrate as production code.
4. The compile gate is off by default.

**A real second language** costs a build-system parser backend (~250 lines), a language-strategy
module replacing `utils/java_checks.py` (~80 lines), about ten call-site changes for the `.java` and
`src/test` hard-codes, and a `testgen/` rewrite (~600 lines) if you want generated tests. Nothing
needs an AST library — the system is regex and XML throughout. [COMPONENTS.md](COMPONENTS.md) has
the full table of where the Java assumptions live.
