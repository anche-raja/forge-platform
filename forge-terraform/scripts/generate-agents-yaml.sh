#!/usr/bin/env bash
# generate-agents-yaml.sh — reads terraform output and writes a complete agents.yaml.
# Run after terraform apply for at least module.foundation and module.observability.
#
# Usage:
#   ./scripts/generate-agents-yaml.sh [environment] [--out PATH]
#
# Example (writes directly to forge-mvp):
#   ./scripts/generate-agents-yaml.sh dev --out ../forge-mvp/agents.yaml
#
# With --out the file is written atomically: the YAML goes to a temp file beside
# the target and is moved into place only once this script has succeeded. Prefer
# it to `> file`, which truncates the target *before* the script runs and so
# leaves an empty agents.yaml behind whenever terraform is unreachable -- an
# empty config is worse than none, because it looks present to every caller.
# Without --out the YAML still goes to stdout, so existing redirects keep working.
#
# The [environment] argument does NOT choose an environment. Which state this
# reads was fixed by the -backend-config="key=forge/<env>/terraform.tfstate"
# you passed at `terraform init`. The argument is a label, and it is checked
# against that key so a mismatch stops here instead of quietly producing a
# config for the wrong account. To switch environments, re-run `terraform init`.

set -euo pipefail

ENV="dev"
OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)   [[ $# -ge 2 ]] || { echo "ERROR: --out needs a path." >&2; exit 2; }
             OUT="$2"; shift 2 ;;
    --out=*) OUT="${1#--out=}"; shift ;;
    -*)      echo "ERROR: unknown option '$1'." >&2
             echo "       Usage: $(basename "$0") [environment] [--out PATH]" >&2
             exit 2 ;;
    *)       ENV="$1"; shift ;;
  esac
done

# Resolve --out against the caller's working directory NOW, because this script
# cd's to TF_DIR below. A relative path would otherwise be read relative to
# forge-terraform/, which is not where the caller meant -- and unlike a `>`
# redirect, whose path the shell resolves before the script ever starts, an
# --out path is this script's job to resolve.
if [[ -n "$OUT" ]]; then
  OUT_DIR="$(dirname "$OUT")"
  [[ -d "$OUT_DIR" ]] || { echo "ERROR: no such directory: ${OUT_DIR}" >&2; exit 2; }
  OUT="$(cd "$OUT_DIR" && pwd)/$(basename "$OUT")"
fi
# Optional org package root. Filters which files the scanner picks up;
# empty = migrate everything under source_dir. Never renames packages.
SCOPE_PACKAGE_PREFIX="${FORGE_SCOPE_PACKAGE_PREFIX:-}"

# Resolve the forge-terraform directory relative to this script's location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/.."

echo "==> Reading terraform outputs from: ${TF_DIR}" >&2
cd "$TF_DIR"

# Which state is actually being read. `terraform init` records the resolved
# backend config here, so this is the ground truth — not the argument above.
# Reported unconditionally: the whole failure mode is an operator believing the
# command line over the backend, so the backend has to be the thing on screen.
BACKEND_META=".terraform/terraform.tfstate"
STATE_KEY=""
if [[ -f "$BACKEND_META" ]]; then
  STATE_KEY="$(jq -r '.backend.config.key // ""' "$BACKEND_META" 2>/dev/null || echo "")"
  STATE_BUCKET="$(jq -r '.backend.config.bucket // ""' "$BACKEND_META" 2>/dev/null || echo "")"
  if [[ -n "$STATE_KEY" ]]; then
    echo "==> State: s3://${STATE_BUCKET}/${STATE_KEY}" >&2
  fi
fi

# A label that contradicts the state is the dangerous case: `… prod` against a
# dev backend yields a dev config whose header says prod. Refuse it. Only when
# the key actually carries the label as a path segment can we judge, so an
# unrecognised layout warns rather than blocking a legitimate run.
if [[ -n "$STATE_KEY" ]]; then
  if [[ "/${STATE_KEY}" != *"/${ENV}/"* ]]; then
    if [[ "$STATE_KEY" =~ /(dev|test|stage|staging|prod|production)/ ]]; then
      echo "ERROR: asked for '${ENV}', but the initialised backend is '${STATE_KEY}'." >&2
      echo "       This argument does not switch environments — \`terraform init\` does." >&2
      echo "       Re-run: terraform init -reconfigure -backend-config=\"key=forge/${ENV}/terraform.tfstate\" …" >&2
      exit 1
    fi
    echo "WARNING: cannot confirm '${ENV}' against state key '${STATE_KEY}'; check it is the one you want." >&2
  fi
fi

