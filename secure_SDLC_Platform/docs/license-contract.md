# License Contract

The first enterprise contract is shared by the backend and Jenkins.

## Backend Request Fields

`POST /devops/pipeline` accepts these client/license fields:

- `client_id`
- `client_name`
- `license_key`
- `license_signature`
- `license_type`
- `license_expires_at`
- `enabled_pipelines`
- `enabled_features`
- `allowed_environments`

If the request omits these fields, the backend falls back to environment/license-file configuration.

## Backend Environment Variables

- `ENTERPRISE_LICENSE_ENFORCEMENT_ENABLED`
- `ENTERPRISE_CLIENT_ID`
- `ENTERPRISE_CLIENT_NAME`
- `ENTERPRISE_LICENSE_KEY`
- `ENTERPRISE_LICENSE_TYPE`
- `ENTERPRISE_LICENSE_EXPIRES_AT`
- `ENTERPRISE_ENABLED_PIPELINES`
- `ENTERPRISE_ENABLED_FEATURES`
- `ENTERPRISE_ALLOWED_ENVIRONMENTS`
- `ENTERPRISE_MAX_REPOS`
- `ENTERPRISE_MAX_BUILDS_PER_MONTH`
- `ENTERPRISE_MAX_USERS`
- `ENTERPRISE_LICENSE_FILE`
- `ENTERPRISE_LICENSE_SIGNING_SECRET`

When `ENTERPRISE_LICENSE_ENFORCEMENT_ENABLED=true`, the backend validates the license before creating or triggering a Jenkins job.

## Jenkins Parameters

The backend passes these parameters to Jenkins:

- `CLIENT_ID`
- `CLIENT_NAME`
- `LICENSE_TYPE`
- `LICENSE_EXPIRES_AT`
- `LICENSED_PIPELINES`
- `LICENSED_FEATURES`
- `LICENSED_ENVIRONMENTS`
- `LICENSE_VALIDATION_MODE`

Jenkins validates expiration, pipeline entitlement, environment entitlement, and requested feature entitlement before checkout/build/scanning starts. Raw license keys/signatures stay backend-side and are not stored as Jenkins job parameters.

## Feature Names

- `build`
- `artifact_publish`
- `code_scan`
- `image_scan`
- `policy_validation`
- `static_application_security`
- `test_suites`
- `notifications`
- `prod_deploy`
- `ai_remediation`

## Current Scope

This phase enforces license status and entitlements. Usage counters such as max repos, users, and monthly builds are defined in the contract and should be backed by database usage tracking in the next iteration.
