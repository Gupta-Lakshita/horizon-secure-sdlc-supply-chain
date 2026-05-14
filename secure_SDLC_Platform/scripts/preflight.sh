#!/usr/bin/env bash
set -euo pipefail

VALUES_FILE=""

usage() {
  echo "Usage: $0 -f <client-values.yaml>"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--file)
      VALUES_FILE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ -z "${VALUES_FILE}" ]]; then
  usage
  exit 1
fi

if [[ ! -f "${VALUES_FILE}" ]]; then
  echo "Values file not found: ${VALUES_FILE}"
  exit 1
fi

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_cmd aws
require_cmd kubectl
require_cmd helm
require_cmd terraform

echo "== Horizon Enterprise Installer Preflight =="
echo "Values file: ${VALUES_FILE}"

echo "Checking AWS identity..."
aws sts get-caller-identity >/dev/null

echo "Checking Helm client..."
helm version --short >/dev/null

echo "Checking kubectl client..."
kubectl version --client >/dev/null

echo "Checking Terraform client..."
terraform version >/dev/null

echo "Checking required values..."
grep -q "mode:" "${VALUES_FILE}" || { echo "Missing installer mode"; exit 1; }
grep -q "clientId:" "${VALUES_FILE}" || { echo "Missing license.clientId"; exit 1; }
grep -q "syncEndpoint:" "${VALUES_FILE}" || { echo "Missing license.syncEndpoint"; exit 1; }
grep -q "accounts:" "${VALUES_FILE}" || { echo "Missing aws.accounts mapping"; exit 1; }
grep -q "accessModel:" "${VALUES_FILE}" || { echo "Missing accessModel"; exit 1; }
grep -q "iamMode: validation-only" "${VALUES_FILE}" || { echo "Enterprise paid installs must use accessModel.iamMode: validation-only"; exit 1; }
grep -q "eksAccessMode: namespace-scoped" "${VALUES_FILE}" || { echo "Enterprise paid installs must use accessModel.eksAccessMode: namespace-scoped"; exit 1; }
grep -q "irsaRoleArn:" "${VALUES_FILE}" || { echo "Missing accessModel.jenkins.irsaRoleArn"; exit 1; }

if grep -q "mode: online-sync" "${VALUES_FILE}"; then
  echo "Online license sync enabled. Ensure outbound HTTPS to license endpoint is allowed."
fi

if grep -q "mode: byo-infra" "${VALUES_FILE}"; then
  echo "BYO infrastructure mode detected. Checking current Kubernetes access..."
  kubectl cluster-info >/dev/null
fi

AWS_REGION="$(grep -A3 '^aws:' "${VALUES_FILE}" | awk '/region:/ {print $2; exit}' | tr -d '\"')"
AWS_REGION="${AWS_REGION:-us-east-1}"

echo "Validating client-created IAM roles exist..."
grep -E '(^|[[:space:]])(roleArn|sourceRoleArn|targetRoleArn|irsaRoleArn):' "${VALUES_FILE}" \
  | awk -F': ' '{print $2}' \
  | tr -d '"' \
  | sort -u \
  | while read -r ROLE_ARN; do
      [[ -z "${ROLE_ARN}" ]] && continue
      ROLE_NAME="${ROLE_ARN##*/}"
      echo "  - ${ROLE_NAME}"
      aws iam get-role --role-name "${ROLE_NAME}" >/dev/null
    done

echo "Validating EKS clusters are visible..."
grep -E '(^|[[:space:]])clusterName:' "${VALUES_FILE}" \
  | awk -F': ' '{print $2}' \
  | tr -d '"' \
  | sort -u \
  | while read -r CLUSTER_NAME; do
      [[ -z "${CLUSTER_NAME}" ]] && continue
      echo "  - ${CLUSTER_NAME}"
      aws eks describe-cluster --region "${AWS_REGION}" --name "${CLUSTER_NAME}" >/dev/null
    done

echo "Validating namespace-scoped EKS access mappings are declared..."
grep -q "namespaceTemplate:" "${VALUES_FILE}" || { echo "Missing namespaceTemplate in environmentCatalog"; exit 1; }
grep -q "backendPreflightEnforced: true" "${VALUES_FILE}" || { echo "backend preflight should be enforced for enterprise paid installs"; exit 1; }

echo "Preflight passed."
