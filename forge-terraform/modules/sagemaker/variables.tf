variable "environment" {
  description = "Deployment environment: dev, staging, or prod"
  type        = string
}

variable "app_name" {
  description = "Application name prefix for resource names"
  type        = string
}

variable "aws_region" {
  description = "AWS region — used to select the correct TGI DLC image URI"
  type        = string
}

variable "model_artifact_s3_uri" {
  description = "S3 URI to the trained model artifact (.tar.gz) — e.g. s3://my-bucket/model.tar.gz"
  type        = string
  default     = ""
}

variable "hf_model_id" {
  description = "HuggingFace model ID or local path passed to the TGI container"
  type        = string
  default     = ""
}

variable "tgi_image_uri" {
  description = "Override for the TGI DLC container image URI. Leave empty to use the module default."
  type        = string
  default     = ""
}
