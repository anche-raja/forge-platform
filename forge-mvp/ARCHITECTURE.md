# FORGE MVP — Architecture (Phase 0)

> **Scope note.** This document describes the **MVP that is actually built** in `forge-mvp/` — a
> single-phase **Java 8 → 21 upgrade** pipeline. It is *not* the 15-agent vision in
> `FORGE-AgentDeepDive.pptx`. Per [prompts/FORGE-Phase0-MVP.md](../prompts/FORGE-Phase0-MVP.md),
> Phase 0 is deliberately *"one transform agent, one review agent, nothing else — no RAG, no SQS,
> no Discovery agent yet."* The deck is the target end-state; this is the foundation.

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

Defined as a system prompt in [forge/agents/java_upgrade.py](forge/agents/java_upgrade.py).

---

## 2. Tech stack

| Layer | Choice |
|---|---|
| Language / runtime | **Python 3.11+** (tested on 3.12) |
| Orchestration | **LangGraph** — `StateGraph` state machine ([forge/graph.py](forge/graph.py)) |
| LLM client | **LangChain** `langchain-aws` → `ChatBedrockConverse` |
| Transform model | **Claude Sonnet 4.5** (`us.anthropic.claude-sonnet-4-5-20251001-v1:0`) on AWS Bedrock |
| Review model | **Amazon Nova Pro** (`us.amazon.nova-pro-v1:0`) on AWS Bedrock — *different model family, deliberate cross-validation* |
| Safety | **AWS Bedrock Guardrails** — standalone `ApplyGuardrail` API ([forge/guardrails/bedrock_guardrails.py](forge/guardrails/bedrock_guardrails.py)) |
| State + checkpoints | **AWS DynamoDB** — 2 tables (app state + LangGraph checkpointer) ([forge/state_store/dynamodb.py](forge/state_store/dynamodb.py)) |
| Config | **PyYAML** — single `agents.yaml` read at startup ([forge/config.py](forge/config.py)) |
| Secrets / env | **python-dotenv** (`.env`) |
| Observability | **LangSmith** (env-var driven, no code change) |
| Infra (provisioning) | **Terraform** in `forge-terraform/` (DynamoDB, Guardrails, IAM, CloudWatch) — or `infrastructure/create_dynamodb.py` for local dev |

> The spec pins `langgraph>=0.2 / langchain>=0.3`; the graph also compiles and tests pass under
> the current `langgraph 1.x / langchain 1.x` line.

**Not in the MVP** (despite being in the deck): RAG / Bedrock Knowledge Base, Strands Agents,
SQS, the Discovery / Risk-Scorer / Spring / Struts / Containerize / Test-Gen agents, the 5-agent
review board, the review portal, and `@tool` function-calling. Agents use plain
system-prompt + `invoke`.

---

## 3. Pipeline graph

The pipeline is a LangGraph `StateGraph` invoked **once per file** (`thread_id = file_path`).

```
                         ┌──────────────────┐
            entry ──────▶│  guardrails_pre  │  Bedrock Guardrails (INPUT) + Sonnet 4.5
                         └────────┬─────────┘  secrets / scope / PII / complexity
                                  │
                     BLOCK ◀──────┤──────▶ PASS
                        │                  │
                  ┌─────▼────┐       ┌─────▼────────┐
                  │ blocked  │       │ java_upgrade │  Sonnet 4.5 — transform
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
`route_reviewer`, `route_post`.

---

## 4. Nodes

| Node | Model / service | Role | Outcome |
|---|---|---|---|
| `guardrails_pre` | Bedrock Guardrails (INPUT) + **Sonnet 4.5** | Secrets, package-scope, PII, complexity (LOC) pre-flight | `BLOCK` → `blocked`; else `TRANSFORMING` |
| `java_upgrade` | **Sonnet 4.5** | Transform Java per 5 rules; on retry, injects prior review feedback into the prompt | `transform_output` (JSON: files + manual_flags) |
| `java_reviewer` | **Nova Pro** | Score 0–100 across 5 weighted checks; emit verdict + feedback | `PASS≥80` / `RETRY 50–79` / `MANUAL<50` |
| `guardrails_post` | Bedrock Guardrails (OUTPUT) + **Sonnet 4.5** | Verify zero `javax.*` left, no new security issues, naming | `BLOCK` → `manual_queue`; else continue |
| `write_file` | local FS | Write transformed files to `./migrated/` preserving package path (no-op on `--dry-run`) | status `DONE` |
| `manual_queue` | — | Mark file for human review | status `MANUAL_REVIEW` |
| `blocked` | — | Terminal block | status `BLOCKED` |
| `update_state` | — | Increment run counters (processed/passed/retried/manual/blocked) | → `END` |

### Retry + feedback loop
`route_reviewer` ([graph.py:71](forge/graph.py#L71)): a `RETRY` verdict (score 50–79) with
`retry_count < max_retries` (default **2**) increments the counter, stamps status `RETRY_n`, and
routes back to `java_upgrade`. The transform agent reads `review_feedback` from state and appends
it to its prompt ([java_upgrade.py:68](forge/agents/java_upgrade.py#L68)). Exhausted retries →
`manual_queue`.

> **Cross-model validation:** the transform is written by Claude Sonnet 4.5 and graded by Amazon
> Nova Pro — two different model families. The two guardrail nodes also use Sonnet 4.5 as a
> second-pass reasoning check *in addition to* the deterministic Bedrock Guardrails policy.

---

## 5. State model

`ForgeState` (graph-level) carries one `current_file` (`FileStatus`) plus run-level counters and
config — see [forge/state.py](forge/state.py).

**File state machine:**
```
PENDING → TRANSFORMING → REVIEWING → RETRY_1 → RETRY_2 → DONE
                              │                            │
                              └──────────────────────────▶ MANUAL_REVIEW
   (pre-flight)  ────────────────────────────────────────▶ BLOCKED
