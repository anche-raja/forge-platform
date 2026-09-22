# Using FORGE

A working guide: from a repository you have, to migrated code on a git branch.

Every command here was run against a real project before it was written down. Output shown is real
output, not illustration.

**What FORGE is.** A pipeline that migrates a Java codebase one file at a time, through a library of
20 *packs* — each one technology transition, like `javax.* → jakarta.*` or `Spring 5 → 6`. A pack
decides what it takes and how; the engine decides what actually runs, what needs a human, and what
it cost.

**What it is not.** It is not a bulk find-and-replace, and it is not autonomous. Every file is
reviewed by a second model, risky ones are held for you, and nothing reaches your repository until
you say so.

---

## Before you start: what actually runs

**10 of the 20 packs migrate code today.** The rest recognise their technology and report it without
transforming it. This is the first thing to check against your own stack, because it decides whether
FORGE can do your migration or only describe it.

```
$ python migrate.py --list-packs
20 packs — 12 complete, 8 detect-only
```

| State | Count | What it means |
|---|---|---|
| **runnable** | 10 | `--phase <id>` works |
| **detect-only** | 8 | Recognised and reported, never migrated |
| **blocked** | 2 | Complete, but waiting on a context extractor that does not exist yet |

Runnable: `build-maven-modernize`, `java8-to-java21`, `javax-to-jakarta`, `spring-to-spring6`,
`springsec-to-springsec6`, `struts2-modernize`, `jsp-jstl-modernize`, `junit4-to-junit5`,
`webapp-bootstrap-jakarta10`, `liberty-server-config` — plus two built-in combined phases, `java21`
and `struts-spring6`.

Detect-only: `ant-to-maven`, `hibernate-to-hibernate6`, `jaxrs-to-jakarta-rs`, `ibatis-to-mybatis`,
`ejb2-to-spring`, `ejb3-to-spring`, `jms-to-spring-jms`, `jsf-to-faces4`.

> ### The Struts → Spring MVC route does not run
>
> `struts1-to-springmvc6` and `struts2-to-springmvc6` are both **blocked** — they need a
> `struts_routing_table` context extractor that has not been built. If you ask for
> `--intent "migrate off Struts to Spring MVC"`, intent will faithfully resolve
> `web_framework: migrate-to-spring`, select those packs, and the engine will then refuse them.
>
> The route that works end to end is **modernize-in-place**: Struts 2 → Struts 7 on Jakarta EE 10,
> via `struts2-modernize`. Plan for that one unless you are prepared to write the extractor.

---

## 1. Configure

```bash
./forge-terraform/scripts/generate-agents-yaml.sh dev > forge-mvp/agents.yaml
```

Re-run it after **any** `terraform apply` — editing a guardrail publishes a new version number, and
the pipeline pins the version.

Three things that bite here:

**`agents.yaml.example` is not runnable as shipped.** It carries
`guardrail_id: "REPLACE_WITH_GUARDRAIL_ID"`, and the first pipeline node fails on it. It is a
reference for the *keys*, not a starting config. It also omits the `secret_scan:` and
`preflight_model_check:` blocks that `GUARDRAILS.md` §9 documents as first-class controls — so
copying it leaves you with no secret-gate configuration.

**Model access is granted per account, and the error comes at run time.** Naming a model you have no
entitlement for produces `AccessDeniedException` on the first call, not at config load. Check before
a long run:

```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query 'inferenceProfileSummaries[?status==`ACTIVE`].inferenceProfileId'
```

That lists profiles that *exist*; entitlement is separate. The only reliable check is a one-token
`converse` call per model.

**An unpriced model costs $0.00 — silently.** `model_pricing` is keyed by model id. A model missing
from it accrues nothing rather than raising, so every estimate and every report quietly becomes
wrong. If you change `transform_model`, `review_model`, `intent.model` or `leader.model`, add the
matching pricing entry in the same edit.

The keys you are most likely to change:

| Key | Default | What it does |
|---|---|---|
| `pass_threshold` | 80 | Review score at or above this → written |
| `retry_threshold` | 50 | Between the two → retry with the reviewer's feedback injected |
| `max_retries` | 2 | Retry budget. **A failed build shares this same budget** |
| `complexity_block_threshold` | 2000 | LOC ceiling for auto-transform; a local check, no model call |
| `scope_package_prefix` | `""` | Filters at scan time by declared package, so an out-of-scope file costs zero Bedrock calls |
| `scope_exclude_globs` | `[]` | "Leave this directory alone" — e.g. `["db/**"]` |
| `decisions.risk_ceiling` | `review-high` | `auto` writes everything; `review-high` holds HIGH-risk units; `review-all` holds every unit |

