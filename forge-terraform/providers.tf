terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.31.0, < 7.0.0" # 6.x deprecates hash_key (warning only); 7.x is untested
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
