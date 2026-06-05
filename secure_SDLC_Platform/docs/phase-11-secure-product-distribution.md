# Phase 11: Secure Product Distribution

## Purpose

Phase 11 protects Horizon Relevance intellectual property while still allowing clients to run the product inside their own AWS accounts. The client should be able to build, validate, promote, and deploy applications without receiving broad source-level access to Horizon's proprietary pipeline logic.

## Recommended Enterprise Model

Use three layers together:

1. Horizon-owned private ECR for product images.
2. Signed product images, SBOM evidence, and release verification.
3. Horizon Thin Runner for pipeline execution plans.

The old private shared-library model is no longer the recommended client distribution path. Jenkins should not need GitHub access to Horizon's private shared-library repository. Jenkins creates a generic wrapper job and calls the in-cluster `horizon-runner` service. The runner requests a signed execution plan from Horizon's licensed execution service and executes only the approved actions.

## What the Client Receives

| Artifact | Client receives? | Notes |
| --- | --- | --- |
| Product images | Yes | Pulled from Horizon private ECR or mirrored into client ECR. |
| Helm chart / installer | Yes | Client-owned values drive infrastructure and platform install. |
| License activation token | Yes, as a secret | Used to sync license and request execution plans. |
| Public verification key | Yes | Used to verify signed licenses and runner execution plans. |
| Jenkins shared-library source | No | Replaced by the Thin Runner execution-plan model. |
| Horizon signing private key | Never | Remains in Horizon-controlled KMS/control plane. |

## Image Distribution Controls

Horizon grants pull access only to licensed AWS accounts and only for approved product repositories. Every release should include:

- immutable image tag and digest
- SBOM artifact
- product image vulnerability scan
- Cosign or equivalent image signature
- release notes and compatibility notes

See [Private ECR Image Distribution](private-ecr-image-distribution.md) and [Secure Product Distribution Runbook](secure-product-distribution-runbook.md).

## Thin Runner Controls

The runner protects business logic by moving decision-making to Horizon-controlled services:

1. Jenkins sends a normalized request to `horizon-runner`.
2. `horizon-runner` calls Horizon's execution-plan endpoint with the client id, installation id, activation token, pipeline kind, and runtime parameters.
3. Horizon validates the license, enabled features, allowed AWS account IDs, environments, and usage limits.
4. Horizon returns a short-lived signed execution plan.
5. The runner verifies the signature with the public key mounted in the client cluster.
6. The runner executes only allowed actions and emits audit/usage events.

For details, see [Thin Client Runner Architecture](thin-client-runner-architecture.md).

## Revocation

For trial expiration or termination:

1. Suspend or revoke the client's license/activation token.
2. Remove the client's AWS account from Horizon ECR repository policies.
3. Disable execution-plan issuance for that installation.
4. Confirm the client backend reports expired, suspended, or revoked license status.

## Production Readiness Checklist

- Private ECR policies are account-scoped and time-bound where possible.
- Client image pull roles have `ecr:GetAuthorizationToken` only in the client account.
- Product images are signed and have SBOM evidence.
- Jenkins jobs use runner mode, not private GitHub shared-library mode.
- Runner has the Horizon public key mounted.
- Runner activation token is stored in a Kubernetes Secret or external secrets manager.
- Backend license sync is online and healthy.
- Audit events are emitted for license sync, execution plan requests, and pipeline completion.
