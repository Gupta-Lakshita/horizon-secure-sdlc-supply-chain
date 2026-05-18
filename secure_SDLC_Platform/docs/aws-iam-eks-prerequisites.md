# AWS IAM and EKS Prerequisites for Client Onboarding

## Table of Contents

1. [Purpose](#purpose)
2. [Required Roles](#required-roles)
3. [Jenkins Runtime Role](#jenkins-runtime-role)
4. [Deployment Role](#deployment-role)
5. [Namespace-Scoped EKS Access](#namespace-scoped-eks-access)
6. [Migrating an Existing Cluster-Scoped Role](#migrating-an-existing-cluster-scoped-role)
7. [Environment Catalog Mapping](#environment-catalog-mapping)
8. [Preflight Validation Checklist](#preflight-validation-checklist)
9. [Client Responsibility Matrix](#client-responsibility-matrix)

## Purpose

Horizon Relevance AI DevSecOps is installed into the client AWS environment. For enterprise paid clients, IAM must run in **validation-only mode**:

- The client creates IAM roles, EKS access entries, namespaces, and platform secrets.
- Horizon Relevance installer/backend validates the client-provided objects.
- Jenkins runs using IRSA, not the EKS node role.
- Jenkins assumes a client-created deployment role for each environment.
- Deployment roles are mapped into EKS with namespace-scoped access.

This avoids broad node-role permissions and lets the client keep cloud governance under their own control.

## Required Roles

At minimum, a client-hosted installation needs these roles.

| Role | Created By | Used By | Purpose |
| --- | --- | --- | --- |
| `HorizonJenkinsRuntimeRole` | Client cloud/platform team | Jenkins pod service account | Jenkins runtime identity through IRSA. It can assume only approved deployment roles. |
| `HorizonBackendValidationRole` or equivalent | Client cloud/platform team | Backend pod service account, or approved backend runtime identity | Lets backend preflight validate STS, S3, ECR, and EKS access. For demos this can be the same AWS identity that already runs the backend, but enterprise clients should use a separate IRSA role. |
| `HorizonDevDeployRole` | Client cloud/platform team | Jenkins and backend preflight | Deploys/builds DEV workloads. |
| `HorizonQaDeployRole` | Client cloud/platform team | Jenkins and backend preflight | Deploys/tests QA workloads. |
| `HorizonStageDeployRole` | Client cloud/platform team | Jenkins and backend preflight | Deploys/promotes STAGE workloads. |
| `HorizonProdPromotionSourceRole` | Client cloud/platform team | Production pipeline | Reads approved non-prod artifacts/images. |
| `HorizonProdPromotionTargetRole` | Client cloud/platform team | Production pipeline | Promotes/deploys approved production release. |

Small demos may use one non-prod deploy role, but enterprise clients should separate DEV, QA, STAGE, and PROD roles.

The role names above are examples. Enterprise clients can use names such as `regeneron-qa-devsecops-deploy`, `platform-jenkins-runtime`, or any other approved convention. The installer reads existing role names from `roleArn`; when it provisions optional roles, it uses `naming.resourceNamePrefix` and explicit fields such as `iam.deployRole.roleName`, `eks.ebsCsiDriver.roleName`, and `eks.nodeGroup.roleName`.

## Jenkins Runtime Role

The Jenkins runtime role is assumed by the Kubernetes service account using IRSA.

Example service account:

```text
namespace: horizon-relevance-dev
serviceAccount: jenkins
```

Example trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowJenkinsServiceAccountIRSA",
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<account-id>:oidc-provider/oidc.eks.<region>.amazonaws.com/id/<oidc-id>"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "oidc.eks.<region>.amazonaws.com/id/<oidc-id>:aud": "sts.amazonaws.com",
          "oidc.eks.<region>.amazonaws.com/id/<oidc-id>:sub": "system:serviceaccount:<platform-namespace>:jenkins"
        }
      }
    }
  ]
}
```

Jenkins runtime permission policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowAssumeEnvironmentDeployRoles",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": [
        "arn:aws:iam::<nonprod-account-id>:role/HorizonDevDeployRole",
        "arn:aws:iam::<nonprod-account-id>:role/HorizonQaDeployRole",
        "arn:aws:iam::<nonprod-account-id>:role/HorizonStageDeployRole",
        "arn:aws:iam::<prod-account-id>:role/HorizonProdPromotionTargetRole"
      ]
    }
  ]
}
```

Jenkins Helm values:

```yaml
serviceAccount:
  create: true
  name: jenkins
  automount: true
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::<account-id>:role/HorizonJenkinsRuntimeRole
```

Validation:

```bash
kubectl exec deploy/jenkins -n <platform-namespace> -c jenkins -- aws sts get-caller-identity
```

Expected identity:

```text
arn:aws:sts::<account-id>:assumed-role/HorizonJenkinsRuntimeRole/<session>
```

## Deployment Role

The deployment role is the environment-specific role assumed by Jenkins and backend preflight.

Trust policy for a non-prod deploy role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowJenkinsRuntimeRole",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<platform-account-id>:role/HorizonJenkinsRuntimeRole"
      },
      "Action": "sts:AssumeRole"
    },
    {
      "Sid": "AllowBackendValidationRole",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<platform-account-id>:role/HorizonBackendValidationRole"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

If the backend uses the same runtime identity as Jenkins for a demo, the second statement can point to the Jenkins runtime role or be omitted. For enterprise clients, keep backend validation and Jenkins runtime separate when possible.

Minimum AWS permission policy for build/deploy roles:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrAuth",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "EcrRepositoryAccess",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:CompleteLayerUpload",
        "ecr:CreateRepository",
        "ecr:DescribeImages",
        "ecr:DescribeRepositories",
        "ecr:GetDownloadUrlForLayer",
        "ecr:InitiateLayerUpload",
        "ecr:ListImages",
        "ecr:PutImage",
        "ecr:UploadLayerPart"
      ],
      "Resource": "arn:aws:ecr:<region>:<account-id>:repository/<allowed-repository-prefix>*"
    },
    {
      "Sid": "ArtifactBucketAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::<artifact-bucket>/*"
    },
    {
      "Sid": "ArtifactBucketList",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": "arn:aws:s3:::<artifact-bucket>"
    },
    {
      "Sid": "EksClusterDiscovery",
      "Effect": "Allow",
      "Action": [
        "eks:DescribeCluster"
      ],
      "Resource": "arn:aws:eks:<region>:<account-id>:cluster/<cluster-name>"
    },
    {
      "Sid": "ProdSecretManagementOptional",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:CreateSecret",
        "secretsmanager:DescribeSecret",
        "secretsmanager:PutSecretValue",
        "secretsmanager:TagResource"
      ],
      "Resource": "arn:aws:secretsmanager:<region>:<account-id>:secret:/horizon/*"
    }
  ]
}
```

Remove `CreateRepository` if the client requires ECR repositories to be pre-created by platform/IaC only. Remove the Secrets Manager statement if production secret generation is not enabled.

## Namespace-Scoped EKS Access

The EKS cluster must support access entries:

```bash
aws eks describe-cluster \
  --region <region> \
  --name <cluster-name> \
  --query 'cluster.accessConfig.authenticationMode'
