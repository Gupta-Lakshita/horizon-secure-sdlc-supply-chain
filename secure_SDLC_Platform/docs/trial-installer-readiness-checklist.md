# Trial Installer Readiness Checklist

## Table of Contents

1. [Purpose](#purpose)
2. [What This Proves](#what-this-proves)
3. [Client Inputs Required](#client-inputs-required)
4. [Recommended Trial Flow](#recommended-trial-flow)
5. [Run the Readiness Smoke Test](#run-the-readiness-smoke-test)
6. [Provision and Publish an Environment](#provision-and-publish-an-environment)
7. [Validation Evidence](#validation-evidence)
8. [Common Failures](#common-failures)

## Purpose

This checklist is the final non-destructive installer readiness pass before a real trial client uses the Horizon Relevance platform. It verifies that the client values file, Terraform backend, selected environment, platform Helm render, Environment Catalog payload, and validation workflow are coherent before Terraform creates or changes infrastructure.

Use it for a 14-day or 30-day trial onboarding call, an internal client simulation, or a client platform-admin handoff.

## What This Proves

The readiness flow proves:

1. The client values YAML is syntactically valid.
2. Required online license sync settings are present.
3. Terraform remote state settings are present.
4. The selected environment has required AWS account, region, ECR, S3, IAM, EKS, and namespace values.
5. Existing resources marked `state: existing` can be checked before install.
6. Resources marked `state: provision` are planned through the environment Terraform root.
7. The platform Helm chart renders with the client values file.
8. Environment Catalog payload can be generated before it is synced to the backend.
9. Validation commands are ready for post-install verification.

## Client Inputs Required

Before running the smoke test, the client platform team must provide:

| Input | Required For | Notes |
| --- | --- | --- |
| AWS account ID | All environments | Must match license entitlement and Environment Catalog. |
| AWS region | All environments | ECR, S3, EKS, KMS, and Secrets Manager use this region. |
| Terraform state bucket | Installer lifecycle | Client-owned S3 bucket. |
| Terraform lock table | Installer lifecycle | Client-owned DynamoDB table when remote state locking is enabled. |
| Artifact bucket | Build and validation evidence | Stores `image.json`, deployment metadata, reports, and test evidence. |
| ECR repository | Build and promotion | Stores client application images. |
| EKS cluster name | Deployable environments | Existing or provisioned by installer. |
| Deploy role ARN | DEV/QA/STAGE | Jenkins assumes this role for deployment. |
| Source/target role ARNs | PROD promotion | Used to read non-prod artifact/image and write/deploy to production. |
| Namespace template | Namespace-scoped mode | Example: `{client_id}-{project_name}-{env}` or a fixed team namespace. |
| License activation secret name | Online license sync | Secret contains Horizon-issued activation token. |
| Identity group mappings | UI/RBAC | Client AD/LDAP/OIDC groups mapped to product roles. |

Do not put activation tokens, bind passwords, SMTP passwords, or private keys directly in the values file.

## Recommended Trial Flow

1. Horizon issues the trial activation token.
2. Client creates or selects the client-owned installer repository.
3. Client copies `secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml`.
4. Client updates the values file with real account, role, cluster, namespace, DNS, ECR, S3, and identity values.
5. Client stores the activation token in the Kubernetes or cloud secret defined by `license.activationTokenSecretName`.
6. Client runs readiness smoke test.
7. Client provisions missing environment resources.
8. Client syncs Environment Catalog.
9. Client opens Horizon UI and confirms the environment is ready.
10. Developer runs Build & Deploy Pipeline to DEV.
11. QA runs Validation Pipeline.
12. Release manager promotes the immutable image digest through QA/STAGE/PROD.

## Run the Readiness Smoke Test

Offline/local check:

```bash
bash secure_SDLC_Platform/scripts/trial-readiness.sh \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml \
  --environment QA \
  --skip-aws
```

Client AWS-connected check:

```bash
export AWS_PROFILE=<client-platform-admin-profile>

bash secure_SDLC_Platform/scripts/trial-readiness.sh \
  -f client-values.local.yaml \
  --environment QA
```

The script writes a report to:

```text
secure_SDLC_Platform/.generated/trial-readiness-qa.txt
```

This file can be attached to the client onboarding ticket or implementation evidence.

## Provision and Publish an Environment

When readiness passes, run the phases in this order.

State backend:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase state \
  -f client-values.local.yaml \
  --auto-approve
```

Environment infrastructure:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase infra \
  --environment QA \
  -f client-values.local.yaml \
  --auto-approve
```

Platform Helm install or upgrade:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase platform \
  -f client-values.local.yaml
```

Environment Catalog sync:

```bash
export CATALOG_SYNC_TOKEN=<backend-automation-token-if-required>

bash secure_SDLC_Platform/scripts/install.sh \
  --phase catalog \
  --environment QA \
  -f client-values.local.yaml
```

Post-install validation:

```bash
bash secure_SDLC_Platform/scripts/validate.sh \
  -f client-values.local.yaml \
  --environment QA
```

## Validation Evidence

Capture these items before allowing client engineers to run pipelines:

| Evidence | Command or Location |
| --- | --- |
| Readiness report | `.generated/trial-readiness-<env>.txt` |
| Terraform plan/apply output | `.generated/terraform-<env>-*.log` |
| Terraform outputs | `.generated/terraform-output-<env>.json` after catalog sync |
| Environment Catalog payload | `.generated/catalog-<env>.json` |
| Helm release | `helm status <release> -n <namespace>` |
| Platform pods | `kubectl get pods -n <namespace>` |
| License status | Horizon UI License page |
| Environment readiness | Horizon UI Pipeline page after selecting target environment |

## Common Failures

| Symptom | Likely Cause | Fix |
| --- | --- | --- |
| `license.activationTokenSecretName is required` | Online license sync is enabled without a token secret reference. | Create the secret and set the secret name in values. |
| Existing IAM role not found | Role ARN or role name is wrong, or client has not created the role. | Client cloud admin creates role or updates values. |
| `Deployment role cannot describe EKS cluster` | Deploy role lacks `eks:DescribeCluster`. | Add `eks:DescribeCluster` for the target cluster ARN. |
| `Unable to validate EKS access policies` | The environment deploy role lacks EKS access-entry read permissions after the backend successfully assumes it. | Add read permissions for `eks:ListAccessEntries`, `eks:DescribeAccessEntry`, `eks:ListAssociatedAccessPolicies`, and `eks:ListAccessPolicies`. |
| Catalog sync TLS error | Local trust store cannot verify backend certificate. | Use a public ACM certificate, set `catalogSync.caBundlePath`, or use the insecure demo flag only for internal testing. |
| Helm render fails | Values file has missing component image or malformed chart value. | Run `install.sh --phase platform --dry-run` and fix the reported chart value. |
