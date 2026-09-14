module "foundation" {
  source = "./modules/foundation"

  environment    = var.environment
  aws_region     = var.aws_region
  app_name       = var.app_name
  aws_account_id = var.aws_account_id
}

module "observability" {
  source = "./modules/observability"

  environment  = var.environment
  app_name     = var.app_name
  alerts_email = var.alerts_email
  aws_region   = var.aws_region
}

# The Phase 6 queue is opt-in.
module "sqs" {
  source = "./modules/sqs"
  count  = var.enable_sqs ? 1 : 0

  environment        = var.environment
  app_name           = var.app_name
  execution_role_arn = module.foundation.execution_role_arn
}

# The sagemaker module is only deployed when enable_sagemaker = true.
# Ensure a trained model artifact exists in S3 before enabling this.
module "sagemaker" {
  source = "./modules/sagemaker"
  count  = var.enable_sagemaker ? 1 : 0

  environment = var.environment
  app_name    = var.app_name
  aws_region  = var.aws_region
}
