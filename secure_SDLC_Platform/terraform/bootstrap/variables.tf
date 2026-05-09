variable "aws_region" {
  description = "AWS region for bootstrap resources."
  type        = string
  default     = "us-east-1"
}

variable "create_artifact_bucket" {
  description = "Whether to create the client artifact bucket."
  type        = bool
  default     = true
}

variable "artifact_bucket_name" {
  description = "Client-owned S3 artifact bucket name."
  type        = string
}

variable "product_repositories" {
  description = "ECR repositories for mirrored Horizon product images."
  type        = list(string)
  default = [
    "horizon/frontend",
    "horizon/backend",
    "horizon/jenkins",
    "horizon/scanner"
  ]
}

variable "application_repositories" {
  description = "ECR repositories for client application images."
  type        = list(string)
  default     = []
}

