# ---------------------------------------------------------------------------
# Provider and version constraints.
#
# Remote state is not configured here on purpose: point this module at your own
# backend (S3 + DynamoDB lock table is the usual choice on AWS) before the first
# shared apply. See terraform/README.md.
# ---------------------------------------------------------------------------
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.50"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.66"
    }
  }

  # backend "s3" {
  #   bucket         = "my-terraform-state"
  #   key            = "insilico-trial-mas/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "terraform-locks"
  #   encrypt        = true
  # }
}

provider "databricks" {
  host      = var.databricks_host
  auth_type = var.databricks_auth_type
  # `databricks` auth type reads DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET
  # from the environment for service-principal based CI runs.
  profile = var.databricks_profile != "" ? var.databricks_profile : null
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "insilico-trial-mas"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
