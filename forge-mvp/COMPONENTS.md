# FORGE components — what each part does

A map of every module in `forge/`, grouped by the job it does, with the one thing about each that
is not obvious from its name. ~13,000 lines across 80 modules.

`ARCHITECTURE.md` explains how the pipeline is wired. This file answers a different question:
**"I am looking at `forge/<something>.py` — what is it for, and what will surprise me?"**

The organising principle, and the thing to hold onto while reading: **a model is never the control
that decides what a model may see, and a model never answers a mechanical question.** Almost every
surprise below is that rule being enforced somewhere you would not expect it.

---

## Map

```
 entry            migrate.py  ·  forge/ui/
   │
 orchestration    service.py  ·  phases.py  ·  state.py
   │
   ├── planning   packs/  ·  discover/  ·  intent/
   │
   ├── per file   graph.py ──▶ agents/ · review/ · guardrails/ · risk/ · context/
   │                                                              ▲
   │                                                        extract/
   │
   ├── output     utils/file_writer.py  ·  review_queue.py  ·  decisions.py  ·  verify/
   │
   ├── chat       leader/
   │
   └── tests      testgen/
```

---

## 1. Entry points

| Module | Lines | What it does |
|---|---|---|
| `migrate.py` | ~330 | The CLI. Argparse, then a dispatch chain in `main()`. |
| `forge/ui/app.py` | 560 | FastAPI app — every HTTP route, and all request validation. |
| `forge/ui/server.py` | 36 | Picks a port, starts uvicorn, opens a browser. |
| `forge/ui/jobs.py` | 164 | `JobRegistry` — one background job at a time, SSE with sequence numbers. |

**`migrate.py` — flags shadow each other.** `main()` resolves in a fixed precedence:
`--ui` → `--list-packs` → `--feedback-report` → *(require `source_dir`)* → `--discover` →
`--apply-decisions` → `--generate-tests-only` → *(require `--phase`)* → `--acceptance-only` →
normal migration. Passing `--discover` and `--phase` together silently runs only discovery.

**`forge/ui/jobs.py` — one job, and replay from zero.** The registry holds a single slot; a second
start returns `JobBusy` → HTTP 409. Every event carries a sequence number and the stream can be
replayed from `seq 0`, which is what lets a browser reload mid-run without losing or doubling
anything. Cancellation is a `threading.Event` the job polls — never a kill.

**`forge/ui/app.py` — validation lives on the request thread, not the job thread.** A bad path or a
missing `agents.yaml` becomes a 400 before a job is ever started, so a failure the user caused never
occupies the single job slot.

---

## 2. Orchestration

| Module | Lines | What it does |
|---|---|---|
| `forge/service.py` | 716 | The API both the CLI and the UI call. `discover`, `run_migration`, `acceptance`, `apply`, `generate_tests`, `feedback`, `packs`. |
| `forge/graph.py` | 204 | The LangGraph `StateGraph` — 11 nodes and the routing between them. |
| `forge/phases.py` | 353 | The built-in `java21` phase, and the adapter that makes a pack look like one. |
| `forge/state.py` | 99 | `ForgeState`, the TypedDict threaded through every node. |
| `forge/config.py` | 44 | Loads `agents.yaml` into `ForgeConfig`. |

