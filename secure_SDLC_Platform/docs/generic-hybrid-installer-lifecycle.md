# Generic Hybrid Installer Lifecycle

## Table of Contents

1. [Purpose](#purpose)
2. [Desired-State Model](#desired-state-model)
3. [Supported Client Scenarios](#supported-client-scenarios)
4. [Terraform Remote State](#terraform-remote-state)
5. [Lifecycle Commands](#lifecycle-commands)
6. [Provisioning Rules](#provisioning-rules)
7. [Destroy Rules](#destroy-rules)
8. [QA Example](#qa-example)

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

Create the remote state bucket and lock table only when `terraformState.state=provision`:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase state \
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
