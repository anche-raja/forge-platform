resource "aws_iam_role" "sagemaker_execution" {
  name = "forge-sagemaker-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "sagemaker.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "sagemaker_full_access" {
  role       = aws_iam_role.sagemaker_execution.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSageMakerFullAccess"
}

resource "aws_sagemaker_model" "forge_llm" {
  name               = "forge-llm-${var.environment}"
  execution_role_arn = aws_iam_role.sagemaker_execution.arn

  primary_container {
    image          = local.tgi_image
    model_data_url = var.model_artifact_s3_uri

    environment = {
      HF_MODEL_ID        = var.hf_model_id
      SM_NUM_GPUS        = "1"
      MAX_INPUT_LENGTH   = "8192"
      MAX_TOTAL_TOKENS   = "16384"
    }
  }
}

resource "aws_sagemaker_endpoint_configuration" "forge_llm" {
  name = "forge-llm-config-${var.environment}"

  production_variants {
    variant_name           = "AllTraffic"
    model_name             = aws_sagemaker_model.forge_llm.name
    instance_type          = "ml.g5.2xlarge"
    initial_instance_count = 1
  }
}

resource "aws_sagemaker_endpoint" "forge_llm" {
  name                 = "forge-llm-${var.environment}"
  endpoint_config_name = aws_sagemaker_endpoint_configuration.forge_llm.name
}

# Placeholder API key — update manually after the endpoint is deployed and
# you have a real key to set. Stored as SecureString in Parameter Store.
resource "aws_ssm_parameter" "internal_llm_key" {
  name        = "/forge/${var.environment}/internal-llm-key"
  type        = "SecureString"
  value       = "PLACEHOLDER_UPDATE_AFTER_DEPLOYMENT"
  description = "Internal LLM API key for FORGE — update manually after SageMaker endpoint is deployed"

  lifecycle {
    ignore_changes = [value]
  }
}
