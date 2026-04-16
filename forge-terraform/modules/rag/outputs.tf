output "knowledge_base_id" {
  description = "Bedrock Knowledge Base ID"
  value       = aws_bedrockagent_knowledge_base.forge.id
}

output "knowledge_base_arn" {
  description = "Bedrock Knowledge Base ARN"
  value       = aws_bedrockagent_knowledge_base.forge.arn
}

output "s3_bucket_name" {
  description = "Knowledge Base S3 bucket name"
  value       = aws_s3_bucket.knowledge_base.id
}

output "s3_bucket_arn" {
  description = "Knowledge Base S3 bucket ARN"
  value       = aws_s3_bucket.knowledge_base.arn
}

output "opensearch_collection_endpoint" {
  description = "OpenSearch Serverless collection endpoint"
  value       = awscc_opensearchserverless_collection.forge_kb.collection_endpoint
}

output "data_source_id" {
  description = "Bedrock Knowledge Base data source ID"
  value       = aws_bedrockagent_data_source.s3_docs.data_source_id
}