---

## 2. Profile the repository — free

```bash
python migrate.py /path/to/app --discover --output-dir ./migrated
```

No model, no AWS, no `agents.yaml` needed. Real output, against a 10-module Struts/Spring project:

```
Discovery — /Users/raja/forge/ams

  build system   maven  (10 module(s))
  java level     8
  sources        376 java (40 test) · 61 jsp · 36 xml · 3 properties
  frameworks     Struts 2 6.8.0 · Spring 5.3.39 · Spring Security 5.3.13.RELEASE ·
                 JUnit 4 4.12 · Servlet API (javax) 3.1.0 · Jackson 2.20.2

  11 pack(s) apply, in dependency order:

   1.  build-maven-modernize        runnable
        file ams-common/pom.xml
        … 3 more
   3.  javax-to-jakarta             runnable
        import javax.servlet.Filter
        … 6 more
   7.* struts2-to-springmvc6        blocked
        dependency org.apache.struts:struts2-core:6.8.0
        … 9 more
```

Every activation is justified by **evidence** — a resolved dependency coordinate, an import, a file,
a property. Versions are resolved through `${properties}`, `dependencyManagement` and imported BOMs,
so `spring-core:5.3.39 … via spring-framework-bom` is a real resolved version, not a literal.

Writes `forge-profile.yaml` (the plan and the ten decision keys) and `stack-profile.json` (the full
detail). **Edit the decisions in `forge-profile.yaml` by hand** — that is the intended way to choose
between routes.

### Or describe what you want, in a sentence

```bash
python migrate.py /path/to/app --discover \
  --intent "migrate to the latest Java and Spring, stay on Struts, ignore the db folder"
```

One model call (~half a cent on Haiku). It sets the decisions, fills `scope_exclude_globs` from
"ignore the db folder", and narrows the pack set.

**It can only narrow, never activate.** A pack the evidence does not support can never be turned on
by a sentence — the request comes back in `unsupported` with a reason. No source code reaches the
model: it sees coordinates, import *prefixes*, descriptor names and counts. Every decision is
recorded with provenance (`prompt` / `config` / `default`), so a rerun from the profile needs no
model at all.

---

## 3. Migrate, one pack at a time

Run packs in the order discovery printed. One `--phase` per invocation.

```bash
# Smoke-test a single file first
python migrate.py /path/to/app --phase javax-to-jakarta --dry-run \
  --file src/main/java/com/corp/UserAction.java

# Then the pack
python migrate.py /path/to/app --phase javax-to-jakarta \
  --output-dir ./migrated --acceptance
```

> **`--dry-run` still calls Bedrock.** It skips the file write and the state update — not the spend.
> There is no offline mode. What it *does* give you is a full `manual-review-queue.json` and
> `migration-review.html` of every transform it would have made, which is the point: a first trial
> exists precisely to be looked at.

Each file goes through: secret scan → risk score → guardrail → transform → review (a *different*
model) → guardrail → write → optional compile. A score below `retry_threshold` goes straight to
manual review; between the thresholds it retries with the reviewer's feedback injected into the
transform prompt.

**Interrupted?** `--resume` picks up only the files still `PENDING` in DynamoDB.

### When your project is Java *and* Struts *and* Spring — read this

This is the common case, and it has a sharp edge.

**Every pack reads the original source tree, never the previous pack's output.**
`run_migration` scans `source_dir`, and the transform opens that path directly. Verified: after
running `javax-to-jakarta` (which produced `import jakarta.servlet…`), the next pack was handed a
file still containing `import javax.servlet…` — the original.

The consequence: **if two packs transform the same file, the second one's output would replace the
first's.** The writer does not merge, and the engine cannot merge for you — two packs' answers to
two different questions are not mechanically combinable.

So FORGE refuses instead, before spending anything:

```
$ python migrate.py ./app --phase java8-to-java21 --output-dir ./migrated
Refused: 'java8-to-java21' would overwrite 1 file(s) that javax-to-jakarta
already migrated into this output directory:
  src/main/java/com/corp/UserAction.java  (written by javax-to-jakarta)

Packs do not compose: this run reads the ORIGINAL source, so its output would
replace the earlier pack's work rather than build on it.
```

