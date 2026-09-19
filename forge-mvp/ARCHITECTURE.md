# FORGE MVP — Architecture (Phase 0)

> **Scope note.** This document describes the Phase 0 engine in `forge-mvp/`. Phase 1 — the
> pack library and context extractors that make it a generic J2EE migration platform — is
> summarised in §12 and specified in
> [prompts/FORGE-Platform-Requirements.md](../prompts/FORGE-Platform-Requirements.md); the
> local web UI and the service layer under both front ends are in §13.
> The engine started as a single-phase **Java 8 → 21 upgrade** pipeline. It is *not* the 15-agent vision in
> `FORGE-AgentDeepDive.pptx`. Per [prompts/FORGE-Phase0-MVP.md](../prompts/FORGE-Phase0-MVP.md),
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
[tests/test_scope.py](tests/test_scope.py) and recorded in `../CLAUDE.md`. A
model-driven leader would reintroduce that failure mode at the layer where it is hardest to debug,
and make the order of a run non-reproducible. The model's influence is deliberately bounded on both
sides: it returns a score, code picks the branch, and `max_retries` caps the loop.

One deliberate drift from the deck while we are here: it specifies **Sonnet 4.5** for the transform,
leader and guardrails roles. This implementation uses **Opus 4.8** for transform and guardrails (§2);
review is Nova Pro, which matches.

---

## 4. Nodes

| Node | Model / service | Role | Outcome |
|---|---|---|---|
| `guardrails_pre` | local code, then Bedrock Guardrails (INPUT); **Opus 4.8** only if `preflight_model_check` | Four ordered steps: the local secret gate, a local LOC ceiling, the guardrail policy on INPUT, and an opt-in model check that is **off by default** and asks nothing about secrets, PII or packages. Detail in [GUARDRAILS.md](GUARDRAILS.md) | `SECRET_BLOCKED_LOCALLY` / `TOO_LARGE` / guardrail intervention → `blocked`; else `TRANSFORMING` |
| `java_upgrade` | **Opus 4.8** | Transform Java per 5 rules; on retry, injects prior review feedback into the prompt | `transform_output` (JSON: files + manual_flags) |
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
| `manual-review-queue.json` (local) | v2: every unit a human must look at — original, transformed, verdicts, risk reasons; written in dry-run too | `forge/review_queue.py` |
| `migration-review.html` (local) | Static review page: side-by-side + diff + decision widget → `decisions.json` | `forge/review_queue.py` |
| `./migrated/.forge-staging/` (local) | Held units, in migrated layout, until approved | `forge/utils/file_writer.py` |
| `decisions-applied.jsonl`, `pack-feedback.md` (local) | Decision audit log; notes grouped by pack and rule | `forge/decisions.py`, `forge/feedback_report.py` |
| `migration-report.md` (local) | Run summary | written by `forge/utils/report.py` |

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
`--discover`, `--acceptance` / `--acceptance-only` / `--acceptance-build`, `--generate-tests` /
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
   (`forge-terraform/scripts/generate-agents-yaml.sh dev > agents.yaml`). The checked-in
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
  tests/                           # 580+ tests, fully mocked (conftest.mocked_aws / mocked_testgen)
```

---

## 11. Known gaps (Phase 0)

These are deliberate Phase-0 limitations, not bugs. The actionable backlog lives in
[TODO.md](TODO.md).

- **Build gate is per file unless `mode: maven`.** A whole-module compile after a batch is the
  reliable form; per-file `javac` needs the classpath configured.
- **Project context is extractor-by-extractor.** Packs whose context is `web_bootstrap` get the
  full descriptor set (§12). Packs naming an unbuilt extractor (`struts_routing_table`,
  `spring_bean_graph`, `view_bindings`, `reactor`, `test_subject`) either run without context or
  are refused if they need a selector.
- **The profile is written but not yet consumed.** `--discover` produces `forge-profile.yaml`;
  a run still takes one `--phase` at a time.
- **`routing_parity` is skipped** until the `struts_routing_table` extractor exists.
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

**Runnable today** (`runnable_phases()`): `java21`, `struts-spring6`, `build-maven-modernize`,
`java8-to-java21`, `javax-to-jakarta`, `spring-to-spring6`, `springsec-to-springsec6`,
`struts2-modernize`, `jsp-jstl-modernize`, `junit4-to-junit5`, `webapp-bootstrap-jakarta10`,
`liberty-server-config`. Refused until their extractor exists: `struts1-to-springmvc6`,
`struts2-to-springmvc6`.

**Two decisions fixed at platform level** (see the requirements spec): the target is a **WAR on
WebSphere/Open Liberty at Jakarta EE 10, Spring Framework 6.2, no Spring Boot**; and
`web_framework: modernize-in-place` (Struts → Struts 7) is the first route, with
`migrate-to-spring` as the later one.

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
`testgen_cancelled`. The CLI's `_print_event` reproduces its historical stdout from those events;
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

The page's steps are Project → Discover → Run → Review → Accept → **Tests** → Feedback → Artifacts.
The Run step can tick *generate tests*, which chains §14 onto the same job.

**Per-run decisions.** The Discover step offers every platform decision; the values go in the run
body and reach the packs, the hold gate and the acceptance checks through
`ForgeConfig.with_overrides({"decisions": ...})` — a deep-copied config, nested mappings merged, no
temp YAML and no environment variable.

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
