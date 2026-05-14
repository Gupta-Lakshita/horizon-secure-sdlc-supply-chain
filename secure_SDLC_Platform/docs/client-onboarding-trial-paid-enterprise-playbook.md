# Client Onboarding Playbook: Trial, Paid, and Enterprise

## Table of Contents

1. [Purpose](#purpose)
2. [Recommended Client-Hosted Model](#recommended-client-hosted-model)
3. [Regeneron Trial Example](#regeneron-trial-example)
4. [Onboarding Phases](#onboarding-phases)
5. [Infrastructure Provisioning in the Client AWS Account](#infrastructure-provisioning-in-the-client-aws-account)
6. [Mapping Existing Client Infrastructure](#mapping-existing-client-infrastructure)
7. [Identity: Client IdP, Keycloak, and OpenLDAP](#identity-client-idp-keycloak-and-openldap)
8. [License Registration and Enforcement](#license-registration-and-enforcement)
9. [Trial, Paid, and Enterprise Packaging](#trial-paid-and-enterprise-packaging)
10. [Container Images, GitHub Access, and Source Protection](#container-images-github-access-and-source-protection)
11. [Commercial Model and Profit Strategy](#commercial-model-and-profit-strategy)
12. [Operational Runbook](#operational-runbook)
13. [Trial Success Criteria](#trial-success-criteria)
14. [Handoff Checklist](#handoff-checklist)

## Purpose

This document explains how Horizon Relevance should onboard a regulated enterprise client such as Regeneron onto the Horizon AI DevSecOps platform for a trial, paid subscription, or enterprise deployment.

The main business principle is simple: the client owns the cloud account, source code, runtime data, artifacts, images, logs, and identity system. Horizon Relevance provides the product, installer, container images, Jenkins library, policy packs, license, and implementation support.

For healthcare and pharma clients, this client-hosted model is the strongest positioning because it avoids asking the client to send proprietary source code, patient-adjacent data, regulated artifacts, or production credentials into Horizon Relevance-owned infrastructure.

## Recommended Client-Hosted Model

For Regeneron, use a client-hosted trial inside Regeneron's AWS account.

The platform should be installed into a dedicated AWS account or a dedicated EKS cluster within an approved non-production account. Client applications are then built, scanned, tested, and deployed from inside the client network boundary.

Recommended ownership:

| Area | Owner | Notes |
| --- | --- | --- |
| AWS account | Client | Regeneron owns billing, CloudTrail, IAM, VPCs, data, and artifacts. |
| EKS clusters | Client | Horizon installer can create clusters if they do not exist. |
| ECR repositories | Client | Built application images stay in client ECR. |
| S3 artifact buckets | Client | Test reports, `image.json`, and deployment metadata stay in client S3. |
| Source code access | Client | GitHub/GitLab/Bitbucket access is read-only and scoped. |
| Horizon product images | Horizon | Delivered through private registry access or mirrored into client ECR. |
| Jenkins shared library | Horizon | Delivered through private GitHub deploy key, release bundle, or packaged image. |
| License | Horizon | Signed license controls trial/paid/enterprise entitlements. |
| Identity | Client preferred | Use client IdP first; deploy Keycloak/OpenLDAP only if needed. |

## Regeneron Trial Example

Example trial registration:

| Field | Example |
| --- | --- |
| Client name | Regeneron |
| Industry | Healthcare / Pharma |
| Client ID | `regeneron-healthcare` |
| Trial duration | 30 days |
| Users | 10 |
| Repositories | 3 |
| Monthly builds | 100 |
| Allowed environments | DEV, QA |
| Pipelines | Devops Pipeline, Test Devops Pipeline |
| Excluded by default | Production deployment, custom policy authoring, unlimited usage |
| Success criteria | Build image, push to client ECR, deploy to DEV/QA, run quality/security tests, show findings dashboard and reports. |

## Onboarding Phases

### Phase 0: Commercial and Governance Intake

Before touching the client AWS account, complete the business and security intake:

1. Execute NDA, trial agreement, and healthcare-specific legal review if needed.
2. Confirm whether a BAA, DPA, or vendor risk assessment is required.
3. Define trial scope: repositories, application types, environments, test suites, and trial duration.
4. Identify client stakeholders: platform owner, security owner, application owner, IAM owner, networking owner, and procurement contact.
5. Confirm success criteria and demo timeline.

### Phase 1: AWS Readiness Assessment

Collect these inputs from the client:

| Input | Required? | Example |
| --- | --- | --- |
| AWS account ID | Yes | `123456789012` |
| Region | Yes | `us-east-1` |
| VPC/subnets | Yes | Existing or provisioned by installer |
| EKS clusters | Optional | DEV, QA, STAGE, PROD |
| DNS zone | Optional | `devsecops.regeneron.example` |
| TLS certificate | Optional | ACM certificate or cert-manager |
| ECR repository policy | Yes | Client-owned repository |
| S3 artifact bucket | Yes | `regeneron-devsecops-artifacts` |
| SNS/SES/email provider | Optional | For notifications |
| Git provider | Yes | GitHub Enterprise, GitLab, Bitbucket |
| Identity provider | Preferred | Okta, Azure AD, Ping, LDAP, or Keycloak |
| Security requirements | Yes | Data retention, encryption, audit, network restrictions |

### Phase 2: Provision or Map Infrastructure

If the client does not have the required platform, Horizon Relevance provides a Terraform and Helm-based installation path.

If the client already has infrastructure, Horizon maps the product to the client-provided clusters, namespaces, IAM roles, DNS, and identity provider.

### Phase 3: Install Horizon Platform

Install these platform components:

1. Horizon frontend.
2. Horizon backend.
3. Jenkins controller and agents.
4. Jenkins shared library.
5. Findings dashboard storage/integration.
6. Optional SonarQube if the client does not already have one.
7. Keycloak and OpenLDAP only when the client does not provide an IdP.
8. Ingress controller, TLS, DNS, and secrets.

### Phase 4: Register License and Validate Entitlements

Register the client license in the backend and Jenkins execution path. The backend validates the license before creating Jenkins jobs. Jenkins validates the same entitlement again before executing build, test, scan, deployment, and promotion logic.

### Phase 5: Trial Execution and Conversion

Run a controlled trial:

1. Onboard one Angular, Spring Boot, or Node.js application.
2. Build and push image into client ECR.
3. Deploy to DEV and QA namespaces.
4. Run Test Devops Pipeline capabilities.
5. Review findings dashboard and S3 reports.
6. Review operational handoff and support needs.
7. Convert to paid or enterprise license.

## Infrastructure Provisioning in the Client AWS Account

If Regeneron has an AWS account but no platform, provision the minimum non-production foundation below.

### Required AWS Services

| Service | Purpose |
| --- | --- |
| VPC | Private network boundary for platform and workloads. |
| EKS | Runs Horizon frontend, backend, Jenkins, identity components, and test workloads. |
| EBS CSI Driver | Persistent storage for Jenkins, LDAP, Keycloak, and SonarQube if installed. |
| ECR | Stores client application container images and optionally mirrored Horizon images. |
| S3 | Stores pipeline artifacts, reports, metadata, and test evidence. |
| KMS | Encrypts EBS, S3, Secrets Manager, and ECR. |
| IAM / IRSA | Grants least-privilege AWS access to platform pods. |
| Secrets Manager | Stores integration secrets, Git tokens, SMTP, and product secrets. |
| SNS or SES | Sends build/test/finding notifications. |
| Route 53 / DNS | Publishes frontend, Jenkins, Keycloak, and optional SonarQube URLs. |
| ACM | TLS certificates for ingress endpoints. |

### Baseline Kubernetes Namespaces

Use separate namespaces for platform and client workloads:

```text
horizon-platform
horizon-identity
horizon-observability
regeneron-app-dev
regeneron-app-qa
regeneron-app-stage
```

For multi-tenant EKS, each application should deploy to its own namespace. Example:

```text
regeneron-patient-portal-dev
regeneron-patient-portal-qa
regeneron-claims-api-dev
regeneron-claims-api-qa
```

### Provisioning Steps

1. Configure AWS access to the client account.

```bash
aws sts get-caller-identity
aws eks update-kubeconfig --region us-east-1 --name regeneron-devsecops-eks
```

2. Create platform namespaces.

```bash
kubectl create namespace horizon-platform
kubectl create namespace horizon-identity
kubectl create namespace horizon-observability
```

3. Install EBS CSI Driver and `gp3` StorageClass if the cluster does not already have persistent storage.

4. Create client artifact bucket and ECR repositories.

```bash
aws s3 mb s3://regeneron-devsecops-artifacts --region us-east-1
aws ecr create-repository --repository-name regeneron-devsecops/apps --region us-east-1
```

5. Create IAM roles for platform execution.

Recommended role separation:

| Role | Purpose |
| --- | --- |
| `HorizonPlatformExecutionRole` | Backend/Jenkins AWS operations in non-production. |
| `HorizonDevDeployRole` | Deploy to DEV cluster/namespace. |
| `HorizonQaDeployRole` | Deploy to QA cluster/namespace. |
| `HorizonProdPromotionSourceRole` | Read trusted non-production image/artifacts. |
| `HorizonProdPromotionTargetRole` | Promote into production ECR/deployment target. |

6. Mirror or authorize Horizon images.

Preferred enterprise approach is to pull from the private Horizon release registry or let Horizon mirror the approved release into the client ECR during onboarding.

```bash
aws ecr get-login-password --region us-east-1   | docker login --username AWS --password-stdin 426946630837.dkr.ecr.us-east-1.amazonaws.com

# Example: mirror backend release from Horizon ECR into client ECR.
docker pull 426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/backend:1.4.24
docker tag 426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/backend:1.4.24   <client-account>.dkr.ecr.us-east-1.amazonaws.com/horizon/backend:1.4.24
docker push <client-account>.dkr.ecr.us-east-1.amazonaws.com/horizon/backend:1.4.24
```

Repeat for frontend, Jenkins, scanner images, and required utility images. For regulated clients, pin deployments by image digest in the approved release record.

7. Install Horizon with Helm values.

Use a client-specific values file based on `examples/client-values.yaml`.

```bash
helm upgrade --install horizon-ai-devsecops ./charts/horizon-ai-devsecops \
  --namespace horizon-platform \
  --values regeneron-client-values.yaml
```

## Mapping Existing Client Infrastructure

If Regeneron already has EKS, ECR, S3, DNS, and identity services, do not recreate them. Map Horizon to existing assets.

Collect this mapping:

| Horizon Field | Client Mapping |
| --- | --- |
| DEV cluster | Existing Regeneron DEV EKS cluster name |
| QA cluster | Existing Regeneron QA EKS cluster name |
| STAGE cluster | Existing Regeneron STAGE EKS cluster name |
| PROD cluster | Existing Regeneron PROD EKS cluster name |
| Namespace strategy | Per application, per environment |
| ECR repository | Existing approved ECR repository |
| Artifact bucket | Existing encrypted S3 bucket |
| Notification topic | Existing SNS topic or SES configuration |
| Source role ARN | Role used to read non-production image/artifacts |
| Target role ARN | Role used to promote/deploy into target account |
| Ingress | Existing ingress class and certificate |
| IdP | Existing Okta/Azure AD/Ping/LDAP integration |

For enterprise production promotion, use separate `sourceRoleArn` and `targetRoleArn`. This avoids over-permissioning a single role and lets the client maintain strict non-production and production account separation.

## Identity: Client IdP, Keycloak, and OpenLDAP

### Preferred Enterprise Model: Client IdP

For healthcare and pharma clients, use the client identity provider as the source of truth.

Recommended options:

1. Connect Horizon frontend/backend to Keycloak, and federate Keycloak to Okta/Azure AD/Ping using OIDC or SAML.
2. Connect Jenkins and SonarQube to the same IdP through OIDC/SAML.
3. Map client AD/LDAP groups to generic Horizon product roles in `identity.ldap.roleGroupMappings`. The product roles are `platform-admin`, `developer`, `qa`, `release-manager`, and `viewer`.

This gives the client MFA, password policy, access reviews, audit, and offboarding through their normal enterprise process while allowing each client to keep its own group naming convention.

### Trial Lab Model: Keycloak and OpenLDAP

If the client has no identity service ready for the trial, deploy Keycloak and OpenLDAP as temporary trial identity components.

Use this only for trial or lab environments unless the client explicitly wants an LDAP-backed production setup.

Flow:

1. Deploy OpenLDAP.
2. Deploy Keycloak.
3. Federate Keycloak to OpenLDAP.
4. Configure self-service password reset.
5. Create trial users and groups.
6. Integrate Horizon UI, Jenkins, and SonarQube with Keycloak.

### Existing LDAP Model

If the client already has LDAP or Active Directory:

1. Connect Keycloak to the client LDAP/AD.
2. Use read-only federation where possible.
3. Keep password changes inside the client's identity system.
4. Do not store client employee passwords in Horizon-managed OpenLDAP.

## License Registration and Enforcement

### License Generation

Horizon Relevance should operate an internal license issuer. For each trial or paid customer, the issuer generates a signed license document.

Example license payload:

```json
{
  "client_id": "regeneron-healthcare",
  "client_name": "Regeneron",
  "industry": "healthcare-pharma",
  "license_type": "trial",
  "issued_at": "2026-05-08T00:00:00Z",
  "expires_at": "2026-06-07T23:59:59Z",
  "enabled_pipelines": [
    "Devops Pipeline",
    "Test Devops Pipeline"
  ],
  "enabled_features": [
    "build",
    "artifact_publish",
    "code_scan",
    "image_scan",
    "policy_validation",
    "static_application_security",
    "test_suites",
    "notifications"
  ],
  "allowed_environments": [
    "DEV",
    "QA"
  ],
  "limits": {
    "max_repos": 3,
    "max_users": 10,
    "max_builds_per_month": 100
  }
}
```

The license should be signed by Horizon Relevance using a private signing key. The backend and Jenkins should validate the signature using the configured public key or shared signing secret.

### License Installation

Store license details in Kubernetes Secret or inject them through Helm values:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: horizon-enterprise-license
  namespace: horizon-platform
type: Opaque
stringData:
  ENTERPRISE_LICENSE_ENFORCEMENT_ENABLED: "true"
  ENTERPRISE_CLIENT_ID: "regeneron-healthcare"
  ENTERPRISE_CLIENT_NAME: "Regeneron"
  ENTERPRISE_LICENSE_TYPE: "trial"
  ENTERPRISE_LICENSE_EXPIRES_AT: "2026-06-07T23:59:59Z"
  ENTERPRISE_ENABLED_PIPELINES: "Devops Pipeline,Test Devops Pipeline"
  ENTERPRISE_ENABLED_FEATURES: "build,artifact_publish,code_scan,image_scan,policy_validation,static_application_security,test_suites,notifications"
  ENTERPRISE_ALLOWED_ENVIRONMENTS: "DEV,QA"
  ENTERPRISE_MAX_REPOS: "3"
  ENTERPRISE_MAX_BUILDS_PER_MONTH: "100"
  ENTERPRISE_MAX_USERS: "10"
  ENTERPRISE_LICENSE_KEY: "<issued-license-key>"
  ENTERPRISE_LICENSE_SIGNING_SECRET: "<signing-secret-or-public-validation-material>"
```

### Enforcement Points

| Enforcement Point | Behavior |
| --- | --- |
| Backend license API | Shows active, expiring, invalid, or expired status. |
| Backend pipeline trigger | Blocks unlicensed pipeline creation. |
| Jenkins start stage | Blocks unlicensed execution even if a request bypasses frontend. |
| Feature gates | Enables or blocks build, scan, test, notification, and production deployment features. |
| Environment gates | Prevents trial users from deploying to STAGE/PROD if not entitled. |
| Usage counters | Enforces max users, repos, and monthly builds after database-backed counters are enabled. |

### Demo-Safe License Handling

For live demos, issue a license with enough runway to avoid expiration mid-demo. Recommended:

1. Use a 30-day trial license.
2. Add a 3-day grace period in the backend display only if commercial policy allows it.
3. Show "Trial expires in X days" in the License page.
4. Do not let an expired license create a confusing runtime failure. Fail early with a clear license status before Jenkins starts.

## Trial, Paid, and Enterprise Packaging

### Trial

Trial is designed to prove value quickly with controlled limits.

Included:

1. Devops Pipeline for build, image publish, artifact publish, and DEV/QA deploy.
2. Test Devops Pipeline for code quality, API regression, UI test, performance smoke, container/IaC vulnerability, and policy validation.
3. Findings dashboard.
4. S3 report publishing.
5. Email/SNS notifications.
6. Limited support window.

Limited:

1. No production deployment by default.
2. Limited repos, users, builds, and environments.
3. Standard policy pack only.
4. Standard Helm/Terraform installer.

### Paid Professional

Paid Professional is for teams ready to use the product beyond POC.

Included:

1. More repositories, users, and monthly builds.
2. DEV/QA/STAGE support.
3. Optional production deployment add-on.
4. Private support channel.
5. Upgrade assistance.
6. Standard compliance reporting.

### Enterprise

Enterprise is for regulated clients with production workloads.

Included:

1. Production deployment pipeline.
2. Separate source and target AWS role support.
3. Private image registry mirroring.
4. Client IdP integration.
5. Custom policy packs.
6. Advanced audit and evidence retention.
7. HA/DR architecture.
8. Premium support SLA.
9. Custom integrations with ServiceNow, Jira, Splunk, Datadog, or enterprise GRC tooling.
10. AI remediation and executive reporting add-ons.

## Container Images, GitHub Access, and Source Protection

Do not give trial clients broad access to all Horizon source code and DockerHub repositories.

Recommended delivery approach:

1. Keep Horizon source code private.
2. Publish release images to private repositories.
3. Use expiring registry credentials for trial customers.
4. Prefer mirroring Horizon product images into the client's ECR during onboarding.
5. Provide Helm charts, Terraform modules, and signed release bundles.
6. Provide Jenkins shared library as one of:
   - Private GitHub repository with read-only deploy key.
   - Versioned release bundle.
   - Packaged inside the Jenkins controller image.
7. Sign product images and provide SBOMs for enterprise customers.
8. Rotate or revoke trial registry credentials when the trial ends.

Client source code remains inside the client build environment. Horizon Relevance should not need to clone or store client source outside the client AWS account.

## Commercial Model and Profit Strategy

Horizon Relevance can monetize the product through multiple layers.

| Revenue Stream | Description |
| --- | --- |
| Trial conversion | 14-30 day paid-convertible POC. |
| Annual subscription | Charged by client, account, cluster, repo count, user count, or build volume. |
| Enterprise license | Larger contract with production deployment, HA, custom policies, and premium support. |
| Implementation services | One-time setup fee for AWS, identity, network, and pipeline onboarding. |
| Managed upgrade package | Quarterly upgrade, CVE patching, policy updates, and release certification. |
| Compliance packs | Healthcare, fintech, telecom, SOC2, HIPAA-aligned evidence packs. |
| AI remediation add-on | Automated remediation guidance, developer fix recommendations, and executive summaries. |
| Premium support | SLA-based support channel with named technical account manager. |
| Private marketplace | AWS Marketplace private offer for procurement-friendly purchase. |

Recommended commercial packaging:

| Tier | Best For | Pricing Lever |
| --- | --- | --- |
| Trial | POC and technical validation | Free or low-cost, time-limited |
| Professional | One business unit | Repos, users, builds/month |
| Enterprise | Regulated multi-team usage | Annual platform license plus support |
| Enterprise Plus | Large strategic clients | Custom contract, SLA, compliance, managed services |

## Operational Runbook

### Step 1: Register Client Internally

Create a client record:

```text
client_id: regeneron-healthcare
client_name: Regeneron
industry: healthcare-pharma
license_type: trial
trial_start: 2026-05-08
trial_end: 2026-06-07
primary_contact: <client-contact>
technical_contact: <client-platform-owner>
```

### Step 2: Generate Trial License

Use the internal Horizon license issuer to create a signed license.

Store:

1. License payload.
2. Signature.
3. Expiration date.
4. Entitlements.
5. Client environment mapping.

### Step 3: Prepare Client AWS

If no infrastructure exists, run the Horizon enterprise installer Terraform modules or client-approved Terraform.

Minimum setup:

1. VPC and subnets.
2. EKS cluster.
3. EBS CSI Driver.
4. Ingress controller.
5. ECR repositories.
6. S3 artifact bucket.
7. KMS keys.
8. IAM roles.
9. DNS and TLS.

### Step 4: Install Product

Create a `regeneron-client-values.yaml` based on the example:

```yaml
client:
  id: regeneron-healthcare
  name: "Regeneron"
  industry: healthcare-pharma

domain:
  baseDomain: devsecops.regeneron.example
  frontendHost: horizon.devsecops.regeneron.example
  backendPath: /pipeline/api
  jenkinsHost: jenkins.devsecops.regeneron.example
  keycloakHost: keycloak.devsecops.regeneron.example

environmentCatalog:
  environments:
    - name: DEV
      accountTier: nonprod
      awsAccountId: "111111111111"
      awsRegion: us-east-1
      ecrRegistry: 111111111111.dkr.ecr.us-east-1.amazonaws.com
      ecrRepositoryTemplate: regeneron-devsecops/${projectName}
      artifactBucket: regeneron-devsecops-artifacts
      clientAwsRoleArn: arn:aws:iam::111111111111:role/HorizonDevDeployRole
      clusterName: regeneron-dev-eks
      namespaceStrategy: per-app
      namespaceTemplate: ${clientId}-${projectName}-dev
      isActive: true
    - name: QA
      accountTier: nonprod
      awsAccountId: "111111111111"
      awsRegion: us-east-1
      ecrRegistry: 111111111111.dkr.ecr.us-east-1.amazonaws.com
      ecrRepositoryTemplate: regeneron-devsecops/${projectName}
      artifactBucket: regeneron-devsecops-artifacts
      clientAwsRoleArn: arn:aws:iam::111111111111:role/HorizonQaDeployRole
      clusterName: regeneron-qa-eks
      namespaceStrategy: per-app
      namespaceTemplate: ${clientId}-${projectName}-qa
      isActive: true

identity:
  mode: existing-ldap
  ldap:
    enabled: true
    host: ldaps://ldap.regeneron.example:636
    baseDn: dc=regeneron,dc=example
    groupBaseDn: ou=Security Groups,dc=regeneron,dc=example
    roleGroupMappings:
      platform-admin:
        - CN=REGN-Horizon-Platform-Admins,OU=Security Groups,DC=regeneron,DC=example
      developer:
        - CN=REGN-Application-Developers,OU=Security Groups,DC=regeneron,DC=example
      qa:
        - CN=REGN-QA-Automation,OU=Security Groups,DC=regeneron,DC=example
      release-manager:
        - CN=REGN-Release-Managers,OU=Security Groups,DC=regeneron,DC=example
      viewer:
        - CN=REGN-Security-Auditors,OU=Security Groups,DC=regeneron,DC=example

license:
  enforcementEnabled: true
  mode: online-sync
  clientId: regeneron-healthcare
  allowedEnvironments:
    - DEV
    - QA
```

Install:

```bash
helm upgrade --install horizon-ai-devsecops ./charts/horizon-ai-devsecops \
  --namespace horizon-platform \
  --values regeneron-client-values.yaml
```

### Step 5: Configure Identity

Preferred:

1. Connect Keycloak to client Okta/Azure AD/Ping.
2. Configure OIDC/SAML.
3. Map client groups to generic Horizon roles through `identity.ldap.roleGroupMappings`.
4. Enforce MFA through the client IdP.

Fallback trial:

1. Deploy Keycloak and OpenLDAP.
2. Create trial users.
3. Enable self-service password change.
4. Remove trial users at the end of the POC.

### Step 6: Configure Pipelines

Register:

1. Git provider credentials.
2. ECR repository.
3. S3 artifact bucket.
4. Environment Catalog entries for DEV/QA/STAGE/PROD cluster, namespace, ECR, S3, and role mapping.
5. Notification settings.
6. Optional SonarQube endpoint.
7. Test suite paths or default framework paths.

### Step 7: Run Trial Workloads

For each selected application:

1. Run Devops Pipeline.
2. Confirm image is pushed to client ECR.
3. Confirm `image.json` and metadata are stored in S3.
4. Confirm deployment to DEV or QA namespace.
5. Run Test Devops Pipeline.
6. Confirm findings dashboard and reports are available.
7. Review remediation recommendations.

### Step 8: Trial Closeout

At trial end:

1. Export trial results and executive summary.
2. Review success criteria.
3. Convert license to paid or enterprise.
4. Rotate trial credentials.
5. Remove temporary users.
6. Extend or decommission platform depending on commercial decision.

## Trial Success Criteria

A Regeneron trial should be considered successful if:

1. The platform installs in Regeneron's AWS account without source code leaving the account.
2. At least one application builds successfully.
3. The application image is pushed to Regeneron-owned ECR.
4. Deployment succeeds in DEV or QA.
5. Test Devops Pipeline produces meaningful test evidence.
6. Vulnerability and policy findings appear in the Horizon dashboard without exposing internal scanner tool names.
7. Reports are stored in Regeneron-owned S3.
8. Identity, audit, and access controls meet the client's security expectations.
9. Client stakeholders understand how to convert to paid production use.

## Handoff Checklist

Use this checklist before declaring onboarding complete.

| Item | Status |
| --- | --- |
| Trial or paid agreement signed | Pending |
| Client ID created | Pending |
| License generated and installed | Pending |
| AWS account access validated | Pending |
| EKS cluster provisioned or mapped | Pending |
| ECR repositories created or mapped | Pending |
| S3 artifact bucket created or mapped | Pending |
| IAM roles configured with least privilege | Pending |
| DNS and TLS configured | Pending |
| Horizon frontend reachable | Pending |
| Backend health endpoint reachable | Pending |
| Jenkins reachable and integrated | Pending |
| Identity configured | Pending |
| License status shows active | Pending |
| Devops Pipeline validated | Pending |
| Test Devops Pipeline validated | Pending |
| Findings dashboard validated | Pending |
| Reports visible in S3 | Pending |
| Trial closeout date scheduled | Pending |
