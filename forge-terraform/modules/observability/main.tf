resource "aws_cloudwatch_log_group" "forge_pipeline" {
  name              = "/forge/${var.environment}/pipeline"
  retention_in_days = 30
}

# ─── SNS Alerts Topic ─────────────────────────────────────────────────────────

resource "aws_sns_topic" "forge_alerts" {
  name = "forge-alerts-${var.environment}"
}

# NOTE: AWS sends a confirmation email to alerts_email after the first apply.
# CloudWatch alarms will NOT deliver notifications until the subscription is confirmed
# by clicking the link in that email.
resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.forge_alerts.arn
  protocol  = "email"
  endpoint  = var.alerts_email
}

# ─── CloudWatch Alarms ────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "high_retry_rate" {
  alarm_name          = "FORGE-HighRetryRate-${var.environment}"
  alarm_description   = "FORGE retry rate exceeds threshold — check LangSmith for agent errors"
  namespace           = "FORGE/Migration"
  metric_name         = "files_retried"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 30
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.forge_alerts.arn]
  ok_actions          = [aws_sns_topic.forge_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "high_manual_rate" {
  alarm_name          = "FORGE-HighManualRate-${var.environment}"
  alarm_description   = "More than 20 files escalated to manual review — complex migration phase in progress"
  namespace           = "FORGE/Migration"
  metric_name         = "files_manual"
  statistic           = "Sum"
  period              = 600
  evaluation_periods  = 1
  threshold           = 20
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.forge_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "pipeline_stalled" {
  alarm_name          = "FORGE-PipelineStalled-${var.environment}"
  alarm_description   = "FORGE pipeline has not processed a file in 15 minutes"
  namespace           = "FORGE/Migration"
  metric_name         = "files_processed"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  # notBreaching: only alarm when the pipeline is actively running and emitting metrics.
  # When idle, missing data is expected and should NOT trigger the alarm.
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.forge_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "cost_spike" {
  alarm_name          = "FORGE-CostSpike-${var.environment}"
  alarm_description   = "FORGE Bedrock cost exceeds $50/hour — review run configuration"
  namespace           = "FORGE/Migration"
  metric_name         = "estimated_cost_usd"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 50
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.forge_alerts.arn]
}

# ─── CloudWatch Dashboard ─────────────────────────────────────────────────────

resource "aws_cloudwatch_dashboard" "forge" {
  dashboard_name = local.dashboard_name

  dashboard_body = jsonencode({
    widgets = [
      # Row 1 — Progress overview (y=0, three widgets at width=8)
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 8
        height = 6
        properties = {
          title   = "Files Processed"
          view    = "timeSeries"
          stat    = "Sum"
          period  = 60
          region  = var.aws_region
          metrics = [
            ["FORGE/Migration", "files_processed"]
          ]
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 0
        width  = 8
        height = 6
        properties = {
          title   = "Files Passed"
          view    = "timeSeries"
          stat    = "Sum"
          period  = 60
          region  = var.aws_region
          metrics = [
            ["FORGE/Migration", "files_passed"]
          ]
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 0
        width  = 8
        height = 6
        properties = {
          title   = "Files Escalated to Manual Review"
          view    = "timeSeries"
          stat    = "Sum"
          period  = 60
          region  = var.aws_region
          metrics = [
            ["FORGE/Migration", "files_manual"]
          ]
        }
      },
      # Row 2 — Quality metrics (y=6, two widgets at width=12)
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title   = "Review Score (Average)"
          view    = "timeSeries"
          stat    = "Average"
          period  = 300
          region  = var.aws_region
          metrics = [
            ["FORGE/Migration", "review_score"]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title   = "Retry Rate %"
          view    = "timeSeries"
          period  = 300
          region  = var.aws_region
          metrics = [
            ["FORGE/Migration", "files_retried", { id = "m1", visible = false }],
            ["FORGE/Migration", "files_processed", { id = "m2", visible = false }],
            [{ expression = "IF(m2>0, m1/m2*100, 0)", label = "Retry Rate %", id = "e1" }]
          ]
        }
      },
      # Row 3 — Cost and performance (y=12, two widgets at width=12)
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title   = "Estimated Cost USD (Sum)"
          view    = "timeSeries"
          stat    = "Sum"
          period  = 3600
          region  = var.aws_region
          metrics = [
            ["FORGE/Migration", "estimated_cost_usd"]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title   = "Bedrock Calls per Hour"
          view    = "timeSeries"
          stat    = "Sum"
          period  = 3600
          region  = var.aws_region
          metrics = [
            ["FORGE/Migration", "bedrock_calls"]
          ]
        }
      },
      # Row 4 — Alarm status (y=18, full width)
      {
        type   = "alarm"
        x      = 0
        y      = 18
        width  = 24
        height = 6
        properties = {
          title = "FORGE Alarm Status"
          alarms = [
            aws_cloudwatch_metric_alarm.high_retry_rate.arn,
            aws_cloudwatch_metric_alarm.high_manual_rate.arn,
            aws_cloudwatch_metric_alarm.pipeline_stalled.arn,
            aws_cloudwatch_metric_alarm.cost_spike.arn
          ]
        }
      }
    ]
  })
}