**`service.py` is the seam.** Everything above it (CLI, HTTP routes, the chat leader's tools) is a
thin wrapper; everything below is the pipeline. Its functions return dataclasses — `RunResult`,
`AcceptanceOutcome`, `ApplyResult`, `TestGenResult` — never raw dicts, so a caller cannot quietly
depend on an internal field.

**`graph.py` is the Leader Agent, and it is code.** The deck puts a model at the centre of the
architecture; here every one of its duties is a static edge or a plain function. No node returns a
node name. The routing functions — `route_pre`, `route_reviewer`, `route_verify`, `route_post` —
read integers and config thresholds, never model prose. The reviewer's own `review_verdict` string
is recorded and **never routed on**; only its numeric score is.

```
guardrails_pre ─▶ java_upgrade ─▶ java_reviewer ─▶ guardrails_post ─▶ write_file
      │                 ▲                │                                 │
      │           increment_retry ◀──────┘                           verify_build
      ▼                                                                    │
   blocked          hold_for_review ─▶ update_state ◀── manual_queue ◀──────┘
```

**`phases.py` degrades silently, and this is the trap worth knowing.** `_packs()` is
`@lru_cache(maxsize=1)` and catches `PackError`, logging *"Pack library did not load, continuing
with built-in phases only"*. **One malformed pack file disables all eighteen.** `--list-packs` is the
only command that reports the failure in full and exits non-zero — which is why it doubles as the
pack-authoring lint.

**`state.py` — the 400 KB ceiling shapes the design.** `ForgeState` is checkpointed to DynamoDB per
file, so anything large is deliberately kept *out* of it. That is why extracted context lives in a
process-global cache instead of in state.

---

## 3. Planning — what will run, and in what order

| Module | Lines | What it does |
|---|---|---|
| `forge/packs/spec.py` | 184 | The pack data model — `DETECT_KINDS`, `ACCEPTANCE_KINDS`, `TIERS`. |
| `forge/packs/loader.py` | 509 | Parser, validator, registry, topological sort. |
| `forge/packs/glob.py` | 36 | Glob matching with path-separator semantics. |
| `forge/discover/profile.py` | 427 | Reads the repository: build system, Java level, dependencies, imports. |
| `forge/discover/resolve.py` | 141 | Evaluates each pack's `detect` rules against the profile. |
| `forge/discover/emit.py` | 168 | Writes `forge-profile.yaml` and `stack-profile.json`. |
| `forge/intent/*` | 552 | Turns a sentence into decisions, scope and a pack subset. |

**Packs live at the repo root, not here.** `loader.py:33` resolves
`Path(__file__).parents[3] / "prompts" / "packs"`, overridable with `FORGE_PACKS_DIR`.

**`glob.py` exists because `fnmatch` is wrong.** `fnmatch`'s `*` crosses `/`, so `*.java` would
match `src/main/Foo.java`. Here `*` stops at a separator and `**/` expands to `(?:[^/]*/)*` — which
also means `**/*.java` matches a bare `Foo.java`.

**`loader.py` validates the rubric arithmetic.** `_validate_rubric` extracts `(N pts)` from the
prose and `<0-N>` from the JSON `checks` block and requires that they sum to **exactly 100** and
match in order — *"a reviewer cannot score against two different rubrics"*, and `pass_threshold` is
meaningless against a rubric that totals anything else.

**`depends_on` is an ordering edge, not a requirement.** A dependency outside the activated set is
neither pulled in nor an error — `jsp-jstl-modernize` must follow `struts2-modernize` when both are
active and says nothing when only one is. `missing_dependencies()` reports dangling edges as
advisory, so the entry that matters is the *unexpected* one: a plan with `struts2-modernize` and no
`javax-to-jakarta` is a hand-edited profile, not a design.

**The sort is deterministic on purpose.** `_topological` is Kahn's algorithm with the ready set
re-sorted by `(tier_index, id)` after every pop — *"or two runs of the same project produce
different plans"*. Tier order: `build → language → namespace → persistence → framework → view →
test → platform`.

**`resolve.py` — a decision is a gate, never evidence.** Every `decision_equals` rule must hold
**and** at least one non-decision rule must fire. Without that, an empty directory would "need" the
Liberty pack because the config names Liberty.

**`intent/` — the model proposes, `reconcile()` disposes.** `agent.py` makes one model call; every
safety property lives in `resolve.py:reconcile()`, which is pure Python. A pack the evidence did not
activate can never be added by a prompt — it comes back as `unsupported`. The model sees
`Profile.to_json()` only: coordinates, import *prefixes*, descriptor names, counts. **Never file
contents.**

---

## 4. The per-file pipeline

| Module | Lines | What it does |
|---|---|---|
| `forge/agents/guardrails_pre.py` | 155 | Secret scan, LOC ceiling, Bedrock guardrail, risk score — before any transform. |
| `forge/agents/java_upgrade.py` | 92 | The transform call. |
| `forge/review/java_reviewer.py` | 81 | The review call, on a different model. |
| `forge/agents/guardrails_post.py` | 102 | Checks the output, including a deterministic `javax.*` sweep. |
| `forge/risk/score.py` | 148 | Deterministic risk tier, before any model call. |
| `forge/guardrails/bedrock_guardrails.py` | 39 | The `ApplyGuardrail` wrapper. |
| `forge/utils/secret_scan.py` | 391 | Local, pattern- and entropy-based credential detection. |

**`java_upgrade.py` and `java_reviewer.py` are misnamed — they are not Java-specific and they are
not one-pack-specific.** Neither contains a single Java token. Both are generic prompt-runners:
`get_phase(state["phase"])` resolves whichever pack is running, and its `transform_prompt` /
`review_prompt` is sent as the SystemMessage. The Struts pack, the Spring pack and a pack you write
tomorrow all run through these same two files. **A transform or reviewer for a new technology is a
new `.pack.md` and zero Python.** This is the cleanest extension point in the system.

**Every pack reads the original source tree.** `run_migration` scans `source_dir` and
`java_upgrade.py` opens that path directly; `write_output` overwrites rather than merging. Two packs
that transform the same file therefore do not compose — which is why the built-in `java21` phase
bundles Java 8 -> 21 and `javax.*` -> `jakarta.*` into a single pass rather than deferring to the
two packs that do them separately.

**`chain=True` is how the second pack composes anyway.** It materialises the merged view of source
⊕ output into a temp tree and runs from there, so the pack transforms the previous pack's result,
and the overlap guard is skipped because overwriting is then the point. The leader turns it on
automatically from the manifest, since the chat surface has to sequence a ten-pack plan without
being told how. `utils/run_manifest.py` records which pack wrote which file and
`run_migration` raises `PackOverlap` before spending anything, because the engine can refuse but
cannot merge: two packs' answers to two different questions are not mechanically combinable.

**The secret gate runs before every remote call — including the Bedrock guardrail.** A file carrying
a credential is refused while its bytes are still in the process, for zero Bedrock calls. The
reasoning is in `GUARDRAILS.md`: the Bedrock guardrail is itself a network call and covers only six
entity types, and *a model asked "does this file contain secrets?" has already been shown the
secret*. Findings never quote the matched bytes. `action: warn` **sends the secret to the model** —
it exists for a team whose policy permits that, not as a way to quiet a noisy scan.

**`score.py` decides risk without a model.** Inputs are LOC bands, descriptor fan-out, authorization
rules, Spring-proxied classes, `ModelDriven`, security configuration, `sun.misc.Unsafe`,
`Thread.stop`, OGNL density, generated units, and whether the file matched a pack's own content
selector. The score sets a tier; `decisions.risk_ceiling` decides what the tier *means*.

**`guardrails_post.py` carries one non-negotiable invariant.** `find_unmigrated_javax_imports`
sends the unit to `MANUAL_REVIEW` if a `javax.*` import survived — with a 28-entry allowlist for the
JDK's own `javax.sql`, `javax.crypto`, `javax.naming` and friends, which must *not* be rewritten.
This runs unconditionally and there is no config key to swap the invariant for another language's.

---

## 5. Context — cross-file facts

| Module | Lines | What it does |
|---|---|---|
| `forge/extract/__init__.py` | 111 | The `Extractor` protocol and registry, plus the cache. |
| `forge/extract/web_bootstrap.py` | 1112 | The one implemented extractor: `web.xml` and app-server descriptors. |
| `forge/extract/selectors.py` | 37 | Named file sets an extractor can resolve. |
| `forge/context/render.py` | 104 | Renders a context into the block appended to prompts. |
| `forge/context/inject.py` | 54 | Puts that block into the transform and review messages. |
| `forge/context/snapshot.py` | 45 | Writes `migration-context.json`. |

**A pack that needs cross-file facts declares an extractor, and the engine guarantees it ran first.**
A pack whose rules reference facts it did not declare is rejected by the loader.

**Runnability turns on selectors, not on context.** `file_scanner.py:63` refuses a pack whose
`selector:` has no extractor, but a pack whose file set is pure globs runs even when its declared
`context:` is unavailable — it just runs blind. Four do: `spring-to-spring6`,
`build-maven-modernize`, `jsp-jstl-modernize`, `junit4-to-junit5`. `degraded_phases()` reports them,
`--list-packs` labels them, and each affected unit carries `context_missing: true` so it stays
distinguishable from a pack that declared `context: none`. Keeping them runnable-but-labelled was
the deliberate call: dropping them would empty the working set.

**This is why 8 of the 18 packs do not run at all** — all marked `detect-only`. Only
`web_bootstrap` is registered. (The two Struts -> Spring MVC packs were *blocked* by this rule
rather than detect-only; they have since been removed along with that route.)
Packs naming `struts_routing_table`, `ejb_bean_table`, `orm_mapping_graph`,
`faces_navigation_graph`, `jaxrs_resource_table`, `jms_destination_table` or `ant_target_graph` are
refused rather than run against zero files — *"worse than not running at all"*. The protocol itself
(`run(source_dir, module_dir) -> dict` plus named selectors) is entirely language-agnostic.

**The cache is process-global and deliberately not in state.** `@lru_cache(maxsize=64)`, cleared per
run. Putting a routing table into `ForgeState` would checkpoint it to DynamoDB on every file and hit
the 400 KB item ceiling.

**`render.py` truncates loudly.** Sections that do not fit `context.max_chars` are *listed as
omitted*; the full context always goes to `migration-context.json`.

---

## 6. Output and verification

| Module | Lines | What it does |
|---|---|---|
| `forge/utils/file_writer.py` | 147 | Writes transformed files into the output tree. |
| `forge/review_queue.py` | 303 | Builds `manual-review-queue.json` and the static review HTML. |
| `forge/decisions.py` | 204 | Applies a human's approve / reject / retry. |
| `forge/verify/acceptance.py` | 332 | Runs a pack's declared acceptance checks. |
| `forge/verify/merged_tree.py` | 121 | Materialises source ⊕ migrated for checking. |
| `forge/verify/build_verifier.py` | 104 | Compiles what was written. |
| `forge/feedback_report.py` | 138 | Groups reviewer notes into `pack-feedback.md`. |

**Acceptance checks gate the *project*; model scores gate a *file*.** A check carrying
`when: {decision: value}` whose decision is unset is **skipped, never passed** — and any skip forces
the verdict to `INCOMPLETE` rather than `PASS`. Checks run over the merged tree, because a pack that
touched 3 files must still be judged against the whole repository.

**`review_queue.py` writes a review UI with no server.** `migration-review.html` is static and
self-contained — no external assets — and opens from `file://`. Its "Download decisions.json" button
and the web UI's review panel run the *same* validator, so the two surfaces are one contract.

**Queue entries embed the original source verbatim** (capped at 200 KB). This is why
`manual-review-queue.json` is on every artifact exclusion list and must never be committed.

**`decisions.py` — approval is final.** On approve, staged files are promoted out of
`.forge-staging/` and the build verifier runs on what was written; a failing build is **reported,
never reversed**. A `BLOCKED` entry cannot be approved — it has no transform. A retry re-runs the
single unit with the human's note injected as `HUMAN REVIEW FEEDBACK`, on a fresh retry budget and a
fresh checkpoint thread.

**`build_verifier.py` is the most portable thing here.** Three modes: `javac`, `maven`, and
`command` — the last substitutes `{file}` and `{output_dir}` and runs anything, so `dotnet build`,
`npm run build`, `tsc --noEmit` and `pytest` all work with no code change. Default is
`enabled: false`. A missing toolchain reports `SKIPPED` rather than failing the file, and a failed
compile consumes one of the *same* retries the reviewer uses.

---

## 7. The leader — the chat surface

| Module | Lines | What it does |
|---|---|---|
| `forge/leader/agent.py` | 545 | Stream → admission gate → execute → observe, up to `max_steps`. |
| `forge/leader/tools.py` | 1215 | The 12 tools, and every gate. |
| `forge/leader/cards.py` | 345 | Card builders, and the reducers that decide what the model may see. |
| `forge/leader/convo.py` | 224 | Two histories: one for the model, one for the browser. |
| `forge/leader/landing.py` | 416 | `land_on_branch`. |
| `forge/leader/settings.py` | 68 | The `leader:` config block. |

The thirteen tools: `set_project`, `profile_project`, `resolve_intent`, `estimate_pack`, `run_pack`,
`check_acceptance`, `list_held_files`, `apply_review_decisions`, `generate_tests`, `pack_feedback`,
`list_artifacts`, `build_project`, `land_on_branch`.

**The model sequences; it does not decide what is true.** It chooses which tool to call and when to
stop and ask. It cannot decide which packs exist (evidence does), which files a pack takes, what
happens inside a run, or whether an approval happens.

**`cards.py` is the trust boundary, and it is the sharpest edge in the codebase.** The same data is
reduced two ways: the browser gets diffs, reviewer prose and full evidence; the model gets counts,
paths, statuses, scores and verdicts. Specifically dropped:

- `guardrail_findings` → `{count, kinds}`, because a Bedrock `sensitiveInformation` finding
  **embeds the matched secret verbatim**.
- `review_feedback` on a build failure, because that is javac output, which echoes source lines.
- `original`, `transformed`, `diff`, `build_output`, and acceptance evidence.

One test plants a marker in every field that carries file bytes and asserts it reaches no
observation, no `ToolMessage` and not the state block — while the browser's card still carries it.

**The admission gate is an allow-list, not a deny-list.** `parse_partial_json` silently *repairs*
truncated tool arguments: `{"pack":"javax-to-jakarta","dry_r` accumulates into a valid-looking
`{"pack":"javax-to-jakarta"}` with `dry_run` gone. Pressing Stop on a proposed dry run could
otherwise have started the real one. Tool calls execute only when the stream completed normally,
the stop reason is in `{tool_use, end_turn}`, and cancel is not set.

**`risk_ceiling` is overwritten after every `resolve_intent`.** Intent decisions with provenance
`prompt` outrank config, so a model-authored sentence containing "don't bother reviewing" could have
yielded `risk_ceiling: auto` — which `must_hold` reads to write every HIGH-risk unit straight to
disk with no human in the loop. The toolbox writes the config's value back, every time.

**Review cards carry a run stamp.** Every run overwrites the one queue file, transcripts keep cards
forever, and `find_entry` falls back to a unique-basename match. Without the stamp, scrolling up and
approving an old card could apply a different pack's transform to a different file.

**`landing.py` refuses rather than repairs.** Not a git work tree, a dirty tree, an existing branch,
a bad ref name — each is a distinct message. It never stashes, never forces, never amends, never
pushes. It stages only the paths it copied, never `-A`. It never commits FORGE's own artifacts, and
a test cross-checks `ARTIFACT_NAMES` against `ui/app.py`'s `ARTIFACTS` so a new artifact cannot
silently start being committed. It copies only files the run manifest (`.forge-writes.json`) or an
approval in `decisions-applied.jsonl` accounts for; any other file in the output directory is left
behind and named in the result (`skipped`), so a stray fixture cannot ride into the commit.

---

## 8. Test generation

`forge/testgen/` — 10 modules, ~1,400 lines. Generates JUnit 5 + Mockito tests for classes the
migration wrote, on its own small graph (`targets → context → generate → review → write`), with its
own thresholds and retry budget.

**Which classes get a test, and where it lands, are decided in code — never by the model.**
`targets.py` classifies by Spring/JPA annotations and name suffixes; the path is
`src/test/java/<pkg>/<Type>Test.java`. An existing test is **never overwritten** — it is a human's
work. This is also the most deeply Java-coupled subsystem after the profiler.

---

## 9. Infrastructure

| Module | Lines | What it does |
|---|---|---|
| `forge/state_store/dynamodb.py` | 231 | Per-file status, and the LangGraph checkpointer. |
| `forge/utils/file_scanner.py` | 260 | Decides which files a pack takes; reports degraded packs. |
| `forge/utils/run_manifest.py` | 132 | Which pack wrote and retired which file — the overlap guard and the chained view. |
| `forge/utils/java_checks.py` | 99 | Java package and import parsing. |
| `forge/utils/cost.py` | 52 | Token accounting from `usage_metadata`. |
| `forge/utils/telemetry.py` | 97 | CloudWatch metrics. |
| `forge/utils/llm_json.py` | 26 | Extracts JSON from a model reply. |
| `forge/utils/report.py` | 83 | `migration-report.md`. |
| `forge/utils/fs.py` | 19 | Excluded directories, test-path predicate. |

**`cost.py` fails open, and that is worth knowing.** A model missing from `model_pricing` accrues
**$0.00** rather than raising. Cost reporting lies rather than errors — so adding a model without
pricing it makes every estimate silently wrong.

**`file_scanner.py` refuses a pack it cannot serve properly.** If a pack's `applies_to` names a
selector with no registered extractor, the scanner refuses the pack — including the mixed case where
some globs *would* have matched. Migrating a subset of a pack's intended file set is worse than not
running it.

---

## Where the Java assumptions actually live

Useful if you are considering a non-JVM stack. There is **no AST library anywhere** — no `javalang`,
no tree-sitter, no ANTLR. The whole system is regex and `ElementTree`, which ports more easily than
a parser-based design.

**Genuinely language-agnostic:** the LangGraph pipeline, retry and score routing, the hold gate,
staging, the review queue, `decisions.py`, guardrails, the secret scan, the extractor protocol, the
transform agent, the reviewer, `build_verification.mode: "command"`, and the `file_glob`,
`content_match`, `xml_element` and `decision_equals` detect kinds.

**Java-specific in code, not just prompts:**

| Where | What it assumes |
|---|---|
| `discover/profile.py` | ~300 lines of Maven/Gradle/Ant semantics; `Dependency` *is* a Maven coordinate |
| `packs/loader.py:98-109` | `_coordinates()` enforces `group:artifact[:version]` — `@angular/core` fails at load |
| `utils/java_checks.py:71` | `^\s*package\s+([\w.]+)\s*;` — this is what `scope_package_prefix` filters on |
| `utils/fs.py:17-19` | `is_test_path` is the literal string test `"src/test" in rel_path` |
| `risk/score.py:106,123,135` | every HIGH-risk rule is gated on `suffix == ".java"` |
| `utils/file_writer.py:12-29` | reconstructs `src/main/java/<pkg>/<Type>.java` from a package declaration |
| `agents/guardrails_post.py:69` | the `javax.*` sweep, with no hook to substitute another |
| `testgen/` | `fqcn`, `<Type>Test.java`, JUnit, Spring/JPA classification |

**Four of these fail silently rather than loudly** on a non-Java repo: `scope_package_prefix` never
fires, the risk ceiling holds nothing, test files migrate as production code, and the compile gate
is off by default. A run will look like it worked.

---

## See also

- `ARCHITECTURE.md` — how the pipeline is wired, node by node
- `GUARDRAILS.md` — the six checks every file passes, and what each costs
- `INTENT.md` — prose → pack selection, and the eight reconciliation rules
- `EXTENDING.md` — adding a technology transition
- `../prompts/FORGE-Platform-Requirements.md` §1 — the pack contract, for authors
