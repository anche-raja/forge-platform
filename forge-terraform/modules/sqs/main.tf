resource "aws_sqs_queue" "dlq" {
  name                      = "${var.app_name}-manual-review-dlq-${var.environment}"
  message_retention_seconds = 1209600 # 14 days (maximum)
  kms_master_key_id         = "alias/aws/sqs"
}

resource "aws_sqs_queue" "main" {
  name                       = "${var.app_name}-manual-review-${var.environment}"
  visibility_timeout_seconds = 1800 # 30 minutes — gives engineer time to review
  message_retention_seconds  = 604800 # 7 days
  receive_wait_time_seconds  = 20 # long polling
  kms_master_key_id          = "alias/aws/sqs"

  # After 3 failed receives, the message is moved to the DLQ for investigation
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
}

data "aws_iam_policy_document" "sqs_policy" {
  statement {
    sid    = "AllowForgeExecutionRole"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = [var.execution_role_arn]
    }

    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility"
    ]

    resources = [aws_sqs_queue.main.arn]
  }
}

resource "aws_sqs_queue_policy" "main" {
  queue_url = aws_sqs_queue.main.url
  policy    = data.aws_iam_policy_document.sqs_policy.json
}
