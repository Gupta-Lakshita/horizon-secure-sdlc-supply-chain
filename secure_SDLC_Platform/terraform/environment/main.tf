terraform {
  required_version = ">= 1.3.9"

  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 4.57.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.23.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.9.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2.0"
    }
    cloudinit = {
      source  = "hashicorp/cloudinit"
      version = "~> 2.3.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

locals {
  resource_name_prefix      = var.resource_name_prefix != "" ? var.resource_name_prefix : var.client_id
  name_prefix               = "${local.resource_name_prefix}-${lower(var.environment_name)}"
  kms_alias_prefix          = var.kms_alias_prefix != "" ? var.kms_alias_prefix : "horizon/${var.client_id}"
  deploy_role_name          = var.deploy_role_name != "" ? var.deploy_role_name : substr("${local.name_prefix}-deploy-role", 0, 64)
  node_group_name           = var.node_group_name != "" ? var.node_group_name : substr("${local.name_prefix}-ng", 0, 38)
  node_group_iam_role_name  = var.node_group_iam_role_name != "" ? var.node_group_iam_role_name : substr("${local.name_prefix}-ng-role", 0, 64)
  ebs_csi_role_name         = var.ebs_csi_role_name != "" ? var.ebs_csi_role_name : substr("${local.name_prefix}-ebs-csi-role", 0, 64)
  created_kms_key           = var.create_kms_key ? aws_kms_key.environment[0].arn : ""
  kms_key_arn               = var.existing_kms_key_arn != "" ? var.existing_kms_key_arn : local.created_kms_key
  vpc_id                    = var.create_vpc ? module.vpc[0].vpc_id : var.existing_vpc_id
  subnet_ids                = var.create_vpc ? module.vpc[0].public_subnets : var.existing_subnet_ids
  effective_deploy_role_arn = var.create_deploy_role ? aws_iam_role.deploy[0].arn : var.deploy_role_arn
  access_namespaces         = var.eks_access_scope_type == "namespace" && var.namespace_name != "" ? [var.namespace_name] : []
  deploy_role_trust_arns    = compact([var.jenkins_runtime_role_arn, var.backend_validation_role_arn])
}

data "aws_iam_policy_document" "deploy_assume_role" {
  count = var.create_deploy_role ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = local.deploy_role_trust_arns
    }
  }
}

resource "aws_iam_role" "deploy" {
  count              = var.create_deploy_role ? 1 : 0
  name               = local.deploy_role_name
  assume_role_policy = data.aws_iam_policy_document.deploy_assume_role[0].json
  tags               = var.tags
}

