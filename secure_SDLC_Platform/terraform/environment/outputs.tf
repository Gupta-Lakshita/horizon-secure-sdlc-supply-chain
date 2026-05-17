output "environment_name" {
  value = var.environment_name
}

output "aws_account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "vpc_id" {
  value = var.create_vpc ? module.vpc[0].vpc_id : var.existing_vpc_id
}

output "subnet_ids" {
  value = var.create_vpc ? module.vpc[0].public_subnets : var.existing_subnet_ids
}

output "kms_key_arn" {
  value = local.kms_key_arn
}

output "artifact_bucket_name" {
  value = var.artifact_bucket_name
}

output "ecr_repository_name" {
  value = var.ecr_repository_name
}

output "eks_cluster_name" {
  value = var.eks_cluster_name
}

output "namespace_name" {
  value = var.namespace_name
}

output "deploy_role_arn" {
  value = local.effective_deploy_role_arn
}
