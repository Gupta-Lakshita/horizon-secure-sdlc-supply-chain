# Horizon License Management Service

This is the Phase 1 Horizon-owned commercial license service for client-hosted Horizon Relevance AI DevSecOps installations.

The older `secure_SDLC_Platform/license-server` service remains a tiny demo stub. This service is the production-shaped MVP with persistent clients, plans, subscriptions, installation records, activation token hashes, usage events, audit events, and signed entitlement payloads.

## What This Service Owns

- Client records such as `acme-fintech` or `regeneron-healthcare`.
- Trial, paid, and enterprise subscription windows.
- Entitlements such as enabled pipelines, enabled features, allowed environments, allowed AWS account IDs, and usage limits.
- Activation tokens stored only as hashes.
- License sync audit history.
- Signed license payloads returned to client-hosted backend deployments.

## Local Run

```bash
cd secure_SDLC_Platform/license-management-service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export HORIZON_LICENSE_DATABASE_URL='sqlite:///./license-management.db'
export HORIZON_LICENSE_ADMIN_API_KEY='dev-admin-key'
export HORIZON_LICENSE_SIGNING_SECRET='dev-signing-secret'
export HORIZON_LICENSE_TOKEN_PEPPER='dev-token-pepper'

uvicorn app.main:app --host 0.0.0.0 --port 8090
```

Health check:

```bash
curl http://localhost:8090/health
```

## Admin Bootstrap Example

Create a trial plan:

```bash
curl -X POST http://localhost:8090/api/v1/admin/plans \
  -H 'Content-Type: application/json' \
  -H 'X-Horizon-Admin-Key: dev-admin-key' \
  -d '{
    "code": "trial-30",
    "name": "30 Day Trial",
    "license_type": "trial",
    "enabled_pipelines": ["Build & Deploy Pipeline", "Validation Pipeline"],
    "enabled_features": ["build", "artifact_publish", "code_scan", "image_scan", "policy_validation", "test_suites"],
    "allowed_environments": ["DEV", "QA"],
    "limits": {"max_users": 10, "max_repos": 3, "max_builds_per_month": 100}
  }'
```

Create a client:

```bash
curl -X POST http://localhost:8090/api/v1/admin/clients \
  -H 'Content-Type: application/json' \
  -H 'X-Horizon-Admin-Key: dev-admin-key' \
  -d '{
    "client_key": "acme-fintech",
    "name": "Acme Fintech",
    "industry": "fintech"
  }'
```

Create a subscription. Use a real future expiration date when running this:

```bash
curl -X POST http://localhost:8090/api/v1/admin/clients/acme-fintech/subscriptions \
  -H 'Content-Type: application/json' \
  -H 'X-Horizon-Admin-Key: dev-admin-key' \
  -d '{
    "plan_code": "trial-30",
    "expires_at": "2026-06-20T23:59:59Z",
    "allowed_aws_account_ids": ["426946630837"],
    "notes": "Internal client-hosted trial"
  }'
```

Create an activation token:

```bash
curl -X POST http://localhost:8090/api/v1/admin/clients/acme-fintech/activation-tokens \
  -H 'Content-Type: application/json' \
  -H 'X-Horizon-Admin-Key: dev-admin-key' \
  -d '{"name": "acme trial activation"}'
```

The raw activation token is returned only once. Store it in a secure channel, then place it in the client platform as a Kubernetes Secret or external secret. Do not commit it to Git.

## Client License Sync Example

The client-hosted backend calls this endpoint:

```bash
curl -X POST http://localhost:8090/api/v1/licenses/sync \
  -H 'Content-Type: application/json' \
  -d '{
    "client_id": "acme-fintech",
    "installation_id": "acme-fintech-us-east-1-eks",
    "activation_token": "<activation-token>",
    "aws_account_id": "426946630837",
    "region": "us-east-1",
    "product_version": "1.4.24",
    "platform": {
      "cluster": "horizon-eks-dev",
      "mode": "client-hosted"
    }
  }'
```

The response contains a signed entitlement document. The client backend caches it and enforces it before pipeline execution.

## Production Notes

- Use PostgreSQL for persistence.
- Use `HORIZON_LICENSE_SIGNING_MODE=aws-kms` and a KMS asymmetric signing key for production signing.
- Keep `HORIZON_LICENSE_ADMIN_API_KEY`, `HORIZON_LICENSE_TOKEN_PEPPER`, and DB credentials in Secrets Manager or Kubernetes Secrets.
- Put the service behind HTTPS, WAF/rate limiting, and Horizon internal admin authentication.
- Use the Helm chart at `secure_SDLC_Platform/helm/license-management-service`.
