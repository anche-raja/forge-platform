data "aws_caller_identity" "current" {}

# ─── DynamoDB Tables ──────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "migration_state" {
  name         = "${var.app_name}-migration-state-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "file_path"

  # All attributes used as table/GSI keys must be declared here
  attribute {
    name = "file_path"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "phase"
    type = "S"
  }

  global_secondary_index {
    name            = "status-index"
    hash_key        = "status"
    range_key       = "phase"
    projection_type = "ALL"
  }

  global_secondary_index {
    name               = "phase-status-index"
    hash_key           = "phase"
    range_key          = "status"
    projection_type    = "INCLUDE"
    non_key_attributes = ["file_path", "review_score", "retry_count", "updated_at"]
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

resource "aws_dynamodb_table" "langgraph_checkpoints" {
  name         = "${var.app_name}-langgraph-checkpoints-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "thread_id"
  range_key    = "checkpoint_id"

  attribute {
    name = "thread_id"
    type = "S"
  }

  attribute {
    name = "checkpoint_id"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }
}

# ─── Bedrock Guardrails ───────────────────────────────────────────────────────

resource "aws_bedrock_guardrail" "forge" {
  name                      = "${var.app_name}-guardrail-${var.environment}"
  description               = "FORGE migration pipeline guardrail — blocks secrets and prompt injection in source code"
  blocked_input_messaging   = "Content blocked by FORGE guardrail — contains sensitive information or prompt injection attempt"
  blocked_outputs_messaging = "Output blocked by FORGE guardrail — response contained sensitive information"

  sensitive_information_policy_config {
    pii_entities_config {
      type   = "AWS_ACCESS_KEY"
      action = "BLOCK"
    }
    pii_entities_config {
      type   = "AWS_SECRET_KEY"
      action = "BLOCK"
    }
    pii_entities_config {
      type   = "CREDIT_DEBIT_CARD_NUMBER"
      action = "BLOCK"
    }
    pii_entities_config {
      type   = "US_SOCIAL_SECURITY_NUMBER"
      action = "BLOCK"
    }
    pii_entities_config {
      type   = "US_BANK_ACCOUNT_NUMBER"
      action = "BLOCK"
    }
    # The pipeline treats ANY intervention on INPUT as BLOCKED (guardrails_pre),
    # and ANONYMIZE counts as an intervention. Source code legitimately carries
    # e-mail addresses (@author tags) and IP literals (127.0.0.1 in config), so
    # those entity types are deliberately NOT listed — listing them would block
    # a large share of an ordinary codebase. Real secrets stay blocked above.
    pii_entities_config {
      type   = "PASSWORD"
      action = "BLOCK"
    }
  }

  content_policy_config {
    filters_config {
      type            = "HATE"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }
    filters_config {
      type            = "INSULTS"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }
    filters_config {
      type            = "SEXUAL"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }
    filters_config {
      type            = "VIOLENCE"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }
    filters_config {
      type            = "MISCONDUCT"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }
    # PROMPT_ATTACK prevents prompt injection via malicious source code comments
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }
  }

  # Only phrases that never occur in application code or UI strings — a
  # phrase like "you are now" appears in login pages and would block them.
  word_policy_config {
    words_config {
      text = "ignore previous instructions"
    }
    words_config {
      text = "disregard your system prompt"
    }
  }
}

# A guardrail edit only reaches the pipeline once a new numbered version is
# published; agents.yaml pins that number. Replacing this resource whenever the
# guardrail changes keeps the published version (and the guardrail_version
# output) current instead of silently serving the old policy.
resource "aws_bedrock_guardrail_version" "forge" {
  guardrail_arn = aws_bedrock_guardrail.forge.guardrail_arn
  description   = "Published from Terraform — re-issued on every guardrail change"

  lifecycle {
    replace_triggered_by = [aws_bedrock_guardrail.forge]
  }
}

# ─── IAM Execution Role ───────────────────────────────────────────────────────

resource "aws_iam_role" "forge_execution" {
  name = "forge-execution-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowEC2"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      },
      {
        Sid    = "AllowECS"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      },
      {
        # Allows the current IAM user/role (developer or CI/CD pipeline) to
        # assume this role via: aws sts assume-role for local development
        Sid    = "AllowCurrentCaller"
        Effect = "Allow"
        Principal = {
          AWS = data.aws_caller_identity.current.arn
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "bedrock" {
  name = "forge-bedrock-policy"
  role = aws_iam_role.forge_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # agents.yaml names cross-region inference profiles (us.anthropic.…,
        # us.amazon.nova-pro…). Invoking one needs the profile ARN in this
        # account AND the foundation model in every region the profile can
        # route to — an in-region foundation-model/* grant alone is AccessDenied.
        Sid    = "InvokeModels"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:${var.aws_region}:${var.aws_account_id}:inference-profile/*"
        ]
      },
      {
        Sid      = "ApplyGuardrail"
        Effect   = "Allow"
        Action   = "bedrock:ApplyGuardrail"
        Resource = aws_bedrock_guardrail.forge.guardrail_arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "dynamodb" {
  name = "forge-dynamodb-policy"
  role = aws_iam_role.forge_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "TableAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:DescribeTable"
        ]
        Resource = [
          aws_dynamodb_table.migration_state.arn,
          "${aws_dynamodb_table.migration_state.arn}/index/*",
          aws_dynamodb_table.langgraph_checkpoints.arn,
          "${aws_dynamodb_table.langgraph_checkpoints.arn}/index/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "cloudwatch" {
  name = "forge-cloudwatch-policy"
  role = aws_iam_role.forge_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "PutMetrics"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
      },
      {
        Sid    = "WriteLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "forge" {
  name = "forge-execution-profile-${var.environment}"
  role = aws_iam_role.forge_execution.name
}
