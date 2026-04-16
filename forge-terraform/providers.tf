terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.31.0"
    }
    awscc = {
      source  = "hashicorp/awscc"
      version = ">= 0.70.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "FORGE"
      Environment = var.environment
      ManagedBy   = "Terraform"
      Team        = var.team_name
    }
  }
}

# awscc provider is required for OpenSearch Serverless collection (awscc_opensearchserverless_collection).
# It does not support default_tags — tag each awscc resource manually using list-of-{key,value} format.
provider "awscc" {
  region = var.aws_region
}
