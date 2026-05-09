terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "artifacts" {
  count  = var.create_artifact_bucket ? 1 : 0
  bucket = var.artifact_bucket_name
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  count  = var.create_artifact_bucket ? 1 : 0
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

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }
}

