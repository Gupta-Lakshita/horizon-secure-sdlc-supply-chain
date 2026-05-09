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

if grep -q "mode: online-sync" "${VALUES_FILE}"; then
  echo "Online license sync enabled. Ensure outbound HTTPS to license endpoint is allowed."
fi

if grep -q "mode: byo-infra" "${VALUES_FILE}"; then
  echo "BYO infrastructure mode detected. Checking current Kubernetes access..."
  kubectl cluster-info >/dev/null
fi

echo "Preflight passed."

