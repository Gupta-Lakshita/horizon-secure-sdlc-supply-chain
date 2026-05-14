# Validation-Only IAM and Namespace-Scoped EKS Access

## Table of Contents

1. [Purpose](#purpose)
2. [Enterprise Access Model](#enterprise-access-model)
3. [Client Responsibilities](#client-responsibilities)
4. [Installer Responsibilities](#installer-responsibilities)
5. [Required YAML Structure](#required-yaml-structure)
6. [EKS Namespace-Scoped Mapping](#eks-namespace-scoped-mapping)
7. [Jenkins IRSA Model](#jenkins-irsa-model)
8. [Backend Environment Preflight](#backend-environment-preflight)
9. [Validation Commands](#validation-commands)

## Purpose

Enterprise paid clients should keep ownership of IAM role creation and approval. Horizon Relevance validates that the required IAM, EKS, S3, and ECR permissions are usable, but the installer does not create broad roles in the client account.

This is the recommended model for regulated clients because it supports least privilege, client change control, and clear audit ownership.

## Enterprise Access Model

Use:

```yaml
accessModel:
  iamMode: validation-only
  eksAccessMode: namespace-scoped
```

In this model:

- The client creates IAM roles.
- The installer validates that the roles exist.
- Jenkins runs with an IRSA service account role.
- The Jenkins IRSA role can assume only the configured target deployment roles.
- The target deployment role is mapped into EKS with namespace-scoped access.
- Developers only select `DEV`, `QA`, `STAGE`, or `PROD` in the UI.

## Client Responsibilities

The client cloud/platform team creates:

- Jenkins runtime IRSA role.
- DEV, QA, STAGE deployment roles.
- PROD source and target promotion roles when production is in a separate AWS account.
- ECR repositories or permissions for the pipeline to create them.
- S3 artifact bucket and object permissions.
- EKS access entries or Kubernetes RBAC scoped to approved namespaces.

## Installer Responsibilities

The installer:

- Reads `client-values.yaml`.
- Validates that required roles and clusters exist.
- Seeds the Environment Catalog.
- Enables backend preflight validation.
- Deploys the platform using Helm.
- Does not create or widen enterprise IAM roles in validation-only mode.

## Required YAML Structure

```yaml
accessModel:
  iamMode: validation-only
  eksAccessMode: namespace-scoped
  jenkins:
    createServiceAccount: true
    serviceAccountName: jenkins
    serviceAccountNamespace: horizon-platform
    irsaRoleArn: arn:aws:iam::<nonprod-account-id>:role/HorizonJenkinsRuntimeRole
  validation:
    backendPreflightEnforced: true
    installerValidatesRoles: true
```

Each Environment Catalog entry must include role, cluster, artifact, registry, and namespace information:

```yaml
environmentCatalog:
  environments:
    - name: QA
      accountTier: nonprod
      awsAccountId: "111122223333"
      awsRegion: us-east-1
      ecrRegistry: 111122223333.dkr.ecr.us-east-1.amazonaws.com
      ecrRepositoryTemplate: acme-devsecops/${projectName}
      artifactBucket: acme-devsecops-artifacts
      nonprodAwsRoleArn: arn:aws:iam::111122223333:role/HorizonQaDeployRole
      clusterName: acme-qa-eks
      namespaceStrategy: per-app
      namespaceTemplate: ${clientId}-${projectName}-qa
      iamValidationMode: validation-only
      eksAccessMode: namespace-scoped
      isActive: true
```

## EKS Namespace-Scoped Mapping

For AWS EKS access entries, scope the deploy role to the application namespace:

```bash
aws eks create-access-entry \
  --cluster-name acme-qa-eks \
  --region us-east-1 \
  --principal-arn arn:aws:iam::111122223333:role/HorizonQaDeployRole \
  --type STANDARD

aws eks associate-access-policy \
  --cluster-name acme-qa-eks \
  --region us-east-1 \
  --principal-arn arn:aws:iam::111122223333:role/HorizonQaDeployRole \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
  --access-scope type=namespace,namespaces=acme-payments-qa
```

For strict Kubernetes RBAC, bind only the required verbs in the namespace used by the application.

## Jenkins IRSA Model

Jenkins should use its service account role, not the EKS node role.

The Jenkins service account must be annotated:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: jenkins
  namespace: horizon-platform
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::111122223333:role/HorizonJenkinsRuntimeRole
```

When `accessModel.jenkins.createServiceAccount` is `true`, the installer creates this annotated service account for the Jenkins chart to reuse. Configure the Jenkins Helm release to use the same service account name instead of creating a separate unannotated account.

The target deployment roles must trust the Jenkins runtime role for `sts:AssumeRole`.

## Backend Environment Preflight

The backend exposes:

```text
GET /pipeline/api/environment-catalog/preflight/{ENV}?project_name=<project>&pipeline_kind=DEVOPS
```

The endpoint validates:

- Required Environment Catalog fields.
- Jenkins API token availability and authentication.
- Jenkins IRSA role visibility.
- Deployment role assumption.
- S3 artifact bucket access.
- ECR repository visibility.
- EKS cluster visibility.
- EKS access policy namespace scope when available.

The frontend displays `Environment Ready`, `Environment Ready with warnings`, or `Environment Not Ready`.

## Validation Commands

Run installer preflight before deployment:

```bash
./scripts/preflight.sh -f examples/client-values.yaml
```

Deploy the platform:

```bash
./scripts/install.sh --phase platform -f examples/client-values.yaml
```

Validate after deployment:

```bash
BACKEND_URL=https://devsecops.client.example/pipeline/api \
  ./scripts/validate.sh -f examples/client-values.yaml
```
