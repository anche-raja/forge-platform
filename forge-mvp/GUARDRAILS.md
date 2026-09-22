# FORGE — Guardrails

> **Scope note.** How every file is screened on its way through the pipeline: the two guardrail
> nodes, the six checks between them, and where each verdict lands. The nodes themselves are
> summarised in [ARCHITECTURE.md](ARCHITECTURE.md) §4; the guardrail resource is specified in
> [forge-terraform/SPEC.md](../forge-terraform/SPEC.md). This document is the
> detail under both.

---

## 1. Two things are called "guardrails"

| | What it is |
|---|---|
| **The Bedrock Guardrail** | A managed AWS policy — PII entities, content filters, banned phrases. Defined in [forge-terraform/modules/foundation/main.tf](../forge-terraform/modules/foundation/main.tf), published as a numbered version, pinned in `agents.yaml`. |
| **The two guardrail nodes** | `guardrails_pre` and `guardrails_post` in the LangGraph graph. Each one *calls* the Bedrock guardrail and adds checks of its own. |

The distinction matters because most of the screening is **not** the AWS policy.

---

## 2. Exposure — the organising idea

The six checks are ordered by how far the file's bytes travel, cheapest and most contained first.
A check that can answer the question without leaving the process runs before one that cannot.

| Lane | Checks | What it means |
|---|---|---|
| **Local** | 3 | Pure Python, no network. A hit costs zero Bedrock calls and the content never leaves the process. |
| **AWS, no model** | 2 | `ApplyGuardrail` is a standalone managed policy engine. The call carries no `modelId` — no foundation model is invoked. Billed per text unit, not per token. |
| **Model** | 1 | Claude Opus 4.8 (`us.anthropic.claude-opus-4-8`), asked the one qualitative question a regex cannot answer — and only *after* the transform. |

**Nothing is asked of a model before the transform.** Secrets, file size and package scope are all
decided locally. This is a hard requirement, not a tuning choice: see §7.

---

## 3. The flow

```mermaid
flowchart TD
    S(["unit: file, or a generated target"]) --> READ["read source<br/>(or the context block, if generated)"]
    READ -->|"unreadable"| BLK["BLOCKED"]
    READ --> RISK["score_unit()<br/>risk tier — always runs first"]

    RISK --> C1["1 · secret_scan<br/>keys · tokens · credentials · entropy"]
    C1 -->|"secret found"| BLK
    C1 -->|"clean"| C2["2 · file size<br/>an integer comparison"]
    C2 -->|"over threshold"| BLK
    C2 --> C3["3 · ApplyGuardrail<br/>source = INPUT"]
    C3 -->|"GUARDRAIL_INTERVENED"| BLK
    C3 -->|"no intervention"| UP["java_upgrade<br/>Opus 4.8 + pack prompt + context"]

    UP --> REV["java_reviewer<br/>Nova Pro + pack rubric"]
    REV -->|"50 ≤ score < 80, retries < 2"| UP
    REV -->|"score < 50, or budget spent"| MAN["MANUAL_REVIEW"]
    REV -->|"score ≥ 80"| C4["4 · ApplyGuardrail<br/>source = OUTPUT"]

    C4 -->|"GUARDRAIL_INTERVENED"| MAN
    C4 -->|"no intervention"| C5["5 · javax.* check<br/>Rule 1 — a regex, never a model"]
    C5 -->|"unmigrated imports"| MAN
    C5 -->|"zero javax.* left"| C6["6 · post-check ask<br/>Opus 4.8 — regressions · lost logic"]
    C6 -->|"BLOCK"| MAN
    C6 -->|"risk above ceiling"| HELD["HELD<br/>staged under .forge-staging/"]
    C6 -->|"PASS"| W["write_file"]

    W --> V["verify_build<br/>opt-in"]
    V -->|"FAIL, retries < 2"| UP
    V -->|"FAIL, budget spent"| MAN
    V -->|"PASS or SKIPPED"| DONE["DONE"]

    BLK --> US["update_state → END"]
    MAN --> US
    HELD --> US
    DONE --> US

    classDef local fill:#e6f2ec,stroke:#15704f,color:#0d2b20
    classDef aws fill:#f6ecdc,stroke:#8f5504,color:#3d2401
    classDef model fill:#f7e6e5,stroke:#96241e,color:#3f0f0c
    classDef term fill:#eceff1,stroke:#5a646e,color:#13181c
    class C1,C2,C5 local
    class C3,C4 aws
    class C6 model
    class BLK,MAN,HELD,DONE term
```

