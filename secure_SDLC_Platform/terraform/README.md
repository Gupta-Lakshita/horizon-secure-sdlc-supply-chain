# Terraform Installer Modules

This directory is the infrastructure provisioning layer for Horizon Relevance AI DevSecOps client-hosted deployments.

## Module Strategy

The Terraform layer should support:

1. Full platform provisioning for clients with only AWS accounts and DNS.
2. BYO infrastructure mapping for clients that already have EKS, ECR, S3, IAM, DNS, and IdP.

## Recommended Modules

```text
terraform/
├── bootstrap/
├── eks/
├── ecr/
├── s3-artifacts/
├── iam-roles/
├── dns/
├── storage/
└── observability/
```

## Sensitive State

Terraform state must be stored in the client's AWS account, not in Horizon Relevance repositories.

Recommended backend:

```hcl
terraform {
  backend "s3" {
    bucket         = "client-owned-terraform-state"
    key            = "horizon-devsecops/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "client-owned-terraform-locks"
    encrypt        = true
  }
}
```

Do not commit `.tfstate`, `.tfvars`, or generated backend configuration.

