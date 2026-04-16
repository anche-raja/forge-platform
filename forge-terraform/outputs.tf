# ─── AGENTS_YAML — paste these values into agents.yaml ───────────────────────

output "aws_region" {
  description = "AWS region — agents.yaml: aws_region"
  value       = var.aws_region
}

output "dynamodb_state_table" {
  description = "Migration state DynamoDB table name — agents.yaml: dynamodb_table"
  value       = module.foundation.dynamodb_state_table_name
}

output "dynamodb_checkpoint_table" {
  description = "LangGraph checkpoint DynamoDB table name — agents.yaml: dynamodb_checkpoint_table"
  value       = module.foundation.dynamodb_checkpoint_table_name
}

output "guardrail_id" {
  description = "Bedrock Guardrail ID — agents.yaml: guardrail_id"
  value       = module.foundation.guardrail_id
}

output "guardrail_version" {
  description = "Bedrock Guardrail version — agents.yaml: guardrail_version"
  value       = module.foundation.guardrail_version
}

output "cloudwatch_namespace" {
  description = "CloudWatch metrics namespace — agents.yaml: cloudwatch_namespace"
  value       = "FORGE/Migration"
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group name — agents.yaml: cloudwatch_log_group"
  value       = module.observability.cloudwatch_log_group_name
}

output "sqs_queue_url" {
  description = "Manual review SQS queue URL — agents.yaml: sqs_queue_url (null if sqs not deployed)"
  value       = try(module.sqs.queue_url, null)
  sensitive   = true
}

output "knowledge_base_id" {
  description = "Bedrock Knowledge Base ID — agents.yaml: knowledge_base_id (null if rag not deployed)"
  value       = try(module.rag.knowledge_base_id, null)
}

output "sagemaker_endpoint_name" {
  description = "SageMaker endpoint name — agents.yaml: sagemaker_endpoint_name (null if not deployed)"
  value       = try(module.sagemaker[0].endpoint_name, null)
}

# ─── ENV FILE — paste these values into .env ──────────────────────────────────

output "execution_role_arn" {
  description = "FORGE execution IAM role ARN — .env: FORGE_EXECUTION_ROLE_ARN"
  value       = module.foundation.execution_role_arn
  sensitive   = true
}

output "sns_topic_arn" {
  description = "CloudWatch alerts SNS topic ARN — .env: FORGE_SNS_TOPIC_ARN"
  value       = module.observability.sns_topic_arn
  sensitive   = true
}

output "sqs_queue_arn" {
  description = "Manual review SQS queue ARN — .env: FORGE_SQS_QUEUE_ARN"
  value       = try(module.sqs.queue_arn, null)
  sensitive   = true
}

output "s3_knowledge_base_bucket" {
  description = "Knowledge Base S3 bucket name — .env: FORGE_KB_BUCKET"
  value       = try(module.rag.s3_bucket_name, null)
}