Exit code 2, no Bedrock calls. Re-running the *same* pack is always allowed — a pack has to stay
re-runnable after a prompt fix.

So the rule is:

| Your packs… | Do this |
|---|---|
| touch **different** files (`liberty-server-config` → `server.xml`, `jsp-jstl-modernize` → `.jsp`) | Run them in sequence. This is safe |
| touch the **same** `.java` files (`javax-to-jakarta`, `java8-to-java21`, `spring-to-spring6`, `struts2-modernize`) | Use a **combined phase** — do not chain them |

The combined built-in phases are what the refusal points you at, and why they bundle concerns that
individual packs keep separate:

```
java21          Java 8 -> 21, javax.* -> jakarta.*, deprecated and date/time APIs
struts-spring6  Struts 1/2 -> Spring MVC 6, Spring 4 -> 6, Jackson 1 -> 2, Java 8 -> 21
```

For a Struts + Spring + Java 8 codebase, `--phase struts-spring6` does the overlapping work in one
pass over each file, which is the only way the transforms compose. Then run the packs whose file
sets *don't* overlap — `jsp-jstl-modernize`, `webapp-bootstrap-jakarta10`, `liberty-server-config`,
`junit4-to-junit5` — separately afterwards.

If you do need two same-file packs in sequence, chain them — make the first pack's output the next
pack's input:

```bash
python migrate.py /path/to/app  --phase javax-to-jakarta --output-dir ./step1
python migrate.py ./step1       --phase java8-to-java21  --output-dir ./step2
```

Check the result: discovery and acceptance both reason about the *original* repository layout, so
verify against the final tree rather than assuming it carried through.

### Four runnable packs run without the context they declare

`--list-packs` labels them:

```
  2.  [build    ] build-maven-modernize   … ← runs WITHOUT context (needs the 'reactor' extractor)
  7.  [framework] spring-to-spring6       … ← runs WITHOUT context (needs the 'spring_bean_graph' extractor)
 17.  [view     ] jsp-jstl-modernize      … ← runs WITHOUT context (needs the 'view_bindings' extractor)
 18.  [test     ] junit4-to-junit5        … ← runs WITHOUT context (needs the 'test_subject' extractor)
```

These packs declare cross-file facts their author said the transform needs — a bean graph, a
reactor model — and those extractors are not built. The pack still runs, but the transform sees only
each file's own bytes, **and the reviewer loses the descriptors it would have cross-checked
"nothing was dropped" against**. Both halves of the safety net go at once.

A run says so, on the CLI and in `migration-report.md`:

```
WARNING: no extractor is registered for context 'reactor'; this pack runs without project context.
         Every file in this run is transformed without project context, and the
         reviewer has no descriptors to cross-check. Results are lower confidence.
```

Each queue entry carries `context_name` plus `context_missing: true`, so a degraded file is
distinguishable after the fact from one whose pack never wanted context. **Review these packs'
output more closely than the others** — especially `spring-to-spring6`, where cross-file wiring is
most of the difficulty.

### What it costs

Three model calls per file; a retry adds two. `leader.unit_cost_usd` (default `$0.07`) is the
documented average. Actuals come from `estimated_cost_usd`, accrued per real call — trust those over
the estimate.

Scale it from discovery's own counts: the 376-java-file project above is roughly 380 units for a
whole-tree pack like `javax-to-jakarta`, and far fewer for a narrow one like `liberty-server-config`,
which takes two files. Cost varies with the model — an estimate produced under Opus pricing does not
transfer to Sonnet, so re-derive it if you change models.

---

## 4. Review what was held

A file lands in `manual-review-queue.json` by one of four routes:

| Status | Why |
|---|---|
| `BLOCKED` | Secret scan hit, over `complexity_block_threshold`, or a guardrail stopped the input. **No transform exists, so it cannot be approved** |
| `MANUAL_REVIEW` | Score below `retry_threshold`, retries exhausted, a guardrail caught the output, or the build failed |
| `HELD` | Passed everything, but `risk_ceiling` caught its risk tier. Staged in `.forge-staging/`, not written |
| *(dry run)* | Everything with a transform, for inspection |

```bash
open ./migrated/migration-review.html
```

Static, self-contained, opens from `file://` — original and transformed side by side with a diff and
a decision widget. Decide, then **Download decisions.json**:

