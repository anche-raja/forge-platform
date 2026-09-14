# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

FORGE is an AI-powered Java migration pipeline. It uses LangGraph + AWS Bedrock (Claude Opus 4.8 for transformation, Amazon Nova Pro for review) to upgrade Java codebases — migrating `javax.*` → `jakarta.*`, modernising deprecated APIs, and upgrading Spring versions. The pipeline runs file-by-file, tracks state in DynamoDB, and evaluates every file through Bedrock Guardrails before and after transformation.

The repo currently contains:
- `forge-terraform/` — all AWS infrastructure as Terraform modules
- `prompts/` — full specifications for each build phase
- `forge-mvp/` — Python pipeline, covered by 300+ tests (`pytest` from `forge-mvp/`, no AWS needed)
- `prompts/packs/` — the stack pack library: one technology transition per file, loaded at runtime

## Terraform — forge-terraform/

### First-time setup
```bash
# 1. Create S3 bucket + DynamoDB table for Terraform state (run once)
bash forge-terraform/scripts/bootstrap-state.sh <aws_account_id>

# 2. Init with backend config
cd forge-terraform
terraform init \
  -backend-config="bucket=forge-terraform-state-<aws_account_id>" \
  -backend-config="key=forge/dev/terraform.tfstate" \
  -backend-config="region=us-east-1"

# 3. Copy and fill in vars
cp terraform.tfvars.example terraform.tfvars
```

### Deploy by phase
```bash
# MVP (Phase 0) — foundation + observability; the other modules are off by default
terraform apply

# Phase 6 — set enable_sqs = true in terraform.tfvars, then
terraform apply

# Future — only when a trained model artifact is in S3: enable_sagemaker = true, then
terraform apply
```

### Generate agents.yaml after apply
```bash
./scripts/generate-agents-yaml.sh dev > ../forge-mvp/agents.yaml
```

### Module map
| Module | Resources | Deploy before |
|---|---|---|
| `foundation` | 2 DynamoDB tables, Bedrock Guardrails, IAM execution role | Phase 0 |
| `observability` | CloudWatch log group, dashboard, 4 alarms, SNS topic | Phase 0 |
| `sqs` | Manual review queue + DLQ | Phase 6 (`enable_sqs`) |
| `sagemaker` | TGI endpoint, SSM parameter | Future only (`enable_sagemaker`) |

### Architecture decisions baked into the Terraform

**Backend variables are literals.** Terraform does not allow variable interpolation inside `backend {}` blocks. The bucket name in `backend.tf` is a placeholder — always pass the real values via `-backend-config` flags at `terraform init` time. Do not attempt to use `var.*` inside the backend block.

**`try()` for optional module outputs.** The root `outputs.tf` wraps `sagemaker` and `sqs` outputs in `try(..., null)`. This prevents index-out-of-range errors when `count = 0` modules are not deployed.

**Conditional Phase 6 modules.** `count = var.enable_* ? 1 : 0` is on the module calls in root `main.tf` (`sqs`, `sagemaker`), not on individual resources inside the modules. All resources inside a module are unconditional — the gate is purely at the root level.

**Bedrock IAM must cover inference profiles.** `agents.yaml` names cross-region profiles (`us.anthropic.…`), which need `bedrock:InvokeModel` on the account's `inference-profile/*` *and* on `foundation-model/*` in every region the profile can route to. An in-region `foundation-model/*` grant alone is `AccessDenied`.

**The guardrail must not intervene on ordinary code.** `guardrails_pre` turns *any* `GUARDRAIL_INTERVENED` on INPUT into `BLOCKED`, and `ANONYMIZE` is an intervention. So the guardrail lists only entity types that are genuinely secrets (AWS keys, card numbers, SSNs, passwords) — never `EMAIL` or `IP_ADDRESS`, which appear in `@author` tags and config literals. A guardrail edit is published as a new version automatically (`replace_triggered_by`); regenerate `agents.yaml` afterwards so `guardrail_version` moves with it.

**IAM execution role trust policy** includes `data.aws_caller_identity.current.arn` so the developer/CI identity that runs Terraform can also assume the role via `aws sts assume-role` for local development. No long-lived access keys needed.

### Cost profile
- MVP only (foundation + observability): ~$5/mo idle, ~$20–40/mo during active migration
- Adding `sagemaker`: +~$1,093/mo for ml.g5.2xlarge always-on — stop endpoint when not in use

## FORGE pipeline — forge-mvp/

Original spec in `prompts/FORGE-Phase0-MVP.md`. Key design points:

- **LangGraph graph**: `guardrails_pre → java_upgrade → java_reviewer → guardrails_post → write_file → verify_build → update_state`
- **Retry loop**: reviewer score 50–79 routes back to `java_upgrade` with feedback injected into prompt; max 2 retries. A failed build reuses the same loop and the same budget.
- **Bedrock Guardrails** called as a standalone `ApplyGuardrail` API call — not inline with model invocation. Used both pre (INPUT) and post (OUTPUT).
- **DynamoDB checkpointer**: LangGraph uses `DynamoDBSaver` with `thread_id = file_path`
- **Two separate models**: Claude Opus 4.8 (`us.anthropic.claude-opus-4-8`) for transformation, Amazon Nova Pro for review — intentional cross-validation. Opus 4.8 is only reachable through the `us.` cross-region inference profile; there is no in-region model ID in `us-east-1`.
- **agents.yaml** is the single config file read at startup — all AWS resource IDs, model IDs, thresholds come from there. Generated by `scripts/generate-agents-yaml.sh` after Terraform apply.

