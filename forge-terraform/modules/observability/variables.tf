variable "environment" {
  description = "Deployment environment: dev, staging, or prod"
  type        = string
}

variable "app_name" {
  description = "Application name prefix for resource names"
  type        = string
}

variable "alerts_email" {
  description = "Email address for CloudWatch alarm SNS notifications"
  type        = string
}

variable "aws_region" {
  description = "AWS region for CloudWatch dashboard widgets"
  type        = string
}
