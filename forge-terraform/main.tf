# Foundation (DynamoDB tables, Bedrock Guardrail, IAM role). Gated so a state
# where these already exist out-of-band can disable management of them.
module "foundation" {
  source = "./modules/foundation"
  count  = var.enable_foundation ? 1 : 0

  environment    = var.environment
  aws_region     = var.aws_region
  app_name       = var.app_name
  aws_account_id = var.aws_account_id
}

locals {
  # Principal granted access to the SQS / RAG resources. Prefer the explicit
  # override (used when the foundation IAM role lives out-of-band); otherwise
  # fall back to the Terraform-managed execution role.
  execution_principal_arn = var.sqs_access_principal_arn != "" ? var.sqs_access_principal_arn : one(module.foundation[*].execution_role_arn)
}

module "observability" {
  source = "./modules/observability"

  environment  = var.environment
  app_name     = var.app_name
  alerts_email = var.alerts_email
  aws_region   = var.aws_region
}

module "sqs" {
  source = "./modules/sqs"

  environment        = var.environment
  app_name           = var.app_name
  execution_role_arn = local.execution_principal_arn
}

# RAG (OpenSearch Serverless + Bedrock KB, ~$175/mo). Gated off by default so a
# bare `terraform apply` never provisions OpenSearch — FORGE uses prompt-stuffing
# RAG by default. Set enable_rag = true to deploy a managed Knowledge Base.
module "rag" {
  source = "./modules/rag"
  count  = var.enable_rag ? 1 : 0

  providers = {
    aws   = aws
    awscc = awscc
  }

  environment        = var.environment
  app_name           = var.app_name
  aws_account_id     = var.aws_account_id
  aws_region         = var.aws_region
  execution_role_arn = local.execution_principal_arn
}

# The sagemaker module is only deployed when enable_sagemaker = true.
# Ensure a trained model artifact exists in S3 before enabling this.
module "sagemaker" {
  source = "./modules/sagemaker"
  count  = var.enable_sagemaker ? 1 : 0

  environment = var.environment
  app_name    = var.app_name
  aws_region  = var.aws_region
}
