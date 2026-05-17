terraform {
  required_version = ">= 1.3.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 4.57.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

locals {
  state_bucket_enabled = var.create_state_bucket && var.state_bucket_name != ""
  state_lock_enabled   = var.create_state_bucket && var.state_lock_table_name != ""
  artifact_enabled     = var.create_artifact_bucket && var.artifact_bucket_name != ""
}

resource "aws_s3_bucket" "terraform_state" {
  count  = local.state_bucket_enabled ? 1 : 0
  bucket = var.state_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  count  = local.state_bucket_enabled ? 1 : 0
  bucket = aws_s3_bucket.terraform_state[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  count                   = local.state_bucket_enabled ? 1 : 0
  bucket                  = aws_s3_bucket.terraform_state[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  count  = local.state_bucket_enabled ? 1 : 0
  bucket = aws_s3_bucket.terraform_state[0].id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = var.state_kms_key_arn != "" ? var.state_kms_key_arn : null
      sse_algorithm     = var.state_kms_key_arn != "" ? "aws:kms" : "AES256"
    }
  }
}

resource "aws_dynamodb_table" "terraform_lock" {
  count        = local.state_lock_enabled ? 1 : 0
  name         = var.state_lock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"
  tags         = var.tags

  attribute {
    name = "LockID"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_s3_bucket" "artifacts" {
  count  = local.artifact_enabled ? 1 : 0
  bucket = var.artifact_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  count  = local.artifact_enabled ? 1 : 0
  bucket = aws_s3_bucket.artifacts[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_ecr_repository" "product" {
  for_each = toset(var.product_repositories)
  name     = each.value
  tags     = var.tags

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }
}

resource "aws_ecr_repository" "applications" {
  for_each = toset(var.application_repositories)
  name     = each.value
  tags     = var.tags

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }
}
