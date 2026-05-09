# Horizon Relevance AI DevSecOps Enterprise Installer

This folder is the Phase 2 packaging layer for client-hosted deployments.

The product code remains in the existing backend, frontend, and Jenkins shared-library repositories. This installer holds the client-facing deployment assets, examples, and runbooks needed to install Horizon Relevance AI DevSecOps inside a client's cloud account.

## Target Deployment Model

- Client owns the EKS cluster, ECR repositories, S3 artifact bucket, IAM roles, DNS, and secrets.
- Horizon Relevance provides Helm values, Terraform modules, signed license, container images, policy packs, and support.
- Source code is cloned, built, scanned, and deployed inside the client-hosted environment.
- License enforcement happens before backend pipeline trigger and again in Jenkins before execution.

## Installer Contents

- `examples/client-values.yaml`: client-hosted values contract.
- `examples/regeneron-trial-values.yaml`: healthcare/pharma trial example with online license sync and DEV/QA/PROD account mapping.
- `helm/horizon-platform`: umbrella Helm chart skeleton for platform configuration and license/enterprise values.
- `terraform/bootstrap`: bootstrap Terraform skeleton for client-owned S3/ECR foundation.
- `scripts/preflight.sh`: validates local tools, AWS access, values, and BYO cluster access.
- `scripts/install.sh`: runs infrastructure and/or platform installation phases.
- `scripts/validate.sh`: validates the installed namespace, enterprise config, license defaults, and pods.
- `docs/client-hosted-test-plan.md`: end-to-end validation flow for a simulated client.
- `docs/license-contract.md`: first backend/Jenkins license contract.
- `docs/client-onboarding-trial-paid-enterprise-playbook.md`: product-owner onboarding, licensing, infrastructure, and commercialization playbook for trial, paid, and enterprise clients.
- `docs/installer-runbook.md`: step-by-step installer guideline for full-provision and BYO infrastructure modes.
- `docs/sensitive-client-data-strategy.md`: repository ownership and sensitive client data handling model.

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

See `docs/installer-runbook.md` for the full installation guide.
