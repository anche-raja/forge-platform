# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

FORGE is an AI-powered Java migration pipeline. It uses LangGraph + AWS Bedrock (Claude Sonnet for transformation, Amazon Nova Pro for review) to upgrade Java codebases — migrating `javax.*` → `jakarta.*`, modernising deprecated APIs, and upgrading Spring versions. The pipeline runs file-by-file, tracks state in DynamoDB, and evaluates every file through Bedrock Guardrails before and after transformation.

The repo currently contains:
- `forge-terraform/` — all AWS infrastructure as Terraform modules
- `prompts/` — full specifications for each build phase
- `forge-mvp/` — Python pipeline (not yet built; spec is in `prompts/FORGE-Phase0-MVP.md`)

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
# MVP (Phase 0) — deploy these two only
terraform apply -target=module.foundation
terraform apply -target=module.observability

# Phase 6 — add when RAG and manual review queue are needed
terraform apply -target=module.sqs
terraform apply -target=module.rag

# Future — only when a trained model artifact is in S3
# Set enable_sagemaker = true in terraform.tfvars first
terraform apply -target=module.sagemaker
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
| `sqs` | Manual review queue + DLQ | Phase 6 |
| `rag` | S3 bucket, OpenSearch Serverless, Bedrock Knowledge Base | Phase 6 |
| `sagemaker` | TGI endpoint, SSM parameter | Future only |

### Architecture decisions baked into the Terraform

**Two providers in the `rag` module.** The `awscc` provider is required for `awscc_opensearchserverless_collection` — the standard `aws` provider does not support it. The root `main.tf` passes both providers explicitly to the `rag` module via `providers = { aws = aws, awscc = awscc }`. Any future change to the rag module that adds awscc resources must keep this in place.

**Backend variables are literals.** Terraform does not allow variable interpolation inside `backend {}` blocks. The bucket name in `backend.tf` is a placeholder — always pass the real values via `-backend-config` flags at `terraform init` time. Do not attempt to use `var.*` inside the backend block.

**`try()` for optional module outputs.** The root `outputs.tf` wraps `sagemaker`, `sqs`, and `rag` outputs in `try(..., null)`. This prevents index-out-of-range errors when `count = 0` modules are not deployed.

**Conditional SageMaker.** `count = var.enable_sagemaker ? 1 : 0` is on the module call in root `main.tf`, not on individual resources inside the sagemaker module. All resources inside the module are unconditional — the gate is purely at the root level.

**OpenSearch Serverless timing.** The collection takes 5–10 minutes to become ACTIVE after creation. If `terraform apply -target=module.rag` fails with "collection not active", wait and re-run. Do not add sleep provisioners — just re-run.

**IAM execution role trust policy** includes `data.aws_caller_identity.current.arn` so the developer/CI identity that runs Terraform can also assume the role via `aws sts assume-role` for local development. No long-lived access keys needed.

### Cost profile
- MVP only (foundation + observability): ~$5/mo idle, ~$20–40/mo during active migration
- Adding `rag` module: +~$175/mo (OpenSearch Serverless minimum, always-on)
- Adding `sagemaker`: +~$1,093/mo for ml.g5.2xlarge always-on — stop endpoint when not in use

## FORGE pipeline — forge-mvp/ (not yet built)

Full spec in `prompts/FORGE-Phase0-MVP.md`. Key design points:

- **LangGraph graph** with nodes: `guardrails_pre → java_upgrade → java_reviewer → guardrails_post → write_file → update_state`
- **Retry loop**: reviewer score 50–79 routes back to `java_upgrade` with feedback injected into prompt; max 2 retries
- **Bedrock Guardrails** called as a standalone `ApplyGuardrail` API call — not inline with model invocation. Used both pre (INPUT) and post (OUTPUT).
- **DynamoDB checkpointer**: LangGraph uses `DynamoDBSaver` with `thread_id = file_path`
- **Two separate models**: Claude Sonnet 4.5 for transformation, Amazon Nova Pro for review — intentional cross-validation
- **agents.yaml** is the single config file read at startup — all AWS resource IDs, model IDs, thresholds come from there. Generated by `scripts/generate-agents-yaml.sh` after Terraform apply.

## Specs
- `prompts/FORGE-Infra-Terraform.md` — full infrastructure specification
- `prompts/FORGE-Phase0-MVP.md` — Phase 0 Python pipeline specification