TF_OUTPUT="$(terraform output -json 2>/dev/null)"

if [[ -z "$TF_OUTPUT" ]] || [[ "$TF_OUTPUT" == "{}" ]]; then
  echo "ERROR: No terraform outputs found. Run terraform apply first." >&2
  exit 1
fi

# Extract values — use // empty to handle null/missing outputs gracefully
AWS_REGION="$(echo "$TF_OUTPUT" | jq -r '.aws_region.value // "us-east-1"')"
DYNAMODB_TABLE="$(echo "$TF_OUTPUT" | jq -r '.dynamodb_state_table.value // ""')"
CHECKPOINT_TABLE="$(echo "$TF_OUTPUT" | jq -r '.dynamodb_checkpoint_table.value // ""')"
GUARDRAIL_ID="$(echo "$TF_OUTPUT" | jq -r '.guardrail_id.value // ""')"
GUARDRAIL_VERSION="$(echo "$TF_OUTPUT" | jq -r '.guardrail_version.value // ""')"
LOG_GROUP="$(echo "$TF_OUTPUT" | jq -r '.cloudwatch_log_group.value // ""')"
SQS_URL="$(echo "$TF_OUTPUT" | jq -r '.sqs_queue_url.value // ""')"
SM_ENDPOINT="$(echo "$TF_OUTPUT" | jq -r '.sagemaker_endpoint_name.value // ""')"

# Emit agents.yaml. With --out, stdout is pointed at a temp file beside the
# target and moved into place only after the heredoc completes. `set -e` means
# any failure above returns before the mv, and the EXIT trap clears the temp
# file -- so a failed run leaves an existing agents.yaml exactly as it was.
if [[ -n "$OUT" ]]; then
  TMP_OUT="$(mktemp "${OUT}.XXXXXX")"
  trap 'rm -f "${TMP_OUT}"' EXIT
  exec 3>&1 1>"$TMP_OUT"
fi

cat <<EOF
# Generated by generate-agents-yaml.sh — do not edit manually.
# Re-generate after any terraform apply:
#   ./scripts/generate-agents-yaml.sh ${ENV} --out ../forge-mvp/agents.yaml

# ─── Models ───────────────────────────────────────────────────────────────────
transform_model: "us.anthropic.claude-opus-4-8"
review_model: "us.amazon.nova-pro-v1:0"
# The chat's "Trial run" box swaps transform_model for this one, for that turn
# only. About 40% of Opus 4.8 per file; keep it priced in model_pricing below.
trial_transform_model: "us.anthropic.claude-sonnet-5"

# Converse maxTokens for every pipeline call (the leader has its own, below).
# Left unset, ChatBedrockConverse omits maxTokens and Bedrock applies a much
# smaller default: a transform that must return a whole file inside a JSON
# envelope is then cut off mid-object, and on a reasoning model that spends the
# budget before emitting any text it returns nothing at all -- which surfaces as
# "Failed to parse transform output as JSON", never as a token limit. Raise it
# for a codebase with large files. This caps output, it does not reserve it:
# only tokens the model actually emits are billed.
max_tokens: 16384

# Output ceiling Bedrock enforces per model (substring of the model ID). Each
# call sends min(max_tokens, its model's limit); above the limit Converse
# rejects the call outright ("exceeds the model limit of 10000").
model_output_limits:
  "anthropic.claude-opus-4-8": 128000
  "anthropic.claude-sonnet-5": 128000
  "amazon.nova-pro": 10000

# Seconds to wait for a Bedrock reply (optional; default 600). Converse is not
# streamed, so nothing arrives until the whole file is written, and botocore's
# own 60s default times out on a large pom.xml with ReadTimeoutError.
bedrock_read_timeout: 600

# ─── AWS ──────────────────────────────────────────────────────────────────────
aws_region: "${AWS_REGION}"

# ─── DynamoDB ─────────────────────────────────────────────────────────────────
dynamodb_table: "${DYNAMODB_TABLE}"
dynamodb_checkpoint_table: "${CHECKPOINT_TABLE}"

# ─── Bedrock Guardrails ───────────────────────────────────────────────────────
guardrail_id: "${GUARDRAIL_ID}"
guardrail_version: "${GUARDRAIL_VERSION}"

# ─── CloudWatch ───────────────────────────────────────────────────────────────
cloudwatch_namespace: "FORGE/Migration"
cloudwatch_log_group: "${LOG_GROUP}"

# ─── Optional: SQS (deploy module.sqs before Phase 6) ────────────────────────
sqs_queue_url: "${SQS_URL}"

# ─── Optional: SageMaker internal LLM (future) ───────────────────────────────
sagemaker_endpoint_name: "${SM_ENDPOINT}"

