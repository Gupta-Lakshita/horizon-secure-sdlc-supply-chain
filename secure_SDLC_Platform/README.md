# Horizon Relevance AI DevSecOps Enterprise Installer

This folder is the Phase 2 packaging layer for client-hosted deployments.

The product code remains in the existing backend, frontend, and Jenkins shared-library repositories. This installer holds the client-facing deployment assets, examples, and runbooks needed to install Horizon Relevance AI DevSecOps inside a client's cloud account.

## Target Deployment Model

- Client owns the EKS cluster, ECR repositories, S3 artifact bucket, IAM roles, DNS, and secrets.
- Horizon Relevance provides Helm values, Terraform modules, signed license, container images, policy packs, and support.
- Source code is cloned, built, scanned, and deployed inside the client-hosted environment.
- License enforcement happens before backend pipeline trigger and again in Jenkins before execution.

## Initial Contents

- `examples/client-values.yaml`: client-hosted values contract.
- `docs/client-hosted-test-plan.md`: end-to-end validation flow for a simulated client.
- `docs/license-contract.md`: first backend/Jenkins license contract.
- `docs/client-onboarding-trial-paid-enterprise-playbook.md`: product-owner onboarding, licensing, infrastructure, and commercialization playbook for trial, paid, and enterprise clients.

Terraform and umbrella Helm charts can be added after the backend/Jenkins contract stabilizes.
