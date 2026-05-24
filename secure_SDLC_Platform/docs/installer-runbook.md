# Horizon Relevance Enterprise Installer Runbook

## Table of Contents

1. [Purpose](#purpose)
2. [Installer Modes](#installer-modes)
3. [Recommended Repository Ownership](#recommended-repository-ownership)
4. [DEV/QA/PROD Account Architecture](#devqaprod-account-architecture)
5. [Prerequisites](#prerequisites)
6. [Client Values File](#client-values-file)
7. [Environment Catalog and Role Mapping](#environment-catalog-and-role-mapping)
8. [Preflight Validation](#preflight-validation)
9. [Trial Readiness Smoke Test](#trial-readiness-smoke-test)
10. [Catalog Sync](#catalog-sync)
11. [Mode 1: Full Platform Provisioning](#mode-1-full-platform-provisioning)
12. [Mode 2: Bring Your Own Infrastructure](#mode-2-bring-your-own-infrastructure)
13. [Mode 3: Hybrid Desired-State Provisioning](#mode-3-hybrid-desired-state-provisioning)
14. [Terraform Remote State](#terraform-remote-state)
15. [Destroy Workflow](#destroy-workflow)
16. [Online License Sync](#online-license-sync)
17. [Identity Configuration](#identity-configuration)
18. [Validation](#validation)
19. [Upgrade and Renewal](#upgrade-and-renewal)
20. [Troubleshooting](#troubleshooting)

## Purpose

The Horizon Relevance Enterprise Installer is the client-hosted packaging layer for the Horizon Relevance AI DevSecOps Platform. It gives enterprise clients a repeatable way to install the product into their AWS accounts without sending source code, artifacts, credentials, or regulated data to Horizon Relevance-managed infrastructure.

The installer supports three practical installation modes:

1. **Full Platform Provisioning**: Horizon provisions the required AWS and Kubernetes foundation for the client.
2. **Bring Your Own Infrastructure**: Horizon maps the product to existing client EKS, ECR, S3, IAM, DNS, and identity services.
3. **Hybrid Desired-State Provisioning**: Horizon validates existing client resources and provisions only selected missing environment resources.

## Installer Modes

| Mode | Use When | What Installer Does |
| --- | --- | --- |
| `full-provision` | Client has AWS accounts and DNS but no platform foundation. | Creates or configures VPC, EKS, ECR, S3, IAM roles, storage, ingress, platform namespace, and product components. |
| `byo-infra` | Client already has EKS, ECR, S3, DNS, IAM, identity, or security tooling. | Validates and maps existing infrastructure, then installs Horizon product components only. |
| `hybrid` | Client has some shared services, but selected environments are missing services such as QA/STAGE EKS. | Validates existing resources and provisions only resources marked `state=provision` in the selected environment. |

The same `client-values.yaml` contract drives all modes.

## Recommended Repository Ownership

Client-specific installer values contain sensitive AWS account IDs, cluster names, role ARNs, internal DNS names, LDAP endpoints, S3 bucket names, and identity mappings.

Recommended ownership model:

| Asset | Recommended Owner | Reason |
| --- | --- | --- |
| Generic installer source | Horizon Relevance | Product IP, release control, supportability. |
| Client values file | Client | Contains client-sensitive environment details. |
| Client Terraform state | Client | Contains infrastructure identifiers and must stay in client boundary. |
| Client secrets | Client | Stored in client Secrets Manager, External Secrets, or sealed secret workflow. |
| Product container images | Horizon Relevance, mirrored to client ECR for enterprise | Avoids broad registry access and improves supply-chain control. |
| License | Horizon Relevance issues, client installs/syncs | Commercial enforcement remains Horizon-owned. |

Best practice:

1. Horizon maintains this installer repository as the golden template.
2. Client maintains a private GitHub/Bitbucket repository with:
   - `client-values.yaml`
   - environment overlays
   - Terraform backend configuration
   - approved change history
3. Horizon supports the client through pull requests or secure screen-share pairing.
4. No real client secrets are committed to Git.

For highly regulated clients, use the client's private repository and CI runner to execute Terraform and Helm. Horizon can provide a release bundle and support guidance without needing direct long-lived credentials.

## DEV/QA/PROD Account Architecture

Enterprise clients usually separate environments by AWS account.

Recommended model:

```text
Client AWS Organization
├── DEV Account
│   ├── DEV EKS cluster
│   ├── DEV namespaces
│   └── DEV deploy role
├── QA Account
│   ├── QA EKS cluster
│   ├── QA namespaces
│   └── QA deploy role
└── PROD Account
    ├── PROD EKS cluster
    ├── PROD namespaces
    └── PROD deploy role
```

For production promotion, use separate source and target roles:

| Role | Account | Purpose |
| --- | --- | --- |
| `sourceRoleArn` | Non-prod | Reads approved image metadata, `image.json`, reports, and source ECR image. |
| `targetRoleArn` | Prod | Writes promoted image to production ECR and deploys to production cluster. |

Jenkins environment resolution should work as follows:

| Selected Environment | Jenkins Action |
| --- | --- |
| DEV | Assume DEV role, update kubeconfig for DEV cluster, deploy to DEV namespace. |
| QA | Assume QA role, update kubeconfig for QA cluster, deploy to QA namespace. |
| STAGE | Assume STAGE role, update kubeconfig for STAGE cluster, deploy to STAGE namespace. |
| PROD | Assume non-prod source role to read artifact/image, assume prod target role to promote/deploy. |

## Prerequisites

Local tools:

```bash
aws --version
kubectl version --client
helm version
terraform version
```

Terraform `>= 1.3.9` is required for the installer Terraform roots. The current Terraform path is pinned for Terraform 1.3.9 compatibility: AWS provider `~> 4.57.0`, VPC module `4.0.0`, and EKS module `19.21.0`. Namespace creation, EKS access entries, and ingress-nginx installation are handled through AWS CLI, `kubectl`, and Helm `local-exec` hooks so the environment root avoids a Kubernetes provider cycle during validation and planning.

AWS permissions:

1. Ability to call `sts:GetCallerIdentity`.
2. Ability to create or access EKS clusters.
3. Ability to create or access ECR repositories.
4. Ability to create or access S3 artifact buckets.
5. Ability to create or access IAM roles.
6. Ability to create or access DNS records/certificates if DNS is managed in AWS.

Kubernetes prerequisites:

1. Cluster admin access for initial install.
2. StorageClass available, preferably encrypted `gp3`.
3. Ingress controller available or enabled through installer.
4. Namespaces approved by client platform team.

## Client Values File

Copy one of the examples:

```bash
cp secure_SDLC_Platform/examples/client-values.yaml client-values.local.yaml
```

For a generic enterprise hybrid onboarding:

```bash
cp secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml client-values.local.yaml
```

For a Regeneron-style trial:

```bash
cp secure_SDLC_Platform/examples/regeneron-trial-values.yaml regeneron-trial.local.yaml
```

Never commit `*.local.yaml`.

For the detailed structure, see `docs/client-values-reference.md`. The important model change is that the installer seeds an admin-owned `environmentCatalog` and generic `identity.ldap.roleGroupMappings`. Developers should not type AWS account IDs, role ARNs, EKS cluster names, or S3 buckets during normal pipeline requests.

## Environment Catalog and Role Mapping

The Environment Catalog is the runtime source of truth for DEV, QA, STAGE, and PROD. It is stored in the Horizon platform namespace with the backend configuration and may later be edited by a platform admin through the admin UI. It points to the client application clusters, but it should not be duplicated in every application namespace.

Expected flow:

1. Client platform/admin team fills `environments` in `client-values.yaml`; in legacy/BYO mode they may fill `environmentCatalog.environments` directly.
2. Installer generates the runtime catalog payload from values and Terraform outputs.
3. Backend serves active environments to the UI.
4. Developer selects only `Target Environment`.
5. Backend resolves ECR, S3, IAM role, EKS cluster, namespace strategy, and notification values before triggering Jenkins.
6. Jenkins updates kubeconfig for the selected cluster and deploys into the resolved namespace.

Generic role mapping works the same way. A client can use any AD/LDAP group names; Horizon maps those groups to stable product roles such as `platform-admin`, `developer`, `qa`, `release-manager`, and `viewer`. Use full group DNs when possible.

Example:

```yaml
identity:
  mode: existing-ldap
  ldap:
    enabled: true
    groupBaseDn: ou=Groups,dc=client,dc=example
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

## Preflight Validation

Run preflight before provisioning or installing:

```bash
bash secure_SDLC_Platform/scripts/preflight.sh -f client-values.local.yaml --environment QA --dry-run --skip-aws
```

Preflight checks:

1. Required CLIs.
2. AWS caller identity.
3. Kubernetes access when BYO mode is selected.
4. Required values exist.
5. License sync endpoint is configured for online sync.
6. Identity mode is valid.
7. The generated Environment Catalog payload can be produced.
8. Existing resources marked `state: existing` are reachable.

## Trial Readiness Smoke Test

Before a real trial client uses the product, run the bundled non-destructive readiness smoke test. This wraps the important dry-run checks into one report:

```bash
bash secure_SDLC_Platform/scripts/trial-readiness.sh \
  -f client-values.local.yaml \
  --environment QA \
  --skip-aws
```

Use `--skip-aws` while editing values locally. Remove it when the client AWS profile is configured and the installer should validate existing S3, DynamoDB, ECR, EKS, and IAM resources:

```bash
export AWS_PROFILE=<client-platform-admin-profile>

bash secure_SDLC_Platform/scripts/trial-readiness.sh \
  -f client-values.local.yaml \
  --environment QA
```

The script writes evidence to:

```text
secure_SDLC_Platform/.generated/trial-readiness-qa.txt
```

Treat this file as the trial onboarding readiness artifact. It should be attached to the client onboarding ticket before moving from dry-run to real provisioning.

## Catalog Sync

Infrastructure provisioning and catalog publication are separate steps. The infrastructure phase creates or validates AWS and Kubernetes resources. The catalog phase publishes the selected environment to the running Horizon backend so the UI and pipeline API can resolve it server-side.

Dry-run the payload:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase catalog \
  --environment QA \
  -f client-values.local.yaml \
  --dry-run
```

Publish to the backend:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase catalog \
  --environment QA \
  -f client-values.local.yaml
```

By default, the endpoint is derived from `domain.platformHosts.frontendHost` and `domain.platformHosts.backendPath`, for example `https://horizonrelevance.com/pipeline/api/environment-catalog`. For a private endpoint, set `catalogSync.backendUrl`, `installer.backendUrl`, or `domain.platformHosts.backendUrl` in the values file. Use `CATALOG_SYNC_TOKEN` when the backend requires bearer-token automation access. TLS verification is enabled by default; enterprise clients should provide a trusted public certificate or `catalogSync.caBundlePath` for a private CA. Use `catalogSync.tlsVerify: false`, `--insecure-catalog-sync`, or `CATALOG_SYNC_INSECURE=true` only for internal demos.

## Mode 1: Full Platform Provisioning

Use this when the client only has AWS accounts and DNS.

1. Configure AWS profile for the target account.

```bash
export AWS_PROFILE=regeneron-devsecops-admin
```

2. Run preflight.

```bash
bash secure_SDLC_Platform/scripts/preflight.sh -f regeneron-trial.local.yaml
```

3. Provision infrastructure.

```bash
bash secure_SDLC_Platform/scripts/install.sh --phase infra -f regeneron-trial.local.yaml
```

4. Install Horizon platform.

```bash
bash secure_SDLC_Platform/scripts/install.sh --phase platform -f regeneron-trial.local.yaml
```

5. Publish Environment Catalog.

```bash
bash secure_SDLC_Platform/scripts/install.sh --phase catalog --environment QA -f regeneron-trial.local.yaml
```

6. Validate.

```bash
bash secure_SDLC_Platform/scripts/validate.sh -f regeneron-trial.local.yaml
```

## Mode 2: Bring Your Own Infrastructure

Use this when the client already has EKS, ECR, S3, IAM roles, DNS, LDAP, or IdP.

1. Fill the `existingInfrastructure` section in the values file.
2. Set:

```yaml
installer:
  mode: byo-infra
```

3. Run preflight.

```bash
bash secure_SDLC_Platform/scripts/preflight.sh -f client-values.local.yaml
```

4. Install platform only.

```bash
bash secure_SDLC_Platform/scripts/install.sh --phase platform -f client-values.local.yaml
```

5. Publish the existing environment mappings to the backend.

```bash
bash secure_SDLC_Platform/scripts/install.sh --phase catalog --environment QA -f client-values.local.yaml
```

## Mode 3: Hybrid Desired-State Provisioning

Use this when the client has a mixed estate, for example:

1. AWS accounts and DNS exist, but EKS/S3/ECR/IAM are missing.
2. S3 and ECR exist, but QA and STAGE clusters are missing.
3. DEV already exists, but QA/STAGE namespaces and EKS access entries need to be created.

Copy the generic desired-state file:

```bash
cp secure_SDLC_Platform/examples/client-hybrid-onboarding-values.yaml client-values.local.yaml
```

Each resource declares its lifecycle:

| State | Installer Behavior |
| --- | --- |
| `existing` | Validate only. Do not create or delete. |
| `provision` | Create/configure through Terraform or Helm. |
| `disabled` | Ignore. |

Each resource also declares a deletion policy:

| Deletion Policy | Installer Behavior |
| --- | --- |
| `retain` | Never destroy through the installer. |
| `delete` | May be destroyed only when the resource is provisioned and tracked in that environment Terraform state. |

Dry-run QA:

```bash
bash secure_SDLC_Platform/scripts/preflight.sh \
  -f client-values.local.yaml \
  --environment QA \
  --dry-run \
  --skip-aws

bash secure_SDLC_Platform/scripts/install.sh \
  --phase infra \
  -f client-values.local.yaml \
  --environment QA \
  --dry-run
```

Apply QA after client approval:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase infra \
  -f client-values.local.yaml \
  --environment QA \
  --auto-approve
```

Publish QA into the product Environment Catalog:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase catalog \
  -f client-values.local.yaml \
  --environment QA
```

## Terraform Remote State

Terraform state must remain inside the client boundary. The hybrid values file uses one client-owned S3 state bucket and one DynamoDB lock table, with separate keys per environment:

```text
horizon-installer/platform/terraform.tfstate
horizon-installer/dev/terraform.tfstate
horizon-installer/qa/terraform.tfstate
horizon-installer/stage/terraform.tfstate
horizon-installer/prod/terraform.tfstate
```

If the client already has a state backend, set:

```yaml
terraformState:
  state: existing
```

If Horizon should bootstrap the state backend during onboarding, set:

```yaml
terraformState:
  state: provision
```

Then run:

```bash
bash secure_SDLC_Platform/scripts/install.sh \
  --phase state \
  -f client-values.local.yaml \
  --auto-approve
```

The state bucket, lock table, and state KMS key should normally use `deletionPolicy: retain`.

## Destroy Workflow

Destroy is selected-environment only. It intentionally does not remove client-owned shared resources, existing IAM roles, or Terraform state.

Dry-run:

```bash
bash secure_SDLC_Platform/scripts/destroy.sh \
  -f client-values.local.yaml \
  --environment QA \
  --dry-run
```

Confirmed destroy:

```bash
bash secure_SDLC_Platform/scripts/destroy.sh \
  -f client-values.local.yaml \
  --environment QA \
  --confirm QA
```

The destroy script removes only resources that satisfy all of these conditions:

1. They are in the selected environment.
2. They are marked `state=provision`.
3. They are marked `deletionPolicy=delete`.
4. Terraform state proves the installer created them.

## Online License Sync

The preferred renewal model is online license sync.

Flow:

1. Horizon Relevance creates a client record in the Horizon license service.
2. Client installs the platform with `license.mode=online-sync`.
3. Backend calls the Horizon license endpoint using the client ID and activation token.
4. License service returns a signed license.
5. Backend stores the signed license in the client cluster secret.
6. Backend and Jenkins enforce entitlements before pipeline execution.

Example:

```yaml
license:
  mode: online-sync
  syncEndpoint: https://license.horizonrelevance.com/api/v1/licenses/sync
  clientId: regeneron-healthcare
  activationTokenSecretName: horizon-license-activation
  renewalCheckHours: 24
```

For restricted networks, allow outbound HTTPS only to the Horizon license endpoint. If the client cannot permit outbound license sync, use offline license file mode as a fallback.

For a local 2-day trial demonstration, run the included Horizon-owned license server skeleton:

```bash
cd secure_SDLC_Platform/license-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export LICENSE_SIGNING_SECRET='change-me-demo-secret'
export REGENERON_ACTIVATION_TOKEN='regeneron-demo-token'
uvicorn app:app --host 0.0.0.0 --port 8090
```

Then configure the client backend with:

```yaml
enterprise:
  licenseEnforcementEnabled: true
  licenseMode: online-sync
  licenseSyncEndpoint: http://horizon-license-server.horizon-platform.svc.cluster.local:8090/api/v1/licenses/sync
  signingSecret:
    existingSecret: horizon-license-signing
    key: ENTERPRISE_LICENSE_SIGNING_SECRET
  activationTokenSecret:
    existingSecret: horizon-license-activation
    key: ENTERPRISE_LICENSE_ACTIVATION_TOKEN
```

In production, the license server should be deployed by Horizon Relevance, not by the client, and should use HTTPS, audit logs, token hashing, rate limits, and KMS/HSM-backed signing.

## Identity Configuration

Supported identity modes:

| Mode | Description |
| --- | --- |
| `client-oidc` | Preferred enterprise model for Okta, Azure AD, Ping, or another OIDC provider. |
| `client-saml` | Enterprise SAML federation through Keycloak or direct product integration. |
| `existing-ldap` | Keycloak federates to client LDAP/AD. |
| `managed-keycloak-openldap` | Installer deploys Keycloak and OpenLDAP for trial or lab use. |

Enterprise recommendation:

1. Use client IdP for production.
2. Use Keycloak as broker only when it simplifies product integration.
3. Use OpenLDAP only for trial/lab or when client explicitly requires LDAP.
4. Keep client-specific group names in `identity.ldap.roleGroupMappings`; the product should expose generic roles, not raw client group names.

## Validation

After install, validate:

1. Frontend URL loads.
2. Backend health endpoint returns OK.
3. License status returns active.
4. Jenkins login works.
5. Identity login works for a non-admin user.
6. Platform admin sees Client, Environment Catalog, and License pages; developer/QA users do not.
7. `GET /pipeline/api/environment-catalog` returns active environments from the values file.
8. ECR push permission works.
9. S3 artifact upload works.
10. Devops Pipeline can create a Jenkins job.
11. Test Devops Pipeline can publish findings/report evidence.

Run:

```bash
bash secure_SDLC_Platform/scripts/validate.sh -f client-values.local.yaml
```

## Upgrade and Renewal

Upgrade product release:

```bash
bash secure_SDLC_Platform/scripts/install.sh --phase platform -f client-values.local.yaml
```

Online license renewal:

1. Horizon updates subscription in the license service.
2. Client clicks **License > Sync License** in the product or waits for scheduled sync.
3. Backend receives renewed signed license.
4. New entitlements apply without changing client source code.

Offline fallback:

1. Horizon sends signed license file.
2. Client applies Kubernetes secret.
3. Restart backend if required.

## Troubleshooting

| Problem | Likely Cause | Action |
| --- | --- | --- |
| License invalid | Wrong client ID, expired token, network blocked, or signature mismatch. | Validate license endpoint reachability and activation token. |
| Jenkins cannot deploy | Role ARN lacks EKS/ECR/S3 permissions. | Check environment role mapping and AWS STS caller identity. |
| Frontend loads but backend fails | Ingress path or backend URL mismatch. | Validate `domain.backendPath` and ingress rules. |
| LDAP login fails | Bind DN, base DN, TLS, or group filter mismatch. | Test LDAP bind from Keycloak pod. |
| User can log in but sees wrong menus | LDAP/AD group did not map to a product role. | Check `LDAP_ROLE_GROUP_MAPPINGS`, group DNs/CNs, and backend `/me` response. |
| Developer form still needs AWS fields | Environment Catalog was not synced to the backend or frontend is using an older release. | Run `install.sh --phase catalog --environment <ENV>`, validate backend `/environment-catalog`, and confirm frontend `1.4.19` or newer. |
| ECR push fails | Missing auth, wrong account ID, or repository does not exist. | Validate `aws ecr get-login-password` and repository mapping. |
