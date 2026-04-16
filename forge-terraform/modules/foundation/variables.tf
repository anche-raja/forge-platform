variable "environment" {
  description = "Deployment environment: dev, staging, or prod"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "app_name" {
  description = "Application name prefix for resource names"
  type        = string
}

variable "aws_account_id" {
  description = "AWS account ID — used to construct Bedrock and IAM ARNs"
  type        = string
}

