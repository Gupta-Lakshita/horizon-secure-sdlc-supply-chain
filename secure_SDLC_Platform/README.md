# Horizon Relevance AI DevSecOps Enterprise Installer

This folder is the Phase 2 packaging layer for client-hosted deployments.

The product code remains in the existing backend, frontend, and Jenkins shared-library repositories. This installer holds the client-facing deployment assets, examples, and runbooks needed to install Horizon Relevance AI DevSecOps inside a client's cloud account.

## Target Deployment Model

- Client owns the EKS clusters, ECR repositories, S3 artifact buckets, IAM roles, DNS, identity groups, Environment Catalog values, and secrets.
- Horizon Relevance provides Helm values, Terraform modules, signed license, hardened container images, policy packs, and support.
- Source code is cloned, built, scanned, and deployed inside the client-hosted environment.
- License enforcement happens before backend pipeline trigger and again in Jenkins before execution.

## Installer Contents

- `examples/client-values.yaml`: client-hosted values contract with Environment Catalog and generic role mapping.
- `examples/regeneron-trial-values.yaml`: healthcare/pharma trial example with online license sync and DEV/QA/PROD account mapping.
- `helm/horizon-platform`: umbrella Helm chart skeleton for platform configuration and license/enterprise values.
- `terraform/bootstrap`: bootstrap Terraform skeleton for client-owned S3/ECR foundation.
- `scripts/preflight.sh`: validates local tools, AWS access, values, and BYO cluster access.
- `scripts/install.sh`: runs infrastructure and/or platform installation phases.
- `scripts/validate.sh`: validates the installed namespace, enterprise config, license defaults, and pods.
- `docs/client-values-reference.md`: YAML structure reference for Environment Catalog, generic LDAP/AD role mapping, and product image settings.
- `docs/validation-only-namespace-scoped-access.md`: enterprise paid-client access model for validation-only IAM, Jenkins IRSA, and namespace-scoped EKS deployment.
- `docs/client-hosted-test-plan.md`: end-to-end validation flow for a simulated client.
- `docs/license-contract.md`: first backend/Jenkins license contract.
- `docs/client-onboarding-trial-paid-enterprise-playbook.md`: product-owner onboarding, licensing, infrastructure, and commercialization playbook for trial, paid, and enterprise clients.
- `docs/installer-runbook.md`: step-by-step installer guideline for full-provision and BYO infrastructure modes.
- `docs/sensitive-client-data-strategy.md`: repository ownership and sensitive client data handling model.
- `docs/private-ecr-image-distribution.md`: private ECR image publishing, client pull access, and container extraction risk model.

## Quick Start

Copy the sample values and run preflight:

```bash
cp secure_SDLC_Platform/examples/regeneron-trial-values.yaml regeneron-trial.local.yaml
bash secure_SDLC_Platform/scripts/preflight.sh -f regeneron-trial.local.yaml
```

Install platform-only for BYO infrastructure:

```bash
bash secure_SDLC_Platform/scripts/install.sh --phase platform -f regeneron-trial.local.yaml
```

Validate:

```bash
bash secure_SDLC_Platform/scripts/validate.sh -f regeneron-trial.local.yaml
```

See `docs/installer-runbook.md` for the full installation guide, `docs/client-values-reference.md` for the current YAML structure, and `docs/validation-only-namespace-scoped-access.md` for the enterprise access model.

## Current Hardened Product Image Contract

Trial and enterprise installs should use Horizon private ECR image references, not public DockerHub references:

| Component | Private ECR image |
| --- | --- |
| Frontend | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/frontend:1.4.19` |
| Backend | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/backend:1.4.22` |
| Jenkins | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/jenkins:1.0.8` |
| SonarQube mirror | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/sonarqube:10.4-community` |
| Container/IaC scanner | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/trivy-scanner:1.1.2` |
| Policy validation service | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/opa-scanner:1.0.1` |

The backend license contract supports `allowedAwsAccountIds` and `installationId`; set both for every client-hosted trial so a copied deployment cannot be reused freely in another AWS account or installation.
