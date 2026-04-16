output "dynamodb_state_table_name" {
  description = "Migration state DynamoDB table name"
  value       = aws_dynamodb_table.migration_state.name
}

output "dynamodb_state_table_arn" {
  description = "Migration state DynamoDB table ARN"
  value       = aws_dynamodb_table.migration_state.arn
}

output "dynamodb_checkpoint_table_name" {
  description = "LangGraph checkpoint DynamoDB table name"
  value       = aws_dynamodb_table.langgraph_checkpoints.name
}

output "dynamodb_checkpoint_table_arn" {
  description = "LangGraph checkpoint DynamoDB table ARN"
  value       = aws_dynamodb_table.langgraph_checkpoints.arn
}

output "guardrail_id" {
  description = "Bedrock Guardrail ID"
  value       = aws_bedrock_guardrail.forge.guardrail_id
}

output "guardrail_arn" {
  description = "Bedrock Guardrail ARN"
  value       = aws_bedrock_guardrail.forge.guardrail_arn
}

output "guardrail_version" {
  description = "Bedrock Guardrail version number"
  value       = aws_bedrock_guardrail_version.forge.version
}

output "execution_role_arn" {
  description = "FORGE execution IAM role ARN"
  value       = aws_iam_role.forge_execution.arn
  sensitive   = true
}

output "execution_role_name" {
  description = "FORGE execution IAM role name"
  value       = aws_iam_role.forge_execution.name
}

output "instance_profile_name" {
  description = "EC2 instance profile name wrapping the execution role"
  value       = aws_iam_instance_profile.forge.name
}
