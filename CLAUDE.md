# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

FORGE is an AI-powered Java migration pipeline. It uses LangGraph + AWS Bedrock (Claude Opus 4.8 for transformation, Amazon Nova Pro for review) to upgrade Java codebases — migrating `javax.*` → `jakarta.*`, modernising deprecated APIs, and upgrading Spring versions. The pipeline runs file-by-file, tracks state in DynamoDB, and evaluates every file through Bedrock Guardrails before and after transformation.

The repo currently contains:
- `forge-terraform/` — all AWS infrastructure as Terraform modules
- `prompts/` — full specifications for each build phase
- `forge-mvp/` — Python pipeline, covered by a fully mocked test suite (`pytest` from `forge-mvp/`, no AWS needed)
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
./scripts/generate-agents-yaml.sh dev --out ../forge-mvp/agents.yaml
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

Original spec in `forge-mvp/PHASE0-SPEC.md`. Key design points:

- **LangGraph graph**: `guardrails_pre → java_upgrade → syntax_check → java_reviewer → guardrails_post → write_file → verify_build → update_state`
- **Syntax check before review** (`syntax_check: true`; off when the key is absent): javac parses each Java output stopped at the PARSE stage (no classpath), XML is checked for well-formedness. A failure retries with javac's errors as the feedback and costs no review call; no javac is SKIPPED, never FAIL. `forge/verify/syntax.py`.
- **Parallel files**: `max_parallel_files` (8 in the generated agents.yaml, 1 when absent) runs that many files of a pack at once; results stay in scan order, cancel starts nothing new, Maven build verification stays sequential. See ARCHITECTURE §8.
- **Packs select by content**: javax-to-jakarta, struts2, spring6 and java21 take only Java files matching what they change (`content_match`), not `**/*.java`. A `content_match` hit is HIGH risk only when the entry says `risk: high`.
- **Unit tests by default**: with `test_generation.after_plan: true` (the generated config), the chat writes tests when the last pack of the plan finishes, before the build — through the same spend gate as `generate_tests` (over `confirm_above_usd` it builds first and parks a confirm card; confirming writes the tests and rebuilds). `forge/leader/tools.py` `_finish_plan`.
- **Project build before landing**: `build_project` (chat) / `--build-project` (CLI) compiles source + output with the project's own build — Maven reactors in order into `~/.forge/m2`, or `project_build.command` (the repo's own script works: it runs from the build copy, `{maven_repo}` points it at the isolated repo, `project_build.env` passes its variables) — and writes `project-build.json`. Landing shows the verdict (passed / failed / not run / stale) and never refuses on it. The leader sees the verdict, never compiler output.
- **javax-to-jakarta is a complement**: it selects and checks any `javax.` not in `STAYS_JAVAX_PREFIXES` (`forge/utils/java_checks.py`: JDK + JSR-305, JCache, JDO ...). Tests hold the pack pattern and that tuple together.
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
python migrate.py ./myapp --discover --intent "latest Java and Spring, stay on Struts, ignore the db folder"
                                          # ...narrowed by a sentence: one cheap model call, needs agents.yaml
