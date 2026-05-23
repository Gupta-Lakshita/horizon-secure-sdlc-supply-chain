# Private ECR Image Distribution and Trial Protection

## Purpose

Horizon Relevance trial and enterprise deployments should pull product images from Horizon-owned private ECR repositories. Private ECR controls who can pull images, while backend license enforcement controls what the installed product is allowed to do.

## Image Repositories

| Component | Repository | Current tag |
| --- | --- | --- |
| Frontend | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/frontend` | `1.4.25` |
| Backend | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/backend` | `1.4.32` |
| License service | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/license-management-service` | `0.1.7` |
| Jenkins | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/jenkins` | `1.0.8` |
| SonarQube mirror | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/sonarqube` | `10.4-community` |
| Container/IaC scanner | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/trivy-scanner` | `1.1.2` |
| Policy validation service | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/opa-scanner` | `1.0.1` |
| Self-service password | `426946630837.dkr.ecr.us-east-1.amazonaws.com/horizon/self-service-password` | `1.7.3-ltb` |

## Client Pull Access

For trials, grant the client AWS account read-only ECR access to only the required repositories and tags. Prefer cross-account repository policies over copying images into the client account.

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
- keep high-value rule packs and pipeline bundles license-gated where possible
- publish SBOM and vulnerability evidence for every approved product image
- sign release images or release manifests before enterprise distribution

## Helper Scripts

Render a least-privilege repository pull policy:

```bash
bash secure_SDLC_Platform/scripts/render-ecr-pull-policy.sh \
  --principal-arn arn:aws:iam::<client-account-id>:role/<client-ecr-pull-role> \
  --repository horizon/backend
```

Verify expected Horizon product image tags:

```bash
bash secure_SDLC_Platform/scripts/verify-product-images.sh --online
```

## Trial License Binding

Every trial values file should include:

```yaml
license:
  enforcementEnabled: true
  mode: online-sync
  allowedAwsAccountIds:
    - "111122223333"
  installationId: client-id-horizon-trial-001
```

The backend denies pipeline creation when the requested ECR/image account does not match the licensed account list, or when the runtime installation ID differs from the signed license payload.

## Jenkins Note

Jenkins image `1.0.8` is a hardened derivative of `ankur1825/horizon-jenkins:1.0.7`. It removes the static bootstrap Groovy admin script and validates the required CI tools on the runtime path. Authentication must be configured through Helm/JCasC/LDAP at deployment time.

For the full operational workflow, see [Secure Product Distribution Runbook](secure-product-distribution-runbook.md).
