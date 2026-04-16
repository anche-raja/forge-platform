output "queue_url" {
  description = "Manual review SQS queue URL"
  value       = aws_sqs_queue.main.url
  sensitive   = true
}

output "queue_arn" {
  description = "Manual review SQS queue ARN"
  value       = aws_sqs_queue.main.arn
}

output "queue_name" {
  description = "Manual review SQS queue name"
  value       = aws_sqs_queue.main.name
}

output "dlq_url" {
  description = "Dead-letter queue URL"
  value       = aws_sqs_queue.dlq.url
  sensitive   = true
}

output "dlq_arn" {
  description = "Dead-letter queue ARN"
  value       = aws_sqs_queue.dlq.arn
}
