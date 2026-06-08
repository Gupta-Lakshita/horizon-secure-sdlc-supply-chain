# Generic Hybrid Installer Lifecycle

## Table of Contents

1. [Purpose](#purpose)
2. [Desired-State Model](#desired-state-model)
3. [Naming And Resource Ownership](#naming-and-resource-ownership)
4. [Supported Client Scenarios](#supported-client-scenarios)
5. [Terraform Remote State](#terraform-remote-state)
6. [Lifecycle Commands](#lifecycle-commands)
7. [Catalog Sync](#catalog-sync)
8. [Provisioning Rules](#provisioning-rules)
9. [Destroy Rules](#destroy-rules)
10. [QA Example](#qa-example)

## Purpose

The Horizon enterprise installer is driven by a client-owned values file. The values file defines which AWS services already exist, which services the installer may provision, and which services must be ignored.

The generic example is:

```text
secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml
```

Use this file for clients that have a mixed estate, for example:

- AWS accounts and DNS only, but no platform foundation.
- AWS accounts and DNS, but missing selected services such as EKS.
- DEV cluster, S3, ECR, and IAM roles already exist, but QA/STAGE clusters are missing.

## Desired-State Model

Every resource uses the same lifecycle state:

| State | Meaning |
| --- | --- |
| `existing` | Client owns the resource. Installer validates it but does not create or delete it. |
| `provision` | Installer may create/configure it through Terraform or Helm. |
| `disabled` | Resource is intentionally not used. |

Every resource also has a deletion policy:

| Deletion Policy | Meaning |
| --- | --- |
| `retain` | Destroy skips the resource. |
| `delete` | Destroy may remove it only if Terraform state proves the installer created it. |

Enterprise default:

```yaml
accessModel:
  iamMode: validation-only
  eksAccessMode: namespace-scoped
```

This means IAM roles are normally client-created, and the installer validates them.

## Naming And Resource Ownership

The installer is intentionally generic. A client can use their own names for AWS accounts, roles, clusters, namespaces, buckets, repositories, DNS zones, and Terraform state keys.

The platform installation name is controlled here:

```yaml
installer:
  releaseName: client-devsecops-platform
  namespace: client-platform-namespace
```

Those fields are not fixed Horizon namespaces. They should be changed to the client's approved Kubernetes release and namespace names before platform installation.

Installer-created AWS resources use the top-level naming contract:

```yaml
naming:
  resourceNamePrefix: client-approved-prefix
  managedBy: horizon-enterprise-installer
  kmsAliasPrefix: platform/client-approved-prefix
```

If a client has exact-name standards, use explicit per-resource overrides such as:

```yaml
environments:
  - name: QA
    iam:
      deployRole:
        state: provision
        roleName: client-qa-devsecops-deploy
    eks:
      ebsCsiDriver:
        state: provision
        roleName: client-qa-ebs-csi-irsa
      nodeGroup:
        state: provision
        name: client-qa-apps-ng
        roleName: client-qa-apps-ng-role
```

The Acme file is an internal demo of one client naming convention. New trials should copy the generic hybrid file and replace `client.id`, `naming`, role ARNs, cluster names, namespaces, DNS, buckets, and repositories with the client's values.

## Supported Client Scenarios

### AWS Accounts And DNS Only

Set shared services, platform cluster, and environments to `provision`:

```yaml
domain:
  state: existing
sharedServices:
  artifactBucket:
    state: provision
  applicationEcr:
    state: provision
platform:
  cluster:
    state: provision
environments:
  - name: DEV
    eks:
      state: provision
```

### AWS Accounts, DNS, S3, And ECR But Missing QA EKS

Reuse S3/ECR and provision only QA infrastructure:

```yaml
sharedServices:
  artifactBucket:
    state: existing
  applicationEcr:
    state: existing
environments:
  - name: QA
    foundation:
      artifactBucket:
        state: existing
      applicationEcr:
        state: existing
    iam:
      deployRole:
        state: existing
    eks:
      state: provision
      namespace:
        state: provision
      accessEntry:
        state: provision
```

### DEV Exists, QA/STAGE Missing

```yaml
environments:
  - name: DEV
    eks:
      state: existing
    iam:
      deployRole:
        state: existing
  - name: QA
    eks:
      state: provision
    iam:
      deployRole:
        state: existing
  - name: STAGE
    eks:
      state: provision
    iam:
      deployRole:
        state: existing
```

## Terraform Remote State

Terraform state is client-owned and stored in S3. Use one bucket and one lock table, with a separate state key per scope.

```yaml
terraformState:
  state: existing
  backend: s3
  bucket: acme-fintech-devsecops-tfstate
  region: us-east-1
  lockTable: acme-fintech-devsecops-tflock
  kmsKeyArn: ""
  keyPrefix: horizon-installer
```

Recommended keys:

```text
horizon-installer/platform/terraform.tfstate
horizon-installer/dev/terraform.tfstate
horizon-installer/qa/terraform.tfstate
horizon-installer/stage/terraform.tfstate
horizon-installer/prod/terraform.tfstate
```

The state bucket, lock table, and state KMS key must be retained during normal environment destroy.

## Lifecycle Commands

### Preflight

Read-only validation:

```bash
bash secure_SDLC_Platform/scripts/preflight.sh \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml \
  --environment QA \
  --dry-run
```

Offline/local validation:

```bash
bash secure_SDLC_Platform/scripts/preflight.sh \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml \
  --environment QA \
  --dry-run \
  --skip-aws
```

### State Backend

Create the default/platform remote state bucket and lock table only when `terraformState.state=provision`:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase state \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml \
  --auto-approve
```

For strict enterprise account separation, define an environment-level backend override such as `environments[].terraform.backend` and run the state phase with that environment while using credentials for that account:

```yaml
environments:
  - name: PROD
    terraform:
      stateKey: horizon-installer/prod/terraform.tfstate
      backend:
        state: provision
        bucket: client-prod-horizon-tfstate
        region: us-east-1
        lockTable: client-prod-horizon-tflock
```

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase state \
  --environment PROD \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml \
  --auto-approve
```

### Environment Infrastructure

Dry-run:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase infra \
  --environment QA \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml \
  --dry-run
```

Apply:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase infra \
  --environment QA \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml \
  --auto-approve
```

### Platform Helm Install

Dry-run:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase platform \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml \
  --dry-run
```

Install or upgrade:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase platform \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml
```

### Catalog Sync

The infrastructure phase provisions or validates AWS and Kubernetes resources. It does not update the running Horizon backend by itself. After an environment is provisioned, publish the resolved environment mapping to the product through the backend Environment Catalog API:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase catalog \
  --environment QA \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml
```

Dry-run mode renders the exact payload that would be sent:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase catalog \
  --environment QA \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml \
  --dry-run
```

The catalog phase uses Terraform outputs when available, then falls back to the values file. It posts to `<frontendHost><backendPath>/environment-catalog` unless `catalogSync.backendUrl`, `installer.backendUrl`, or `domain.platformHosts.backendUrl` is configured. Set `CATALOG_SYNC_TOKEN` when the backend requires a bearer token. TLS verification is enabled by default. For private enterprise CAs, set `catalogSync.caBundlePath`; for internal demos only, set `catalogSync.tlsVerify: false`, pass `--insecure-catalog-sync`, or run with `CATALOG_SYNC_INSECURE=true`.

### Validate

```bash
bash secure_SDLC_Platform/scripts/validate.sh \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml \
  --environment QA
```

## Provisioning Rules

The installer provisions only resources marked:

```yaml
state: provision
```

It validates but does not create resources marked:

```yaml
state: existing
```

It ignores resources marked:

```yaml
state: disabled
```

## Destroy Rules

Destroy is intentionally separate from install.

Dry-run:

```bash
bash secure_SDLC_Platform/scripts/destroy.sh \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml \
  --environment QA \
  --dry-run
```

Confirmed destroy:

```bash
bash secure_SDLC_Platform/scripts/destroy.sh \
  -f secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml \
  --environment QA \
  --confirm QA
```

Destroy only removes resources that satisfy all conditions:

1. Selected environment matches.
2. Resource has `state: provision`.
3. Resource has `deletionPolicy: delete`.
4. Resource exists in the selected environment Terraform state.

Destroy never removes:

- resources marked `state: existing`
- resources marked `deletionPolicy: retain`
- Terraform state bucket
- Terraform lock table
- Terraform state KMS key

## QA Example

For a client with existing DNS, S3, ECR, and IAM roles, but missing QA EKS:

```yaml
environments:
  - name: QA
    foundation:
      dns:
        state: existing
      artifactBucket:
        state: existing
      applicationEcr:
        state: existing
    iam:
      deployRole:
        state: existing
    eks:
      state: provision
      deletionPolicy: delete
      namespace:
        state: provision
        deletionPolicy: delete
      accessEntry:
        state: provision
        deletionPolicy: delete
```

The dry-run output should show that S3/ECR/DNS/IAM are retained and only QA EKS, namespace, access entry, KMS, VPC, Secrets Manager placeholder, EBS CSI, and ingress are eligible for provisioning.