```bash
python migrate.py /path/to/app --apply-decisions decisions.json --output-dir ./migrated
```

```json
{
  "run": "2026-09-21T12:00:00+00:00",
  "decisions": [
    { "file": "src/main/java/com/corp/UserAction.java",
      "pack": "javax-to-jakarta",
      "decision": "approve",
      "note": "free text — becomes the retry prompt, or the rejection reason",
      "rule": "short label, so --feedback-report can group repeated corrections" }
  ]
}
```

- **approve** — promotes the staged files into the output tree and runs the build verifier on them.
  A failing build is **reported, never reversed**: a human approval is final.
- **reject** — discards the staged files.
- **retry** — re-runs that one file with your note injected as `HUMAN REVIEW FEEDBACK`, on a fresh
  retry budget and a fresh checkpoint.

Exit code is 0 only when every decision applied, so CI can gate on it. `--apply-decisions --dry-run`
narrates without touching anything.

Once you have reviewed a few packs:

```bash
python migrate.py --feedback-report --output-dir ./migrated
```

Groups your notes by pack and by `rule` into `pack-feedback.md`. **A correction you make three times
is a pack edit waiting to happen** — that is what this report is for.

---

## 5. Accept the project

```bash
python migrate.py /path/to/app --phase javax-to-jakarta --acceptance-only --output-dir ./migrated
```

Model scores gate a *file*. Acceptance checks gate the *project*, and they are mechanical — no model
is involved. They run over the merged tree (your source ⊕ `./migrated`), because a pack that touched
3 files must still be judged against the whole repository.

Two behaviours to expect:

- A check carrying `when: {decision: value}` whose decision is **unset** is *skipped, never passed* —
  and any skip forces the verdict to `INCOMPLETE` rather than `PASS`. `agents.yaml.example` pre-sets
  only 5 of the 10 decision keys, so set the rest in `forge-profile.yaml` or expect `INCOMPLETE`.
- `--acceptance-build` additionally runs `build:` checks (`mvn -q -DskipTests package`). Slow, needs
  the toolchain — nightly, not per-iteration.

Exit code is the gate.

---

## 6. Land it on a branch

> **This is UI-only.** The CLI gets you to `./migrated/` plus artifacts. The only supported path to
> a commit is the chat surface.

```bash
python migrate.py --ui
```

Then, in the chat: say where the repository is, run what you want, review, and ask it to land the
work on a branch. Landing is always a confirmation click, never a model decision.

`land_on_branch` **refuses rather than repairs**. Each of these is a distinct message, not a
best-effort fix: not a git work tree; a dirty work tree; a branch that already exists; an invalid ref
name. It never stashes, never forces, never amends, and **never pushes** — it hands you the
`git push -u origin <branch>` to run yourself.

Two sharp edges worth knowing before you hit them:

- **If `./migrated` sits inside your repository, landing refuses forever**, because
  `git status --porcelain` is never clean. Put it outside the repo, or gitignore it.
- **FORGE's own artifacts are never committed** — `manual-review-queue.json` above all, because it
  embeds your source verbatim.

---

## 7. Tests the legacy code never had

```bash
python migrate.py /path/to/app --phase javax-to-jakarta --output-dir ./migrated --generate-tests
python migrate.py /path/to/app --generate-tests-only --output-dir ./migrated --run-tests
```

JUnit 5 + Mockito, for the classes this run wrote. Two model calls per class. With `--run-tests`,
each test is executed against the merged tree and a failing one is **taken back out and held** with
its output — you never inherit a red test you did not write.

Which classes get a test, and where it lands, are decided in code, never by the model. An existing
test is never overwritten.

---

## Adding your own transition

The pack library is the extension point. A new transition for a JVM stack — Hibernate 5→6,
Log4j→SLF4J, WebLogic→Liberty — is **one markdown file and zero Python**.

Packs live at `prompts/packs/<id>.pack.md` at the **repository root**, not under `forge-mvp/`.
(Override with `FORGE_PACKS_DIR`.)

```yaml
---
id: log4j1-to-slf4j              # kebab-case, and MUST equal the filename stem
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

context: none                    # or a named extractor
depends_on: []                   # ORDERING edge, not a requirement
decisions: []                    # profile keys this pack reads
eliminates: ["log4j:log4j"]      # coordinates the build pack must remove
acceptance:
  - no_match: '^import org\.apache\.log4j'
    scope: "**/*.java"
---

## transform
(sent verbatim as the system prompt — your rules, and the JSON response contract)

## review
(the rubric — weights must total exactly 100)
```

