# forge-terraform

The AWS infrastructure FORGE runs on. One `terraform apply` creates everything the migration
pipeline needs; one script turns the outputs into the `agents.yaml` that `forge-mvp` reads at
startup.

Nothing in `forge-mvp` works without this — the pipeline calls Bedrock Guardrails on every file and
keeps its state in DynamoDB, so both must exist before the first run.

---

## What gets deployed

| Module | Resources | When |
|---|---|---|
| `foundation` | 2 DynamoDB tables (migration state, LangGraph checkpoints), a Bedrock Guardrail + published version, an IAM execution role with Bedrock/DynamoDB/CloudWatch policies, an instance profile | Always |
| `observability` | CloudWatch log group, dashboard, 4 metric alarms, SNS topic + email subscription | Always |
| `sqs` | Manual-review queue + DLQ | Only when `enable_sqs = true` |
| `sagemaker` | TGI model, endpoint config, endpoint, SSM parameter | Only when `enable_sagemaker = true` |

**Cost:** roughly **$5/month idle**, $20–40/month during an active migration.
`enable_sagemaker` adds **~$1,093/month** — it provisions one always-on `ml.g5.2xlarge`. Leave it
off unless a trained model artifact is already in S3, and stop the endpoint when it is idle.

---

## Prerequisites

- **Terraform >= 1.6.0.** The AWS provider is pinned `>= 5.31.0, < 7.0.0`.
- **AWS credentials that work.** Check with `aws sts get-caller-identity` — it must print an
  Account and Arn. Everything below fails without this, usually in a way that looks like a
  different problem.
- **`jq`**, used by `scripts/generate-agents-yaml.sh` to read terraform outputs.
- **Bedrock model access**, enabled in the console under *Bedrock → Model access*, for
  **`us.anthropic.claude-opus-4-8`** and **Amazon Nova Pro**. Do this *before* applying. Skipping
  it lets `terraform apply` succeed and makes the **first migration run** fail with `AccessDenied`,
  which is a much more confusing place to find out.

---

## First-time deploy

### 1. Create the state backend

Terraform keeps its state in S3 with a DynamoDB lock table. Those two have to exist before
`terraform init`, so they are created by a script rather than by Terraform itself:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
bash scripts/bootstrap-state.sh "$ACCOUNT_ID"          # optional 2nd arg: region, default us-east-1
```

Creates the bucket `forge-terraform-state-<account_id>` (versioned, AES256-encrypted, public access
blocked) and the DynamoDB table `forge-terraform-lock`. It is **idempotent** — re-running it on an
existing backend is safe.

### 2. Init — the `-backend-config` flags are mandatory

Terraform does not allow variables inside a `backend` block, so the values in `backend.tf` are
**placeholders**. They are overridden at init time, and a bare `terraform init` will try to use the
literal string `forge-terraform-state-ACCOUNT_ID` and fail:

```bash
terraform init \
  -backend-config="bucket=forge-terraform-state-${ACCOUNT_ID}" \
  -backend-config="key=forge/dev/terraform.tfstate" \
  -backend-config="region=us-east-1"
```

The `key` is what actually selects the environment, and it is fixed from this point on. See
[Switching environments](#switching-environments).

### 3. Set the variables

```bash
cp terraform.tfvars.example terraform.tfvars
```

`terraform.tfvars` is gitignored — it holds your account ID and an email address. Keep it that way.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `aws_account_id` | **yes** | — | Used in bucket names and Bedrock ARN construction |
| `alerts_email` | **yes** | — | SNS alarm target; **needs manual confirmation**, see below |
| `environment` | no | `dev` | One of `dev`, `staging`, `prod` (validated) |
| `aws_region` | no | `us-east-1` | |
| `app_name` | no | `forge` | Prefix on every resource name |
| `team_name` | no | `platform` | Applied as a tag |
| `enable_sqs` | no | `false` | Phase 6 |
| `enable_sagemaker` | no | `false` | Future only — see the cost warning above |

### 4. Apply

```bash
terraform plan      # read it before you apply
terraform apply
```

Then **confirm the SNS subscription email**. AWS sends a confirmation link to `alerts_email`; until
someone clicks it the subscription stays `PendingConfirmation` and every alarm is silent.

### 5. Generate `agents.yaml`

```bash
./scripts/generate-agents-yaml.sh dev --out ../forge-mvp/agents.yaml
```

Prefer `--out` over a `>` redirect. A redirect truncates its target *before* the script runs, so a
failure leaves an empty `agents.yaml` behind — which is worse than no file, because it looks
present to everything downstream. `--out` writes to a temp file and moves it into place only on
success, leaving any existing config untouched if the script fails.

Verify it:

```bash
wc -c ../forge-mvp/agents.yaml                 # must be non-zero
grep REPLACE_WITH ../forge-mvp/agents.yaml     # must find nothing
cd ../forge-mvp && python -m pytest            # the suite needs no AWS
```

An empty or placeholder `agents.yaml` no longer fails obscurely: `ForgeConfig` raises a
`ConfigError` naming the cause, and the web UI returns it as a 400 rather than a 500.

---

## Day-two operations

### Re-applying

`terraform apply` is the normal way to change anything. Re-run
`generate-agents-yaml.sh ... --out ...` afterwards — several outputs feed `agents.yaml`, and a
stale config points the pipeline at the wrong resources.

### After any guardrail edit

The guardrail version is **republished automatically** on every guardrail change —
`aws_bedrock_guardrail_version` carries `replace_triggered_by = [aws_bedrock_guardrail.forge]`,
because a guardrail edit that is never published silently keeps serving the old policy.
`agents.yaml` pins the version number, so **regenerate it after every apply that touches the
guardrail** or the pipeline runs against the previous version.

### Switching environments

The environment is decided by the backend `key` you passed at `init`, **not** by the
`environment` variable and **not** by the argument to `generate-agents-yaml.sh`. To switch, re-init:

```bash
terraform init -reconfigure \
  -backend-config="bucket=forge-terraform-state-${ACCOUNT_ID}" \
  -backend-config="key=forge/staging/terraform.tfstate" \
  -backend-config="region=us-east-1"
