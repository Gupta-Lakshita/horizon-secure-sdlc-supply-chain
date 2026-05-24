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
| Frontend | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/frontend:1.4.26` |
| Backend | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/backend:1.4.33` |
| License service | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/license-management-service:0.1.9` |
| Jenkins | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/jenkins:1.0.8` |
| SonarQube mirror | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/sonarqube:10.4-community` |
| Container/IaC scanner | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/trivy-scanner:1.1.2` |
| Policy validation service | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/opa-scanner:1.0.1` |
| Self-service password | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/self-service-password:1.7.3-ltb` |

## Grant Client Pull Access

Generate a repository policy for the client pull role:

```bash
bash secure_SDLC_Platform/scripts/render-ecr-pull-policy.sh \
  --principal-arn arn:aws:iam::<client-account-id>:role/<client-ecr-pull-role> \
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

The client pull role still needs identity permission for `ecr:GetAuthorizationToken`.

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

Package a signed rules/templates bundle:

```bash
bash secure_SDLC_Platform/scripts/package-rule-bundle.sh \
  --source /path/to/private/horizon-rules \
  --version 2026.05.23 \
  --signing-key awskms://arn:aws:kms:us-east-1:<horizon-account-id>:key/<key-id> \
  --yes
```

See [Phase 11: Secure Product Distribution](phase-11-secure-product-distribution.md) for the full signed image, SBOM, private ECR, and signed rule bundle workflow.

## Protection Boundaries

Private ECR controls distribution, not reverse engineering. Any party that can pull a container can save and inspect layers. Horizon protects the product through layered controls:

- private ECR pull access per licensed client
- license entitlement enforced in backend and Jenkins
- AWS account ID and installation ID binding
- no static admin credentials baked into product images
- no customer secrets in images
- scanner/rule content distributed as protected release bundles where required
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
