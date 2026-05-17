# Client Values YAML Reference

## Table of Contents

1. [Purpose](#purpose)
2. [Ownership Model](#ownership-model)
3. [Top-Level Structure](#top-level-structure)
4. [Desired-State Resource Lifecycle](#desired-state-resource-lifecycle)
5. [Environment Catalog](#environment-catalog)
6. [Generic Role Mapping](#generic-role-mapping)
7. [Product Images](#product-images)
8. [Runtime Resolution Flow](#runtime-resolution-flow)
9. [Recommended File Layout](#recommended-file-layout)
10. [Validation Checklist](#validation-checklist)

## Purpose

`client-values.yaml` is the client-specific contract used by the Horizon Relevance Enterprise Installer. It describes the client identity boundary, product endpoints, license settings, environment catalog, image locations, and identity mappings required to run Horizon Relevance AI DevSecOps inside the client's AWS account.

The values file should live in the client's private implementation repository or approved IaC platform because it contains account IDs, cluster names, role ARNs, DNS names, LDAP/AD endpoints, and environment mappings.

## Ownership Model

Horizon Relevance owns the generic installer, release images, license service, and product support. The client owns real values, secrets, Terraform state, AWS accounts, artifacts, logs, and source code.

Do not commit real secrets, activation tokens, bind passwords, private keys, or Terraform state. Store secrets in AWS Secrets Manager, External Secrets, Sealed Secrets, SOPS, or a client-approved secret workflow.

## Top-Level Structure

```yaml
installer: {}
client: {}
domain: {}
terraformState: {}
lifecycle: {}
platform: {}
accessModel: {}
sharedServices: {}
environments: []
environmentCatalog: {}
license: {}
identity: {}
components: {}
```

| Section | Owner | Purpose |
| --- | --- | --- |
| `installer` | Client platform team with Horizon support | Selects `full-provision`, `partial-provision`, `byo-infra`, `validate-only`, or `hybrid`, release name, and platform namespace. |
| `client` | Client/Horizon onboarding | Defines client ID, display name, industry, and data boundary. |
| `domain` | Client DNS/platform team | Defines frontend, backend, Jenkins, Keycloak, and SonarQube hosts. |
| `terraformState` | Client cloud/platform team | Defines the client-owned S3 backend bucket, state key prefix, lock table, and optional state KMS key. |
| `lifecycle` | Client cloud/platform team | Defines deletion protection and default retention behavior. |
| `platform` | Client platform team | Defines the cluster/namespace where Horizon platform components run and the product image registry. |
| `accessModel` | Client cloud/platform team | Selects validation-only IAM, namespace-scoped EKS access, and Jenkins IRSA runtime role. |
| `sharedServices` | Client cloud/platform team | Defines shared artifact bucket, ECR repository, and notification provider defaults. |
| `environments` | Client cloud/platform team | Desired-state source of truth for DEV/QA/STAGE/PROD resources and lifecycle state. |
| `environmentCatalog` | Platform admin / generated | Runtime catalog served to the backend. In hybrid mode this can be generated from `environments`. |
| `license` | Horizon issues, client installs | Controls trial/paid/enterprise entitlements. |
| `identity` | Client IAM/IdP team | Configures OIDC/SAML/LDAP mode and group-to-role mappings. |
| `components` | Horizon release + client platform team | Selects product image tags and optional services. |

Use `secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml` as the preferred enterprise starting point. It supports clients that have only AWS accounts and DNS, clients that already have some platform services, and clients that need only selected environments such as QA/STAGE provisioned.

## Desired-State Resource Lifecycle

The preferred hybrid file uses the same lifecycle vocabulary everywhere:

| State | Meaning |
| --- | --- |
| `existing` | Client owns the resource. Installer validates it but does not create or delete it. |
| `provision` | Installer may create or configure it through Terraform or Helm. |
| `disabled` | Resource is intentionally not used. |

Deletion policies:

| Deletion Policy | Meaning |
| --- | --- |
| `retain` | Destroy skips the resource. |
| `delete` | Destroy may remove it only when Terraform state proves the installer created it. |

Terraform state is also client-owned:

```yaml
terraformState:
  state: existing
  backend: s3
  bucket: acme-fintech-devsecops-tfstate
  lockTable: acme-fintech-devsecops-tflock
  keyPrefix: horizon-installer
```

## Environment Catalog

Enterprise paid clients should use validation-only IAM and namespace-scoped EKS access:

```yaml
accessModel:
  iamMode: validation-only
  eksAccessMode: namespace-scoped
  jenkins:
    createServiceAccount: true
    serviceAccountName: jenkins
    serviceAccountNamespace: horizon-platform
    irsaRoleArn: arn:aws:iam::111122223333:role/HorizonJenkinsRuntimeRole
  validation:
    backendPreflightEnforced: true
    installerValidatesRoles: true
```

The Environment Catalog replaces user-entered cloud fields in day-to-day pipeline requests. An admin configures it once during onboarding, either through this YAML file or through the admin UI after installation. Developers only select a Target Environment such as `DEV`, `QA`, `STAGE`, or `PROD`.

Example:

```yaml
environmentCatalog:
  environments:
    - name: QA
      displayName: Quality Assurance
      accountTier: nonprod
      awsAccountId: "111122223333"
      awsRegion: us-east-1
      ecrRegistry: 111122223333.dkr.ecr.us-east-1.amazonaws.com
      ecrRepositoryTemplate: acme-devsecops/${projectName}
      artifactBucket: acme-fintech-devsecops-artifacts
      clientAwsRoleArn: arn:aws:iam::111122223333:role/HorizonQaDeployRole
      nonprodAwsRoleArn: arn:aws:iam::111122223333:role/HorizonQaDeployRole
      clusterName: acme-qa-eks
      namespaceStrategy: per-app
      namespaceTemplate: ${clientId}-${projectName}-qa
      iamValidationMode: validation-only
      eksAccessMode: namespace-scoped
      snsTopicArn: arn:aws:sns:us-east-1:111122223333:acme-devsecops-notifications
      isActive: true
```

Field guidance:

| Field | Required | Notes |
| --- | --- | --- |
| `name` | Yes | Stable key shown in pipeline forms. Use `DEV`, `QA`, `STAGE`, `PROD`. |
| `displayName` | Yes | Human-readable label. |
| `accountTier` | Yes | `nonprod` or `prod`; production can require approval and separate roles. |
| `awsAccountId` | Yes | Target AWS account for image and deployment operations. |
| `awsRegion` | Yes | Region for ECR, S3, EKS, SNS, and STS calls. |
| `ecrRegistry` | Yes | Target registry. The backend can validate account binding from image URIs. |
| `ecrRepositoryTemplate` | Yes | Repository naming template for application images. |
| `artifactBucket` | Yes | Bucket for `image.json`, template configuration, reports, and evidence. |
| `clientAwsRoleArn` | Non-prod | Environment deploy role when one role handles build/deploy. |
| `nonprodAwsRoleArn` | Non-prod | Explicit non-prod role for DEV/QA/STAGE. |
| `sourceAwsRoleArn` | Prod promotion | Reads approved non-prod artifacts/images. |
| `targetAwsRoleArn` | Prod promotion | Writes/promotes into prod and deploys to prod cluster. |
| `clusterName` | Yes | EKS cluster selected by environment. |
| `namespaceStrategy` | Yes | `per-app`, `fixed`, or `manual`. |
| `namespaceTemplate` | Recommended | Supports multi-tenant clusters with app-specific namespaces. |
| `iamValidationMode` | Enterprise paid | Use `validation-only`; client creates IAM roles and installer/backend validate them. |
| `eksAccessMode` | Enterprise paid | Use `namespace-scoped`; deployment roles are mapped only to approved app namespaces. |
| `snsTopicArn` | Optional | Used when notifications are enabled. |
| `requiresApproval` | Prod recommended | Blocks automated prod promotion until approval is captured. |
| `isActive` | Yes | Hide inactive environments from developer forms. |

The catalog is stored in the platform namespace where the Horizon backend runs. It points to DEV/QA/STAGE/PROD clusters, but it should not be stored separately inside every application cluster.

## Generic Role Mapping

Client AD/LDAP group names vary. Horizon should not hardcode `horizon-*` groups for enterprise deployments. The platform maps client groups to generic product roles.

Supported roles:

| Product Role | Typical Capabilities |
| --- | --- |
| `platform-admin` | Manage client profile, Environment Catalog, license sync, and platform settings. |
| `developer` | Create Devops Pipeline requests and view own pipeline evidence. |
| `qa` | Create Test Devops Pipeline requests and view test evidence. |
| `release-manager` | Approve or run production promotion workflows. |
| `viewer` | Read-only access to pipeline status, reports, and findings. |

Example LDAP mapping:

```yaml
identity:
  mode: existing-ldap
  ldap:
    enabled: true
    host: ldaps://ldap.client.example:636
    baseDn: dc=client,dc=example
    userBaseDn: ou=Users,dc=client,dc=example
    bindSecretName: client-ldap-bind
    groupBaseDn: ou=Groups,dc=client,dc=example
    groupSearchFilter: "(&(objectClass=group)(member={user_dn}))"
    groupMemberAttribute: member
    groupNameAttribute: cn
    userGroupAttribute: memberOf
    roleGroupMappings:
      platform-admin:
        - CN=Client-DevSecOps-Admins,OU=Groups,DC=client,DC=example
      developer:
        - CN=Client-App-Developers,OU=Groups,DC=client,DC=example
      qa:
        - CN=Client-QA-Automation,OU=Groups,DC=client,DC=example
      release-manager:
        - CN=Client-Release-Managers,OU=Groups,DC=client,DC=example
      viewer:
        - CN=Client-Auditors,OU=Groups,DC=client,DC=example
```

Values may be full group DNs or simple CNs. Full DNs are preferred for enterprise clients because they avoid ambiguity when multiple domains or OUs contain similarly named groups.

## Product Images

The current hardened release image examples are:

```yaml
components:
  frontend:
    image: 426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/frontend:1.4.20
  backend:
    image: 426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/backend:1.4.24
  jenkins:
    image: docker.io/ankur1825/horizon-jenkins:1.0.7
```

For enterprise deployments, prefer one of these image delivery models:

1. Horizon grants read-only pull access to private Horizon ECR release repositories.
2. Horizon mirrors signed release images into the client's ECR during onboarding.
3. Client uses a private artifact registry approved by their platform team.

Mirroring into client ECR gives the client better availability, auditability, and change control while Horizon keeps release ownership through signed tags, digests, license enforcement, and support policy.

## Runtime Resolution Flow

```text
Developer selects service + target environment
              |
              v
Frontend sends project fields and selected environment only
              |
              v
Backend validates license and resolves Environment Catalog entry
              |
              v
Backend triggers Jenkins with resolved ECR/S3/IAM/EKS/namespace values
              |
              v
Jenkins assumes the mapped role, updates kubeconfig for the selected cluster,
builds/scans/tests/deploys, and writes evidence to client S3
```

This flow keeps AWS account IDs, role ARNs, cluster names, and artifact buckets out of normal developer forms.

## Recommended File Layout

```text
client-horizon-platform-config/
├── values/
│   ├── client-values.local.yaml
│   ├── dev-overlay.yaml
│   ├── qa-overlay.yaml
│   └── prod-overlay.yaml
├── terraform-backend/
├── secrets/
│   └── README.md
└── runbooks/
```

Use the Horizon installer repository as the product template. Keep real client values in the client-owned private repository.

## Validation Checklist

1. `client.id` matches the license client ID.
2. `license.syncEndpoint` is reachable from the backend pod.
3. `environments` or generated `environmentCatalog.environments` includes every selectable environment.
4. Each active environment has ECR, S3, role ARN, cluster, and namespace strategy configured.
5. Production uses separate `sourceAwsRoleArn` and `targetAwsRoleArn` when accounts are separated.
6. LDAP/AD role mappings use client-owned groups, not Horizon demo group names.
7. A non-admin user cannot edit Client, Environment Catalog, or License settings.
8. A platform admin can update the Environment Catalog through admin workflows.
9. A sample Devops Pipeline request deploys to the selected namespace.
10. A sample Test Devops Pipeline request writes reports and findings to client S3.