Three details the picture carries that prose does not:

- **Checks 1–3 all precede `java_upgrade`.** The secret gate is the first thing that happens after
  the file is read and scored — ahead of `ApplyGuardrail`, not after it.
- `guardrails_post` **returns to the local lane** for check 5. The mechanical `javax.*` check sits
  deliberately between two remote ones, so a file that failed Rule 1 never pays for check 6.
- **A held unit never reaches `verify_build`** — nothing was written to the output tree, so there is
  nothing to compile. `hold_for_review` goes straight to `update_state`.

---

## 4. `guardrails_pre` — before the transform

[forge/agents/guardrails_pre.py](forge/agents/guardrails_pre.py)

| # | Check | Where | What it asks | On a hit |
|---|---|---|---|---|
| — | read source | local | Can the file be read? A generated unit has no source, so the rendered context block stands in — vendor descriptors are where credentials leak. | `BLOCKED` |
| — | `score_unit()` | local | Deterministic risk tier. Runs **before any verdict** so even a blocked file reaches the queue with its reasons. | records tier |
| 1 | `secret_scan` | local | Any credential: key material, vendor-prefixed tokens, credentials in URLs and connection strings, credential-named assignments across Java/XML/properties/YAML, and high-entropy literals. | `BLOCKED`, verdict `SECRET_BLOCKED_LOCALLY` |
| 2 | file size | local | Does the line count exceed `complexity_block_threshold`? | `BLOCKED`, verdict `TOO_LARGE` |
| 3 | `ApplyGuardrail` | AWS policy | The published guardrail, `source = INPUT`. | `BLOCKED` |
| — | pre-flight ask | Opus 4.8 | **Off by default** (`preflight_model_check`). When on, asks only about reflection, native calls, generated code and unreachable control flow — never secrets, PII or packages. | `BLOCK` → `BLOCKED` |

Clean output sets status `TRANSFORMING`. `route_pre` sends `BLOCKED` to the `blocked` node and
everything else to `java_upgrade`.

## 5. `guardrails_post` — after the transform

[forge/agents/guardrails_post.py](forge/agents/guardrails_post.py)

| # | Check | Where | What it asks | On a hit |
|---|---|---|---|---|
| — | collect output | local | Did the transform produce any files at all? | `MANUAL_REVIEW` |
| 4 | `ApplyGuardrail` | AWS policy | The same policy over the transformed bytes, `source = OUTPUT`. `PROMPT_ATTACK` is set to `NONE` on output. | `MANUAL_REVIEW` |
| 5 | `javax.*` check | local | Rule 1: zero Jakarta-EE `javax.*` imports remain. [forge/utils/java_checks.py](forge/utils/java_checks.py) | `MANUAL_REVIEW` |
| 6 | post-check ask | Opus 4.8 | Deprecated patterns left behind, a security regression introduced, business logic or null checks destroyed. | `MANUAL_REVIEW` |

Clean output leaves the status untouched. `route_post` then decides between `manual_queue`,
`hold_for_review` (risk above `decisions.risk_ceiling`) and `write_file`.

---

## 6. The Bedrock guardrail policy

[forge-terraform/modules/foundation/main.tf](../forge-terraform/modules/foundation/main.tf)

| Policy | Contents |
|---|---|
| `sensitive_information_policy` | `BLOCK` on `AWS_ACCESS_KEY`, `AWS_SECRET_KEY`, `CREDIT_DEBIT_CARD_NUMBER`, `US_SOCIAL_SECURITY_NUMBER`, `US_BANK_ACCOUNT_NUMBER`, `PASSWORD` |
| `content_policy` | `HIGH`/`HIGH` on `HATE`, `INSULTS`, `SEXUAL`, `VIOLENCE`, `MISCONDUCT`; `PROMPT_ATTACK` at `HIGH` on input, `NONE` on output |
| `word_policy` | Two literal phrases: `ignore previous instructions`, `disregard your system prompt` |

There is **no `regexes_config`** block, and no entity type for certificates or cryptographic keys —
which is why check 1 exists and why it cannot be delegated here.

---

## 7. Architecture decisions

### A model can never be the control that decides what a model may see

The pipeline used to ask Claude *"does this file contain secrets?"* — a check that works by sending
the file to Claude. It is the disclosure it claims to prevent. The same objection applies to the
Bedrock guardrail: `ApplyGuardrail` is a network call of its own, so it cannot clear a file for
transmission either.

So secret detection is mechanical, local and **first**
([forge/utils/secret_scan.py](forge/utils/secret_scan.py)), and every question the old pre-flight
model call answered is now answered without a model:

