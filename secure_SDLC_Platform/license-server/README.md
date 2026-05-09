# Horizon License Server

This is the Horizon Relevance-owned license service used by client-hosted deployments when `license.mode=online-sync`.

It is intentionally separate from the client-hosted backend. The client backend calls this service with:

1. `client_id`
2. `activation_token`
3. current license metadata
4. platform metadata

The service validates the subscription and returns a signed license payload. The client-hosted backend then validates the signature, caches the license locally, and enforces entitlements before pipeline execution.

## Local Demo

```bash
cd secure_SDLC_Platform/license-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export LICENSE_SIGNING_SECRET='change-me-demo-secret'
export REGENERON_ACTIVATION_TOKEN='regeneron-demo-token'
uvicorn app:app --host 0.0.0.0 --port 8090
```

Sync endpoint:

```text
POST http://localhost:8090/api/v1/licenses/sync
```

Sample request:

```json
{
  "client_id": "regeneron-healthcare",
  "client_name": "Regeneron",
  "activation_token": "regeneron-demo-token",
  "force": true
}
```

For the initial demo this service grants Regeneron a 2-day trial license. For renewal testing, update the client subscription record or change the issued expiration in this service, then click **Sync License** in the client-hosted platform.

## Production Notes

Before using this service commercially:

1. Store clients and subscriptions in a real database.
2. Store activation token hashes, not raw tokens.
3. Use asymmetric signing with a private key in KMS/HSM.
4. Add audit logs for every license sync.
5. Add admin APIs for renewals, tier conversion, suspension, and revocation.
6. Put the service behind HTTPS, WAF, rate limits, and authentication.

