# FORGE MVP — Architecture

> **Scope note.** This document describes what is **actually built** in `forge-mvp/` — a
> single-phase **Java 8 → 21 upgrade** pipeline (one transform agent + one review agent), now
> extended with the **cheap-cloud-buildout**: prompt-stuffing RAG, SQS manual-review escalation,
> CloudWatch metric emission, and a Streamlit review portal. It is still *not* the full 15-agent
> vision in `FORGE-AgentDeepDive.pptx` (Discovery / Risk-Scorer / Spring / Struts / Containerize /
> Test-Gen agents remain unbuilt). The deck is the target end-state; this is the foundation plus
> its first operational layer. See §11 for what is and isn't done.

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

The transformation rules live in an **external prompt file**,
[prompts/java_upgrade.md](prompts/java_upgrade.md), loaded at runtime by
[forge/agents/java_upgrade.py](forge/agents/java_upgrade.py) via
[forge/utils/prompts.py](forge/utils/prompts.py) — so prompts can be tuned without code changes.
Override the prompt directory with the `FORGE_PROMPTS_DIR` environment variable.

---

## 2. Tech stack

| Layer | Choice |
|---|---|
| Language / runtime | **Python 3.11+** (tested on 3.12) |
| Orchestration | **LangGraph** — `StateGraph` state machine ([forge/graph.py](forge/graph.py)) |
| LLM client | **LangChain** `langchain-aws` → `ChatBedrockConverse` |
| Transform model | **Claude Sonnet 4.5** (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) on AWS Bedrock |
| Review model | **Amazon Nova Pro** (`us.amazon.nova-pro-v1:0`) on AWS Bedrock — *different model family, deliberate cross-validation* |
| Safety | **AWS Bedrock Guardrails** — standalone `ApplyGuardrail` API ([forge/guardrails/bedrock_guardrails.py](forge/guardrails/bedrock_guardrails.py)) |
| RAG | **Prompt-stuffing** — enterprise-standards docs selected by file type, injected into prompts ([forge/rag/](forge/rag/)); no vector store. `knowledge_base` mode stubbed for a future Bedrock KB |
| Manual-review queue | **AWS SQS** — `escalate_sqs` node sends a pointer on escalation ([forge/queue/sqs_client.py](forge/queue/sqs_client.py)) |
| Metrics | **AWS CloudWatch** — per-file `put_metric_data` feeding the alarms/dashboard ([forge/observability/metrics.py](forge/observability/metrics.py)) |
| Review portal | **Streamlit** — human approve/reject over the manual queue ([review_portal.py](review_portal.py)) |
| State + checkpoints | **AWS DynamoDB** — 2 tables (app state + LangGraph checkpointer) ([forge/state_store/dynamodb.py](forge/state_store/dynamodb.py)) |
| Config | **PyYAML** — single `agents.yaml` read at startup ([forge/config.py](forge/config.py)) |
| Secrets / env | **python-dotenv** (`.env`) |
| Observability | **LangSmith** (env-var driven) + **CloudWatch** metrics |
| Infra (provisioning) | **Terraform** in `forge-terraform/` (DynamoDB, Guardrails, IAM, CloudWatch, SQS) — or `infrastructure/create_dynamodb.py` for local dev |

> The spec pins `langgraph>=0.2 / langchain>=0.3`; the graph also compiles and tests pass under
> the current `langgraph 1.x / langchain 1.x` line.

**Still not built** (despite being in the deck): a managed **Bedrock Knowledge Base** RAG
(prompt-stuffing is used instead), Strands Agents, the Discovery / Risk-Scorer / Spring / Struts /
Containerize / Test-Gen agents, the 5-agent review board, and `@tool` function-calling. Agents use
plain system-prompt + `invoke`.

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
                  └─────┬────┘       └─────┬────────┘  (+ RAG standards context;
                        │                  │            review feedback on retry)
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
                        │   PASS │ BLOCK ──────────────────────▶ │
                        │        ▼                          ┌─────▼────────┐
                        │  ┌────────────┐                   │ escalate_sqs │ → SQS pointer
                        │  │ write_file │ → ./migrated/<pkg> └─────┬────────┘  (no-op if unset)
                        │  └──────┬─────┘                          │
                        │         │                                │
                        └─────────┴──────────┬───────────────────-┘
                                             ▼
                                      ┌──────────────┐
                                      │ update_state │ → counters + CloudWatch metrics
                                      └──────┬───────┘
                                             ▼
                                            END