| Question | Was | Is |
|---|---|---|
| Hardcoded secrets / credentials | asked of Opus 4.8 | `secret_scan`, local, check 1 |
| PII in comments and literals | asked of Opus 4.8 | the guardrail's `sensitive_information_policy`, check 3 |
| Too large to migrate | asked of Opus 4.8 | an integer comparison, check 2 |
| Is this file ours to migrate | asked of Opus 4.8 (removed earlier) | `file_scanner`, before the graph |

What remains for a model is the one thing a regex cannot do — reading transformed output and judging
whether the *meaning* survived (check 6). That runs after the transform, on a file the gate has
already cleared.

The old call survives as `preflight_model_check`, **off by default**, with a prompt that asks only
about migration safety. Turning it on sends source to a model before the gate has finished, so it is
a deliberate policy decision, not a default.

### What `secret_scan` looks for

| Family | Examples |
|---|---|
| Key material | PEM and PGP private key blocks; a literal reaching `SecretKeySpec`, `PBEKeySpec`, `IvParameterSpec`; a key-shaped literal or byte array assigned to a key-named field |
| Vendor-prefixed tokens | AWS access key ids, GitHub, Slack, Stripe, Google, OpenAI-style keys, JWTs, Azure storage keys |
| Credentials in transit | `scheme://user:pass@host`, connection strings carrying `AccountKey=` |
| Credential-named assignments | Java assignment, Spring `<property name= value=>`, XML attribute, XML element, `.properties` line, YAML key — all six shapes |
| High-entropy literals | base64-shaped values ≥ 20 chars at ≥ 4.0 bits/char with no other explanation |

**Recall is weighted over precision on purpose.** A false positive blocks one file and names it in
the report, which a human clears with `secret_scan.allow`; a false negative sends a credential to a
third party. The asymmetry is the whole point.

Four things the detector gets right on purpose:

- **Identifiers are tokenised, not substring-matched.** `monkeyCount` must not read as a key.
- **`key` alone is not a credential.** `sortKey`, `cacheKey`, `primaryKey` and `rowKey` are ordinary
  code, so `key` counts only when a qualifier alongside it says the key is cryptographic or an API
  credential — `apiKey`, `encryptionKey`, `privateKey`. The key-material rules keep the looser
  reading, because they carry a second constraint the name alone does not: the value must have key
  *shape*.
- **Placeholders and references are not secrets.** `${db.password}`, `@db.password@`, `changeme`,
  `ENC(...)` (jasypt — already encrypted), empty values and `xxxx` are all suppressed. Without this
  the gate blocks a large share of any real configuration tree.
- **Dense is not the same as secret.** UUIDs, checksums, fully-qualified class names, paths and MIME
  types are excluded from the entropy rule, and long runs of pure hex are left to the
  credential-named and key-shape rules — flagging every `sha256` constant would bury the real
  findings.

### Findings never quote the matched bytes

`guardrail_findings` is persisted to DynamoDB, the CloudWatch log group and
`migration-review.html`. Echoing the secret into a finding would copy it into three more places, so
a finding carries a kind and a line number and nothing else. A test pins this.

### Mechanical invariants get mechanical answers

"Zero `javax.*` in output" is not a judgement call, so a regex decides it. The allowlist
distinguishes JDK `javax.*` (`javax.crypto`, `javax.sql`, `javax.xml.parsers`) from Jakarta EE —
`javax.xml.bind` **is** Jakarta, so a blanket `javax.xml` carve-out would silently pass unmigrated
JAXB imports, while rewriting `javax.crypto` would break the build.

### The model is never asked about package names

Whether a file is ours to migrate is a string comparison, decided by `file_scanner` before any model
runs. Asking an LLM inside `guardrails_post` is what sent both early live runs to `MANUAL_REVIEW` —
the second at a *passing* score of 80. `tests/test_scope.py` asserts the pre-flight prompt never
asks again, and `tests/test_secret_scan.py` asserts it never asks about secrets or PII either.

### Ordinary code must not trip the AWS policy

The pipeline turns *any* intervention on INPUT into `BLOCKED`, and `ANONYMIZE` is an intervention.
So the policy lists only entity types that are genuinely secrets. `EMAIL` and `IP_ADDRESS` are
deliberately absent — they appear in `@author` tags and config literals, and listing them would
block a large share of an ordinary codebase. The banned-phrase list is held to two strings for the
same reason: something like "you are now" lives in login pages.

### The two nodes carry different severities