# ─── Migration settings ───────────────────────────────────────────────────────
source_java_version: "8"
target_java_version: "21"
pass_threshold: 80
retry_threshold: 50
max_retries: 2
# Which files are ours to migrate. Filters at scan time by the file's declared
# package, so an out-of-scope file costs zero Bedrock calls. Empty (the default)
# migrates everything under source_dir. Matching is on a package boundary, so
# "com.corp" covers com.corp.user but not com.corporate. Files with no package
# declaration (XML configs, default-package classes) are always in scope.
# This NEVER renames a package — the declaration is read, never rewritten.
scope_package_prefix: "${SCOPE_PACKAGE_PREFIX}"
# The other half of the same question: "leave this directory alone". Glob
# patterns relative to source_dir, matched the way a pack's file_glob rules are.
# Excluding only ever shrinks the unit set, and an excluded path the phase would
# otherwise have taken is reported in the run's skipped list, never dropped
# silently. \`--intent\` fills this in from a phrase like "ignore the db folder".
#   scope_exclude_globs: ["db/**", "**/vendor/**"]
scope_exclude_globs: []
complexity_block_threshold: 2000

# ─── Intent (optional; only read by \`--discover --intent "..."\`) ─────────────
# One model call that maps a plain-English request onto the decisions below and
# a subset of the packs discovery already activated. It can narrow that set and
# never extend it — evidence stays the only thing that activates a pack.
# \`model\` defaults to transform_model; this is classification over a closed
# vocabulary, so a small model is the right call. Whatever you name here must
# also appear in model_pricing or its cost silently accrues as \$0.00.
intent:
  model: "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# ─── Leader (optional; only read by the chat surface in the web UI) ──────────
# The conductor for the *conversation*, not for a run. It picks which wrapper
# around forge/service.py to call next and when to stop and ask; it never
# decides which packs exist, which files a pack takes, or what needs a human.
# \`model\` defaults to transform_model. Whatever you name here must also appear
# in model_pricing or its cost silently accrues as \$0.00.
#
# confirm_above_usd is the dial that decides how much authority it actually
# has: a tool whose estimated spend exceeds it does not run, it comes back as a
# card for you to click. 0 means "never ask" — a deliberate choice, and not the
# default, because one ambiguous sentence should not be able to start a \$100
# run. Applying review decisions is confirmed whatever this says: an approval is
# your signature on someone else's code.
#
# max_steps caps the tool calls in one turn, so a confused loop costs a turn
# rather than a budget; history_messages caps what is re-sent on every call,
# which is the other half of the same bill.
leader:
  model: ""
  max_steps: 8
  max_tokens: 2048
  confirm_above_usd: 0      # never ask: runs go ahead without a cost card
  # The documented average cost of one migrated file (GUARDRAILS.md §8): three
  # model calls, about \$0.07. Estimates only — what a run actually cost comes
  # from \`estimated_cost_usd\`, accrued per real call.
  unit_cost_usd: 0.07
  history_messages: 40

# ─── The secret gate ──────────────────────────────────────────────────────────
# Local, deterministic, and ahead of EVERY remote call — ApplyGuardrail included.
# A file carrying a credential is refused while its bytes are still in the
# process, for zero Bedrock calls. Nothing downstream can substitute for this:
# the Bedrock guardrail is itself a network call and covers only six entity
# types, and a model asked "does this file contain secrets?" has already been
# shown the secret.
#   action: block -> BLOCKED. warn -> a finding only, and the file IS then sent.
#   entropy      -> catches base64-shaped credentials with no vendor prefix.
#   allow        -> regexes for literals the team has cleared (test vectors,
#                   sample tokens); a matching line is dropped from findings.
secret_scan:
  enabled: true
  action: block
  entropy:
    enabled: true
    min_length: 20
    min_bits: 4.0
  allow: []

# ─── Optional qualitative pre-flight ─────────────────────────────────────────
# OFF: sending source to a model to look for secrets is the disclosure a secret
# policy forbids, and every other question the check used to ask is now answered
# locally. Enable only for the migration-safety questions in guardrails_pre, and
# only if your policy permits source to reach a model before the gate clears it.
preflight_model_check: false

