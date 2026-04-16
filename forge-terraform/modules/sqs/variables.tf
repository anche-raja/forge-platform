variable "environment" {
  description = "Deployment environment: dev, staging, or prod"
  type        = string
}

variable "app_name" {
  description = "Application name prefix for resource names"
  type        = string
}

variable "execution_role_arn" {
  description = "FORGE execution IAM role ARN — granted send/receive permissions on the queue"
  type        = string
}
