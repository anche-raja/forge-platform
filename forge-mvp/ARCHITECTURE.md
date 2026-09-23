# FORGE MVP — Architecture (Phase 0)

> **Scope note.** This document describes the Phase 0 engine in `forge-mvp/`. Phase 1 — the
> pack library and context extractors that make it a generic J2EE migration platform — is
> summarised in §12 and specified in
> [prompts/FORGE-Platform-Requirements.md](../prompts/FORGE-Platform-Requirements.md); the
> local web UI and the service layer under both front ends are in §13.
> The engine started as a single-phase **Java 8 → 21 upgrade** pipeline. It is *not* the 15-agent vision in
> `FORGE-AgentDeepDive.pptx`. Per [PHASE0-SPEC.md](PHASE0-SPEC.md),
> Phase 0 is deliberately *"one transform agent, one review agent, nothing else."* The deck is the target end-state; this is the foundation.

---

## 1. What the MVP does

Takes a Java source file, runs it through a safety + transform + review + safety pipeline on AWS
Bedrock, and writes the upgraded file to `./migrated/` — with a full audit trail (review score,
retry count, model pair, guardrail verdicts) tracked in DynamoDB.

The single transformation it performs (Java 8 → 21):
- `javax.*` → `jakarta.*` namespace migration (zero-tolerance)
- Deprecated API replacement (`Thread.stop()`, `finalize()`, `StringBuffer`-in-loops, …)
- Date/Time modernisation (`new Date()` → `Instant.now()`, `Calendar` → `LocalDateTime`, …)
- Conservative `var` inference
- Flags `sun.misc.Unsafe` / reflective access for manual review (does not change them)

That is the built-in `java21` phase. Every other transition — Jakarta namespace, Spring 5 → 6,
Spring Security, Struts 2 → 7, JSP/JSTL, JUnit 5, the WAR bootstrap, the Liberty `server.xml` —
is a **pack** loaded from `prompts/packs/` and selected with `--phase <pack-id>` (§12).

After a migration, **test generation** (§14) writes the JUnit 5 tests the legacy code never had
for the classes that run actually wrote — `--generate-tests`, or `--generate-tests-only` over an
output directory from an earlier run.

The transformation rules live in [forge/phases.py](forge/phases.py), paired with the reviewer
rubric that grades them in one `PhaseSpec`; [forge/agents/java_upgrade.py](forge/agents/java_upgrade.py)
reads the transform prompt from there. Change a prompt and its rubric together. A pack's rules
come from its `.pack.md` file instead.

---

## 2. Tech stack

| Layer | Choice |
|---|---|
| Language / runtime | **Python 3.11+** (tested on 3.12) |
| Orchestration | **LangGraph** — `StateGraph` state machine ([forge/graph.py](forge/graph.py)) |
| LLM client | **LangChain** `langchain-aws` → `ChatBedrockConverse` |
| Transform model | **Claude Opus 4.8** (`us.anthropic.claude-opus-4-8`, 1M context, 128K output) on AWS Bedrock — reachable only through the `us.` cross-region inference profile |
| Review model | **Amazon Nova Pro** (`us.amazon.nova-pro-v1:0`) on AWS Bedrock — *different model family, deliberate cross-validation* |
| Safety | **AWS Bedrock Guardrails** — standalone `ApplyGuardrail` API ([forge/guardrails/bedrock_guardrails.py](forge/guardrails/bedrock_guardrails.py)) |
| State + checkpoints | **AWS DynamoDB** — 2 tables (app state + LangGraph checkpointer) ([forge/state_store/dynamodb.py](forge/state_store/dynamodb.py)) |
| Config | **PyYAML** — single `agents.yaml` read at startup ([forge/config.py](forge/config.py)) |
| Secrets / env | **python-dotenv** (`.env`) |
| Observability | **LangSmith** (env-var driven, no code change) |
| Packs | Markdown + YAML front matter in `prompts/packs/`, parsed by `forge/packs/loader.py` |
| Web UI | **FastAPI + uvicorn** on loopback, vanilla JS front end, no build step (§13) |
| Infra (provisioning) | **Terraform** in `forge-terraform/` (DynamoDB, Guardrails + published version, IAM incl. inference profiles, CloudWatch; Phase 6 modules behind `enable_*` flags) — or `infrastructure/create_dynamodb.py` for local dev |

> The spec pins `langgraph>=0.2 / langchain>=0.3`; the graph also compiles and tests pass under
> the current `langgraph 1.x / langchain 1.x` line.

**Not in the MVP** (despite being in the deck): Strands Agents, SQS, the Containerize agent, the
5-agent review board, and `@tool` function-calling. The deck's **Test-Gen** agent is built — as a
transform/review pair around a graph of its own (§14), not as a tool-calling agent. Agents use plain system-prompt + `invoke`. The deck's Discovery agent,
Risk-Scorer, review portal and **Leader Agent** exist in deterministic form — `--discover`,
`forge/risk/`, the review page + web UI, and the LangGraph state machine itself — rather than as
model-driven agents. The Leader's six duties map onto the graph one for one; see §3.

---

## 3. Pipeline graph

The pipeline is a LangGraph `StateGraph` invoked **once per file** (`thread_id = file_path`).

```
                         ┌──────────────────┐
            entry ──────▶│  guardrails_pre  │  secret gate (local) · size (local)
                         └────────┬─────────┘  Guardrails INPUT · model check opt-in
                                  │
                     BLOCK ◀──────┤──────▶ PASS
                        │                  │
                  ┌─────▼────┐       ┌─────▼────────┐
                  │ blocked  │       │ java_upgrade │  Opus 4.8 — transform
                  └─────┬────┘       └─────┬────────┘  (injects review feedback on retry)
                        │                  │
                        │           ┌──────▼────────┐
                        │           │ java_reviewer │  Nova Pro — score 0–100
                        │           └──────┬────────┘
                        │                  │
                        │   ┌──────────────┼───────────────────┐
                        │   │ score≥80     │ 50–79 & retry<2    │ <50 or retries exhausted
                        │   │              │ (retry_count++)    │
                        │   ▼              ▼                    ▼
                        │ ┌──────────────┐ └─▶ java_upgrade ┌─────────────┐
                        │ │guardrails_post│   (loop back)    │manual_queue │
                        │ └──────┬────────┘                  └──────┬──────┘
                        │   PASS │ BLOCK ─────────────────────────▶ │
                        │        ▼                                  │
                        │  ┌────────────┐                           │
                        │  │ write_file │ → ./migrated/<pkg path>   │
                        │  └──────┬─────┘                           │
                        │         │                                 │
                        └─────────┴──────────┬────────────────────-─┘
                                             ▼
                                      ┌──────────────┐
                                      │ update_state │ → counters
                                      └──────┬───────┘
                                             ▼
                                            END
```