```

Source of truth: [forge/graph.py](forge/graph.py). Routing functions: `route_pre`,
`route_reviewer`, `route_post`. RAG standards context is injected into the `java_upgrade` and
`java_reviewer` prompts (see §4); CloudWatch metrics are emitted per file from `migrate.py` after
`update_state`.

---

## 4. Nodes

| Node | Model / service | Role | Outcome |
|---|---|---|---|
| `guardrails_pre` | Bedrock Guardrails (INPUT) + **Sonnet 4.5** | Secrets, package-scope, PII, complexity (LOC) pre-flight | `BLOCK` → `blocked`; else `TRANSFORMING` |
| `java_upgrade` | **Sonnet 4.5** | Transform Java per 5 rules; prepends RAG standards context; injects prior review feedback on retry | `transform_output` (JSON: files + manual_flags) |
| `java_reviewer` | **Nova Pro** | Score 0–100 across 5 weighted checks (also grounded with RAG context); emit verdict + feedback | `PASS≥80` / `RETRY 50–79` / `MANUAL<50` |
| `guardrails_post` | Bedrock Guardrails (OUTPUT) + **Sonnet 4.5** | Verify zero `javax.*` left, no new security issues, naming | `BLOCK` → `manual_queue`; else continue |
| `write_file` | local FS | Write transformed files to `./migrated/` preserving package path (no-op on `--dry-run`) | status `DONE` |
| `manual_queue` | — | Mark file for human review | status `MANUAL_REVIEW` |
| `escalate_sqs` | **AWS SQS** | Send a pointer message to the manual-review queue (no-op when `sqs_queue_url` unset); pass-through | unchanged status |
| `blocked` | — | Terminal block | status `BLOCKED` |
| `update_state` | — | Increment run counters (processed/passed/retried/manual/blocked) | → `END` |

> RAG context comes from `retrieve_context()` ([forge/rag/retriever.py](forge/rag/retriever.py))
> when `rag_mode: prompt_stuff`; per-file CloudWatch metrics are emitted from `migrate.py`
> ([forge/observability/metrics.py](forge/observability/metrics.py)) on non-dry-run runs.

### Retry + feedback loop
`route_reviewer` ([graph.py:71](forge/graph.py#L71)): a `RETRY` verdict (score 50–79) with
`retry_count < max_retries` (default **2**) increments the counter, stamps status `RETRY_n`, and
routes back to `java_upgrade`. The transform agent reads `review_feedback` from state and appends
it to its prompt ([java_upgrade.py:68](forge/agents/java_upgrade.py#L68)). Exhausted retries →
`manual_queue`.

> **Cross-model validation:** the transform is written by Claude Sonnet 4.5 and graded by Amazon
> Nova Pro — two different model families. The two guardrail nodes also use Sonnet 4.5 as a
> second-pass reasoning check *in addition to* the deterministic Bedrock Guardrails policy.

> ⚠️ **No build / compile verification.** Every check in this pipeline is **LLM/text-based**, not
> compiler-based. The pipeline never runs `javac`, Maven, or Gradle against the migrated output —
> there are no `subprocess` calls anywhere. `java_reviewer`'s "syntactically and logically valid"
> check is the *model's opinion* of one file (truncated to 8,000 chars, no classpath, no `pom.xml`
> resolution, no cross-file context), not a real compile. **A file can score PASS (≥80) and be
> written to `./migrated/` even if it would not actually compile** — e.g. a missing `jakarta`
> dependency, a broken cross-file reference, or a malformed edit the model didn't catch. See
> §11 Known gaps.

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
| `manual-review-queue.json` (local) | Files needing human review, with full context | written by `migrate.py` (portal fallback) |
| `migration-report.md` (local) | Run summary | written by `forge/utils/report.py` |
| `forge-manual-review-dev` + DLQ (SQS) | Manual-review escalation pointers (the "human needed" signal) | sent by `escalate_sqs` |
| `FORGE/Migration` (CloudWatch) | Per-file metrics feeding the 4 alarms + dashboard | emitted by `MetricsEmitter` |
| `forge-terraform/docs/*.md` | RAG enterprise-standards corpus (prompt-stuffing source) | read by `forge/rag/corpus.py` |

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
| `guardrail_id` / `guardrail_version` | *(from TF / local)* | Bedrock Guardrail to apply |
| `rag_mode` | `prompt_stuff` | `off` \| `prompt_stuff` \| `knowledge_base` (stub) |
| `rag_docs_dir` / `rag_max_chars` | `""` / `6000` | Standards corpus dir (defaults to `forge-terraform/docs`) and injected-context cap |
| `sqs_queue_url` | `""` | Manual-review queue; empty → `escalate_sqs` is a no-op |
| `cloudwatch_namespace` | `FORGE/Migration` | Metrics namespace; must match the Terraform alarms |
| `avg_cost_per_bedrock_call` | `0.02` | Multiplier for the `estimated_cost_usd` metric |
| `build_tool` | `none` | `maven` \| `gradle` \| `none` — reserved for the future compile gate |

In production `agents.yaml` is generated from Terraform outputs via
`forge-terraform/scripts/generate-agents-yaml.sh`. The committed `agents.yaml` is a template
(placeholder guardrail, `com.corp`); live values are supplied via the generated file or
`agents.local.yaml` (gitignored).

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

1. `pip install -r requirements.txt` (includes `streamlit` for the review portal)
2. AWS credentials with Bedrock + DynamoDB access (`AWS_PROFILE` or env vars). For metric/queue
   emission the identity also needs `cloudwatch:PutMetricData` and `sqs:SendMessage` (granted to
   the FORGE execution role, or to the dev user via `sqs_access_principal_arn`)
3. **Bedrock model access** enabled for Claude Sonnet 4.5 + Amazon Nova Pro in `us-east-1`
4. Both DynamoDB tables created
5. A real **Bedrock Guardrail** id in the live config (generated `agents.yaml` / `agents.local.yaml`)
6. *(optional)* SQS queue + CloudWatch observability deployed (`forge-terraform`) for escalation +
   metrics; both degrade to no-ops when unconfigured

---

## 10. Layout

```
forge-mvp/
  migrate.py                       # CLI entry point (+ per-file CloudWatch metric emission)
  review_portal.py                 # Streamlit human review portal (approve/reject)
  agents.yaml                      # single config file (template; live = agents.local.yaml)
  prompts/
    java_upgrade.md                # externalised agent system prompts (editable, no code change)
    java_reviewer.md  guardrails_pre.md  guardrails_post.md
  forge/
    config.py                      # YAML loader
    state.py                       # ForgeState / FileStatus / state machine
    graph.py                       # LangGraph wiring (incl. escalate_sqs node)
    agents/
      base.py
      guardrails_pre.py            # Bedrock Guardrails (INPUT) + Sonnet pre-flight
      java_upgrade.py              # the one transform agent (Sonnet 4.5) + RAG injection
      guardrails_post.py           # Bedrock Guardrails (OUTPUT) + Sonnet post-check
    review/
      base_reviewer.py
      java_reviewer.py             # the one review agent (Nova Pro) + RAG injection
    rag/
      corpus.py  retriever.py      # prompt-stuffing RAG (doc selection + injection)
    queue/
      sqs_client.py                # SqsEscalator — manual-review queue sender
    observability/
      metrics.py                   # MetricsEmitter — CloudWatch put_metric_data
    guardrails/
      bedrock_guardrails.py        # ApplyGuardrail wrapper
    state_store/
      dynamodb.py                  # state manager + LangGraph checkpointer
    utils/
      prompts.py                   # external prompt loader (FORGE_PROMPTS_DIR)
      jsonio.py  file_scanner.py  file_writer.py  report.py
  infrastructure/
    create_dynamodb.py             # dev table creation (non-Terraform)
  tests/                           # test_graph, test_guardrails, test_java_upgrade
```

---

## 11. Known gaps (Phase 0)

These are deliberate Phase-0 limitations, not bugs. The actionable backlog lives in
[TODO.md](TODO.md).

- **No build/compile gate.** The pipeline never runs `javac` / Maven / Gradle on the output
  (no `subprocess` calls). All verification is LLM/text-based, so output can be written without
  being compile-verified. *(Top of the next-cycle backlog. `build_tool` config key reserved.)*
- **Per-file, no project context.** Reviewer sees one file (truncated to 8,000 chars) — no
  classpath, no `pom.xml` resolution, no cross-file references.
- **Single phase only.** `--phase java21` is the only implemented phase; the deck's Spring,
  Struts→MVC, Discovery, Risk-Scorer, Containerize, and Test-Gen agents are not built.

### Resolved (built after Phase 0)

- **RAG wired (prompt-stuffing).** `rag_mode: prompt_stuff` selects relevant enterprise-standards
  docs (`forge-terraform/docs/*.md`) by file type and injects them into the transform + review
  prompts — no vector store, ~$0. `forge/rag/{corpus,retriever}.py`. `knowledge_base` mode is a
  stubbed future branch (Bedrock KB). Avoids the ~$175/mo OpenSearch cost.
- **Review portal built.** `review_portal.py` (Streamlit) lists MANUAL_REVIEW files from DynamoDB,
  renders the transform diff, and supports Approve (write + DONE) / Reject (re-enqueue PENDING).
- **SQS escalation wired.** `escalate_sqs` graph node sends a pointer to the manual-review queue
  on every escalation (`forge/queue/sqs_client.py`); no-op when `sqs_queue_url` is empty.
- **CloudWatch metrics emitted.** `forge/observability/metrics.py` publishes the metrics the
  observability alarms/dashboard watch (`files_processed/passed/retried/manual/blocked`,
  `bedrock_calls`, `estimated_cost_usd`, `review_score`) per file.
- **Prompts externalized.** `java_reviewer`, `guardrails_pre`, `guardrails_post` now load from
  `prompts/*.md` via the existing loader (was inline `_SYSTEM`).
- **Guardrail resolved.** Real guardrail id is supplied via the generated `agents.yaml` /
  `agents.local.yaml` (no more `REPLACE_WITH_GUARDRAIL_ID` placeholder in live config).