```

`generate-agents-yaml.sh` cross-checks its label against the initialised backend key and **refuses**
a mismatch, so `... prod` against a dev backend stops rather than quietly producing a dev config
with a prod header.

### Tearing down

```bash
terraform destroy
```

Deletes the pipeline resources. It does **not** touch the state bucket or lock table — those were
created outside Terraform by `bootstrap-state.sh` and must be removed by hand if you want them
gone. The state bucket is versioned, so emptying it means deleting object versions, not just
objects.

---

## Troubleshooting

**`Failed to get existing workspaces: S3 bucket "forge-terraform-state-ACCOUNT_ID" does not exist`**

You ran `terraform init` without the `-backend-config` flags, so it used the literal placeholder.
Re-run step 2 with the flags, adding `-reconfigure` since the failed init already recorded a
backend.

**`Unsupported argument ... sse_algorithm`**

An old checkout. `sse_algorithm` is an argument of the
`aws_s3_bucket_server_side_encryption_configuration` *resource*, not of the S3 backend block;
`encrypt = true` already requests AES256. Delete the line.

**`The parameter "dynamodb_table" is deprecated. Use "use_lockfile" instead`**

Harmless. DynamoDB-based locking still works; `use_lockfile = true` (S3 conditional writes) is the
modern replacement and would make the lock table unnecessary. Changing it is a deliberate decision
about locking behaviour, not a fix.

**`hash_key is deprecated. Use key_schema instead`**

Expected under AWS provider 6.x, and warning-only. The pin `< 7.0.0` in `providers.tf` is there
because 7.x is untested.

**`InvalidClientTokenId` / `InvalidToken` / `UnrecognizedClientException`**

Credentials are absent or expired. Confirm with `aws sts get-caller-identity` **in the same shell**
you run Terraform from — an exported `AWS_ACCESS_KEY_ID`, a different `AWS_PROFILE`, or an SSO
session can make credentials work in one terminal and not another.

**`Backend initialization required, please run "terraform init"`**

The backend configuration changed since the last init. Re-run step 2 with `-reconfigure`.

**First migration run fails with `AccessDenied` on Bedrock**

Model access was never enabled, or the IAM grant does not cover inference profiles. `agents.yaml`
names cross-region profiles (`us.anthropic....`), which need `bedrock:InvokeModel` on the account's
`inference-profile/*` **and** on `foundation-model/*` in every region the profile can route to. An
in-region `foundation-model/*` grant alone is not enough.

---

## Layout

```
forge-terraform/
├── backend.tf                      S3 backend — values are placeholders, see step 2
├── main.tf                         Module wiring; count = 0 gates the optional modules
├── variables.tf                    Inputs, with validation on `environment`
├── outputs.tf                      Grouped: agents.yaml values, then .env values
├── providers.tf                    Version pins and default tags
├── terraform.tfvars.example        Copy to terraform.tfvars (gitignored)
├── modules/
│   ├── foundation/                 DynamoDB, Bedrock Guardrail, IAM
│   ├── observability/              Logs, dashboard, alarms, SNS
│   ├── sqs/                        Phase 6, opt-in
│   └── sagemaker/                  Future, opt-in, expensive
└── scripts/
    ├── bootstrap-state.sh          Creates the S3/DynamoDB state backend (idempotent)
    └── generate-agents-yaml.sh     terraform output → forge-mvp/agents.yaml
```

`SPEC.md` in this directory is the build prompt the Terraform was written from. If the code and the
spec disagree, the code is what runs.
