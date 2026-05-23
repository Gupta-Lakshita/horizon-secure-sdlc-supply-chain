# License Operations And Commercial Readiness

## Purpose

This document explains how a client-hosted Horizon Relevance installation should use online license sync, activation tokens, public key verification, scheduled renewal, and commercial upgrade workflow without placing signed license payloads in client values files.

## Enterprise License Model

Horizon Relevance owns the commercial source of truth:

- client record
- subscription
- activation token hash
- installation record
- signed entitlement
- usage events
- upgrade request
- commercial offer

The client owns the platform runtime:

- AWS account
- EKS clusters and namespaces
- ECR repositories
- S3 artifact buckets
- IAM roles
- source code and application images

The client-hosted backend calls the Horizon license service and caches a signed entitlement. It enforces that entitlement locally before triggering Jenkins.

## Values File Contract

The client values file should not contain a hand-written signed license. It should contain only the license sync contract:

```yaml
license:
  mode: online-sync
  syncEndpoint: https://license.horizonrelevance.com/api/v1/licenses/sync
  activationTokenSecretName: horizon-license-activation
```

The backend Helm values should resolve to:

```yaml
enterprise:
  licenseEnforcementEnabled: true
  licenseMode: online-sync
  licenseSyncEndpoint: https://license.horizonrelevance.com/api/v1/licenses/sync
  licenseAutoSyncEnabled: true
  licenseAutoSyncIntervalSeconds: "21600"
  licenseCacheGraceHours: "72"
  licenseUsageReportingEnabled: true
  activationTokenSecret:
    existingSecret: horizon-license-activation
    key: ENTERPRISE_LICENSE_ACTIVATION_TOKEN
  publicKeySetSecret:
    existingSecret: horizon-license-public-key-set
    key: ENTERPRISE_LICENSE_PUBLIC_KEY_SET_JSON
```

## Scheduled Sync

The backend should sync automatically every few hours. This allows Horizon to activate renewals, revoke compromised installs, or suspend expired subscriptions without waiting for manual action.

Recommended default:

```text
licenseAutoSyncIntervalSeconds = 21600
licenseCacheGraceHours = 72
```

The grace period protects clients from a temporary license-service outage. It is not a paid renewal and should not be treated as contract extension.

## Commercial Upgrade Flow

1. Client starts a trial.
2. Client submits upgrade request from the License page.
3. Horizon reviews the request.
4. Horizon creates a manual invoice, contract, or AWS Marketplace private offer.
5. Client accepts or pays.
6. Horizon activates the offer into an enterprise subscription.
7. Client backend syncs license and receives updated entitlements.

## Revocation And Suspension

Horizon can disable:

- client
- installation
- activation token
- current license key
- subscription

For emergency revocation, Horizon should disable the installation and activation token. That prevents the client platform from replacing a revoked license with a fresh entitlement.

## Installer Responsibilities

The installer should:

- create or validate the activation token secret
- create or validate the public key set secret
- configure backend online-sync values
- seed the Environment Catalog
- run preflight validation

The installer should not:

- store signed license JSON in Git
- ask the client to type a license signature into values files
- create always-on QA/STAGE/PROD clusters unless the client explicitly enables them

## Low-Cost Readiness Pattern

For a Horizon-owned demo or pre-client environment:

- keep one DEV EKS cluster
- keep one small node group
- use in-cluster PostgreSQL for the license service
- avoid RDS Multi-AZ until paying clients exist
- provision QA/STAGE only for demos and destroy them afterward

This keeps the product enterprise-ready without carrying enterprise-sized monthly AWS spend.

## Readiness Endpoint

The Horizon license service exposes operational health endpoints:

| Endpoint | Use |
| --- | --- |
| `/live` | Kubernetes liveness probe. |
| `/ready` | Database and signing readiness probe. |
| `/health` | Lightweight health and version metadata. |

In production, `/ready` should report `ready` with `signing=ok`. In demo mode using local HMAC signing, `ready_with_warnings` is acceptable while the commercial workflow is being tested.

See [Production Control Plane Hardening](production-control-plane-hardening.md) for the low-cost production hardening path.
