# FORGE — Infrastructure as Code
# Terraform — AWS provider
# Provision everything FORGE needs before running any phase

## Goal
Build all AWS infrastructure FORGE requires across all 6 phases using Terraform.
Infrastructure is split into modules — deploy only what each phase needs.
Each module outputs the values (ARNs, URLs, table names, IDs) that feed directly into FORGE agents.yaml and .env files.

## How to use this prompt
Send this to Claude Code. It will scaffold the full Terraform project.
Then deploy phase by phase — only deploy the module for the phase you are about to run.

## Deployment sequence
terraform apply -target=module.foundation    # before Phase 0 MVP
terraform apply -target=module.observability # before Phase 0 MVP
terraform apply -target=module.sqs          # before Phase 6
terraform apply -target=module.rag          # before Phase 6
terraform apply -target=module.sagemaker    # future — internal LLM only

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
    rag/                    # S3 bucket + Bedrock Knowledge Base (Phase 6)
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

environment         string   — dev, staging, prod
aws_region          string   — default us-east-1
aws_account_id      string   — your AWS account ID
app_name            string   — default "forge"
team_name           string   — your team name for tagging
scope_package_prefix string  — Java package prefix for scope validation (e.g. com.corp)
target_java_version string   — default "21"
target_spring_version string — default "6"

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

### DynamoDB — Migration Manifest Table
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
  PASSWORD — action ANONYMIZE
  IP_ADDRESS — action ANONYMIZE
  EMAIL — action ANONYMIZE
  US_BANK_ACCOUNT_NUMBER — action BLOCK

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
  "you are now"
  These block the most common prompt injection patterns that could appear in malicious source code comments.

Description: "FORGE migration pipeline guardrail — blocks secrets and prompt injection in source code"

Create a guardrail version resource: aws_bedrock_guardrail_version pointing at the guardrail. Output the guardrail_id and version.

### IAM — FORGE Execution Role
Resource: aws_iam_role
Name: forge-execution-role-{environment}

Trust policy: allow EC2, ECS, and the current user/role to assume this role.
This is the role FORGE runs as — attach it to your EC2 instance or ECS task or use it with aws sts assume-role for local development.

Inline policies:

Bedrock policy:
  bedrock:InvokeModel — on all Bedrock models in the region
  bedrock:ApplyGuardrail — on the guardrail ARN created above
  bedrock:Retrieve — on the knowledge base ARN (output from rag module, use data source if module not deployed yet)

DynamoDB policy:
  dynamodb:PutItem, GetItem, UpdateItem, DeleteItem, Query, Scan — on all three DynamoDB table ARNs
  dynamodb:DescribeTable — on all three DynamoDB table ARNs

CloudWatch policy:
  cloudwatch:PutMetricData — resource *
  logs:CreateLogGroup, CreateLogStream, PutLogEvents — resource *

SQS policy (conditional — only if sqs module is deployed):
  sqs:SendMessage, ReceiveMessage, DeleteMessage, GetQueueAttributes — on the SQS queue ARN

S3 policy (conditional — only if rag module is deployed):
  s3:GetObject, PutObject, ListBucket — on the RAG S3 bucket ARN

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

## MODULE 4 — rag
Path: modules/rag/
Deploy before: Phase 6

### S3 Bucket — Knowledge Base Documents
Resource: aws_s3_bucket
Name: {app_name}-knowledge-base-{account_id}-{environment}
Versioning: enabled
Server side encryption: AES256
Block all public access: true

Lifecycle rule: transition objects older than 90 days to S3 Intelligent-Tiering

### S3 Bucket Policy
Allow the Bedrock service principal to GetObject from this bucket.
Allow the FORGE execution role to PutObject, GetObject, DeleteObject.

### S3 Bucket Objects — Seed Documents
Resource: aws_s3_object
Upload these placeholder files from a local docs/ directory:
  docs/coding_standards.md → s3://{bucket}/coding_standards.md
  docs/spring_migration_patterns.md → s3://{bucket}/spring_migration_patterns.md
  docs/struts2_to_mvc_rules.md → s3://{bucket}/struts2_to_mvc_rules.md
  docs/arch_decisions.md → s3://{bucket}/arch_decisions.md
  docs/liberty_config_standards.md → s3://{bucket}/liberty_config_standards.md

Create a docs/ directory with placeholder markdown files. Each file has a header comment explaining what the team should fill in.

### IAM Role — Bedrock Knowledge Base Service Role
Resource: aws_iam_role
Name: forge-bedrock-kb-role-{environment}
Trust policy: allow bedrock.amazonaws.com to assume this role

