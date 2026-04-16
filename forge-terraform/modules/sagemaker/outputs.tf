output "endpoint_name" {
  description = "SageMaker endpoint name"
  value       = aws_sagemaker_endpoint.forge_llm.name
}

output "endpoint_arn" {
  description = "SageMaker endpoint ARN"
  value       = aws_sagemaker_endpoint.forge_llm.arn
}

output "endpoint_url" {
  description = "SageMaker endpoint invocation URL"
  value       = "https://runtime.sagemaker.${var.aws_region}.amazonaws.com/endpoints/${aws_sagemaker_endpoint.forge_llm.name}/invocations"
}

output "ssm_parameter_name" {
  description = "SSM parameter name storing the internal LLM API key"
  value       = aws_ssm_parameter.internal_llm_key.name
}

output "ssm_parameter_arn" {
  description = "SSM parameter ARN for the internal LLM API key"
  value       = aws_ssm_parameter.internal_llm_key.arn
  sensitive   = true
}
