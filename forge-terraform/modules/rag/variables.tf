variable "environment" {
  description = "Deployment environment: dev, staging, or prod"
  type        = string
}

variable "app_name" {
  description = "Application name prefix for resource names"
  type        = string
}

variable "aws_account_id" {
  description = "AWS account ID — used in S3 bucket name for global uniqueness"
  type        = string
}

variable "aws_region" {
  description = "AWS region — used to construct Bedrock and OpenSearch ARNs"
  type        = string
}

variable "execution_role_arn" {
  description = "FORGE execution IAM role ARN — granted read/write access to the knowledge base S3 bucket"
  type        = string
}