Permissions:
  s3:GetObject, ListBucket on the knowledge base S3 bucket
  bedrock:InvokeModel on the embedding model (amazon.titan-embed-text-v2:0)

### Bedrock Knowledge Base
Resource: aws_bedrockagent_knowledge_base
Name: forge-knowledge-base-{environment}
Role ARN: the KB service role above
Embedding model ARN: arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0

Knowledge base configuration:
  type: VECTOR
  vector knowledge base configuration:
    embedding model ARN: amazon.titan-embed-text-v2:0

Storage configuration:
  type: OPENSEARCH_SERVERLESS
  This requires an OpenSearch Serverless collection — create it:

### OpenSearch Serverless Collection
Resource: awscc_opensearchserverless_collection
Name: forge-kb-{environment}
Type: VECTORSEARCH

Security policies required for OpenSearch Serverless:
  aws_opensearchserverless_security_policy — encryption: AWS managed key, resource pattern: collection/forge-kb-{environment}
  aws_opensearchserverless_security_policy — network: allow public access (or VPC policy if private)
  aws_opensearchserverless_access_policy — allow the KB role to: aoss:CreateIndex, DeleteIndex, UpdateIndex, DescribeIndex, ReadDocument, WriteDocument — on collection/forge-kb-{environment} and index/forge-kb-{environment}/*

### Bedrock Knowledge Base Data Source
Resource: aws_bedrockagent_data_source
Name: forge-s3-docs-{environment}
Knowledge base ID: from the knowledge base above
Data source configuration:
  type: S3
  S3 bucket ARN: the knowledge base bucket
  Inclusion prefixes: none (include all files)
Chunking strategy: FIXED_SIZE, max tokens 512, overlap 50 tokens

### outputs.tf — rag module
Output:
  knowledge_base_id
  knowledge_base_arn
  s3_bucket_name
  s3_bucket_arn
  opensearch_collection_endpoint

---

## MODULE 5 — sagemaker (future — deploy only when internal LLM is ready)
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

module "sqs" {
  source      = "./modules/sqs"
  environment = var.environment
  app_name    = var.app_name
  execution_role_arn = module.foundation.execution_role_arn
}

module "rag" {
  source      = "./modules/rag"
  environment = var.environment
  app_name    = var.app_name
  aws_account_id = var.aws_account_id
  aws_region  = var.aws_region
  execution_role_arn = module.foundation.execution_role_arn
}

module "sagemaker" {
  source      = "./modules/sagemaker"
  count       = var.enable_sagemaker ? 1 : 0
  environment = var.environment
  app_name    = var.app_name
}

---

## outputs.tf — root module

Output every value FORGE needs, grouped by which agents.yaml field they map to:

group "AGENTS_YAML — paste these into agents.yaml":
  aws_region                    = var.aws_region
  dynamodb_table                = module.foundation.dynamodb_state_table_name
  dynamodb_checkpoint_table     = module.foundation.dynamodb_checkpoint_table_name
  dynamodb_manifest_table       = module.foundation.dynamodb_manifest_table_name
  guardrail_id                  = module.foundation.guardrail_id
  guardrail_version             = module.foundation.guardrail_version
  cloudwatch_namespace          = "FORGE/Migration"
  cloudwatch_log_group          = module.observability.cloudwatch_log_group_name
  sqs_queue_url                 = module.sqs.queue_url (null if sqs not deployed)
  knowledge_base_id             = module.rag.knowledge_base_id (null if rag not deployed)
  sagemaker_endpoint_name       = module.sagemaker[0].endpoint_name (null if not deployed)

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
variable "scope_package_prefix" description "Java package prefix for scope validation e.g. com.corp"
variable "enable_sagemaker"     default false
variable "target_java_version"  default "21"
variable "target_spring_version" default "6"

---

## terraform.tfvars.example — committed to repo

environment          = "dev"
aws_region           = "us-east-1"
aws_account_id       = "123456789012"
app_name             = "forge"
team_name            = "platform-engineering"
alerts_email         = "your-team@corp.com"
scope_package_prefix = "com.corp"
enable_sagemaker     = false

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
  ./scripts/generate-agents-yaml.sh dev > ../forge-mvp/agents.yaml

Script logic:
1. Run terraform output -json to get all values
2. Use jq to extract each field
3. Write agents.yaml with all values filled in

---

## docs/ — Knowledge Base seed documents

Create these placeholder markdown files in docs/. Each has a header explaining what content the team should add. They are uploaded to S3 by the rag module.

docs/coding_standards.md:
  Header: "# Enterprise Java Coding Standards"
  Placeholder sections: package naming conventions, class naming rules, method naming rules, annotation usage standards, logging standards, exception handling patterns
  Note: "Fill in your enterprise standards here. FORGE transform agents will use these to generate code that matches your conventions."

docs/spring_migration_patterns.md:
  Header: "# Approved Spring MVC Migration Patterns"
  Placeholder sections: approved @Controller patterns, approved security config patterns, approved data access patterns, approved exception handling, approved validation patterns

docs/struts2_to_mvc_rules.md:
  Header: "# Struts 2 to Spring MVC Migration Rules — Project Specific"
  Placeholder sections: known edge cases in this codebase, custom interceptors that exist and how they should map, custom result types, known OGNL patterns and their Spring equivalents

docs/arch_decisions.md:
  Header: "# Architecture Decisions (ADRs)"
  Placeholder: paste your ADRs here, particularly those relevant to the target architecture

docs/liberty_config_standards.md:
  Header: "# Open Liberty Configuration Standards"
  Placeholder sections: approved feature sets per application type, datasource configuration patterns, ECS resource allocation guidelines

---

## Acceptance criteria — infrastructure is ready when

1. bash scripts/bootstrap-state.sh completes and prints "State backend ready"
2. terraform init succeeds with the S3 backend
3. terraform plan -target=module.foundation shows no errors and plans 8-12 resources
4. terraform apply -target=module.foundation completes and outputs guardrail_id, dynamodb table names, execution_role_arn
5. terraform plan -target=module.observability shows CloudWatch dashboard and 4 alarms
6. terraform apply -target=module.observability completes
7. ./scripts/generate-agents-yaml.sh dev produces a valid agents.yaml with all values filled in
8. Pasting that agents.yaml into the FORGE MVP directory: python migrate.py ./myapp --phase java21 --file any_file.java runs without configuration errors

---

## Deployment order summary

| Step | Command | Before which FORGE phase |
|---|---|---|
| 1 | bash scripts/bootstrap-state.sh | Once, before anything |
| 2 | terraform init | Once |
| 3 | terraform apply -target=module.foundation | Phase 0 MVP |
| 4 | terraform apply -target=module.observability | Phase 0 MVP |
| 5 | terraform apply -target=module.sqs | Phase 6 |
| 6 | terraform apply -target=module.rag | Phase 6 |
| 7 | terraform apply -target=module.sagemaker | Future — internal LLM |

---

## Cost estimate (us-east-1, dev environment, idle)

DynamoDB (3 tables, PAY_PER_REQUEST): ~$0/month at rest, ~$1-5/month during active migration
Bedrock Guardrails: charged per API call — ~$0.01 per 1000 text units
CloudWatch (dashboard + alarms): ~$3/month for 1 dashboard + 4 alarms + log group
SQS (when deployed): ~$0/month at low volume (first 1M requests free)
OpenSearch Serverless (when deployed): ~$0.24/OCU/hour — minimum 2 OCU = ~$175/month
  Note: OpenSearch Serverless has a minimum cost. If RAG is not urgent, delay this module.
SageMaker endpoint (when deployed): ml.g5.2xlarge ~$1.41/hour — stop endpoint when not in use

Total before Phase 6: < $10/month

---

## Notes for Claude Code

1. Use the awscc provider for OpenSearch Serverless resources — the standard aws provider does not support all OpenSearch Serverless resource types.

2. The Bedrock Knowledge Base resource (aws_bedrockagent_knowledge_base) requires the awscc provider or the aws provider version >= 5.31.0.

3. For the Bedrock Guardrails resource, check the current AWS provider version for aws_bedrock_guardrail support — it was added in provider version 5.26.0.

4. The sagemaker module should only be applied when the team has a trained model artifact in S3. The count = var.enable_sagemaker ? 1 : 0 pattern ensures it is never accidentally deployed.

5. All sensitive outputs (role ARNs, queue URLs) should be marked sensitive = true in outputs.tf so they do not print to console during terraform apply.

6. Add a locals.tf to each module that computes the resource name suffix: locals { suffix = "${var.app_name}-${var.environment}" } — use this consistently across all resource names.

7. The OpenSearch Serverless collection takes 5-10 minutes to become active after creation. Add a depends_on from the Bedrock Knowledge Base resource to the collection, and add a note in the README that the first apply may need to be run twice if the collection is not ready.