resource "aws_iam_role_policy" "deploy" {
  count = var.create_deploy_role ? 1 : 0
  name  = substr("${local.deploy_role_name}-policy", 0, 128)
  role  = aws_iam_role.deploy[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EcrRepositoryAccess"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeImages",
          "ecr:DescribeRepositories",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:ListImages",
          "ecr:PutImage",
          "ecr:UploadLayerPart"
        ]
        Resource = "arn:aws:ecr:${var.aws_region}:${var.aws_account_id}:repository/${var.ecr_repository_name}"
      },
      {
        Sid    = "ArtifactBucketAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject"
        ]
        Resource = "arn:aws:s3:::${var.artifact_bucket_name}/*"
      },
      {
        Sid    = "ArtifactBucketList"
        Effect = "Allow"
        Action = [
          "s3:GetBucketLocation",
          "s3:ListBucket"
        ]
        Resource = "arn:aws:s3:::${var.artifact_bucket_name}"
      },
      {
        Sid      = "EksClusterDiscovery"
        Effect   = "Allow"
        Action   = "eks:DescribeCluster"
        Resource = "arn:aws:eks:${var.aws_region}:${var.aws_account_id}:cluster/${var.eks_cluster_name}"
      },
      {
        Sid    = "EksAccessPolicyReadOnlyValidation"
        Effect = "Allow"
        Action = [
          "eks:DescribeAccessEntry",
          "eks:ListAccessEntries",
          "eks:ListAssociatedAccessPolicies",
          "eks:ListAccessPolicies"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_key" "environment" {
  count                   = var.create_kms_key ? 1 : 0
  description             = "Horizon ${var.environment_name} environment key for ${var.client_id}"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = var.tags
}

resource "aws_kms_alias" "environment" {
  count         = var.create_kms_key ? 1 : 0
  name          = "alias/${local.kms_alias_prefix}/${lower(var.environment_name)}"
  target_key_id = aws_kms_key.environment[0].key_id
}

module "vpc" {
  count   = var.create_vpc ? 1 : 0
  source  = "terraform-aws-modules/vpc/aws"
  version = "4.0.0"

  name = "${local.name_prefix}-vpc"
  cidr = "10.40.0.0/16"

  azs            = ["${var.aws_region}a", "${var.aws_region}b"]
  public_subnets = ["10.40.1.0/24", "10.40.2.0/24"]

  enable_nat_gateway      = false
  map_public_ip_on_launch = true

  tags = var.tags
}

resource "aws_s3_bucket" "artifacts" {
  count  = var.create_artifact_bucket ? 1 : 0
  bucket = var.artifact_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "artifacts" {
  count  = var.create_artifact_bucket ? 1 : 0
  bucket = aws_s3_bucket.artifacts[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  count                   = var.create_artifact_bucket ? 1 : 0
  bucket                  = aws_s3_bucket.artifacts[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  count  = var.create_artifact_bucket ? 1 : 0
  bucket = aws_s3_bucket.artifacts[0].id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = local.kms_key_arn != "" ? local.kms_key_arn : null
      sse_algorithm     = local.kms_key_arn != "" ? "aws:kms" : "AES256"
    }
  }
}

resource "aws_ecr_repository" "application" {
  count = var.create_ecr_repository ? 1 : 0
  name  = var.ecr_repository_name
  tags  = var.tags

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = local.kms_key_arn != "" ? local.kms_key_arn : null
  }
}

resource "aws_secretsmanager_secret" "environment_placeholder" {
  count       = var.create_secret_prefix && var.secret_prefix != "" ? 1 : 0
  name        = "${var.secret_prefix}/installer-placeholder"
  description = "Placeholder secret proving Horizon installer can create environment secrets."
  kms_key_id  = local.kms_key_arn != "" ? local.kms_key_arn : null
  tags        = var.tags
}

resource "aws_acm_certificate" "environment" {
  count             = var.create_acm_certificate && var.base_domain != "" ? 1 : 0
  domain_name       = "*.${var.base_domain}"
  validation_method = "DNS"
  tags              = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

module "eks" {
  count   = var.create_eks_cluster ? 1 : 0
  source  = "terraform-aws-modules/eks/aws"
  version = "19.21.0"

  cluster_name                   = var.eks_cluster_name
  cluster_version                = var.kubernetes_version
  cluster_endpoint_public_access = var.eks_endpoint_public_access

  vpc_id     = local.vpc_id
  subnet_ids = local.subnet_ids

  eks_managed_node_groups = var.create_node_group ? {
    default = {
      name                     = local.node_group_name
      iam_role_name            = local.node_group_iam_role_name
      iam_role_use_name_prefix = false
      instance_types           = var.node_instance_types
      desired_size             = var.node_desired_size
      min_size                 = var.node_min_size
      max_size                 = var.node_max_size
    }
  } : {}

  tags = var.tags
}

data "aws_iam_policy_document" "ebs_csi_assume_role" {
  count = var.create_ebs_csi_driver && var.create_eks_cluster ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [module.eks[0].oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${module.eks[0].oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${module.eks[0].oidc_provider}:sub"
      values   = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  count              = var.create_ebs_csi_driver && var.create_eks_cluster ? 1 : 0
  name               = local.ebs_csi_role_name
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_assume_role[0].json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  count      = var.create_ebs_csi_driver && var.create_eks_cluster ? 1 : 0
  role       = aws_iam_role.ebs_csi[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_eks_addon" "ebs_csi" {
  count                    = var.create_ebs_csi_driver ? 1 : 0
  cluster_name             = var.eks_cluster_name
  addon_name               = "aws-ebs-csi-driver"
  service_account_role_arn = var.create_eks_cluster ? aws_iam_role.ebs_csi[0].arn : null
  tags                     = var.tags

  timeouts {
    create = "30m"
    update = "30m"
    delete = "30m"
  }

  depends_on = [
    module.eks,
    aws_iam_role_policy_attachment.ebs_csi
  ]
}

resource "null_resource" "eks_authentication_mode" {
  count = var.create_eks_access_entry ? 1 : 0

  triggers = {
    cluster_name = var.eks_cluster_name
    aws_region   = var.aws_region
    mode         = "API_AND_CONFIG_MAP"
  }

  provisioner "local-exec" {
    interpreter = ["/usr/bin/env", "bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      current_mode="$(aws eks describe-cluster \
        --region "${self.triggers.aws_region}" \
        --name "${self.triggers.cluster_name}" \
        --query 'cluster.accessConfig.authenticationMode' \
        --output text)"

      if [ "$${current_mode}" = "CONFIG_MAP" ]; then
        update_id="$(aws eks update-cluster-config \
          --region "${self.triggers.aws_region}" \
          --name "${self.triggers.cluster_name}" \
          --access-config authenticationMode="${self.triggers.mode}" \
          --query 'update.id' \
          --output text)"

        for i in $(seq 1 60); do
          status="$(aws eks describe-update \
            --region "${self.triggers.aws_region}" \
            --name "${self.triggers.cluster_name}" \
            --update-id "$${update_id}" \
            --query 'update.status' \
            --output text)"
          [ "$${status}" = "Successful" ] && exit 0
          [ "$${status}" = "Failed" ] && exit 1
          [ "$${status}" = "Cancelled" ] && exit 1
          sleep 10
        done
        echo "Timed out waiting for EKS authentication mode update" >&2
        exit 1
      fi
    EOT
  }

  depends_on = [module.eks]
}

resource "null_resource" "application_namespace" {
  count = var.create_namespace && var.namespace_name != "" ? 1 : 0

  triggers = {
    cluster_name     = var.eks_cluster_name
    aws_region       = var.aws_region
    namespace_name   = var.namespace_name
    client_id        = var.client_id
    environment_name = lower(var.environment_name)
    managed_by       = lookup(var.tags, "ManagedBy", "horizon-enterprise-installer")
  }

  provisioner "local-exec" {
    interpreter = ["/usr/bin/env", "bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      export KUBECONFIG="$(mktemp "$${TMPDIR:-/tmp}/horizon-kubeconfig.XXXXXX")"
      trap 'rm -f "$${KUBECONFIG}"' EXIT
      aws eks update-kubeconfig --region "${self.triggers.aws_region}" --name "${self.triggers.cluster_name}" >/dev/null
      kubectl create namespace "${self.triggers.namespace_name}" --dry-run=client -o yaml | kubectl apply -f -
      kubectl label namespace "${self.triggers.namespace_name}" \
        app.kubernetes.io/managed-by="${self.triggers.managed_by}" \
        horizonrelevance.com/client="${self.triggers.client_id}" \
        horizonrelevance.com/env="${self.triggers.environment_name}" \
        --overwrite >/dev/null
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/usr/bin/env", "bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      export KUBECONFIG="$(mktemp "$${TMPDIR:-/tmp}/horizon-kubeconfig.XXXXXX")"
      trap 'rm -f "$${KUBECONFIG}"' EXIT
      aws eks update-kubeconfig --region "${self.triggers.aws_region}" --name "${self.triggers.cluster_name}" >/dev/null
      kubectl delete namespace "${self.triggers.namespace_name}" --ignore-not-found=true
    EOT
  }

  depends_on = [module.eks]
}

resource "null_resource" "eks_access_entry" {
  count = var.create_eks_access_entry && (var.create_deploy_role || var.deploy_role_arn != "") ? 1 : 0

  triggers = {
    cluster_name  = var.eks_cluster_name
    principal_arn = local.effective_deploy_role_arn
    policy_arn    = var.eks_access_policy_arn
    scope_type    = var.eks_access_scope_type
    namespaces    = join(",", local.access_namespaces)
  }

  provisioner "local-exec" {
    interpreter = ["/usr/bin/env", "bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      aws eks describe-access-entry \
        --cluster-name "${self.triggers.cluster_name}" \
        --principal-arn "${self.triggers.principal_arn}" >/dev/null 2>&1 || \
      aws eks create-access-entry \
        --cluster-name "${self.triggers.cluster_name}" \
        --principal-arn "${self.triggers.principal_arn}" \
        --type STANDARD >/dev/null

      if [ "${self.triggers.scope_type}" = "namespace" ] && [ -n "${self.triggers.namespaces}" ]; then
        aws eks associate-access-policy \
          --cluster-name "${self.triggers.cluster_name}" \
          --principal-arn "${self.triggers.principal_arn}" \
          --policy-arn "${self.triggers.policy_arn}" \
          --access-scope "type=namespace,namespaces=${self.triggers.namespaces}" >/dev/null 2>&1 || true
      else
        aws eks associate-access-policy \
          --cluster-name "${self.triggers.cluster_name}" \
          --principal-arn "${self.triggers.principal_arn}" \
          --policy-arn "${self.triggers.policy_arn}" \
          --access-scope "type=cluster" >/dev/null 2>&1 || true
      fi
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/usr/bin/env", "bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      aws eks disassociate-access-policy \
        --cluster-name "${self.triggers.cluster_name}" \
        --principal-arn "${self.triggers.principal_arn}" \
        --policy-arn "${self.triggers.policy_arn}" >/dev/null 2>&1 || true
      aws eks delete-access-entry \
        --cluster-name "${self.triggers.cluster_name}" \
        --principal-arn "${self.triggers.principal_arn}" >/dev/null 2>&1 || true
    EOT
  }

  depends_on = [
    module.eks,
    null_resource.eks_authentication_mode,
    aws_iam_role.deploy,
    null_resource.application_namespace
  ]
}

resource "null_resource" "ingress_nginx" {
  count = var.create_ingress_controller ? 1 : 0

  triggers = {
    cluster_name = var.eks_cluster_name
    aws_region   = var.aws_region
    release_name = "ingress-nginx"
    namespace    = "ingress-nginx"
    repository   = "https://kubernetes.github.io/ingress-nginx"
    chart        = "ingress-nginx"
  }

  provisioner "local-exec" {
    interpreter = ["/usr/bin/env", "bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      export KUBECONFIG="$(mktemp "$${TMPDIR:-/tmp}/horizon-kubeconfig.XXXXXX")"
      trap 'rm -f "$${KUBECONFIG}"' EXIT
      aws eks update-kubeconfig --region "${self.triggers.aws_region}" --name "${self.triggers.cluster_name}" >/dev/null
      helm repo add ingress-nginx "${self.triggers.repository}" >/dev/null 2>&1 || true
      helm repo update ingress-nginx >/dev/null
      helm upgrade --install "${self.triggers.release_name}" "${self.triggers.chart}" \
        --repo "${self.triggers.repository}" \
        --namespace "${self.triggers.namespace}" \
        --create-namespace
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/usr/bin/env", "bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      export KUBECONFIG="$(mktemp "$${TMPDIR:-/tmp}/horizon-kubeconfig.XXXXXX")"
      trap 'rm -f "$${KUBECONFIG}"' EXIT
      aws eks update-kubeconfig --region "${self.triggers.aws_region}" --name "${self.triggers.cluster_name}" >/dev/null
      helm uninstall "${self.triggers.release_name}" --namespace "${self.triggers.namespace}" >/dev/null 2>&1 || true
    EOT
  }

  depends_on = [module.eks]
}
