# Secure Product Distribution Runbook

## Purpose

This runbook explains how Horizon Relevance distributes product images and protected runtime assets to trial, paid, and enterprise clients without exposing product control-plane secrets or broad source-level intellectual property.

## Distribution Model

Horizon Relevance publishes approved release images into Horizon-owned private ECR. A licensed client receives time-bound pull access to only the repositories required by its subscription. The client-hosted platform then pulls those images directly or mirrors them into a client-owned ECR repository for stricter change control.

The recommended enterprise model is:

1. Horizon publishes signed product images to private ECR.
2. Horizon grants the client pull access through an ECR repository policy.
3. Client mirrors images into its account when required by governance.
4. Client deploys by immutable tag or digest through Helm.
5. Backend license enforcement controls what pipelines and environments may run.
6. Jenkins validates license entitlement again before execution.

## Current Product Images

| Component | Horizon private ECR image |
| --- | --- |
| Frontend | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/frontend:1.4.27` |
| Backend | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/backend:1.4.35` |
| Thin runner | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/runner:0.1.0` |
| License service | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/license-management-service:0.1.9` |
| Jenkins | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/jenkins:1.0.8` |
| SonarQube mirror | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/sonarqube:10.4-community` |
| Container/IaC scanner | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/trivy-scanner:1.1.2` |
| Policy validation service | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/opa-scanner:1.0.1` |
| Self-service password | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/self-service-password:1.7.3-ltb` |

## Grant Client Pull Access

For the early enterprise model, maintain a licensed client AWS account allowlist and render the same read-only repository policy for each approved Horizon product repository. Grant account roots in the Horizon ECR repository policy, then let each client account restrict pull/copy access to its own EKS node role, import role, or image pull role.

```bash
bash secure_SDLC_Platform/scripts/render-ecr-pull-policy.sh \
  --client-account-id 921570400913 \
  --client-account-id <another-licensed-client-account-id> \
  --repository horizon/backend \
  --expires-at 2026-06-30T23:59:59Z \
  --output /tmp/horizon-backend-ecr-policy.json
```

Apply the policy to each approved Horizon ECR repository:

```bash
aws ecr set-repository-policy \
  --region us-east-1 \
  --repository-name horizon/backend \
  --policy-text file:///tmp/horizon-backend-ecr-policy.json
```

Apply the policy to all approved repositories:

```bash
for repo in \
  horizon/frontend \
  horizon/backend \
  horizon/runner \
  horizon/jenkins \
  horizon/sonarqube \
  horizon/trivy-scanner \
  horizon/opa-scanner \
  horizon/self-service-password; do
  bash secure_SDLC_Platform/scripts/render-ecr-pull-policy.sh \
    --client-account-id 921570400913 \
    --repository "${repo}" \
    --output "/tmp/${repo//\//-}-pull-policy.json"

  aws ecr set-repository-policy \
    --region us-east-1 \
    --repository-name "${repo}" \
    --policy-text "file:///tmp/${repo//\//-}-pull-policy.json"
done
```

The client pull role still needs identity permission for `ecr:GetAuthorizationToken`; do not put that action in the Horizon repository policy.

## Verify Release Images

Render the expected release manifest:

```bash
bash secure_SDLC_Platform/scripts/verify-product-images.sh
```

Verify the tags exist in Horizon ECR:

```bash
bash secure_SDLC_Platform/scripts/verify-product-images.sh --online
```

## Generate SBOM and Signatures

Generate SBOM evidence:

```bash
bash secure_SDLC_Platform/scripts/generate-product-sbom.sh
```

Sign all required product images:

```bash
bash secure_SDLC_Platform/scripts/sign-product-images.sh \
  --key awskms://arn:aws:kms:us-east-1:<horizon-account-id>:key/<key-id> \
  --yes
```

Verify image signatures:

```bash
bash secure_SDLC_Platform/scripts/verify-product-signatures.sh \
  --key awskms://arn:aws:kms:us-east-1:<horizon-account-id>:key/<key-id>
```

## Protected Pipeline Logic

Do not grant client Jenkins direct GitHub access to Horizon's private Jenkins shared-library repository. Client Jenkins should run generic wrapper jobs that call the in-cluster `horizon-runner` service. The runner requests short-lived signed execution plans from Horizon's control plane after license validation.

See [Thin Client Runner Architecture](thin-client-runner-architecture.md) and [Phase 11: Secure Product Distribution](phase-11-secure-product-distribution.md).

## Protection Boundaries

Private ECR controls distribution, not reverse engineering. Any party that can pull a container can save and inspect layers. Horizon protects the product through layered controls:

- private ECR pull access per licensed client
- license entitlement enforced in backend and Jenkins
- AWS account ID and installation ID binding
- no static admin credentials baked into product images
- no customer secrets in images
- proprietary pipeline logic served through signed execution plans
- public-key license verification, with Horizon retaining signing authority
- audit events for license sync, usage reporting, upgrade requests, and revocation

## Revocation

For trial expiration or contract termination:

1. Revoke or disable the client's activation token.
2. Suspend the installation or subscription in the license service.
3. Remove the client principal from Horizon ECR repository policies.
4. Confirm the client backend reports expired, suspended, or revoked license status.

## Enterprise Evidence

For regulated clients, preserve:

- release image tag and digest
- SBOM artifact
- vulnerability scan report for each product image
- license entitlement record
- repository policy change record
- client approval to mirror or deploy the product release