# ─── Cost model (drives estimated_cost_usd + the FORGE-CostSpike alarm) ──────
# USD per 1,000 tokens. Update when Bedrock pricing changes — no code change needed.
# The \`us.\` cross-region profiles bill at AWS's regional rate, 10% over the
# global list price -- these are the regional numbers (AWS Pricing API, us-east-1).
model_pricing:
  "us.anthropic.claude-opus-4-8":
    input_per_1k: 0.0055
    output_per_1k: 0.0275
  "us.anthropic.claude-sonnet-5":
    input_per_1k: 0.0022
    output_per_1k: 0.011
  "us.amazon.nova-pro-v1:0":
    input_per_1k: 0.0008
    output_per_1k: 0.0032
  "us.anthropic.claude-haiku-4-5-20251001-v1:0":
    input_per_1k: 0.0011
    output_per_1k: 0.0055

# Publish pipeline counters to the FORGE/Migration namespace. The Terraform
# alarms and dashboard read these; turning it off leaves them blind.
emit_cloudwatch_metrics: true

# ─── Build verification ───────────────────────────────────────────────────────
# A file can score 95 and still not compile. When enabled, each written file is
# compiled and a failure is fed back to the transform agent as review feedback.
#   mode: javac   — fast syntax/symbol check of the single file
#         maven   — mvn -q compile at the output project root (needs a pom.xml)
#         command — run \`command\` verbatim; {file} and {output_dir} are substituted
build_verification:
  enabled: false
  mode: "javac"
  command: ""
  classpath: ""
  timeout_seconds: 300

# ─── Test generation ──────────────────────────────────────────────────────────
# After a migration, write the JUnit 5 tests the legacy code never had:
#   migrate.py ./app --phase javax-to-jakarta --output-dir ./migrated --generate-tests
#   migrate.py ./app --generate-tests-only --output-dir ./migrated     # over an existing run
# Two model calls per class (generate + review, cross-validated like the
# migration), and none at all for a class this file's rules exclude.
#   model / review_model  — default to transform_model / review_model
#   overwrite             — regenerate over an existing test. Leave false: an
#                           existing test is a human's work.
#   kinds                 — restrict to some of controller/service/repository/
#                           entity/config/plain. Empty means every kind.
#   run_tests             — execute each generated test against the merged tree.
#                           A test that fails is taken back out of the tree and
#                           held, with its output, in the report. Needs the
#                           toolchain, so it is off by default.
test_generation:
  enabled: true
  style: "junit5"
  model: ""
  review_model: ""
  pass_threshold: 75
  retry_threshold: 50
  max_retries: 1
  overwrite: false
  max_source_chars: 60000
  context_max_chars: 12000
  kinds: []
  run_tests:
    enabled: false
    mode: "maven"          # maven | gradle | command
    command: ""            # {test_class} {test_fqcn} {workspace} {test_file}
    timeout_seconds: 900

# ─── Decisions ────────────────────────────────────────────────────────────────
# Project-level choices that packs read (see prompts/FORGE-Platform-Requirements.md
# §4). An acceptance check guarded by \`when:\` is skipped — never passed — while
# the decision it names is unset.
decisions:
  web_framework: modernize-in-place     # modernize-in-place | migrate-to-spring
  runtime: war-xml-bootstrap            # war-xml-bootstrap | war-programmatic-bootstrap
  container: liberty                    # liberty | wildfly | tomcat | jetty
  views: in-place                       # in-place | thymeleaf | defer
  url_compat: preserve-with-redirect
  # auto: a file the reviewer passes (score >= pass_threshold) is written with no
  # human click; a file it does not pass still waits in the review queue.
  risk_ceiling: auto

# ─── Risk ─────────────────────────────────────────────────────────────────────
# Every unit is scored deterministically before any model call (LOC, descriptor
# fan-out, security constraints, Spring-proxied actions, OGNL density, Unsafe).
# The score sets a tier; \`decisions.risk_ceiling\` decides what the tier means:
#   auto         nothing is held for a human
#   review-high  HIGH-tier units are staged and held until approved   (default)
#   review-all   every unit is held
risk:
  high_at: 60
  medium_at: 30

# ─── Context extraction ──────────────────────────────────────────────────────
# A pack that declares \`context:\` gets the extracted descriptor set (web.xml,
# vendor descriptors, datasources, ...) appended to its transform and review
# prompts, rendered section by section up to this many characters. Sections
# that do not fit are listed as omitted; the full context is written to
# migration-context.json in the output directory.
context:
  max_chars: 60000

# ─── LangSmith observability ─────────────────────────────────────────────────
langsmith_project: "forge-migration"
EOF

if [[ -n "$OUT" ]]; then
  exec 1>&3 3>&-
  mv "$TMP_OUT" "$OUT"
  trap - EXIT
  echo "==> agents.yaml written to ${OUT}" >&2
else
  echo "==> agents.yaml written to stdout." >&2
  echo "    Prefer --out PATH: a shell redirect empties the target before this script runs." >&2
fi
