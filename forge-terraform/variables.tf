variable "environment" {
  description = "Deployment environment: dev, staging, or prod"
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod"
  }
}

variable "aws_region" {
  description = "AWS region to deploy resources into"
  type        = string
  default     = "us-east-1"
}

variable "aws_account_id" {
  description = "AWS account ID — required, no default. Used in S3 bucket names and Bedrock ARN construction."
  type        = string
}

variable "app_name" {
  description = "Application name prefix used in all resource names"
  type        = string
  default     = "forge"
}

variable "team_name" {
  description = "Team name applied as a tag to every resource"
  type        = string
  default     = "platform"
}

variable "alerts_email" {
  description = "Email address for CloudWatch alarm SNS notifications — requires manual subscription confirmation"
  type        = string
}

variable "scope_package_prefix" {
  description = "Java package prefix used for scope validation in FORGE agents (e.g. com.corp)"
  type        = string
}

variable "enable_sagemaker" {
  description = "Set to true to deploy the SageMaker internal LLM endpoint. Only enable when a trained model artifact is ready in S3."
  type        = bool
  default     = false
}

variable "target_java_version" {
  description = "Target Java version for FORGE migration agents"
  type        = string
  default     = "21"
}

variable "target_spring_version" {
  description = "Target Spring Boot version for FORGE migration agents"
  type        = string
  default     = "6"
}
