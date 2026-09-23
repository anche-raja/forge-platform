# FORGE — Infrastructure as Code
# Terraform — AWS provider
# Provision everything FORGE needs before running any phase

> **Build prompt.** This is what `forge-terraform/` was built from, and it still
> matches it — the same four modules (`foundation`, `observability`, `sqs`,
> `sagemaker`) behind the same `enable_*` flags. Deploying is
> [the root README](../README.md#deployment); the deployed resources reach the
> pipeline through `scripts/generate-agents-yaml.sh` in this directory. If the Terraform and this
> file ever disagree, the Terraform is the truth.

## Goal
Build all AWS infrastructure FORGE requires across all 6 phases using Terraform.
Infrastructure is split into modules — deploy only what each phase needs.
Each module outputs the values (ARNs, URLs, table names, IDs) that feed directly into FORGE agents.yaml and .env files.

## How to use this prompt
This is the specification the `forge-terraform/` project was built from, kept current with
what is implemented. Deploy phase by phase — the Phase 6 and future modules are behind flags,
so a plain apply only creates what Phase 0 needs.

## Deployment sequence
terraform apply                                  # Phase 0: foundation + observability (flags default false)
enable_sqs = true      → terraform apply         # before Phase 6 — manual review queue
enable_sagemaker = true → terraform apply        # future — internal LLM only

After every apply: scripts/generate-agents-yaml.sh {env} --out ../forge-mvp/agents.yaml
(a guardrail change publishes a new version; agents.yaml pins the number).

---

## Project structure

forge-terraform/
  main.tf                   # root module — wires all child modules
  variables.tf              # input variables (env, region, app name, etc.)
  outputs.tf                # all outputs in one place → feeds agents.yaml
  terraform.tfvars          # actual values — gitignored
  terraform.tfvars.example  # template — committed to repo
  backend.tf                # S3 + DynamoDB state backend
  providers.tf              # AWS provider config
  modules/
    foundation/             # DynamoDB + Bedrock Guardrails + IAM roles (Phase 0)
      main.tf
      variables.tf
      outputs.tf
    observability/          # CloudWatch dashboards + alarms + log groups (Phase 0)
      main.tf
      variables.tf
      outputs.tf
    sqs/                    # SQS manual review queue + DLQ (Phase 6)
      main.tf
      variables.tf
      outputs.tf
    sagemaker/              # SageMaker endpoint for internal LLM (future)
      main.tf
      variables.tf
      outputs.tf
  scripts/
    generate-agents-yaml.sh # reads terraform output → writes agents.yaml
    bootstrap-state.sh      # creates S3 bucket + DynamoDB for Terraform state

---

## backend.tf — Terraform state in S3

Use S3 for remote state and DynamoDB for state locking.
The bootstrap-state.sh script creates these before terraform init.

S3 bucket: forge-terraform-state-{account_id}
DynamoDB table: forge-terraform-lock
State file key: forge/{env}/terraform.tfstate
Encryption: SSE-S3
Versioning: enabled

---

## providers.tf

AWS provider. Region from variable. Default tags applied to every resource:
  Project = "FORGE"
  Environment = var.environment
  ManagedBy = "Terraform"
  Team = var.team_name

---

## variables.tf — root module

environment         string   — dev, staging, prod (validated)
aws_region          string   — default us-east-1
aws_account_id      string   — your AWS account ID (no default)
app_name            string   — default "forge"
team_name           string   — your team name for tagging
alerts_email        string   — CloudWatch alarm e-mail (needs SNS confirmation)
enable_sqs          bool     — default false
enable_sagemaker    bool     — default false

The Java package scope (scope_package_prefix) is a pipeline setting, not infrastructure:
generate-agents-yaml.sh reads it from $FORGE_SCOPE_PACKAGE_PREFIX.

---

## MODULE 1 — foundation
Path: modules/foundation/
Deploy before: Phase 0 MVP

### DynamoDB — Migration State Table
Resource: aws_dynamodb_table
Name: {app_name}-migration-state-{environment}
Billing mode: PAY_PER_REQUEST
Hash key: file_path (S)

GSI 1 — status-index:
  Hash key: status (S)
  Range key: phase (S)
  Projection type: ALL

GSI 2 — phase-status-index:
  Hash key: phase (S)
  Range key: status (S)
  Projection type: INCLUDE
  Non key attributes: file_path, review_score, retry_count, updated_at

TTL: attribute name = expires_at

Point in time recovery: enabled
Server side encryption: enabled (AWS managed key)
Stream: disabled

### DynamoDB — LangGraph Checkpoint Table
Resource: aws_dynamodb_table
Name: {app_name}-langgraph-checkpoints-{environment}
Billing mode: PAY_PER_REQUEST
Hash key: thread_id (S)
Range key: checkpoint_id (S)

No GSI needed. No TTL. This is managed entirely by LangGraph's DynamoDB checkpointer.

### DynamoDB — Migration Manifest Table (NOT IMPLEMENTED — Phase 0 uses the two tables above; the manifest is the per-run migration-report.md / manual-review-queue.json on disk)
Resource: aws_dynamodb_table
Name: {app_name}-migration-manifest-{environment}
Billing mode: PAY_PER_REQUEST
Hash key: source_dir (S)

No GSI. Stores the full manifest JSON per application.

### Bedrock Guardrails
Resource: aws_bedrock_guardrail

Name: {app_name}-guardrail-{environment}

Sensitive information policy — enable these entity types:
  AWS_ACCESS_KEY — action BLOCK
  AWS_SECRET_KEY — action BLOCK
  CREDIT_DEBIT_CARD_NUMBER — action BLOCK
  US_SOCIAL_SECURITY_NUMBER — action BLOCK
  PASSWORD — action BLOCK
  US_BANK_ACCOUNT_NUMBER — action BLOCK

Do NOT list EMAIL or IP_ADDRESS (and do not use ANONYMIZE for anything). The pipeline treats
any GUARDRAIL_INTERVENED on INPUT as BLOCKED, and ANONYMIZE is an intervention — an @author
e-mail or a 127.0.0.1 literal would block the file. Only entity types that are genuinely
secrets belong here.

Content policy filters — set threshold HIGH for:
  HATE
  INSULTS
  SEXUAL
  VIOLENCE
  MISCONDUCT
  PROMPT_ATTACK — this prevents prompt injection via malicious source code comments

Word policy — blocked phrases:
  "ignore previous instructions"
  "disregard your system prompt"
  These block the most common prompt injection patterns that could appear in malicious source
  code comments. Only phrases that never occur in application code or UI strings — "you are now"
  was removed because it appears in login pages.

Description: "FORGE migration pipeline guardrail — blocks secrets and prompt injection in source code"

Create a guardrail version resource: aws_bedrock_guardrail_version pointing at the guardrail, with
lifecycle { replace_triggered_by = [aws_bedrock_guardrail.forge] } so every guardrail edit publishes
a new numbered version (the pipeline pins the number). Output the guardrail_id and version.

### IAM — FORGE Execution Role
Resource: aws_iam_role
Name: forge-execution-role-{environment}

Trust policy: allow EC2, ECS, and the current user/role to assume this role.
This is the role FORGE runs as — attach it to your EC2 instance or ECS task or use it with aws sts assume-role for local development.

Inline policies:

Bedrock policy:
  bedrock:InvokeModel, bedrock:InvokeModelWithResponseStream — on
    arn:aws:bedrock:*::foundation-model/*                                 (every region)
    arn:aws:bedrock:{region}:{account}:inference-profile/*                (this account)
  agents.yaml names cross-region inference profiles (us.anthropic.…, us.amazon.nova-pro…);
  invoking one needs the profile ARN AND the foundation model in every region it routes to.
  An in-region foundation-model/* grant alone is AccessDenied.
  bedrock:ApplyGuardrail — on the guardrail ARN created above

DynamoDB policy:
  dynamodb:PutItem, GetItem, UpdateItem, DeleteItem, Query, Scan, DescribeTable — on both table ARNs and their /index/*

CloudWatch policy:
  cloudwatch:PutMetricData — resource *
  logs:CreateLogGroup, CreateLogStream, PutLogEvents — resource *

SQS access is granted by a queue policy inside the sqs module naming the execution role —
same-account, so no identity policy is needed and the foundation module has no dependency on a
module that may not be deployed.

### IAM — Instance Profile (for EC2 local dev)
Resource: aws_iam_instance_profile
Wraps the execution role for use with EC2 instances.
Developers running FORGE on an EC2 instance use this profile — no long-lived access keys needed.

### outputs.tf — foundation module
Output these values:
  dynamodb_state_table_name
  dynamodb_state_table_arn
  dynamodb_checkpoint_table_name
  dynamodb_manifest_table_name
  guardrail_id
  guardrail_version
  execution_role_arn
  execution_role_name

---

## MODULE 2 — observability
Path: modules/observability/
Deploy before: Phase 0 MVP

### CloudWatch Log Group — FORGE application logs
Resource: aws_cloudwatch_log_group
Name: /forge/{environment}/pipeline
Retention: 30 days
KMS encryption: none for dev, aws_kms_key for prod

### CloudWatch Dashboard
Resource: aws_cloudwatch_dashboard
Name: FORGE-Migration-{environment}

Dashboard body JSON with these widgets:

Row 1 — Progress overview (3 metric widgets side by side):
  Files Processed — metric: FORGE/Migration files_processed, stat: Sum, period: 60
  Files Passed — metric: FORGE/Migration files_passed, stat: Sum
  Files Manual — metric: FORGE/Migration files_manual, stat: Sum

Row 2 — Quality metrics (2 widgets):
  Review Score Distribution — metric: FORGE/Migration review_score, stat: Average, period: 300
  Retry Rate — metric: FORGE/Migration files_retried / files_processed, expression widget

Row 3 — Cost and performance (2 widgets):
  Estimated Cost USD — metric: FORGE/Migration estimated_cost_usd, stat: Sum
  Bedrock Calls per Hour — metric: FORGE/Migration bedrock_calls, stat: Sum, period: 3600

Row 4 — Alarms summary:
  Alarm status widget showing all FORGE alarms

### CloudWatch Alarms — 4 alarms

Alarm 1 — High retry rate:
  Metric: FORGE/Migration files_retried
  Period: 300 seconds
  Statistic: Sum
  Threshold: > 30 (more than 30 retries in 5 minutes signals a systemic agent problem)
  Alarm action: SNS topic (create aws_sns_topic forge-alerts-{environment})
  Description: "FORGE retry rate exceeds threshold — check LangSmith for agent errors"

Alarm 2 — High manual escalation rate:
  Metric: FORGE/Migration files_manual
  Period: 600 seconds
  Statistic: Sum
  Threshold: > 20
  Description: "More than 20 files escalated to manual review — complex migration phase in progress"

Alarm 3 — Pipeline stalled:
  Metric: FORGE/Migration files_processed
  Period: 900 seconds
  Statistic: Sum
  Threshold: < 1 (less than 1 file processed in 15 minutes during an active run)
  Treat missing data: notBreaching (only alarm when pipeline is actively running)
  Description: "FORGE pipeline has not processed a file in 15 minutes"

Alarm 4 — Cost spike:
  Metric: FORGE/Migration estimated_cost_usd
  Period: 3600 seconds
  Statistic: Sum
  Threshold: > 50 (more than $50 in one hour)
  Description: "FORGE Bedrock cost exceeds $50/hour — review run configuration"

### SNS Topic for Alarm Notifications
Resource: aws_sns_topic
Name: forge-alerts-{environment}
Create aws_sns_topic_subscription for email — email address from variable alerts_email.

### outputs.tf — observability module
Output:
  cloudwatch_log_group_name
  dashboard_name
  sns_topic_arn
  alarm_high_retry_arn
  alarm_high_manual_arn

---

## MODULE 3 — sqs
Path: modules/sqs/
Deploy before: Phase 6

### SQS Dead-Letter Queue
Resource: aws_sqs_queue
Name: {app_name}-manual-review-dlq-{environment}
Message retention: 14 days (maximum)
KMS encryption: aws_kms_key or SQS managed key

### SQS Main Queue — Manual Review
Resource: aws_sqs_queue
Name: {app_name}-manual-review-{environment}
Visibility timeout: 1800 seconds (30 minutes — gives engineer time to review)
Message retention: 7 days
Receive message wait time: 20 seconds (long polling)
Redrive policy: maxReceiveCount = 3, deadLetterTargetArn = DLQ ARN
KMS encryption: same key as DLQ

### SQS Queue Policy
Resource: aws_sqs_queue_policy
Allow the FORGE execution role to: sqs:SendMessage, ReceiveMessage, DeleteMessage, GetQueueAttributes, ChangeMessageVisibility

### outputs.tf — sqs module
Output:
  queue_url
  queue_arn
  dlq_url
  dlq_arn

---

## MODULE 4 — sagemaker (future — deploy only when internal LLM is ready)
Path: modules/sagemaker/
Deploy when: internal LLM model is ready to host

### SageMaker Model
Resource: aws_sagemaker_model
Name: forge-llm-{environment}
Execution role: a new IAM role with sagemaker:* and s3:GetObject on the model artifact bucket
Primary container:
  Image: use the TGI (Text Generation Inference) DLC image for your region
  Model data URL: s3://{model-bucket}/{model-artifact.tar.gz}
  Environment variables:
    HF_MODEL_ID: your model name or path
    SM_NUM_GPUS: 1
    MAX_INPUT_LENGTH: 8192
    MAX_TOTAL_TOKENS: 16384

### SageMaker Endpoint Config
Resource: aws_sagemaker_endpoint_configuration
Name: forge-llm-config-{environment}
Production variants:
  variant name: AllTraffic
  model name: from above
  instance type: ml.g5.2xlarge (single A10G GPU — good for 7B-13B models)
  initial instance count: 1

### SageMaker Endpoint
Resource: aws_sagemaker_endpoint
Name: forge-llm-{environment}
Endpoint config: from above

### SSM Parameter — Internal LLM API Key
Resource: aws_ssm_parameter
Name: /forge/{environment}/internal-llm-key
Type: SecureString
Value: placeholder — update manually after deployment

### outputs.tf — sagemaker module
Output:
  endpoint_name
  endpoint_arn
  endpoint_url (constructed: https://runtime.sagemaker.{region}.amazonaws.com/endpoints/{name}/invocations)
  ssm_parameter_name

---

## main.tf — root module

Wire all modules. Pass outputs from one module to the next where needed.

module "foundation" {
  source      = "./modules/foundation"
  environment = var.environment
  aws_region  = var.aws_region
  app_name    = var.app_name
}

module "observability" {
  source      = "./modules/observability"
  environment = var.environment
  app_name    = var.app_name
  alerts_email = var.alerts_email
}

# Phase 6 and future modules are opt-in.
module "sqs" {
  source      = "./modules/sqs"
  count       = var.enable_sqs ? 1 : 0
  environment = var.environment
  app_name    = var.app_name
  execution_role_arn = module.foundation.execution_role_arn
}

module "sagemaker" {
  source      = "./modules/sagemaker"
  count       = var.enable_sagemaker ? 1 : 0
  environment = var.environment
  app_name    = var.app_name
  aws_region  = var.aws_region
}

---

## outputs.tf — root module

Output every value FORGE needs, grouped by which agents.yaml field they map to:

group "AGENTS_YAML — paste these into agents.yaml":
  aws_region                    = var.aws_region
  dynamodb_table                = module.foundation.dynamodb_state_table_name
  dynamodb_checkpoint_table     = module.foundation.dynamodb_checkpoint_table_name
  guardrail_id                  = module.foundation.guardrail_id
  guardrail_version             = module.foundation.guardrail_version
  cloudwatch_namespace          = "FORGE/Migration"
  cloudwatch_log_group          = module.observability.cloudwatch_log_group_name
  sqs_queue_url                 = try(module.sqs[0].queue_url, null)
  sagemaker_endpoint_name       = try(module.sagemaker[0].endpoint_name, null)

group "ENV FILE — paste these into .env":
  execution_role_arn            = module.foundation.execution_role_arn
  sns_topic_arn                 = module.observability.sns_topic_arn

---

## variables.tf — root module

variable "environment"          default "dev"
variable "aws_region"           default "us-east-1"
variable "aws_account_id"       description "Your AWS account ID — no default, must be provided"
variable "app_name"             default "forge"
variable "team_name"            default "platform"
variable "alerts_email"         description "Email for CloudWatch alarm notifications"
variable "enable_sqs"           default false
variable "enable_sagemaker"     default false

---

## terraform.tfvars.example — committed to repo

environment          = "dev"
aws_region           = "us-east-1"
aws_account_id       = "123456789012"
app_name             = "forge"
team_name            = "platform-engineering"
alerts_email         = "your-team@corp.com"
enable_sqs           = false   # Phase 6
enable_sagemaker     = false   # future

---

## scripts/bootstrap-state.sh

Shell script that creates the S3 bucket and DynamoDB table for Terraform state before terraform init is run.
Steps:
1. aws s3api create-bucket --bucket forge-terraform-state-{account_id} --region us-east-1 --create-bucket-configuration LocationConstraint=us-east-1
2. aws s3api put-bucket-versioning --bucket forge-terraform-state-{account_id} --versioning-configuration Status=Enabled
3. aws s3api put-bucket-encryption --bucket forge-terraform-state-{account_id} with AES256
4. aws dynamodb create-table --table-name forge-terraform-lock --attribute-definitions AttributeName=LockID,AttributeType=S --key-schema AttributeName=LockID,KeyType=HASH --billing-mode PAY_PER_REQUEST --region us-east-1
5. Print: "State backend ready. Now run: terraform init"

---

## scripts/generate-agents-yaml.sh

Shell script that reads terraform output and writes a ready-to-use agents.yaml.
Run after terraform apply:
  ./scripts/generate-agents-yaml.sh dev --out ../forge-mvp/agents.yaml

Script logic:
1. Run terraform output -json to get all values
2. Use jq to extract each field
3. Write agents.yaml with all values filled in

---

## Acceptance criteria — infrastructure is ready when

1. bash scripts/bootstrap-state.sh completes and prints "State backend ready"
2. terraform init succeeds with the S3 backend
3. terraform plan (flags default false) shows no errors and plans only foundation + observability:
   2 DynamoDB tables, guardrail + version, execution role + 3 inline policies + instance profile,
   log group, SNS topic + subscription, 4 alarms, dashboard
4. terraform apply completes and outputs guardrail_id, guardrail_version, dynamodb table names, execution_role_arn
5. scripts/generate-agents-yaml.sh dev produces an agents.yaml that ForgeConfig loads
6. The execution role can invoke the us.* inference profiles (test: aws bedrock converse via assume-role)
7. ./scripts/generate-agents-yaml.sh dev produces a valid agents.yaml with all values filled in
8. Pasting that agents.yaml into the FORGE MVP directory: python migrate.py ./myapp --phase java21 --file any_file.java runs without configuration errors

---

## Deployment order summary

| Step | Command | Before which FORGE phase |
|---|---|---|
| 1 | bash scripts/bootstrap-state.sh | Once, before anything |
| 2 | terraform init | Once |
| 3 | terraform apply (foundation + observability; flags off) | Phase 0 MVP |
| 4 | scripts/generate-agents-yaml.sh dev --out ../forge-mvp/agents.yaml | Phase 0 MVP |
| 5 | enable_sqs = true → terraform apply | Phase 6 |
| 6 | enable_sagemaker = true → terraform apply | Future — internal LLM |

---

## Cost estimate (us-east-1, dev environment, idle)

DynamoDB (2 tables, PAY_PER_REQUEST): ~$0/month at rest, ~$1-5/month during active migration
Bedrock Guardrails: charged per API call — ~$0.01 per 1000 text units
CloudWatch (dashboard + alarms): ~$3/month for 1 dashboard + 4 alarms + log group
SQS (when deployed): ~$0/month at low volume (first 1M requests free)
SageMaker endpoint (when deployed): ml.g5.2xlarge ~$1.41/hour — stop endpoint when not in use

Total before Phase 6: < $10/month

---

## Notes for Claude Code

1. For the Bedrock Guardrails resource, check the current AWS provider version for aws_bedrock_guardrail support — it was added in provider version 5.26.0.

2. The sagemaker module should only be applied when the team has a trained model artifact in S3. The count = var.enable_sagemaker ? 1 : 0 pattern ensures it is never accidentally deployed.

3. All sensitive outputs (role ARNs, queue URLs) should be marked sensitive = true in outputs.tf so they do not print to console during terraform apply.

4. Add a locals.tf to each module that computes the resource name suffix: locals { suffix = "${var.app_name}-${var.environment}" } — use this consistently across all resource names.
