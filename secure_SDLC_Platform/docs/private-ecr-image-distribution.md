# Private ECR Image Distribution and Trial Protection

## Purpose

Horizon Relevance trial and enterprise deployments should pull product images from Horizon-owned private ECR repositories. Private ECR controls who can pull images, while backend license enforcement controls what the installed product is allowed to do.

## Image Repositories

| Component | Repository | Current tag |
| --- | --- | --- |
| Frontend | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/frontend` | `1.4.27` |
| Backend | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/backend` | `1.4.34` |
| Thin runner | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/runner` | `0.1.0` |
| License service | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/license-management-service` | `0.1.9` |
| Jenkins | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/jenkins` | `1.0.8` |
| SonarQube mirror | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/sonarqube` | `10.4-community` |
| Container/IaC scanner | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/trivy-scanner` | `1.1.2` |
| Policy validation service | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/opa-scanner` | `1.0.1` |
| Self-service password | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/self-service-password` | `1.7.3-ltb` |

## Client Pull Access

For the early enterprise distribution model, keep a small licensed-account allowlist and grant each licensed client AWS account read-only ECR access to only the approved Horizon product repositories. Use the client account root ARN in the repository policy, such as `arn:aws:iam::921570400913:root`; the client account still controls which internal roles can actually pull through its own IAM policies.

Required pull actions:

```json
[
  "ecr:BatchCheckLayerAvailability",
  "ecr:BatchGetImage",
  "ecr:GetDownloadUrlForLayer"
]
```

The client-side EKS node role or image pull role must also be able to obtain an ECR authorization token.

## Extraction Risk

Private ECR does not prevent a client who can pull an image from saving and inspecting it with `docker save`, `podman save`, or similar tooling. This is normal for container distribution. The product must not rely on image secrecy alone.

Mitigations:

- keep Horizon ECR private
- use time-limited trial pull access
- require signed license validation
- bind licenses to allowed AWS account IDs
- bind licenses to a generated installation ID
- keep signing secrets and activation tokens in Kubernetes secrets
- avoid baking customer secrets or static admin credentials into images
- keep proprietary pipeline logic behind Horizon-controlled signed execution plans
- publish SBOM and vulnerability evidence for every approved product image
- sign release images or release manifests before enterprise distribution

## Helper Scripts

Render a least-privilege repository pull policy:

```bash
bash secure_SDLC_Platform/scripts/render-ecr-pull-policy.sh \
  --client-account-id 921570400913 \
  --client-account-id <another-licensed-client-account-id> \
  --repository horizon/backend \
  --expires-at 2026-06-30T23:59:59Z
```

Apply the rendered policy to each approved Horizon product repository from the Horizon AWS account:

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

Do not add `ecr:GetAuthorizationToken` to the repository policy. That action must be granted to the client-side node role, import role, or image pull role as an identity policy in the client AWS account.

Verify expected Horizon product image tags:

```bash
bash secure_SDLC_Platform/scripts/verify-product-images.sh --online
```

Generate SBOM and verify signatures before client handoff:

```bash
bash secure_SDLC_Platform/scripts/generate-product-sbom.sh

bash secure_SDLC_Platform/scripts/verify-product-signatures.sh \
  --key awskms://arn:aws:kms:us-east-1:<horizon-account-id>:key/<key-id>
```

Do not grant client Jenkins direct GitHub access to Horizon's private Jenkins shared-library repository. Use the Horizon Thin Runner so Jenkins receives only a generic wrapper job and the runner requests signed execution plans from Horizon. See [Thin Client Runner Architecture](thin-client-runner-architecture.md).

## Trial License Binding

Every trial values file should include only the online-sync bootstrap contract:

```yaml
license:
  enforcementEnabled: true
  mode: online-sync
  syncEndpoint: https://license.horizonrelevance.com/api/v1/licenses/sync
  clientId: client-id-issued-by-horizon
  activationTokenSecretName: horizon-license-activation
```

Horizon sets allowed AWS accounts, installation binding, expiration, enabled
pipelines/features, and usage limits in the subscription record. The backend
denies pipeline creation when the requested ECR/image account does not match the
signed license payload, or when the runtime installation ID differs from the
signed entitlement.

## Jenkins Note

Jenkins image `1.0.8` is a hardened derivative of `ankur1825/horizon-jenkins:1.0.7`. It removes the static baked-in `admin/admin123` bootstrap and validates the required CI tools on the runtime path. Authentication is configured at deployment time through the Helm-managed Jenkins credentials Secret and startup security bootstrap; clients can later replace that with LDAP/JCasC/SSO according to their enterprise identity model.

For the full operational workflow, see [Secure Product Distribution Runbook](secure-product-distribution-runbook.md).
