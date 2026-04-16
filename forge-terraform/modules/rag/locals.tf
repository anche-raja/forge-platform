locals {
  suffix              = "${var.app_name}-${var.environment}"
  bucket_name         = "${var.app_name}-knowledge-base-${var.aws_account_id}-${var.environment}"
  collection_name     = "forge-kb-${var.environment}"
  embedding_model_arn = "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
}
