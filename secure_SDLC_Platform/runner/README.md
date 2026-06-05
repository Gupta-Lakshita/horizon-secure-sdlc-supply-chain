# Horizon Thin Runner

The Horizon Thin Runner is deployed inside the client-hosted Horizon platform. Jenkins creates a generic wrapper job and calls the runner instead of loading Horizon private Jenkins shared-library code.

The runner:

1. Receives a pipeline execution request from Jenkins.
2. Sends the request to Horizon's licensed execution-plan service.
3. Verifies the signed execution plan with Horizon's public key.
4. Executes only approved local actions.
5. Emits execution events back to Horizon for audit and usage metering.

This keeps customer source code and deployment credentials inside the client account while keeping Horizon's proprietary pipeline logic in Horizon-controlled services.

## Local Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

## Container

```bash
podman build -t 426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/runner:0.1.0 .
```

## Required Configuration

| Environment variable | Purpose |
| --- | --- |
| `HORIZON_CLIENT_ID` | Licensed client id assigned by Horizon. |
| `HORIZON_INSTALLATION_ID` | Unique installation id for this client-hosted platform. |
| `HORIZON_ACTIVATION_TOKEN` | Activation token used to authenticate to Horizon's execution service. |
| `HORIZON_EXECUTION_PLAN_ENDPOINT` | Horizon endpoint that returns signed execution plans. |
| `HORIZON_EXECUTION_EVENT_ENDPOINT` | Optional Horizon endpoint for usage and audit events. |
| `HORIZON_PUBLIC_KEY_PATH` | Public key used to verify signed plans. Required for production. |
| `HORIZON_RUNNER_ALLOW_SHELL` | Disabled by default. Allows signed shell actions during controlled rollout. |
| `HORIZON_RUNNER_EXECUTE` | Disabled by default. When false, shell actions are planned but not executed. |