### Run it

```bash
cd forge-mvp
pytest                                    # no AWS needed
python migrate.py --list-packs            # the pack library, in dependency order
python migrate.py ./myapp --discover      # which packs apply to this repo, with evidence; writes forge-profile.yaml
python migrate.py ./myapp --phase javax-to-jakarta --dry-run --file path/to/Foo.java
python migrate.py ./myapp --phase webapp-bootstrap-jakarta10 --output-dir ./migrated --acceptance
python migrate.py ./myapp --phase javax-to-jakarta --acceptance-only --output-dir ./migrated   # re-check an existing output
python migrate.py ./myapp --apply-decisions decisions.json --output-dir ./migrated            # approve / reject / retry from the review page
python migrate.py --feedback-report --output-dir ./migrated                                    # notes grouped by pack and rule
```

**Local web UI.** `python migrate.py --ui` starts a FastAPI app on `127.0.0.1` (port 8765 or the
next free one; `--port` to fix it, `--no-browser` to just print the URL) and opens a page that walks
the same flow as steps: Project → Discover → Run → Review → Accept → Feedback → Artifacts. It is
for one engineer on their own machine: loopback only, no auth, one job at a time (a second run is
refused with 409 because the extract cache and boto3 clients are process-global). **The UI and the
CLI call the same functions** — `forge/service.py` holds the one implementation of a run, and
`migrate.py` is a printer over its events (`tests/test_service.py` pins the CLI's exact stdout).
Add behaviour to the service, never to a route or a CLI branch. Per-run decisions (`risk_ceiling`
from the page) reach the hold gate through `ForgeConfig.with_overrides`, not a temp file.
`/api/files` serves artifacts only from inside the chosen output directory.

**Discovery** (`forge/discover/`) profiles a repository with no model — build system, Java level,
resolved dependency versions (through `${properties}`, `dependencyManagement` and the well-known
imported BOMs), imports, descriptors — and evaluates every pack's `detect` rules against it.
`decision_equals` rules are gates, never evidence: a decision cannot activate a pack by itself.

**Human in the loop** — three commands, one channel. Every unit is risk-scored deterministically
in the pre-flight node (`forge/risk/score.py`; a security config or a generated unit is HIGH by
rule). `decisions.risk_ceiling` decides what the tier means: `review-high` (default) stages HIGH
units under `./migrated/.forge-staging/` as `HELD` instead of writing them; `review-all` holds
everything; `auto` holds nothing. A held unit never reaches the build gate. Every run writes
`manual-review-queue.json` (v2: original + transformed + verdicts + risk reasons) and
`migration-review.html` — static, self-contained, original and transformed side by side with a
diff and a decision widget that emits `decisions.json`. **A dry run writes them too**, listing every
transform the model would have made. `migrate.py <source> --apply-decisions decisions.json
--output-dir ...` makes decisions real: approve promotes staged files and runs the build verifier
(a FAIL is reported, never reversed — the approval is the human's); reject discards with the reason;
retry re-runs the unit with the note as a `HUMAN REVIEW FEEDBACK` prompt block on a fresh budget
and a fresh checkpoint thread. The note lives in `human_note`, not `review_feedback` — that field
is only rendered on retries and a build failure overwrites it. `--feedback-report` groups notes by
pack and rule into `pack-feedback.md` so a repeated correction becomes a pack edit.

**Acceptance** (`forge/verify/acceptance.py`) runs a pack's declared checks over the *merged* view
(source tree with `./migrated` overlaid — `forge/verify/merged_tree.py`), since the output holds
only the files that were written. Every outcome is pass, fail with evidence, or skip with the
reason; the verdict is `INCOMPLETE`, never `PASS`, while anything was skipped. The exit code
reaches the shell, so `--acceptance-only` is a CI gate.

`--phase` accepts the two built-in phases (`java21`, `struts-spring6`) and every *complete* pack.
A pack that needs a context extractor which is not built yet is refused with a message listing
what is runnable; `forge.utils.file_scanner.runnable_phases()` is the source of truth.

### Packs, extractors and context (Phase 1)

The contract is `prompts/FORGE-Platform-Requirements.md`; read it before touching a pack.

**Packs are data, loaded at startup.** `forge/packs/loader.py` parses `prompts/packs/*.pack.md`
(YAML frontmatter + `## transform` + `## review`), validates strictly — rubric weights must total
100 **and** match the response schema's per-check maxima in order; detect/acceptance kinds must be
known; `depends_on` must resolve acyclically — and orders them topologically with tier as the
tie-break. `depends_on` is an **ordering edge, not a requirement**: `jsp-jstl-modernize` lists
both Struts packs because it must follow whichever runs. A malformed pack degrades the library to
the built-in phases with a warning rather than stopping Phase 0; `--list-packs` prints the error.

**`applies_to` has three kinds, and the difference is whether the pack runs today.** `file_glob`
names files by path. `content_match: {glob, pattern}` names them by what is in them — "the class
that extends `WebSecurityConfigurerAdapter`" is decidable from one file's bytes, so no extractor
is needed. `selector:` names a set only a context extractor can resolve, and the scanner **refuses**
the pack until that extractor is registered — running only the glob half would migrate the
configuration and skip the classes it refers to, which is worse than not running.

**Context extractors are deterministic and model-free** (`forge/extract/`). A pack declares
`context: <name>`; the extractor runs once per module, cached in-process, never stored in
`ForgeState` (DynamoDB item cap). `web_bootstrap` parses `web.xml` under every Servlet namespace,
the JBoss/WebLogic/WebSphere vendor descriptors (`.xml` and `.xmi`), EAR, datasource sources and
an existing Liberty `server.xml`, in declaration order and dropping nothing — unknown elements go
to `raw_unmapped`. It resolves `servlet_components` (real files) and `server_config` (one
**generated** `server.xml` per module; an existing one is an edit, not a generate).

**Context reaches the prompts through `forge/context/`.** `render_context` produces a
deterministic block bounded by `context.max_chars` — a hard guarantee, with section priority
depending on the target and `summary` never omitted. `context_block_for` appends it in
`java_upgrade`, `java_reviewer` and (for generated units) `guardrails_pre`; `FileStatus` records
the extractor name and a sha256 of the block. The full context is written to
`migration-context.json` beside the report, in dry-run too.

Two idioms that have already bitten: never `a or b` on `ElementTree` elements (an element with no
children is falsy — use `is not None`), and never write a regex in a double-quoted YAML scalar
(`"\."` is an invalid escape — single-quote it).

### Architecture decisions baked into the pipeline

**Rule 1 is enforced in code, not by a model.** "Zero `javax.*` in output" is a mechanical
invariant, so `forge/utils/java_checks.py` checks it with a regex and `guardrails_post`
escalates on a hit. The model is only asked the qualitative questions. The allowlist there
distinguishes JDK `javax.*` (`javax.crypto`, `javax.sql`, `javax.xml.parsers`) from Jakarta EE
(`javax.xml.bind` **is** Jakarta) — a blanket `javax.xml` carve-out would silently pass
unmigrated JAXB imports, and rewriting `javax.crypto` would break the build.

**A migration never renames a package, and no model is asked about one.** Renaming would break
every import, `component-scan` base package, and reflective lookup in the codebase — the struts
spec's own rule is *"Do not auto-rename; flag."* `scope_package_prefix` answers one question
only: *is this file ours to migrate?* That is a string comparison, so `file_scanner.py` decides
it before any model call, and an out-of-scope file costs **zero** Bedrock calls instead of four.
Skipped files are listed in the migration report — never silently dropped.

Two rules in `java_checks.in_scope()` are easy to get wrong: match on a **package boundary**
(`com.corp` must not swallow `com.corporate`), and treat a file with **no package declaration**
as in scope — Struts XML configs and default-package classes have nothing to judge, and absence
of evidence is not grounds for skipping.

Asking an LLM this question inside `guardrails_post` is what sent both early live runs to
MANUAL_REVIEW, the second at a *passing* score of 80. The clause that caused it came from
`prompts/FORGE-Phase0-MVP.md` — that spec line has been corrected, because leaving it in place is
how the bug gets reimplemented. Regression tests: `tests/test_scope.py` (including a guard that
the pre-flight prompt never asks about packages again) and `tests/test_phase0_closeout.py`.

**Prompts live in `forge/phases.py`, not in the agents.** Each `PhaseSpec` pairs a transform
prompt with the reviewer rubric that grades it. They must be changed together — a rubric whose
checks no longer total 100 makes `pass_threshold` meaningless, and `test_phases.py` asserts the
weights still sum to 100. Adding a phase means adding one registry entry; the CLI's `--phase`
choices, the agents, and the file scanner all read from it.

**Metric names are a contract with Terraform.** `forge/utils/telemetry.py` publishes exactly the
names the alarms and dashboard reference, **without dimensions** — the alarms declare none, so a
dimensioned metric would never match them. Telemetry is fail-soft everywhere: a migration run
must never die because CloudWatch is unreachable.

**Build verification is opt-in.** `javac` on a single file only succeeds when the project's
dependencies are on the classpath, so `build_verification.enabled` defaults to `false`. Set
`classpath`, or use `mode: maven` against a real `pom.xml`. A missing compiler yields SKIPPED,
not FAIL — a toolchain gap is an environment problem, not a bad migration.

**The file writer treats model output as untrusted.** Destination paths come from the LLM, so
`write_output` refuses anything resolving outside `output_dir`, and reconstructs the package path
from the source's own `package` declaration when the model returns a bare filename.

## Specs
- `prompts/FORGE-Infra-Terraform.md` — full infrastructure specification
- `prompts/FORGE-Phase0-MVP.md` — Phase 0 Python pipeline specification