```

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
| `manual-review-queue.json` (local) | Files needing human review, with full context | written by `migrate.py` |
| `migration-report.md` (local) | Run summary | written by `forge/utils/report.py` |

Tables: create with [infrastructure/create_dynamodb.py](infrastructure/create_dynamodb.py) (dev)
or `forge-terraform/modules/foundation` (prod).

---

## 7. Configuration

Single file [agents.yaml](agents.yaml), loaded by `ForgeConfig`. Key knobs:

| Key | Default | Meaning |
|---|---|---|
| `transform_model` | `…claude-sonnet-4-5…` | Transform + guardrail-reasoning model |
| `review_model` | `…amazon.nova-pro…` | Reviewer model |
| `pass_threshold` | `80` | Score ≥ → PASS |
| `retry_threshold` | `50` | Score ≥ (and < pass) → RETRY |
| `max_retries` | `2` | Retry cap before MANUAL |
| `scope_package_prefix` | `com.corp` | Files outside scope are flagged |
| `complexity_block_threshold` | `2000` | LOC ceiling for auto-transform |
| `guardrail_id` / `guardrail_version` | *(placeholder)* | Bedrock Guardrail to apply |

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

Flags: `--phase java21` (only phase implemented), `--dry-run`, `--resume`, `--file`,
`--output-dir` (default `./migrated`), `--config`.

> **Note:** `--dry-run` skips file writes and the DynamoDB *state* update, but the graph still
> calls **Bedrock** (guardrails + both models) and the **checkpointer still writes** to DynamoDB.
> There is no fully offline run mode.

---

## 9. Prerequisites to run a live migration

1. `pip install -r requirements.txt`
2. AWS credentials with Bedrock + DynamoDB access (`AWS_PROFILE` or env vars)
3. **Bedrock model access** enabled for Claude Sonnet 4.5 + Amazon Nova Pro in `us-east-1`
4. Both DynamoDB tables created
5. A real **Bedrock Guardrail** — set `guardrail_id` in `agents.yaml` (the checked-in value is a
   `REPLACE_WITH_GUARDRAIL_ID` placeholder; the first node will fail without a valid ID)

---

## 10. Layout

```
forge-mvp/
  migrate.py                       # CLI entry point
  agents.yaml                      # single config file
  forge/
    config.py                      # YAML loader
    state.py                       # ForgeState / FileStatus / state machine
    graph.py                       # LangGraph wiring (the pipeline)
    agents/
      base.py
      guardrails_pre.py            # Bedrock Guardrails (INPUT) + Sonnet pre-flight
      java_upgrade.py              # the one transform agent (Sonnet 4.5)
      guardrails_post.py           # Bedrock Guardrails (OUTPUT) + Sonnet post-check
    review/
      base_reviewer.py
      java_reviewer.py             # the one review agent (Nova Pro)
    guardrails/
      bedrock_guardrails.py        # ApplyGuardrail wrapper
    state_store/
      dynamodb.py                  # state manager + LangGraph checkpointer
    utils/
      file_scanner.py  file_writer.py  report.py
  infrastructure/
    create_dynamodb.py             # dev table creation (non-Terraform)
  tests/                           # test_graph, test_guardrails, test_java_upgrade
```
