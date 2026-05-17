variable "aws_region" {
  description = "AWS region for bootstrap resources."
  type        = string
  default     = "us-east-1"
}

variable "tags" {
  description = "Common tags applied to created resources."
  type        = map(string)
  default     = {}
}

variable "create_state_bucket" {
  description = "Whether to create the Terraform remote-state bucket and lock table."
  type        = bool
  default     = false
}

variable "state_bucket_name" {
  description = "Client-owned Terraform state bucket name."
  type        = string
  default     = ""
}

variable "state_lock_table_name" {
  description = "DynamoDB table name for Terraform state locking."
  type        = string
  default     = ""
}

variable "state_kms_key_arn" {
  description = "Optional KMS key ARN for state bucket encryption."
  type        = string
  default     = ""
}

variable "state_key_prefix" {
  description = "Key prefix used for environment state objects."
  type        = string
  default     = "horizon-installer"
}

variable "create_artifact_bucket" {
  description = "Legacy bootstrap option for a client artifact bucket."
  type        = bool
  default     = false
}

variable "artifact_bucket_name" {
  description = "Legacy client-owned S3 artifact bucket name."
  type        = string
  default     = ""
}

variable "product_repositories" {
  description = "Legacy ECR repositories for mirrored Horizon product images."
  type        = list(string)
  default     = []
}

variable "application_repositories" {
  description = "Legacy ECR repositories for client application images."
  type        = list(string)
  default     = []
}