python migrate.py ./myapp --phase javax-to-jakarta --dry-run --file path/to/Foo.java
python migrate.py ./myapp --phase webapp-bootstrap-jakarta10 --output-dir ./migrated --acceptance
python migrate.py ./myapp --phase javax-to-jakarta --acceptance-only --output-dir ./migrated   # re-check an existing output
python migrate.py ./myapp --apply-decisions decisions.json --output-dir ./migrated            # approve / reject / retry from the review page
python migrate.py --feedback-report --output-dir ./migrated                                    # notes grouped by pack and rule
python migrate.py ./myapp --phase javax-to-jakarta --output-dir ./migrated --generate-tests    # ...and write JUnit 5 tests for what it wrote
python migrate.py ./myapp --generate-tests-only --output-dir ./migrated --run-tests            # tests over an earlier run, executed
```

**Local web UI.** `python migrate.py --ui` starts a FastAPI app on `127.0.0.1` (port 8765 or the
next free one; `--port` to fix it, `--no-browser` to just print the URL) and opens **a chat**. The
nine-step wizard is gone — the owner counted the steps and asked for "prompt instead of this project
setup" — so the leader agent (`forge/leader/`) asks which folder the repository is in, calls the
tools, and every step the wizard had is now a card in the transcript: the plan, the evidence behind
it, an estimate, review cards with diffs and approve/reject, acceptance, artifacts, and
`land_on_branch` to put the result on a git branch. With no output directory named, the chat writes
into the repository itself, `<repo>/.migrated`; every source walk prunes FORGE output
(`forge/utils/fs.py` `prune_dirs`: that name, or a directory holding a FORGE marker file), and
landing adds the folder to `.git/info/exclude` (never the project's `.gitignore`) before its
clean-tree check. Discovery is still free; intent still costs one
model call; anything over `leader.confirm_above_usd` parks a card and waits for a click. It is
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

**Test generation** (`forge/testgen/`) writes the JUnit 5 + Mockito tests the legacy code never
had, *after* the migration — `--generate-tests` on a run, `--generate-tests-only` over an existing
output directory, the `generate_tests` tool in the chat, or `service.generate_tests()`. It runs over the
**output** tree, because "the new code" is what the migration wrote; chained onto a run it is
narrowed to that run's `written_paths`. Graph: `testgen_pre → generate → static_checks → review →
write_tests → run_tests`, with the same retry-with-feedback loop and the same two-model
cross-validation as the migration. Two model calls per class, zero for one the rules exclude.
Artifacts are `test-generation-report.md` and `generated-tests.json`; `--generate-tests-only`
exits non-zero when anything was held, so it gates CI.

**Acceptance** (`forge/verify/acceptance.py`) runs a pack's declared checks over the *merged* view
(source tree with `./migrated` overlaid — `forge/verify/merged_tree.py`), since the output holds
only the files that were written. Every outcome is pass, fail with evidence, or skip with the
reason; the verdict is `INCOMPLETE`, never `PASS`, while anything was skipped. The exit code
reaches the shell, so `--acceptance-only` is a CI gate.

`--phase` accepts the built-in `java21` phase and every *complete* pack.
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

**Decisions reach the prompts through `decisions_block`** (`forge/context/inject.py`): the
decisions a pack declares, with their values (agents.yaml over `DEFAULT_DECISIONS`), are appended
to its transform and its review. Before it, "per the `container` decision" was a branch no model
could resolve — `container: tomcat` switched the Liberty pack off and changed nothing written.
`tomcat-context-config` is the Tomcat 10.1 counterpart of `liberty-server-config`: gated on
`container: tomcat`, it generates one `META-INF/context.xml` per web module (`tomcat_context`
selector) and retires the module's `src/main/liberty/config/*` through `deleted_files`. A retired
path leaves the manifest's writes (`run_manifest.record`, latest pack wins), so landing removes
it rather than copying an earlier pack's copy back in.

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

**No model is asked anything before the transform, and secrets never leave the machine.**
A model cannot be the control that decides what a model may see, and the pipeline used to ask Claude
*"does this file contain secrets?"* — the disclosure it claimed to prevent. `ApplyGuardrail` cannot
clear a file either: it is a network call of its own, and its policy has no entity type for key
material and no custom regex. So `forge/utils/secret_scan.py` is the gate, and it runs **first** in
`guardrails_pre` — ahead of `ApplyGuardrail`, for zero Bedrock calls. Every question the old
pre-flight call asked is now local: secrets to the scan, file size to an integer comparison, package
scope to `file_scanner`. The model call survives as `preflight_model_check`, **off by default**, with
a prompt that asks only about migration safety; turning it on is a policy decision. The happy path is
now **3** model calls per file, not 4 — `test_phase0_closeout` and `test_service` pin that.

The scan covers key material, vendor-prefixed tokens, credentials in URLs and connection strings,
credential-named assignments in Java/Spring/XML/properties/YAML, and high-entropy literals. **Recall
is weighted over precision on purpose**: a false positive blocks one file and names it in the report,
which a human clears with `secret_scan.allow`; a false negative ships a credential to a third party.
Four rules keep it usable, and each one was a real bug first: identifiers are **tokenised** not
substring-matched (`monkeyCount` is not a key); **`key` alone is not a credential** (`sortKey`,
`primaryKey`), so it needs a qualifier like `apiKey` or `encryptionKey` — the key-material rules keep
the looser reading only because they also require key *shape*; placeholders are suppressed
(`${...}`, `@...@`, `changeme`, `ENC(...)`) or the gate blocks most of a real config tree; and dense
is not secret, so UUIDs, checksums, FQCNs and paths are out of the entropy rule. Findings carry a
kind and a line number and **never the matched bytes** — `guardrail_findings` reaches DynamoDB, the
CloudWatch log group and `migration-review.html`, so quoting a secret would copy it into three more
places. Note none of this covers `.jks` / `.p12` / `.pem` files: no phase or pack glob matches those
extensions, so they never enter the pipeline. Full detail in `forge-mvp/GUARDRAILS.md`.

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
`forge-mvp/PHASE0-SPEC.md` — that spec line has been corrected, because leaving it in place is
how the bug gets reimplemented. Regression tests: `tests/test_scope.py` (including a guard that
the pre-flight prompt never asks about packages again) and `tests/test_phase0_closeout.py`.

**Orchestration is code, not an agent.** `FORGE-AgentDeepDive.pptx` puts a **Leader Agent** at the
centre of the architecture — "The Conductor", on Sonnet 4.5, one of its 15 agents. It is not built,
and that is the decision, not a gap: the LangGraph `StateGraph` in `forge/graph.py` *is* the Leader.
Each of the deck's six Leader duties is a static edge or a plain Python function — unit order and
`PENDING` selection in `forge/service.py`, specialist selection in `file_scanner` plus the pack's
`applies_to`, retry and feedback injection on the `increment_retry → java_upgrade` edge, verdict
reading and escalation in `route_reviewer` / `route_post` / `must_hold`, bookkeeping in
`update_state`. No node returns a node name; there is no `bind_tools`, `@tool`, `ToolNode` or
`create_react_agent` anywhere in the tree.

**There is now a model-driven leader, and it is bounded rather than forbidden.** This rule used to
read *"do not add a model-driven leader"*. The owner reversed it deliberately, and the reversal is
recorded here rather than quietly dropped, because the objection it was protecting against is still
the right objection — a mechanical question handed to a model, at the layer where a wrong answer is
hardest to debug, with a run order that stops being reproducible.

What changed is the scope of the question, not the answer to it. `forge/leader/` (ARCHITECTURE §16,
LEADER.md) lets a model **sequence** work and talk to a human; it does not let a model decide what
the repository contains. The split is enforced in code, not in the prompt:

- **Pack activation stays mechanical.** `resolve_packs` over `detect` evidence. A pack the leader
  names that discovery did not select never reaches `service` — the conversation's `selected_packs`
  is the bound.
- **`forge/graph.py` is untouched.** Everything inside a run — unit order, `route_reviewer`,
  `max_retries`, `must_hold` — is the same code as before. The leader chooses *which run*, never
  what happens in one.
- **Money and mutation are a click.** Anything over `leader.confirm_above_usd` parks until a human
  confirms; applying review decisions, `land_on_branch` and `open_pull_request` are always
  confirmed, whatever the estimate. FORGE pushes only through `open_pull_request`, only on the
  user's click: the branch this chat landed, to `origin`, never forced, then `gh pr create` into
  the branch landing started from, with a body FORGE builds from its own records (no source,
  diffs or compiler output). **`leader.auto_publish` is the owner's opt-in exception** (off in the
  generated config; the owner turned it on for AMS): when the plan's own build passes and is
  current, `_finish_plan` lands on `<branch_prefix>-<timestamp>` and opens the PR with no click,
  by calling the same two handlers — so every refusal (dirty tree, existing branch, no `gh`)
  still stands. A failed, skipped or stale build publishes nothing; tests parked for a click hold
  the publish until that click. It is decided in code, never by the leader.
- **`risk_ceiling` never comes from a prompt.** The toolbox overwrites it with the config's value
  after every `resolve_intent`, because a model-authored intent sentence outranking config would
  disable the hold gate.

The reviewer's influence is bounded on both sides for the same reason — it returns an integer,
`route_reviewer` picks the branch, `max_retries` caps the loop, and the model's own `review_verdict`
string is recorded and never routed on. If a change would move one of the four limits above into the
prompt, that is the mistake this rule is still about. §3 and §16 of `forge-mvp/ARCHITECTURE.md`
carry the duty-by-duty mapping.

**The intent layer is not an exception to that — check it against the rule before extending it.**
`--discover --intent "..."` (`forge/intent/`, ARCHITECTURE §15, INTENT.md) does hand a model a
question at the control plane, and it is allowed because the question is not the mechanical one.
Pack *activation* stays `resolve_packs` over `detect` evidence; the model may only narrow that set
and fill in the `decisions` a human would otherwise hand-edit into `forge-profile.yaml`. It cannot
activate a pack the repository shows no evidence for, it cannot order anything — `resolve_order`
still does — and the resolved plan is persisted with per-decision provenance so a rerun needs no
model at all. `reconcile()` is pure and holds every one of those limits; if a change would move one
of them into the prompt instead, it is the mistake this rule is about. The test that keeps it
honest is `test_discover_without_intent_makes_no_model_call`: discovery with no intent must stay
free, with no model and no AWS.

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

**A generated test is never written unless it is worth having, and the model never chooses where
it lands.** Which classes get one is decided in `forge/testgen/targets.py` from the type
declaration, its annotations and its public signatures — an interface, an abstract class, a class
with no public members and a class that **already has a test** are skipped, each with a reason in
the report, because a silent skip is indistinguishable from an oversight. An existing test is a
human's work and the one artifact this pipeline must not overwrite (`overwrite: false`). The
destination is rebuilt from the generated file's own `package` and type name under
`src/test/java`, so unlike `write_output` the model's path key is **discarded**, not merely
guarded: there is one right place for `com.corp.UserServiceTest`.

The mechanical invariants are checked in code and **before** the review, so a JUnit 4 import costs
zero review calls — `forge/testgen/checks.py` covers JUnit 4 (`org.junit.Test`, `@RunWith`,
`Assert`), Jakarta-EE `javax.*`, a missing `@Test`, `@Disabled`, the class name and package,
non-determinism (`Thread.sleep`, `Math.random`, unseeded `Random`, `System.getenv`) and a secret
scan of the generated file. A unit that fails them, that scores below `pass_threshold` after its
retries, or whose test **ran and failed**, is staged under `.forge-staging/` with the reason and is
not written — a broken test in `src/test/java` breaks every build after it, so a failing test is
also removed from the output tree before the retry. Whether the test or the migrated code is wrong
is the human's call, and the failure output reaches both the retry prompt and the report. FORGE
never edits the build file: the test dependencies a generated test needs are *reported*, not added.
The prompt's first rule is "never invent API", which is why `forge/testgen/context.py` supplies the
collaborators' public signatures and the test libraries the build actually carries.

**The file writer treats model output as untrusted.** Destination paths come from the LLM, so
`write_output` refuses anything resolving outside `output_dir`, and reconstructs the package path
from the source's own `package` declaration when the model returns a bare filename.

## Specs
- `forge-terraform/SPEC.md` — the build prompt that directory came from
- `forge-mvp/PHASE0-SPEC.md` — the Phase 0 build prompt (historical)
