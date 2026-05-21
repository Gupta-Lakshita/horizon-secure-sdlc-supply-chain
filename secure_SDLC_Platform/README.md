# Horizon Relevance AI DevSecOps Enterprise Installer

This folder is the Phase 2 packaging layer for client-hosted deployments.

The product code remains in the existing backend, frontend, and Jenkins shared-library repositories. This installer holds the client-facing deployment assets, examples, and runbooks needed to install Horizon Relevance AI DevSecOps inside a client's cloud account.

## Target Deployment Model

- Client owns the EKS clusters, ECR repositories, S3 artifact buckets, IAM roles, DNS, identity groups, Environment Catalog values, and secrets.
- Horizon Relevance provides Helm values, Terraform modules, signed license, hardened container images, policy packs, and support.
- Source code is cloned, built, scanned, and deployed inside the client-hosted environment.
- License enforcement happens before backend pipeline trigger and again in Jenkins before execution.

## Installer Contents

- `docs/00-client-documentation-index.md`: recommended client reading order by role and implementation phase.
- `examples/client-values.yaml`: client-hosted values contract with Environment Catalog and generic role mapping.
- `examples/client-hybrid-onboarding-values.yaml`: generic desired-state file for mixed client estates where some resources exist and others must be provisioned.
- `examples/regeneron-trial-values.yaml`: healthcare/pharma trial example with online license sync and DEV/QA/PROD account mapping.
- `helm/horizon-platform`: umbrella Helm chart skeleton for platform configuration and license/enterprise values.
- `helm/license-management-service`: Horizon-owned license service chart for trial, paid, enterprise subscription, activation, and online license sync.
- `license-management-service`: production-shaped MVP service for persistent client licensing, subscription management, activation-token hashes, signed entitlements, usage events, and audit history.
- `terraform/bootstrap`: bootstrap Terraform for client-owned artifact/ECR resources and optional Terraform remote-state S3/DynamoDB backend.
- `terraform/environment`: environment-scoped Terraform for VPC, KMS, EKS, EBS CSI, ingress, namespace, EKS access entries, S3/ECR, and optional deploy roles.
- `scripts/preflight.sh`: validates local tools, AWS access, values, and BYO cluster access.
- `scripts/install.sh`: runs infrastructure and/or platform installation phases.
- `scripts/validate.sh`: validates the installed namespace, enterprise config, license defaults, and pods.
- `scripts/destroy.sh`: safely destroys only selected environment resources marked `state=provision` and `deletionPolicy=delete`.
- `docs/client-enterprise-architecture.md`: conceptual techno-functional architecture for client-hosted enterprise deployments.
- `docs/client-values-reference.md`: YAML structure reference for Environment Catalog, generic LDAP/AD role mapping, and product image settings.
- `docs/aws-iam-eks-prerequisites.md`: client AWS IAM, Jenkins IRSA, deploy-role, EKS access-entry, and namespace-scoped prerequisites.
- `docs/validation-only-namespace-scoped-access.md`: enterprise paid-client access model for validation-only IAM, Jenkins IRSA, and namespace-scoped EKS deployment.
- `docs/client-hosted-test-plan.md`: end-to-end validation flow for a simulated client.
- `docs/license-contract.md`: first backend/Jenkins license contract.
- `docs/license-management/phase-1-license-management-service.md`: Horizon-owned license-management service workflow, architecture, admin APIs, Helm deployment, and validation checklist.
- `docs/client-onboarding-trial-paid-enterprise-playbook.md`: product-owner onboarding, licensing, infrastructure, and commercialization playbook for trial, paid, and enterprise clients.
- `docs/installer-runbook.md`: step-by-step installer guideline for full-provision and BYO infrastructure modes.
- `docs/sensitive-client-data-strategy.md`: repository ownership and sensitive client data handling model.
- `docs/private-ecr-image-distribution.md`: private ECR image publishing, client pull access, and container extraction risk model.
- `docs/generic-hybrid-installer-lifecycle.md`: desired-state lifecycle for provision, validate, remote state, and destroy.
- `docs/build-release-deployment-runbook.md`: end-to-end client engineer guide for build, validate, release promotion, S3/ECR evidence, and EKS deployment.
- `docs/build-release-deployment-runbook.docx`: downloadable client-facing runbook with embedded screenshots and evidence captures.

## Quick Start

Copy the generic hybrid sample values and run local preflight:

```bash
cp secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml client-values.local.yaml
bash secure_SDLC_Platform/scripts/preflight.sh -f client-values.local.yaml --environment QA --dry-run --skip-aws
```

Before using the copied file for a new client, update `client.id`, `installer.releaseName`, `installer.namespace`, and the top-level `naming` block. The installer is not tied to Acme or Horizon demo names; client-owned names are supplied through the values file and explicit role/cluster/namespace fields.

Provision or validate in this order:

```bash
# Optional: create remote Terraform state backend when terraformState.state=provision
bash secure_SDLC_Platform/scripts/install.sh --phase state -f client-values.local.yaml --dry-run

# Provision only selected-environment resources marked state=provision
bash secure_SDLC_Platform/scripts/install.sh --phase infra -f client-values.local.yaml --environment QA --dry-run

# Render/install the Horizon platform Helm chart
bash secure_SDLC_Platform/scripts/install.sh --phase platform -f client-values.local.yaml --dry-run

# Publish the resolved environment to the backend Environment Catalog
bash secure_SDLC_Platform/scripts/install.sh --phase catalog -f client-values.local.yaml --environment QA --dry-run
```

Validate:

```bash
bash secure_SDLC_Platform/scripts/validate.sh -f client-values.local.yaml --environment QA --skip-aws
```

Start with `docs/00-client-documentation-index.md` for the recommended client reading order. See `docs/client-enterprise-architecture.md` for the conceptual architecture, `docs/installer-runbook.md` for the full installation guide, `docs/generic-hybrid-installer-lifecycle.md` for lifecycle commands, `docs/client-values-reference.md` for the current YAML structure, `docs/aws-iam-eks-prerequisites.md` for client AWS prerequisites, `docs/validation-only-namespace-scoped-access.md` for the enterprise access model, and `docs/build-release-deployment-runbook.md` for the build, validate, release promotion, and deployment workflow.

## Current Hardened Product Image Contract

Trial and enterprise installs should use Horizon private ECR image references, not public DockerHub references:

| Component | Private ECR image |
| --- | --- |
| Frontend | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/frontend:1.4.20` |
| Backend | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/backend:1.4.24` |
| Jenkins | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/jenkins:1.0.8` |
| SonarQube mirror | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/sonarqube:10.4-community` |
| Container/IaC scanner | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/trivy-scanner:1.1.2` |
| Policy validation service | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/opa-scanner:1.0.1` |
| License management service | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/license-management-service:0.1.0` |

The backend license contract supports `allowedAwsAccountIds` and `installationId`; set both for every client-hosted trial so a copied deployment cannot be reused freely in another AWS account or installation.
