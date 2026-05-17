output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "terraform_state_bucket_name" {
  value = var.create_state_bucket && var.state_bucket_name != "" ? aws_s3_bucket.terraform_state[0].bucket : var.state_bucket_name
}

output "terraform_lock_table_name" {
  value = var.create_state_bucket && var.state_lock_table_name != "" ? aws_dynamodb_table.terraform_lock[0].name : var.state_lock_table_name
}

output "artifact_bucket_name" {
  value = var.create_artifact_bucket && var.artifact_bucket_name != "" ? aws_s3_bucket.artifacts[0].bucket : var.artifact_bucket_name
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
