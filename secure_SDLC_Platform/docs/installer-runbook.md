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
9. [Mode 1: Full Platform Provisioning](#mode-1-full-platform-provisioning)
10. [Mode 2: Bring Your Own Infrastructure](#mode-2-bring-your-own-infrastructure)
11. [Online License Sync](#online-license-sync)
12. [Identity Configuration](#identity-configuration)
13. [Validation](#validation)
14. [Upgrade and Renewal](#upgrade-and-renewal)
15. [Troubleshooting](#troubleshooting)

## Purpose

The Horizon Relevance Enterprise Installer is the client-hosted packaging layer for the Horizon Relevance AI DevSecOps Platform. It gives enterprise clients a repeatable way to install the product into their AWS accounts without sending source code, artifacts, credentials, or regulated data to Horizon Relevance-managed infrastructure.

The installer supports two installation modes:

1. **Full Platform Provisioning**: Horizon provisions the required AWS and Kubernetes foundation for the client.
2. **Bring Your Own Infrastructure**: Horizon maps the product to existing client EKS, ECR, S3, IAM, DNS, and identity services.

## Installer Modes

| Mode | Use When | What Installer Does |
| --- | --- | --- |
| `full-provision` | Client has AWS accounts and DNS but no platform foundation. | Creates or configures VPC, EKS, ECR, S3, IAM roles, storage, ingress, platform namespace, and product components. |
| `byo-infra` | Client already has EKS, ECR, S3, DNS, IAM, identity, or security tooling. | Validates and maps existing infrastructure, then installs Horizon product components only. |

The same `client-values.yaml` contract drives both modes.

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

For a Regeneron-style trial:

```bash
cp secure_SDLC_Platform/examples/regeneron-trial-values.yaml regeneron-trial.local.yaml
```

Never commit `*.local.yaml`.

For the detailed structure, see `docs/client-values-reference.md`. The important model change is that the installer seeds an admin-owned `environmentCatalog` and generic `identity.ldap.roleGroupMappings`. Developers should not type AWS account IDs, role ARNs, EKS cluster names, or S3 buckets during normal pipeline requests.

## Environment Catalog and Role Mapping

The Environment Catalog is the runtime source of truth for DEV, QA, STAGE, and PROD. It is stored in the Horizon platform namespace with the backend configuration and may later be edited by a platform admin through the admin UI. It points to the client application clusters, but it should not be duplicated in every application namespace.

Expected flow:

1. Client platform/admin team fills `environmentCatalog.environments` in `client-values.yaml`.
2. Installer writes the catalog into the platform config.
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
bash secure_SDLC_Platform/scripts/preflight.sh -f regeneron-trial.local.yaml
```

Preflight checks:

1. Required CLIs.
2. AWS caller identity.
3. Kubernetes access when BYO mode is selected.
4. Required values exist.
5. License sync endpoint is configured for online sync.
6. Identity mode is valid.

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

5. Validate.

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
| Developer form still needs AWS fields | Environment Catalog was not seeded or frontend is using an older release. | Validate `ENVIRONMENT_CATALOG_JSON`, backend `/environment-catalog`, and frontend `1.4.19` or newer. |
| ECR push fails | Missing auth, wrong account ID, or repository does not exist. | Validate `aws ecr get-login-password` and repository mapping. |