`guardrails_pre` produces `BLOCKED` — nothing was transformed, terminal for this run.
`guardrails_post` produces `MANUAL_REVIEW` — work exists and a human should look at it. Nothing in
the post node can silently discard a transform. Findings accumulate across every check, so a file
can reach `DONE` still carrying WARN-level notes; only `guardrail_pre_verdict` and
`guardrail_post_verdict` hold a single value each.

---

## 8. What a file costs

| | Calls | Note |
|---|---|---|
| `ApplyGuardrail` | 2 | One INPUT, one OUTPUT. No model; billed per text unit. |
| Model calls | 3 | Opus transform, Nova review, Opus post-check. |
| If check 1 or 2 fires | **0** | Returns before any network call at all. |
| If out of scope | **0** | `file_scanner` refuses it before the graph starts. |
| With `preflight_model_check` on | 4 | Adds one Opus call per file, before the transform. |

Only the model calls increment `bedrock_calls` and accrue into `estimated_cost_usd`, which drives
the `FORGE-CostSpike` alarm. A retry re-runs the transform and the review, adding two model calls
against the same budget.

---

## 9. Configuration

All of it from `agents.yaml`, generated by
[forge-terraform/scripts/generate-agents-yaml.sh](../forge-terraform/scripts/generate-agents-yaml.sh)
after a Terraform apply. A guardrail edit publishes a new numbered version automatically
(`replace_triggered_by`) — regenerate `agents.yaml` afterwards so `guardrail_version` moves with it.

```yaml
# ─── Bedrock Guardrails ───
guardrail_id: "y2o57muetcaf"
guardrail_version: "1"

complexity_block_threshold: 2000   # check 2, a local comparison

# ─── The secret gate (check 1) ───
secret_scan:
  enabled: true
  action: block     # warn = a finding only, and the file IS then sent
  entropy:
    enabled: true
    min_length: 20
    min_bits: 4.0
  allow: []         # regexes for literals the team has cleared

# ─── Optional qualitative pre-flight ───
preflight_model_check: false   # on = source reaches a model before the gate clears it
```

### Tuning the gate

- A legitimate literal that trips the scan — a NIST test vector, a sample token in a fixture — goes
  in `secret_scan.allow` as a regex. It matches against the **line**, so a trailing
  `// NIST vector` comment is enough to clear it.
- `action: warn` records findings and migrates anyway. **It sends the secret to the model**, so it
  exists for a team whose policy permits that, not as a way to quiet a noisy scan.
- `entropy.enabled: false` drops the catch-all rule and keeps the named detectors. Prefer raising
  `min_bits` or adding `allow` entries.

---

## 10. Known gaps

- **Keystores and certificate files are never scanned.** No phase or pack glob matches `.jks`,
  `.p12`, `.pem` or `.crt`, so those files never enter the pipeline. Nothing screens them because
  nothing sends them.
- **The gate reads one unit at a time.** A credential split across two files, or assembled at
  runtime from parts, is not detectable by a per-file scan.
- **No alarm watches `files_blocked`.** The metric is published
  ([forge/utils/telemetry.py](forge/utils/telemetry.py)), but the four CloudWatch alarms cover
  retries, manual rate, throughput and cost. A run where the gate blocks everything would only trip
  the stalled-pipeline alarm, and only indirectly.
- **The guardrail has no custom regex policy.** Adding `regexes_config` would give the OUTPUT side
  the same key-material coverage check 1 gives the input, at the cost of a Terraform apply and a
  version bump.

---

## 11. Source map

| Concern | File |
|---|---|
| Pre-transform node | [forge/agents/guardrails_pre.py](forge/agents/guardrails_pre.py) |
| Post-transform node | [forge/agents/guardrails_post.py](forge/agents/guardrails_post.py) |
| `ApplyGuardrail` client | [forge/guardrails/bedrock_guardrails.py](forge/guardrails/bedrock_guardrails.py) |
| The secret gate | [forge/utils/secret_scan.py](forge/utils/secret_scan.py) |
| Rule 1 + scope | [forge/utils/java_checks.py](forge/utils/java_checks.py) |
| Risk scoring | [forge/risk/score.py](forge/risk/score.py) |
| Routing | [forge/graph.py](forge/graph.py) |
| The guardrail resource | [forge-terraform/modules/foundation/main.tf](../forge-terraform/modules/foundation/main.tf) |
| Tests | [tests/test_secret_scan.py](tests/test_secret_scan.py), [tests/test_guardrails.py](tests/test_guardrails.py), [tests/test_scope.py](tests/test_scope.py) |
