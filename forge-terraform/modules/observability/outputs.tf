output "cloudwatch_log_group_name" {
  description = "CloudWatch log group name for FORGE pipeline logs"
  value       = aws_cloudwatch_log_group.forge_pipeline.name
}

output "cloudwatch_log_group_arn" {
  description = "CloudWatch log group ARN"
  value       = aws_cloudwatch_log_group.forge_pipeline.arn
}

output "dashboard_name" {
  description = "CloudWatch dashboard name"
  value       = aws_cloudwatch_dashboard.forge.dashboard_name
}

output "sns_topic_arn" {
  description = "SNS alerts topic ARN"
  value       = aws_sns_topic.forge_alerts.arn
  sensitive   = true
}

output "alarm_high_retry_arn" {
  description = "ARN of the high retry rate alarm"
  value       = aws_cloudwatch_metric_alarm.high_retry_rate.arn
}

output "alarm_high_manual_arn" {
  description = "ARN of the high manual escalation rate alarm"
  value       = aws_cloudwatch_metric_alarm.high_manual_rate.arn
}

output "alarm_pipeline_stalled_arn" {
  description = "ARN of the pipeline stalled alarm"
  value       = aws_cloudwatch_metric_alarm.pipeline_stalled.arn
}

output "alarm_cost_spike_arn" {
  description = "ARN of the cost spike alarm"
  value       = aws_cloudwatch_metric_alarm.cost_spike.arn
}