Source of truth: [forge/graph.py](forge/graph.py). Routing functions: `route_pre`,
`route_reviewer`, `route_post`, `route_verify`.

### The deck's Leader Agent

`FORGE-AgentDeepDive.pptx` puts a **Leader Agent** at the centre of this picture — "The Conductor",
on Sonnet 4.5, one of the deck's 15 agents. There is no such node here, and its absence is a
substitution rather than an omission: the graph above *is* the Leader. Every duty the deck gives it
is a static edge or a plain Python function, and no model ever names the next step.

| Leader duty (deck slide 7) | Implementation |
|---|---|
| Read the project log, find the next `PENDING` | [forge/service.py](forge/service.py) `run_migration()` — unit order is the scanner's `sorted()` output, generated targets last; `--resume` reads `get_files_by_status("PENDING")` |
| Select the right specialist | [forge/utils/file_scanner.py](forge/utils/file_scanner.py) `scan_java_files()` plus the pack's `applies_to` and `--phase` — globs, content regexes and a package-prefix compare |
| Send the file to the transform agent, injecting retry feedback | the `increment_retry → java_upgrade` edge; the feedback blocks are assembled in [forge/agents/java_upgrade.py](forge/agents/java_upgrade.py) |
| Read the review verdict | `route_reviewer` — reads the integer `review_score`, *not* the reviewer's own `review_verdict` string, which is recorded but never routed on |
| Decide: approve, retry, or escalate | `route_reviewer`, `route_post`, and the `must_hold` risk gate, all against `agents.yaml` thresholds |
| Update the project log | the `update_state` node |

Why it stays this way: orchestration decisions are mechanical, and this repo has already paid for
handing a mechanical question to a model — asking one whether a file was in scope sent both early
live runs to `MANUAL_REVIEW`, the second at a *passing* score of 80 — guarded now by
[tests/test_scope.py](tests/test_scope.py) and recorded in `../CLAUDE.md`. The model's influence is
deliberately bounded on both sides: it returns a score, code picks the branch, and `max_retries`
caps the loop.