```

Supported values:

- `API`
- `API_AND_CONFIG_MAP`

If the cluster still uses only the legacy `CONFIG_MAP` mode, the client platform team should plan migration to access entries before enabling enterprise namespace-scoped mode.

When `eks.accessEntry.state=provision`, the installer updates a newly provisioned cluster from `CONFIG_MAP` to `API_AND_CONFIG_MAP` before creating the access entry. For an existing production cluster, clients may choose to perform this migration themselves through their standard change process and then run installer preflight again.

When `eks.ebsCsiDriver.state=provision` for a provisioned cluster, the installer creates an EBS CSI IRSA role and attaches `arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy`. Use `eks.ebsCsiDriver.roleName` if the client requires a specific role name.

Create or confirm the access entry:

```bash
aws eks create-access-entry \
  --region <region> \
  --cluster-name <cluster-name> \
  --principal-arn arn:aws:iam::<account-id>:role/HorizonQaDeployRole \
  --type STANDARD
```

Pre-create the application namespace:

```bash
kubectl create namespace acme-fintech-angular
```

Associate namespace-scoped edit access:

```bash
aws eks associate-access-policy \
  --region <region> \
  --cluster-name <cluster-name> \
  --principal-arn arn:aws:iam::<account-id>:role/HorizonQaDeployRole \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy \
  --access-scope type=namespace,namespaces=acme-fintech-angular
```

Validate:

```bash
aws eks list-associated-access-policies \
  --region <region> \
  --cluster-name <cluster-name> \
  --principal-arn arn:aws:iam::<account-id>:role/HorizonQaDeployRole
```

Expected access scope:

```json
{
  "type": "namespace",
  "namespaces": [
    "acme-fintech-angular"
  ]
}
```

`AmazonEKSEditPolicy` is usually enough for Helm application deployments inside a namespace. It does not allow cluster-admin actions and should not be used to create namespaces. Namespaces are pre-created by the client platform team or installer bootstrap running with platform-admin credentials.

### Optional Custom RBAC Pattern

If the client does not want to use AWS-managed EKS access policies, use an EKS access entry with Kubernetes groups and bind that group to a namespace Role.

Create access entry with group:

```bash
aws eks create-access-entry \
  --region <region> \
  --cluster-name <cluster-name> \
  --principal-arn arn:aws:iam::<account-id>:role/HorizonQaDeployRole \
  --kubernetes-groups horizon-qa-deployers \
  --type STANDARD
