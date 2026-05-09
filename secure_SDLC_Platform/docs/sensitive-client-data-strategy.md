# Sensitive Client Data Strategy

## Purpose

This document defines how Horizon Relevance should handle installer assets that contain client-sensitive data such as AWS account IDs, role ARNs, cluster names, DNS zones, internal LDAP endpoints, repository URLs, and environment mappings.

## Recommended Approach

Horizon Relevance should maintain the generic installer source. The client should maintain client-specific values, Terraform state, and secrets inside their own private repository or approved infrastructure-as-code platform.

## What Stays With Horizon Relevance

1. Generic Terraform modules.
2. Generic Helm chart.
3. Installation scripts.
4. Documentation.
5. Product container image references.
6. License contract schema.
7. Example values with fake data only.

## What Stays With The Client

1. Real AWS account IDs.
2. Real role ARNs.
3. Real DNS zones and private endpoints.
4. Real LDAP/IdP configuration.
5. Terraform backend configuration.
6. Terraform state.
7. Kubernetes secrets.
8. License activation token.
9. Git provider tokens.
10. Environment-specific overlays.

## Repository Models

### Model A: Client-Owned Private Repo

Best for enterprise clients.

The client creates a private repository:

```text
regeneron-horizon-platform-config/
├── values/
│   ├── dev.yaml
│   ├── qa.yaml
│   └── prod.yaml
├── terraform-backend/
├── secrets/
│   └── README.md
└── runbooks/
```

Horizon Relevance provides pull requests, release notes, and support. The client runs the installer from their own runner or workstation.

### Model B: Horizon-Assisted Secure Repo

Good for trial.

Horizon creates a private implementation repository and grants the client access. Before production, ownership should transfer to the client.

### Model C: No Client Repo

Acceptable only for short POCs.

Client values are stored as encrypted artifacts or in the client's secrets manager. This model is weaker for auditability and should not be the long-term enterprise model.

## Secret Handling Rules

1. Do not commit real secrets.
2. Do not commit real license activation tokens.
3. Do not commit Terraform state.
4. Store secrets in AWS Secrets Manager, External Secrets Operator, SOPS, or Sealed Secrets.
5. Use expiring credentials for trial.
6. Rotate credentials at trial end or conversion.
7. Mirror Horizon images into client ECR for enterprise deployments.

## License Activation Token

Online license sync requires an activation token. Store it as a Kubernetes Secret or in AWS Secrets Manager.

Example Kubernetes Secret:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: horizon-license-activation
  namespace: horizon-platform
type: Opaque
stringData:
  activationToken: "<provided-by-horizon-license-service>"
```

The token allows the client-hosted platform to request a signed license from Horizon Relevance. It should not grant access to source code, DockerHub administration, or other client data.