### The nine `detect` kinds

Detection must be decidable **without a model** — evidence, not opinion.

| Kind | Syntax | Matches |
|---|---|---|
| `dependency` | `"group:artifact"` (artifact may be `*`) | Resolved Maven/Gradle coordinates |
| `dependency_lt` | `{coord: "g:a", value: "6.0.0"}` | Same, only below that version |
| `file_glob` | `"**/WEB-INF/web.xml"` | Any relative path |
| `import_prefix` | `"com.opensymphony.xwork2"` | Java imports |
| `content_match` | `'java\.sun\.com/jsp/jstl'` | File text (regex, compile-checked at load) |
| `property_lt` | `{name: "maven.compiler.source", value: "21"}` | Maven properties |
| `gradle_property_lt` | same shape | Gradle properties |
| `xml_element` | `"namespace:element"` | Descriptor elements |
| `decision_equals` | `{key: "container", value: "liberty"}` | **A gate, never evidence** |

**`decision_equals` cannot activate a pack on its own.** Every decision rule must hold *and* at least
one non-decision rule must fire — otherwise an empty directory would "need" the Liberty pack because
your config names Liberty.

### The three `applies_to` kinds — and the one that will cost you

| Kind | Needs an extractor? |
|---|---|
| `file_glob: "<glob>"` | No |
| `content_match: {glob, pattern}` | No — decidable from one file's own bytes |
| `selector: <name>` | **Yes, and the pack will not run until it exists** |

> **Prefer `content_match` to a `selector` wherever the question is answerable from a single file.**
> The difference is whether your pack runs today. This is not style advice — it is exactly why 10 of
> the 20 shipped packs do not run, including both Struts → Spring MVC routes. A pack whose selector
> has no registered extractor is *refused*, not run against a subset: migrating part of a pack's
> intended file set is worse than not running it.

### Rules the loader enforces

- **`id` must equal the filename stem.** The filename is how a profile pins a pack.
- **Review weights must total exactly 100**, and the `(N pts)` numbers in the prose must match the
  `<0-N>` maxima in the JSON `checks` block, in order. A reviewer cannot score against two different
  rubrics, and `pass_threshold` is meaningless otherwise.
- **`depends_on` is an ordering edge, not a requirement.** An edge pointing outside the activated set
  is reported, not enforced.
- **`eliminates` / `upgrades` are how you talk to the build pack.** A pack never edits a build file
  itself, so two packs can never fight over the same `pom.xml`.
- **A pack that references facts it did not declare in `context` is rejected.**

Two traps that cost real time:

- **Never write a regex in a double-quoted YAML scalar.** `"\."` is an invalid escape. Single-quote
  every pattern: `'^import javax\.'`.
- **Never use `a or b` on an ElementTree element** — an element with no children is falsy.

### The authoring loop

```bash
python migrate.py --list-packs                       # lints the library; exits 1 on a bad pack
python migrate.py /path/to/fixture --discover        # does it activate on the right evidence?
python migrate.py /path/to/fixture --phase <id> --dry-run --file <one file>
```

> **`--list-packs` is the only command that reports a malformed pack.** Everywhere else, a load
> failure is caught and logged as *"Pack library did not load, continuing with built-in phases
> only"* — **one bad file silently disables all twenty packs**. Run `--list-packs` after every edit.

---

## Using FORGE on a non-JVM stack

The honest answer: **not today, and it will not tell you so.**

### What already works, with no code change

The entire orchestration layer is language-agnostic — the pipeline, the retry and score routing, the
hold gate, staging, the review queue, guardrails, the secret scan, and the extractor protocol. So
are the transform agent and the reviewer: despite their `java_*` filenames, neither contains a single
Java token. Both just run the prompt your pack supplies. **A reviewer for a new technology is a new
`.pack.md` and zero Python.**

Build verification is already general:

```yaml
build_verification:
  enabled: true
  mode: "command"
  command: "dotnet build {output_dir}"    # or npm run build / tsc --noEmit / pytest
```

And four of the nine detect kinds — `file_glob`, `content_match`, `xml_element`, `decision_equals` —
have nothing to do with Java.

### What breaks loudly