```

Namespace Role:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: horizon-namespace-deployer
  namespace: acme-fintech-angular
rules:
  - apiGroups: ["", "apps", "batch", "networking.k8s.io", "autoscaling"]
    resources:
      - configmaps
      - secrets
      - services
      - serviceaccounts
      - pods
      - pods/log
      - deployments
      - replicasets
      - jobs
      - cronjobs
      - ingresses
      - horizontalpodautoscalers
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

Namespace RoleBinding:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: horizon-namespace-deployer
  namespace: acme-fintech-angular
subjects:
  - kind: Group
    name: horizon-qa-deployers
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: horizon-namespace-deployer
  apiGroup: rbac.authorization.k8s.io
```

Use either AWS-managed namespace-scoped access policy or custom RBAC. Do not grant cluster-admin unless the role is a platform-admin bootstrap role.

## Migrating an Existing Cluster-Scoped Role

Current demo example:

```text
cluster: horizon-eks-dev
principalArn: arn:aws:iam::426946630837:role/horizon-admin-role
current policy: arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy
current scope: cluster
target namespace: acme-fintech-angular
```

Step 1: ensure the namespace exists:

```bash
kubectl get namespace acme-fintech-angular \
  || kubectl create namespace acme-fintech-angular
```

Step 2: associate namespace-scoped edit access:

```bash
aws eks associate-access-policy \
  --region us-east-1 \
  --cluster-name horizon-eks-dev \
  --principal-arn arn:aws:iam::426946630837:role/horizon-admin-role \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy \
  --access-scope type=namespace,namespaces=acme-fintech-angular
```

Step 3: validate the namespace-scoped association:

```bash
aws eks list-associated-access-policies \
  --region us-east-1 \
  --cluster-name horizon-eks-dev \
  --principal-arn arn:aws:iam::426946630837:role/horizon-admin-role
```

Step 4: after validation, remove cluster-admin access:

```bash
aws eks disassociate-access-policy \
  --region us-east-1 \
  --cluster-name horizon-eks-dev \
  --principal-arn arn:aws:iam::426946630837:role/horizon-admin-role \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy
```

Step 5: run Horizon backend preflight again from the product UI or API:

```bash
curl -k \
  "https://<platform-host>/pipeline/api/environment-catalog/preflight/DEV?project_name=<project>&pipeline_kind=DEVOPS"
```

Expected:

- `Jenkins IRSA role`: `PASS`
- `Deployment role assumption`: `PASS`
- `EKS cluster visibility`: `PASS`
- `Namespace-scoped EKS access`: `PASS`

## Environment Catalog Mapping

Each deployable environment must point to the correct deploy role, cluster, and namespace template.

Example:

```yaml
environmentCatalog:
  environments:
    - name: QA
      displayName: Quality Assurance
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

Developer forms should only require:

- Project name
- Project type
- Repository URL
- Branch
- Target environment
- Notification recipients, if needed

AWS account IDs, role ARNs, buckets, clusters, and namespaces are resolved server-side from the Environment Catalog.

## Preflight Validation Checklist

Before a client runs a pipeline, the Horizon platform should validate:

| Check | Expected Result |
| --- | --- |
| Required Environment Catalog fields | `PASS` |
| Jenkins API backend secret | `PASS` |
| Jenkins runtime role ARN configured | `PASS` |
| Jenkins pod assumes IRSA role | `PASS` |
| Deployment role trusts Jenkins runtime role | `PASS` |
| Backend validation identity can assume deployment role | `PASS` |
| Artifact bucket access | `PASS` |
| ECR repository access | `PASS` |
| EKS cluster visibility | `PASS` |
| EKS access policy is namespace-scoped | `PASS` |

Manual validation commands:

```bash
kubectl exec deploy/jenkins -n <platform-namespace> -c jenkins -- aws sts get-caller-identity

kubectl exec deploy/jenkins -n <platform-namespace> -c jenkins -- \
  aws sts assume-role \
    --role-arn arn:aws:iam::<account-id>:role/HorizonQaDeployRole \
    --role-session-name jenkins-irsa-validation \
    --query AssumedRoleUser.Arn \
    --output text

aws eks list-associated-access-policies \
  --region <region> \
  --cluster-name <cluster-name> \
  --principal-arn arn:aws:iam::<account-id>:role/HorizonQaDeployRole
```

## Client Responsibility Matrix

| Item | Client Owns | Horizon Validates |
| --- | --- | --- |
| EKS OIDC provider | Yes | Yes |
| Jenkins IRSA role trust | Yes | Yes |
| Jenkins IRSA role permissions | Yes | Yes |
| Deploy role trust | Yes | Yes |
| Deploy role AWS permissions | Yes | Partially through preflight |
| EKS access entries | Yes | Yes |
| Namespaces | Yes | Yes |
| S3 artifact buckets | Yes | Yes |
| ECR repositories or repository prefixes | Yes | Yes |
| Environment Catalog values | Yes, with Horizon support | Yes |
| Jenkins backend API token secret | Client platform admin | Yes |