There **is** a model-driven leader now — [§16](#16-the-leader-agent) — and this table is still
accurate, because it is a table of what happens *inside a run*. The leader chooses which run to
start and explains the result; every duty listed above stays the static edge or plain function it
already was. The failure mode this section warns about is a model being asked whether a file is in
scope, and that question is still never asked.

One deliberate drift from the deck while we are here: it specifies **Sonnet 4.5** for the transform,
leader and guardrails roles. This implementation uses **Opus 4.8** for transform and guardrails (§2);
review is Nova Pro, which matches.

---

## 4. Nodes

| Node | Model / service | Role | Outcome |
|---|---|---|---|
| `guardrails_pre` | local code, then Bedrock Guardrails (INPUT); **Opus 4.8** only if `preflight_model_check` | Four ordered steps: the local secret gate, a local LOC ceiling, the guardrail policy on INPUT, and an opt-in model check that is **off by default** and asks nothing about secrets, PII or packages. Detail in [GUARDRAILS.md](GUARDRAILS.md) | `SECRET_BLOCKED_LOCALLY` / `TOO_LARGE` / guardrail intervention → `blocked`; else `TRANSFORMING` |
| `java_upgrade` | **Opus 4.8** | Transform Java per 5 rules; on retry, injects prior review feedback into the prompt | `transform_output` (JSON: files + manual_flags) |
| `syntax_check` | local `javac` (parse only) + XML parser | When `syntax_check: true`: every Java file in the output is parsed by javac stopped at the PARSE stage — no classpath, so an unresolved import is not an error — and XML is checked for well-formedness. [forge/verify/syntax.py](forge/verify/syntax.py) | FAIL → `increment_retry` with javac's errors as the feedback (the review is never called on a broken answer), `manual_queue` once retries are spent; PASS / SKIPPED (no javac) → `java_reviewer` |
| `java_reviewer` | **Nova Pro** | Score 0–100 across 5 weighted checks; emit verdict + feedback | `PASS≥80` / `RETRY 50–79` / `MANUAL<50` |
| `guardrails_post` | Bedrock Guardrails (OUTPUT) + **Opus 4.8** | Verify zero `javax.*` left, no new security issues, naming | `BLOCK` → `manual_queue`; else continue |
| `hold_for_review` | local FS | Stage transformed files under `./migrated/.forge-staging/` when `decisions.risk_ceiling` says a human decides first | status `HELD` |
| `write_file` | local FS | Write transformed files to `./migrated/` preserving package path (no-op on `--dry-run`) | status `DONE` |
| `manual_queue` | — | Mark file for human review | status `MANUAL_REVIEW` |
| `blocked` | — | Terminal block | status `BLOCKED` |
| `update_state` | — | Increment run counters (processed/passed/retried/manual/blocked) | → `END` |

### Retry + feedback loop
`route_reviewer` ([graph.py:126](forge/graph.py#L126)): a `RETRY` verdict (score 50–79) with
`retry_count < max_retries` (default **2**) increments the counter, stamps status `RETRY_n`, and
routes back to `java_upgrade`. The transform agent reads `review_feedback` from state and appends
it to its prompt ([java_upgrade.py:68](forge/agents/java_upgrade.py#L68)). Exhausted retries →
`manual_queue`.

> **Cross-model validation:** the transform is written by Claude Opus 4.8 and graded by Amazon
> Nova Pro — two different model families. The two guardrail nodes also use Opus 4.8 as a
> second-pass reasoning check *in addition to* the deterministic Bedrock Guardrails policy.

> **Build verification is opt-in.** `verify_build` runs `javac` (per file) or `mvn compile`
> (per output project) when `build_verification.enabled` is set; a failure re-enters the retry
> loop with the compiler output as feedback. Off by default because single-file `javac` needs the
> project's classpath. When it is off, "syntactically valid" is the reviewer's opinion, not a
> compile — see §11.

---

## 5. State model

`ForgeState` (graph-level) carries one `current_file` (`FileStatus`) plus run-level counters and
config — see [forge/state.py](forge/state.py).

**File state machine:**
```
PENDING → TRANSFORMING → REVIEWING → RETRY_1 → RETRY_2 → DONE
                              │            │               ▲
                              │            └──▶ HELD ──approve──┘   (staged; human decides)
                              │                   └──reject──▶ REJECTED
                              └──────────────────────────▶ MANUAL_REVIEW ──approve/retry──▶ …
   (pre-flight)  ────────────────────────────────────────▶ BLOCKED
```
`HELD` is reached from `guardrails_post` when `risk_ceiling` holds the unit's risk tier; a human's
`--apply-decisions` moves it on. Every unit carries `risk_score`/`risk_tier`/`risk_reasons` from
the pre-flight node and, once decided, `human_decision`/`human_note`/`human_rule`/`human_decided_at`.

Per-file audit fields persisted: `review_score`, `review_verdict`, `retry_count`,
`transform_model`, `review_model`, `guardrail_pre_verdict`, `guardrail_post_verdict`,
`guardrail_findings[]`, `error`.

---

## 6. Data stores

| Store | Purpose | Schema |
|---|---|---|
| `forge-migration-state-dev` (DynamoDB) | Per-file final status / audit trail | PK `file_path`; GSIs `status-index`, `phase-status-index` |
| `forge-langgraph-checkpoints-dev` (DynamoDB) | LangGraph checkpointer (resumable runs) | PK `thread_id` + SK `checkpoint_id` |
| `./migrated/` (local FS) | Transformed output, package paths preserved | — |
| `manual-review-queue.json` (local) | v2: every unit a human must look at — original, transformed, verdicts, risk reasons; written in dry-run too. Accumulates across the packs of a plan: keyed by pack + `rel_path`, each entry stamped with the `run` that produced it; a newer run of a pack replaces only that pack's entries, and a dry run never displaces a real held one | `forge/review_queue.py` |
| `migration-review.html` (local) | Static review page over the whole accumulated queue: side-by-side + diff + decision widget → `decisions.json` | `forge/review_queue.py` |
| `./migrated/.forge-staging/` (local) | Held units, in migrated layout, until approved | `forge/utils/file_writer.py` |
| `decisions-applied.jsonl`, `pack-feedback.md` (local) | Decision audit log; notes grouped by pack and rule | `forge/decisions.py`, `forge/feedback_report.py` |
| `migration-report-<pack>.md`, `migration-acceptance-<pack>.json` (local) | One pack's latest run and acceptance record, kept until that pack runs again | `forge/utils/report.py`, `forge/service.py` |
| `migration-report.md`, `migration-acceptance.json` (local) | The latest run, whichever pack it was (kept for compatibility) | `forge/utils/report.py` |
| `migration-summary.md` (+ `migration-summary.json`) (local) | Plan-level summary: one row per pack (files, passed, manual, blocked, held, awaiting review now, cost, acceptance) and the project build; regenerated after every run, build and applied decision | `forge/utils/report.py` |

Tables: create with [infrastructure/create_dynamodb.py](infrastructure/create_dynamodb.py) (dev)
or `forge-terraform/modules/foundation` (prod).

---

## 7. Configuration

Single file [agents.yaml](agents.yaml), loaded by `ForgeConfig`. Key knobs:

| Key | Default | Meaning |
|---|---|---|
| `transform_model` | `us.anthropic.claude-opus-4-8` | Transform + guardrail-reasoning model |
| `review_model` | `…amazon.nova-pro…` | Reviewer model |
| `pass_threshold` | `80` | Score ≥ → PASS |
| `retry_threshold` | `50` | Score ≥ (and < pass) → RETRY |
| `max_retries` | `2` | Retry cap before MANUAL |
| `scope_package_prefix` | `com.corp` | Files outside scope are flagged |
| `complexity_block_threshold` | `2000` | LOC ceiling for auto-transform |
| `guardrail_id` / `guardrail_version` | *(placeholder)* | Bedrock Guardrail to apply |
| `test_generation` | *(block)* | Test-Gen (§14): models, thresholds, `overwrite`, `kinds`, `run_tests` |

In production `agents.yaml` is generated from Terraform outputs via
`forge-terraform/scripts/generate-agents-yaml.sh`.

---

## 8. Entry point & CLI

[migrate.py](migrate.py) — scans for `.java` files (skips `src/test` and `DO NOT EDIT`
generated files via [file_scanner.py](forge/utils/file_scanner.py)), marks them `PENDING`, then
invokes the graph per file.

**Files run in parallel.** `max_parallel_files` in `agents.yaml` (8 in the generated config, 1 when
the key is absent) sets how many files of one pack are in flight at once. Each file is its own graph
invocation with its own state and checkpoint thread, so they do not interact. `service.run_migration`
keeps what depended on the old sequential loop:

- **Scan order in the record.** Results are collected by scan index, so the report, review queue,
  run manifest and summary read the same whatever order files finished in.
- **Progress counts completions.** A `file` event's `index` is "k-th to finish", so `[k/total]`
  still counts 1..n. With one worker it equals the scan index, byte for byte.
- **Generated targets last.** They run as a second phase, after every real file has finished.
- **Cancel starts nothing new.** Files already running finish and are kept.
- **Maven build verification is sequential.** `mvn` compiles the whole output tree.

Thread safety: boto3 clients are shared (they are thread-safe; the Bedrock connection pool is sized
to `2 × max_parallel_files`), DynamoDB `Table` resources are per thread because resources are not.
The one-job-at-a-time rule in the UI is unchanged: it protects *across* runs, since the extract cache
is cleared at the start of each run.

```bash
# Single file
python migrate.py ./myapp --phase java21 --file src/main/java/com/corp/UserAction.java

# Whole project, no writes / no DynamoDB updates
python migrate.py ./myapp --phase java21 --dry-run

# Resume only PENDING files from a prior run
python migrate.py ./myapp --phase java21 --resume
```

Flags: `--phase` (a built-in phase or any complete pack — see §12), `--dry-run`, `--resume`,
`--file`, `--output-dir` (default `./migrated`), `--config`, `--no-metrics`, `--list-packs`,
`--discover`, `--intent` (§15), `--acceptance` / `--acceptance-only` / `--acceptance-build`, `--build-project`
(the project's own build over source + output; exit 1 on a failure, 2 when nothing could be built), `--generate-tests` /
`--generate-tests-only` / `--run-tests` (§14), `--apply-decisions`, `--feedback-report`, and
`--ui` / `--port` / `--no-browser` for the local web UI (§13).

`migrate.py` is a thin printer: every command calls a function in [forge/service.py](forge/service.py)
and turns its events into the lines above. The web UI calls the same functions.

> **Note:** `--dry-run` skips file writes and the DynamoDB *state* update, but the graph still
> calls **Bedrock** (guardrails + both models) and the **checkpointer still writes** to DynamoDB.
> There is no fully offline run mode.

---

## 9. Prerequisites to run a live migration

1. `pip install -r requirements.txt` (includes `fastapi` / `uvicorn` for `--ui`).
2. The Phase 0 infrastructure applied from `forge-terraform/` — two DynamoDB tables, the
   guardrail and its published version, the execution role, the log group and alarms.
3. `agents.yaml` generated from the Terraform outputs
   (`forge-terraform/scripts/generate-agents-yaml.sh dev --out agents.yaml`). The checked-in
   `agents.yaml.example` carries a `REPLACE_WITH_GUARDRAIL_ID` placeholder and is not runnable.
   Regenerate after any Terraform change: a guardrail edit publishes a new version number.
4. AWS credentials (`AWS_PROFILE` or env vars) that can call Bedrock, DynamoDB and CloudWatch —
   the execution role via `aws sts assume-role`, or your own identity with equivalent rights.
   Bedrock permissions must cover the **inference profile** (`inference-profile/*` in the
   account) *and* `foundation-model/*` in every region the `us.*` profile routes to; the
   Terraform role does.
5. **Bedrock model access** enabled for Claude Opus 4.8 and Amazon Nova Pro in `us-east-1`,
   `us-east-2` and `us-west-2` (the cross-region profile's destinations).
6. Optional: a Java toolchain on the path if `build_verification.enabled` or `--acceptance-build`.

---

## 10. Layout

```
forge-mvp/
  migrate.py                       # CLI entry point — a printer over forge/service.py; --ui
  agents.yaml                      # single config file (generated from Terraform outputs)
  agents.yaml.example              # documented template incl. decisions / risk / context blocks
  forge/
    service.py                     # the one implementation of packs/discover/run/acceptance/apply/feedback
    ui/                            # local web UI: app.py (FastAPI routes), jobs.py (job registry), server.py, static/
    config.py                      # YAML loader; with_overrides() for per-run decisions
    state.py                       # ForgeState / FileStatus / state machine
    graph.py                       # LangGraph wiring (the pipeline) incl. the hold gate
    phases.py                      # built-in phases + every pack, resolved by name
    packs/                         # spec.py, loader.py, glob.py — parse and validate prompts/packs/*.pack.md
    discover/                      # profile.py, resolve.py, emit.py — stack profile → pack activation
    intent/                        # vocabulary.py (the closed world), agent.py (one call, metadata only),
                                   #   resolve.py (reconcile — pure, holds the rules), plan.py (IntentPlan)
    extract/                       # web_bootstrap.py, selectors.py — deterministic context extractors
    context/                       # render.py, inject.py, snapshot.py — context into prompts, bounded
    risk/score.py                  # deterministic risk score and tier
    testgen/                       # test generation: targets.py (which classes), checks.py (mechanical
                                   #   invariants), context.py (collaborator API + test libraries),
                                   #   writer.py (where a test lands), runner.py (execute), graph.py,
                                   #   report.py, settings.py, state.py
    review_queue.py                # manual-review-queue.json v2 + migration-review.html
    decisions.py                   # approve / reject / retry from a decisions file or the UI
    feedback_report.py             # reviewers' notes grouped by pack and rule
    agents/
      base.py
      guardrails_pre.py            # risk score, Bedrock Guardrails (INPUT), Opus pre-flight
      java_upgrade.py              # the one transform agent (Opus 4.8) + pack prompt + context
      guardrails_post.py           # Bedrock Guardrails (OUTPUT), zero-javax check, Opus post-check
      test_gen.py                  # the Test-Gen agent (Opus 4.8) — one class in, one test class out
    review/
      base_reviewer.py
      java_reviewer.py             # the one review agent (Nova Pro) + pack rubric
      test_reviewer.py             # grades a generated test (Nova Pro) against the JUnit 5 rubric
    guardrails/
      bedrock_guardrails.py        # ApplyGuardrail wrapper
    verify/
      build_verifier.py            # javac / mvn / command compile gate
      acceptance.py  merged_tree.py  # pack acceptance checks over source ⊕ migrated
    state_store/
      dynamodb.py                  # state manager + LangGraph checkpointer
    utils/
      file_scanner.py  file_writer.py  report.py  java_checks.py  telemetry.py  cost.py
  infrastructure/
    create_dynamodb.py             # dev table creation (non-Terraform)
  tests/                           # fully mocked (conftest.mocked_aws / mocked_testgen)
```

---

## 11. Known gaps

Deliberate limitations, not bugs. The actionable backlog lives in [TODO.md](TODO.md).

- **Nobody has measured whether the output is good.** The suite mocks `ChatBedrockConverse`, so it
  proves orchestration, not migration quality. There is no recorded pass rate from a real
  multi-file run anywhere in the repository. This is the largest open question about the system.

- **Build gate is per file unless `mode: maven`.** A whole-module compile after a batch is the
  reliable form; per-file `javac` needs the classpath configured.
- **Four runnable packs run without the context they declare.** Runnability turns on *selectors*,
  not on context availability, so a pack whose file set is pure globs runs even when its declared
  extractor is unbuilt — `build-maven-modernize` (`reactor`), `spring-to-spring6`
  (`spring_bean_graph`), `jsp-jstl-modernize` (`view_bindings`), `junit4-to-junit5`
  (`test_subject`). The transform sees only each file's own bytes, and the reviewer loses the
  descriptors it would have cross-checked against. `degraded_phases()` reports them,
  `--list-packs` labels them, and each affected unit carries `context_missing: true`. A pack that
  needs a *selector* is refused outright instead.
- **The profile is written but not yet consumed.** `--discover` produces `forge-profile.yaml`;
  a run still takes one `--phase` at a time. The chat leader sequences the plan instead (§16).
- **Packs do not compose.** Each reads the original source, so two over the same file would have
  the second replace the first. Refused by `PackOverlap`; chat chains instead (§16).
- **`routing_parity` is declared by `struts2-modernize` and skipped**, because the
  `struts_routing_table` extractor does not exist.
- **Review is single-user.** The web UI (§13) runs on loopback for one engineer with one job at
  a time; the static review page + `--apply-decisions` is the CI-friendly form. There is no
  shared, multi-user review service.
- **Generated tests are unit tests, and nobody measures them.** Test-Gen (§14) writes one test
  class per class under test with collaborators mocked. It does not write integration tests, does
  not start a Spring context, does not measure coverage, and never edits the build file — the test
  dependencies it needs are reported, not added.
- **The guardrail is only as good as its entity list.** Any intervention on INPUT blocks the
  file, so the Terraform guardrail lists secrets only (never `EMAIL` / `IP_ADDRESS`); an
  operator who adds entity types in the console can block ordinary code.

---

## 12. Phase 1 — packs, extractors, context

The engine above is unchanged. What Phase 1 adds is *what it runs* and *what it is given*.

| Piece | Where | What it does |
|---|---|---|
| Pack library | `../prompts/packs/*.pack.md` | One technology transition each: detection rules, transform rules, a rubric totalling 100, mechanical acceptance checks. Loaded by `forge/packs/loader.py`; ordered by `depends_on` then tier. |
| Registry | `forge/phases.py` | `get_phase()` resolves a built-in phase or a pack; `PHASE_NAMES` feeds `--phase`. A broken library degrades to the built-ins with a warning. |
| Scanner | `forge/utils/file_scanner.py` | Matches `file_glob` and `content_match` selectors itself; resolves `selector:` entries through the pack's extractor, or refuses the pack if that extractor is not registered. Reports `generated` targets a pack creates. |
| Extractors | `forge/extract/` | Deterministic parsers named by a pack's `context:`. `web_bootstrap` covers `web.xml` (all namespaces), JBoss/WebLogic/WebSphere descriptors (`.xml`/`.xmi`), EAR, datasources, Liberty `server.xml`; declaration order kept, nothing dropped, literal secrets masked. Cached per module, never in state. |
| Context | `forge/context/` | `render_context` → a deterministic block under `context.max_chars`; `context_block_for` appends it to the transform, review and (for generated units) pre-flight prompts; `snapshot` writes `migration-context.json`. |
| Discovery | `forge/discover/` | `--discover`: one walk builds a stack profile (build system, Java level, BOM-aware dependency versions, imports, descriptors); every pack's `detect` rules are evaluated against it with evidence. Writes `forge-profile.yaml` + `stack-profile.json`. |
| Human loop | `forge/risk/`, `forge/review_queue.py`, `forge/decisions.py`, `forge/feedback_report.py` | Risk score → `risk_ceiling` hold gate → review page → `--apply-decisions` (approve / reject / retry-with-note) → `--feedback-report`. The note is its own prompt block and outranks automated feedback. |
| Acceptance | `forge/verify/acceptance.py` | `--acceptance` / `--acceptance-only`: a pack's checks over the merged view (`merged_tree.py`). Pass / fail-with-evidence / skip-with-reason; `INCOMPLETE` while anything is skipped. Appends to the report, writes `migration-acceptance.json`, sets the exit code. |

**Runnable today** (`runnable_phases()`): `java21`, `build-maven-modernize`, `java8-to-java21`,
`javax-to-jakarta`, `spring-to-spring6`, `springsec-to-springsec6`, `struts2-modernize`,
`jsp-jstl-modernize`, `junit4-to-junit5`, `webapp-bootstrap-jakarta10`, `liberty-server-config`.

Four of those run **without the context they declare**, because runnability turns on selectors
rather than context availability: `build-maven-modernize` (`reactor`), `spring-to-spring6`
(`spring_bean_graph`), `jsp-jstl-modernize` (`view_bindings`) and `junit4-to-junit5`
(`test_subject`). `degraded_phases()` reports them and `--list-packs` labels them; each affected
unit carries `context_missing: true`.

**Two decisions fixed at platform level** (see the requirements spec): the target is a **WAR on
WebSphere/Open Liberty at Jakarta EE 10, Spring Framework 6.2, no Spring Boot**; and
`web_framework: modernize-in-place` (Struts → Struts 7) is the **only** route. The Struts → Spring
MVC packs and the `struts-spring6` built-in phase were removed, and `migrate-to-spring` with them —
a decision value no pack implements resolves to a plan that selects nothing.

---

## 13. Local web UI

`python migrate.py --ui` serves [forge/ui/app.py](forge/ui/app.py) with uvicorn on `127.0.0.1`
and opens the browser. It replaces nothing: the CLI stays for CI, and both sit on the same
service layer.

**Service contract** ([forge/service.py](forge/service.py)). Plain functions, no argparse, no
`print`, no `sys.exit`: `packs()`, `discover()`, `run_migration()`, `acceptance()`,
`generate_tests()`, `apply()`, `feedback()`. Progress is an `on_event(dict)` callback with a `type`
key — `start`, `skipped`, `file`, `snapshot`, `queue`, `acceptance`, `summary`, `cancelled`,
`nothing`, `apply_outcome`, `apply_done`, `testgen_start`, `testgen_unit`, `testgen_summary`,
`testgen_cancelled`, plus `chained` and `context_missing` (§16). The CLI's `_print_event`
reproduces its historical stdout from those events;
`tests/test_service.py::test_cli_prints_exactly_the_historical_lines` pins it. "Nothing to do" is
`NoEligibleFiles`, which the CLI turns into exit 0. `run_migration` takes a `threading.Event` for
cancellation, checked between units; on cancel the report and queue are still written, because held
files are already staged and must not be orphaned.

**Job model** ([forge/ui/jobs.py](forge/ui/jobs.py)). A run or an apply is a `Job` on a daemon
thread. Events get a sequence number and are kept for the job's lifetime, so `/api/runs/{id}/events`
(Server-Sent Events, keep-alive comment every 15 s) can replay from `Last-Event-ID` after a reload
or a dropped connection. The terminal event is always `done` or `error`. **One job at a time**: the
registry raises `JobBusy` → HTTP 409 while any job is unfinished. That rule is what makes the rest
safe — the extract cache is process-global and cleared per run, and boto3 resources are not
thread-safe, so only the job thread ever touches the graph, DynamoDB, metrics or the cache.

**Routes.** `GET /api/packs` · `POST /api/discover` · `POST /api/runs` (202 + job id; 400 for a
phase that is not runnable or a bad path; 409 when busy) · `GET /api/runs/{id}` · `POST
/api/runs/{id}/cancel` · `GET /api/runs/{id}/events` · `GET /api/jobs` · `GET /api/review` (queue
metadata + the entry HTML from `review_queue.render_entries`, embedded in an iframe with the same
CSS as the static page, so the decision widget is one DOM contract) · `POST /api/review/decisions`
(validated by `decisions_from`, the same function `--apply-decisions` uses; runs as an `apply` job)
· `POST /api/acceptance` (synchronous; the page warns that `run_build` blocks) · `POST /api/testgen`
(202 + job id, like a run; 404 when there is no output directory to generate from) · `GET
/api/testgen?output_dir=` (the last `generated-tests.json`) · `GET /api/feedback` · `GET
/api/artifacts` · `GET /api/files?output_dir=&name=`.

**The page is a chat, and only a chat.** The nine-step wizard (Project → Intent → Discover → Run
→ Review → Accept → Tests → Feedback → Artifacts) was deleted: the owner counted the steps and
asked for *"prompt instead of this project setup"*. `index.html` is one section, `app.js` is a
helpers module with no router, and every step became a card in the transcript — `plan`,
`evidence`, `estimate`, `confirm`, `review_file`, `acceptance`, `tests`, `feedback`, `artifacts`,
`land`. `forge/leader/` holds the agent that draws them, and `tests/test_ui_api.py` pins the
wizard's absence so it cannot come back a step at a time.

The leader asks which folder the repository is in and calls `set_project`, so `POST /api/chat`
takes an OPTIONAL `source_dir` and a turn with no project bound runs anyway — asking is the answer,
and it cannot be asked from behind a 400. Intent still costs one model call and discovery is still
free; the difference is that `resolve_intent` is a tool the leader chooses rather than a step the
user clicks. `land_on_branch` is the one thing in FORGE that writes into the user's own repository:
a new branch, one commit, never a push, and always a click.

The routes above all remain — the CLI and the tests use them and they are the service layer's HTTP
face — but the browser now calls only `/api/chat`, `/api/runs/{id}/events`, `/api/review` (to ask
which held entries are still staged) and `/api/files`.

**Per-run decisions.** The decisions the Discover step used to offer are the ones `resolve_intent`
maps a plain-English request onto; the values reach the packs, the hold gate and the acceptance
checks through `ForgeConfig.with_overrides({"decisions": ...})` — a deep-copied config, nested
mappings merged, no temp YAML and no environment variable. `risk_ceiling` is the exception: it
never comes from a model-authored sentence, because the leader may not lower the hold gate.

**Boundaries.** Loopback only (there is no `--host`); no authentication because there is no network;
`/api/files` refuses absolute names, `..` and symlink escapes (403) and serves nothing outside the
chosen output directory. The front end is three static files (`forge/ui/static/`), vanilla JS with
`fetch` + `EventSource`, no build step and no CDN — it must work on a machine with no internet.
Stopping the server kills a job mid-unit; *Cancel* is the clean stop and `--resume` recovers as
before.

---

## 14. Test generation

The migration answers "did it transform correctly?" (the reviewer) and "does it still compile?"
(the build gate). Neither answers *does it still do what it did* — and a legacy J2EE codebase is
the least likely place to find the tests that would. Test-Gen writes them.

```bash
python migrate.py ./app --phase javax-to-jakarta --output-dir ./migrated --generate-tests
python migrate.py ./app --generate-tests-only --output-dir ./migrated        # over an earlier run
python migrate.py ./app --generate-tests-only --output-dir ./migrated --run-tests
```

**It runs over the output tree, not the source tree.** "The new code" is what the migration wrote;
a class it never touched already has whatever tests it always had. Chained onto a run, the scan is
narrowed further to that run's `written_paths`, so a second pack over the same project costs
nothing for the files the first one migrated.

**Graph** ([forge/testgen/graph.py](forge/testgen/graph.py)), one class under test per invocation,
`thread_id = testgen::<rel path>`:

```
testgen_pre ─▶ generate ─▶ static_checks ─▶ review ─▶ write_tests ─▶ run_tests ─▶ finish
                   ▲            │             │                          │
                   └── increment_retry ◀──────┴──────────────────────────┘
                                │
                                └─▶ hold  (staged under .forge-staging/, a human decides)
```

| Node | Model / service | Role |
|---|---|---|
| `testgen_pre` | local code | The secret gate and a size ceiling, ahead of every remote call. No model is asked anything. |
| `generate` | **Opus 4.8** | One class in, one `<Type>Test` out, with the collaborator signatures and the project's test libraries in the prompt |
| `static_checks` | local code | JUnit 4 imports, `javax.*`, a missing `@Test`, `@Disabled`, the class name and package, non-determinism, and a secret scan of the generated file |
| `review` | **Nova Pro** | Scores framework / coverage / assertions / isolation / faithfulness, 100 points, `PASS ≥ 75` |
| `write_tests` | local FS | `src/test/java/<pkg>/<Type>Test.java` inside the output directory |
| `run_tests` | `mvn` / `gradle` / a command | Opt-in. Runs the test against a materialised merged tree |
| `hold` | local FS | Stage under `.forge-staging/` with the reason — the test exists, but not in the build |

Five decisions are worth stating, because each one is the answer to a way this could go wrong.

**Which classes, and where the test lands, are decided in code.**
[targets.py](forge/testgen/targets.py) reads the type declaration, its annotations and its public
signatures, classifies it (controller / service / repository / entity / config / plain) and skips
what cannot be unit tested — an interface, an abstract class, a class with no public members, a
class that already has a test. Every skip is reported: a silently skipped class is
indistinguishable from one nobody thought about. The destination is derived from the file's own
`package` and type name, so unlike `write_output` the model's path is **discarded entirely** —
there is exactly one right place for `com.corp.UserServiceTest`, and the model's key adds only a
way out of the output directory.

**An existing test is never overwritten.** `overwrite` defaults to false. A test already in the
tree is a human's work and the one artifact this pipeline must not touch.

**The mechanical checks run before the review, not after.** JUnit 4 imports, `javax.*`, a missing
`@Test` and the wrong class name are invariants, so [checks.py](forge/testgen/checks.py) decides
them with regexes and a failure goes straight back to the generator — costing zero review calls.
It is `guardrails_post`'s zero-`javax` rule, one layer up.

**A test that is not good enough is staged, never written.** Below the pass threshold after its
retries, or failing a mechanical check, or having run and failed, the file goes to
`.forge-staging/` and the reason goes in the report. A broken test in `src/test/java` breaks every
build that follows it, so a failing generated test is also removed from the output tree before the
retry. Whether it is the test or the migrated code that is wrong is a human's call, and the
failure output is in the report either way — that signal is the most valuable thing this stage
produces.

**The prompt is told what the API is.** Its first rule is *never invent API*, which is only fair
if the API is supplied: [context.py](forge/testgen/context.py) resolves the collaborators the class
declares to files in the merged tree and renders their public signatures, and it lists the test
libraries the build files actually carry. A library that is absent is named as absent; what the
test needs and does not have is reported in `dependencies` rather than added, because FORGE does
not edit build files.

**Two model calls per class** — generate and review, cross-validated across model families exactly
as the migration is — and zero for a class the rules exclude or the secret gate stops. Prompts and
rubric live in [forge/phases.py](forge/phases.py) as a `TestGenSpec`, beside the migration's, and
`tests/test_testgen.py` asserts the weights still total 100 and still match the response schema.

**Artifacts.** `test-generation-report.md` (dependencies, per-class results, what was held and why,
members left untested, what was skipped) and `generated-tests.json` (the machine record; a held or
dry-run unit carries its generated source inline, since that file is nowhere else a reader would
look). `--generate-tests-only` exits non-zero when anything was held, blocked or failed, so it
gates CI. Test units are **not** written to the DynamoDB state table — that table is the
migration's audit trail, keyed by source file — and the two metrics emitted are `bedrock_calls`
and `estimated_cost_usd` only, never `files_processed`, which the PipelineStalled alarm counts.

---

## 15. Intent — plain English into a pack selection

Discovery answers *what is in this repository*. It cannot answer what is left over: **how much of
what is there did you want changed, and how aggressively?** Evidence says a project has JSPs, an
ORM and a risk profile; it does not say whether to move the view tier, keep the ORM mapping, or
hold every HIGH-risk file for a human. Ten `decisions` keys arbitrate that, and until now every one
was set by a human editing `forge-profile.yaml`.

```bash
python migrate.py ./app --discover --intent "latest Java and Spring, stay on Struts, ignore the db folder"
```

**It narrows; it never invents.** `resolve_packs` remains the only thing that decides what a
repository contains — a pack with no `detect` match cannot be activated by any prompt, and a request
for one is reported as `unsupported`. That is the limit CLAUDE.md's leader rule is really about:
pack activation is mechanical and stays mechanical, while intent→decisions is the one genuinely
linguistic step, replacing a human's YAML edit rather than the evidence engine. The leader in §16
calls `resolve_intent` as a tool and inherits exactly this bound. The order still comes from `resolve_order`, and the plan is persisted with
provenance, so it replays with zero model calls.

**No source code reaches the model.** It is given `Profile.to_json()` — build system, Java level,
dependency coordinates, import *prefixes*, descriptor *names*, counts. `_profile_block` assembles
those fields explicitly rather than dumping the profile, and a test asserts a file body never
appears in the prompt. This is §7 of [GUARDRAILS.md](GUARDRAILS.md) one layer up.

**Shape.** One model call produces a proposal; `reconcile` in
[forge/intent/resolve.py](forge/intent/resolve.py) checks it against the activations and returns an
`IntentPlan`. `reconcile` is pure, so its eight rules — include ⊆ activated, nothing silently
dropped, closed decision vocabulary, mutual exclusion between the Struts routes, state labels
survive, coherence via `missing_dependencies`, order from `resolve_order`, provenance on every
decision — are ordinary unit tests with no AWS. A response that will not parse becomes `None`, and
`reconcile(None, …)` returns the plan discovery would have produced: a guess is worse than a
default, so there is no retry.

**A decision can re-gate discovery.** `liberty-server-config` is gated on `container: liberty`, so
asking for Tomcat must stop it firing. `service.discover` re-runs `resolve_packs` with the resolved
decisions and re-reconciles the same proposal — pure, deterministic, no second call.

**Scope.** `scope.exclude_globs` and `scope.package_prefix` were in the generated profile and read by
nothing; `scan_java_files` now takes `exclude_globs` and matches with the same `glob_match` the
`file_glob` detect rules use. Excluding can only shrink the unit set, so it needs no ceiling.

**Cost.** ~2k tokens in, ~600 out — about half a cent on Haiku 4.5, which is what `intent.model`
defaults to. Name a model there and it must also be in `model_pricing`, or the cost accrues as zero.

`--discover` without `--intent` is unchanged: no model, no AWS.
`test_discover_without_intent_makes_no_model_call` pins that contract.

Full detail: [INTENT.md](INTENT.md).

---

## 16. The leader agent

`migrate.py --ui` opens a chat. A model sequences the work there: it reads the user's sentence,
picks which tool to call next, explains what came back, and stops to ask before anything costs
money. `forge/leader/` is that agent.

This reverses a standing rule. `CLAUDE.md` used to say *"do not add a model-driven leader"*, and the
objection behind it is still the right objection — a mechanical question handed to a model, at the
layer where a wrong answer is hardest to debug. What changed is the **scope of the question**, and
the split is enforced in code rather than in the prompt.

```
   THE MODEL DECIDES                    CODE DECIDES — unchanged
   ─────────────────                    ────────────────────────
   which tool to call next          │   which packs exist for a repo
   which pack to run when           │     resolve_packs over detect evidence
   when to stop and ask             │   which files a pack takes
   how to explain a result          │     scan_java_files · globs · scope
   which decisions to PROPOSE       │   everything inside a run
                                    │     route_reviewer · max_retries · hold gate
                                    │   what a decision may be  (DECISION_OPTIONS)
                                    │   the risk ceiling        (never from a prompt)
                                    │   whether an approval happens (always a click)
```

Four locks make that hold:

1. **`selected_packs` is the bound.** A pack the leader names that discovery did not select never
   reaches `service`. `activations` is the wrong bound — it still lists packs an intent plan
   deliberately excluded.
2. **`forge/graph.py` is untouched.** Unit order, `route_reviewer`, `max_retries` and `must_hold`
   are the same code as before. The leader chooses *which run*, never what happens inside one.
3. **Spend and mutation are a click.** Anything over `leader.confirm_above_usd` parks as a card;
   `apply_review_decisions` and `land_on_branch` are confirmed whatever the estimate, because an
   approval is a human's signature on someone else's code.
4. **`risk_ceiling` never comes from a prompt.** The toolbox overwrites it with the config value
   after every `resolve_intent` — an intent decision carries provenance `prompt`, which outranks
   config, so a sentence containing "don't bother reviewing" could otherwise have yielded
   `risk_ceiling: auto` and sent every HIGH-risk unit straight to disk.

### One turn

```
POST /api/chat {message}
   │  request thread: validate, convo.bind(source, output), registry.start("chat")
   ▼  job thread
   ├─ emit turn_start ──────────────────────────── the stream is self-sufficient on reload
   │
   ├─▶ STEP ─────────────────────────────────────────────────────────────┐
   │     messages = [System(_SYSTEM + state_block)] + trimmed_history()  │
   │     for chunk in bound.stream(messages):                            │
   │         emit assistant_delta        (coalesced ≥24 chars / newline) │
   │                                                                     │
   │     ┌─── ADMISSION GATE ───────────────────────────────┐            │
   │     │ execute tool calls ONLY when the stream completed │            │
   │     │ normally, stopReason ∈ {tool_use, end_turn}, and  │            │
   │     │ cancel is not set — otherwise drop them all       │            │
   │     └───────────────────────────────────────────────────┘           │
   │                                                                     │
   │     for each kept call: tool_start ▶ execute ▶ cards ▶ tool_result   │
   └──────────────────────────────────────────── repeat ≤ leader.max_steps
   └─ emit usage {leader_calls, leader_cost_usd, spend_usd}
```

**Why the gate is an allow-list and not a deny-list.** `parse_partial_json` silently *repairs*
truncated tool arguments: `{"pack":"javax-to-jakarta","dry_r` accumulates into a valid-looking
`{"pack":"javax-to-jakarta"}` — with `dry_run` gone. Pressing Stop on a proposed dry run could
otherwise have started the real one.

History is LangChain messages and is never the raw chunk: a `tool_use` block with no input delta
raises `KeyError` on replay. Every `toolUse` gets a matching `toolResult`, including invalid ones,
because Bedrock rejects a `toolResult` with no `toolUse`.

### The thirteen tools

`set_project`, `profile_project`, `resolve_intent`, `estimate_pack`, `run_pack`,
`check_acceptance`, `list_held_files`, `apply_review_decisions`, `generate_tests`, `pack_feedback`,
`list_artifacts`, `build_project`, `land_on_branch`.

`build_project` compiles source + output with the project's own build
([forge/verify/project_build.py](forge/verify/project_build.py)): `project_build.command` if set,
otherwise every Maven reactor — a pom no other pom lists as a module — with `mvn install` in
dependency order, into an isolated local repository (`~/.forge/m2`), on the JDK
`/usr/libexec/java_home` reports for `target_java_version`. It writes `project-build.json`;
`land_on_branch`'s confirmation card shows that verdict — passed, failed, not run, or **stale**
when the migrated files changed after the build — and never refuses on it. The leader sees the
verdict and the failing step, never the compiler output, which goes to the card only.

Every one is a wrapper over `forge/service.py` — "add behaviour to the service, never to a route or
a CLI branch" applies to a tool too. None of them raises: a failure is an `ok: false` observation,
so a broken tool costs a sentence rather than the job. A run that succeeded and whose *cards* then
failed to render still returns the run result; paid work is never discarded by a rendering bug.

### The trust boundary

The sharpest edge in the design, and where most of the pre-build critique's blockers lived. The
same data is reduced two ways by `forge/leader/cards.py`:

```
        ┌──────────────────────── THE MODEL ────────────────────────┐
        │  counts · pack ids · statuses · risk tiers · verdicts     │
        │  scores · $ · file PATHS · rule-generated risk reasons    │
        └─────────────────────────▲─────────────────────────────────┘
   ┌──────────────────────────────┴──────────────────────────────────┐
   │  original · transformed · diff        ──▶ dropped               │
   │  review_feedback on a build FAIL      ──▶ verdict only          │
   │      (javac output echoes source)                               │
   │  guardrail_findings                   ──▶ {count, kinds}        │
   │      (a sensitiveInformation finding embeds the matched         │
   │       secret VERBATIM)                                          │
   │  acceptance evidence                  ──▶ counts only           │
   └──────────────────────────────▲──────────────────────────────────┘
        ┌─────────────────────────┴─────────────────────────────────┐
        │  THE BROWSER — diffs, reviewer feedback, full evidence    │
        └───────────────────────────────────────────────────────────┘
```

Same rule as [GUARDRAILS.md](GUARDRAILS.md) §7, one layer up: *a model is never the control that
decides what a model may see.* One test plants a marker in every field that carries file bytes and
asserts it reaches no observation, no `ToolMessage` and not the state block — while the browser's
card still carries it.

### Review cards carry a run stamp

The one `manual-review-queue.json` accumulates across packs, transcripts keep cards indefinitely,
and `decisions.find_entry` falls back to a *unique basename* match. Without the stamp, scrolling up
and approving an old card for `src/Foo.java` could apply a different pack's transform to a different
file. Each entry carries the `run` that produced it and each card carries its entry's stamp, so an
earlier pack's card stays valid while later packs run. `apply_review_decisions` accepts a decision
when its stamp is the queue's current `run` (the leader's `list_held_files` answer) or its entry's
own; it refuses a card whose entry was replaced since (the pack re-ran, a retry re-held it), and
drops any decision whose pack disagrees with the entry's.

### Chaining — how a ten-pack plan runs unattended

Packs do not compose. `run_migration` scans `source_dir` and the transform opens that path, so the
second pack over the same files reads the *original* and its output replaces the first pack's work.
`forge/utils/run_manifest.py` records which pack wrote which file and raises `PackOverlap` before
any spend.

Refusing is right for the CLI, where the operator can pick another `--output-dir`. It is not enough
for chat: the user says "migrate my app" and nothing else, and AMS needs five packs over the same
336 Java files. So `run_migration(chain=True)` materialises the merged view of source ⊕ output —
`forge/verify/merged_tree.py`, the same helper acceptance uses — into a temp tree and runs from
there. The pack transforms the previous pack's result, and the overlap guard is skipped, because
overwriting is the point when the input was that output.

The leader turns it on from the manifest rather than from `convo.completed`, so a chat resumed
against a directory an earlier session wrote chains too. The run emits `chained`, since a run that
silently changed what it read would be impossible to debug. The manifest also records files a pack
*retired*, so a descriptor an earlier pack replaced is not handed to the next one.

**Damage an earlier run wrote is re-checked before it is read.** The `syntax_check` node only sees
fresh model output, and a pack's content filter passes over a file with nothing left to modernise —
so on AMS three files a java8-to-java21 run had damaged (`}ßßß`, a doubled `}`) were carried by
every later pack into the project build. With `syntax_check: true`, a chained run first calls
`service.check_output`: one javac over every Java file `.forge-writes.json` says FORGE wrote (a
quarter of a second for AMS's 297; `syntax.check_tree`), XML for well-formedness. A copy that does
not parse is **moved** — never deleted, a human-approved one included — to
`.forge-staging/.damaged/<path>`, and its manifest entry dropped, so the merged view reads the
original again and this pack re-migrates it if it selects it. Each file is a `damaged_output` event
(with `approved: true` when a human had signed it off), a section of the run's report, and a row in
`migration-summary.md` naming the pack that wrote it, which has to run again to redo its changes —
also when this pack does not select the file at all. The row stays until that pack runs. A file
whose original does not parse either is reported and left in place: there is nothing better to
revert to. A dry run reports and moves nothing. Errors javac gives for a newer language feature
than its JDK ("preview feature", "not supported in -source") are the toolchain, not damage.

### Conversation state

`forge/leader/convo.py` keeps two histories, deliberately apart. `history` is what goes back to the
model — LangChain messages, nothing else. `transcript` is what the browser renders on reload: user
turns, replies, tool rows, cards. A card may carry a diff; an observation in `history` never does.

Both are in memory for the process lifetime, mirroring `JobRegistry`. A conversation that outlived
the server would be a promise the rest of the UI does not make. `trimmed_history` cuts only at a
`HumanMessage` boundary, because a `toolResult` without its `toolUse` is rejected.

A chat turn is a job in the *existing* single registry, so the thread-safety invariant that protects
the graph, DynamoDB and the extract cache holds without a second scheduler.
