# Production Control Plane Hardening

## Purpose

This document defines the low-cost but enterprise-ready hardening path for the Horizon-owned license control plane. It lets Horizon operate commercially before carrying unnecessary multi-AZ production cost.

## Current Recommended Cost Posture

Before paid clients exist, keep the Horizon control plane small:

- one DEV EKS cluster
- one `t3.large` managed node group
- in-cluster PostgreSQL for the license service
- private ECR for product images
- KMS-backed license signing when enabled
- manual backups and release verification
- no always-on QA/STAGE/PROD EKS clusters
- no RDS Multi-AZ until client revenue justifies it

This keeps the platform credible for trials while avoiding a fixed cost profile that belongs to a mature SaaS control plane.

## License Service Readiness

The license service exposes:

| Endpoint | Purpose |
| --- | --- |
| `/live` | Process liveness check. |
| `/ready` | Database and signing readiness check. |
| `/health` | Lightweight service health and version metadata. |

The Helm chart uses `/live` for liveness and `/ready` for readiness. If KMS signing is not configured, `/ready` returns `ready_with_warnings` rather than failing local or demo deployments.

## Production Controls

Before onboarding regulated enterprise clients, enable:

1. AWS KMS asymmetric signing.
2. Private ECR release distribution.
3. Activation-token hashing and rotation.
4. Scheduled client license sync.
5. Usage reporting from client backend.
6. CloudWatch alarms for license service availability.
7. Database backup workflow.
8. Admin API key rotation or internal SSO for the license portal.
9. WAF or ingress rate limiting for the public license endpoint.
10. Audit event retention policy.

## Upgrade Trigger To RDS

Move from in-cluster PostgreSQL to Amazon RDS when one of these becomes true:

- first paid enterprise client signs
- license service is required for production SLAs
- audit retention becomes contractual
- support team needs point-in-time recovery
- multiple Horizon operators manage license operations

Start with single-AZ RDS PostgreSQL on a small class. Move to Multi-AZ only when availability requirements justify the cost.

## Operational Validation

After every license service release:

```bash
kubectl rollout status deployment/horizon-license-management-service -n horizon-relevance-dev
kubectl exec -n horizon-relevance-dev deploy/horizon-license-management-service -- \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8090/ready').read().decode())"
```

Expected result:

```json
{
  "status": "ready",
  "service": "horizon-license-management-service"
}
```

In demo mode with local HMAC signing, `ready_with_warnings` is acceptable.

## Remaining Gap

This hardening does not replace commercial controls such as invoicing, AWS Marketplace private offers, customer success workflows, or enterprise SSO. Those remain separate product phases.

