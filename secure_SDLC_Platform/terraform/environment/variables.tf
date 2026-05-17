variable "environment_name" {
  description = "Environment key such as DEV, QA, STAGE, or PROD."
  type        = string
}

variable "client_id" {
  description = "Client identifier."
  type        = string
}

variable "aws_region" {
  description = "AWS region."
  type        = string
}

variable "aws_account_id" {
  description = "Target AWS account ID."
  type        = string
}

variable "tags" {
  description = "Common tags."
  type        = map(string)
  default     = {}
}

variable "create_vpc" {
  type    = bool
  default = false
}

variable "existing_vpc_id" {
  type    = string
  default = ""
}

variable "existing_subnet_ids" {
  type    = list(string)
  default = []
}

variable "create_kms_key" {
  type    = bool
  default = false
}

variable "existing_kms_key_arn" {
  type    = string
  default = ""
}

variable "create_artifact_bucket" {
  type    = bool
  default = false
}

variable "artifact_bucket_name" {
  type = string
}

variable "create_ecr_repository" {
  type    = bool
  default = false
}

variable "ecr_repository_name" {
  type = string
}

variable "create_secret_prefix" {
  type    = bool
  default = false
}

variable "secret_prefix" {
  type    = string
  default = ""
}

variable "create_acm_certificate" {
  type    = bool
  default = false
}

variable "existing_acm_certificate_arn" {
  type    = string
  default = ""
}

variable "create_route53_records" {
  type    = bool
  default = false
}

variable "base_domain" {
  type    = string
  default = ""
}

variable "create_eks_cluster" {
  type    = bool
  default = false
}

variable "eks_cluster_name" {
  type = string
}

variable "kubernetes_version" {
  type    = string
  default = "1.30"
}

variable "eks_endpoint_public_access" {
  type    = bool
  default = true
}

variable "create_ebs_csi_driver" {
  type    = bool
  default = false
}

variable "create_ingress_controller" {
  type    = bool
  default = false
}

variable "create_node_group" {
  type    = bool
  default = true
}

variable "node_instance_types" {
  type    = list(string)
  default = ["t3.small"]
}

variable "node_desired_size" {
  type    = number
  default = 1
}

variable "node_min_size" {
  type    = number
  default = 1
}

variable "node_max_size" {
  type    = number
  default = 2
}

variable "create_namespace" {
  type    = bool
  default = false
}

variable "namespace_name" {
  type    = string
  default = ""
}

variable "create_eks_access_entry" {
  type    = bool
  default = false
}

variable "eks_access_policy_arn" {
  type    = string
  default = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"
}

variable "eks_access_scope_type" {
  type    = string
  default = "namespace"
}

variable "deploy_role_arn" {
  type    = string
  default = ""
}

variable "jenkins_runtime_role_arn" {
  type    = string
  default = ""
}

variable "create_deploy_role" {
  type    = bool
  default = false
}

variable "deletion_protection" {
  type    = bool
  default = true
}
