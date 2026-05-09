output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "artifact_bucket_name" {
  value = var.create_artifact_bucket ? aws_s3_bucket.artifacts[0].bucket : var.artifact_bucket_name
}

output "product_repository_urls" {
  value = {
    for name, repo in aws_ecr_repository.product : name => repo.repository_url
  }
}

output "application_repository_urls" {
  value = {
    for name, repo in aws_ecr_repository.applications : name => repo.repository_url
  }
}

