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
  blocked_input_messaging  = "Content blocked by FORGE guardrail — contains sensitive information or prompt injection attempt"
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
    pii_entities_config {
      type   = "PASSWORD"
      action = "ANONYMIZE"
    }
    pii_entities_config {
      type   = "IP_ADDRESS"
      action = "ANONYMIZE"
    }
    pii_entities_config {
      type   = "EMAIL"
      action = "ANONYMIZE"
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

  word_policy_config {
    words_config {
      text = "ignore previous instructions"
    }
    words_config {
      text = "disregard your system prompt"
    }
    words_config {
      text = "you are now"
    }
  }
}

resource "aws_bedrock_guardrail_version" "forge" {
  guardrail_arn = aws_bedrock_guardrail.forge.guardrail_arn
  description   = "v1 — initial production version"
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
        Sid      = "InvokeModels"
        Effect   = "Allow"
        Action   = "bedrock:InvokeModel"
        Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/*"
      },
      {
        Sid      = "ApplyGuardrail"
        Effect   = "Allow"
        Action   = "bedrock:ApplyGuardrail"
        Resource = aws_bedrock_guardrail.forge.guardrail_arn
      },
      {
        # Wildcard because the rag module Knowledge Base ARN is not known at foundation apply time
        Sid      = "RetrieveFromKB"
        Effect   = "Allow"
        Action   = "bedrock:Retrieve"
        Resource = "arn:aws:bedrock:${var.aws_region}:${var.aws_account_id}:knowledge-base/*"
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