Pack coordinates are Maven-shaped. The loader enforces `group:artifact[:version]`, so a pack
declaring `@angular/core` or a NuGet id **fails at load** — and via the degradation above, takes the
whole library down with it.

### What breaks silently — the dangerous part

Point FORGE at a Python or .NET repository and it runs, spends money, and produces plausible output
with four safety controls quietly disabled:

1. **`scope_package_prefix` becomes inert.** It matches the Java `package x.y;` declaration, and
   treats a file with no declaration as in scope. Nothing is filtered, and nothing is *reported* as
   filtered.
2. **The risk ceiling stops holding anything.** Every HIGH-risk rule is gated on a `.java` suffix —
   including your own pack's content matchers. Non-Java files score on line count alone, so
   `risk_ceiling: review-high` holds nothing.
3. **Test files migrate as production code.** The test-path check is the literal string `src/test`.
   `*.Tests/`, `tests/`, `__tests__/` and `*.spec.ts` all fall through — and `test_parity`
   acceptance, scoped to `**/src/test/**/*.java`, reports "no test sources" and *skips* rather than
   fails.
4. **The compile gate is off by default** (`enabled: false`, `mode: "javac"`).

A run will look like it worked.

### What a real second language costs

A build-system parser backend (~250 lines — the seam is the `pom.xml` / `build.gradle` / `build.xml`
chain in `discover/profile.py`), a language-strategy module to replace `utils/java_checks.py`
(~80 lines), about ten call-site changes for the `.java` and `src/test` hard-codes, and a `testgen/`
rewrite (~600 lines) only if you want generated tests.

Nothing needs an AST library — the system is regex and XML throughout, which ports more easily than a
parser-based design would. `COMPONENTS.md` has the full table of where the Java assumptions live.

---

## Command reference

| Command | Needs AWS? | What it does |
|---|---|---|
| `--list-packs` | No | The library, in dependency order. Exits 1 on a malformed pack |
| `--discover` | No | Profiles the repo, writes `forge-profile.yaml` |
| `--discover --intent "…"` | Yes (1 call) | Also sets decisions, scope and a narrowed pack set |
| `--phase <id>` | Yes | Runs one pack |
| `--dry-run` | **Yes** | No writes — but still calls Bedrock |
| `--file <path>` | Yes | One file only |
| `--resume` | Yes | Only files still `PENDING` |
| `--acceptance` / `--acceptance-only` | No | Project-level checks; exit code is the gate |
| `--acceptance-build` | No | Also runs `build:` checks |
| `--generate-tests` / `--generate-tests-only` | Yes | JUnit 5 + Mockito for what was written |
| `--run-tests` | No | Executes them; a failing test is held, not kept |
| `--apply-decisions <file>` | Sometimes | Applies approve/reject/retry. Retry re-runs the file |
| `--feedback-report` | No | Groups reviewer notes into `pack-feedback.md` |
| `--ui` | Yes | The chat surface — and the only path to a git branch |

> **Flags shadow each other.** `main()` dispatches in a fixed order: `--ui` → `--list-packs` →
> `--feedback-report` → `--discover` → `--apply-decisions` → `--generate-tests-only` →
> `--acceptance-only` → migrate. Passing `--discover` and `--phase` together runs *only* discovery,
> without complaining.

### Artifacts, all written to `--output-dir`

| File | What it is |
|---|---|
| `forge-profile.yaml` | The plan and the ten decisions — **edit this by hand** |
| `stack-profile.json` | Full discovery detail |
| `intent-plan.json` | What `--intent` decided, with provenance |
| `manual-review-queue.json` | Held files. **Embeds your source verbatim — never commit it** |
| `migration-review.html` | The review UI. Static, opens from `file://` |
| `migration-report.md` | What ran, what it scored, what it cost |
| `migration-acceptance.json` | Acceptance results |
| `decisions-applied.jsonl` | Append-only audit of every human decision |
| `pack-feedback.md` | Your notes, grouped by pack and rule |
| `.forge-staging/` | Held units, waiting for your decision |

---

## See also

- `COMPONENTS.md` — what each module does, and where the Java assumptions live
- `ARCHITECTURE.md` — how the pipeline is wired, node by node
- `GUARDRAILS.md` — the six checks every file passes, and what each costs
- `INTENT.md` — prose → pack selection, and the eight reconciliation rules
- `../prompts/FORGE-Platform-Requirements.md` §1 — the full pack contract
