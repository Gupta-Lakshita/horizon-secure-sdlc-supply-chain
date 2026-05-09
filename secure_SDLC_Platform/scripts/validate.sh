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

NAMESPACE="$(grep -A5 '^installer:' "${VALUES_FILE}" | awk '/namespace:/ {print $2; exit}' | tr -d '\"')"
NAMESPACE="${NAMESPACE:-horizon-platform}"

echo "== Horizon Enterprise Installer Validation =="
echo "Namespace: ${NAMESPACE}"

echo "Checking namespace..."
kubectl get namespace "${NAMESPACE}" >/dev/null

echo "Checking installer config..."
kubectl get configmap horizon-enterprise-config -n "${NAMESPACE}" >/dev/null

echo "Checking license defaults..."
kubectl get secret horizon-enterprise-license-defaults -n "${NAMESPACE}" >/dev/null

echo "Checking platform pods..."
kubectl get pods -n "${NAMESPACE}"

echo "Validation completed. Next validate application-level URLs, identity login, license status, ECR push, S3 artifact upload, and a sample pipeline trigger."

