#!/usr/bin/env bash
set -euo pipefail

VALUES_FILE=""
ENVIRONMENT=""
SKIP_AWS="false"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="${SCRIPT_DIR}/values-helper.rb"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--file) VALUES_FILE="${2:-}"; shift 2 ;;
    -e|--environment) ENVIRONMENT="${2:-}"; shift 2 ;;
    --skip-aws) SKIP_AWS="true"; shift ;;
    *) shift ;;
  esac
done

[[ -f "${VALUES_FILE}" ]] || { echo "Values file not found: ${VALUES_FILE}" >&2; exit 1; }
if [[ -n "${ENVIRONMENT}" ]]; then ruby "${HELPER}" validate --file "${VALUES_FILE}" --environment "${ENVIRONMENT}"; else ruby "${HELPER}" validate --file "${VALUES_FILE}"; fi
NAMESPACE="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path installer.namespace)"
NAMESPACE="${NAMESPACE:-horizon-platform}"
echo "== Horizon Enterprise Installer Validation =="
echo "Namespace: ${NAMESPACE}"
[[ -n "${ENVIRONMENT}" ]] && echo "Environment: ${ENVIRONMENT}"
[[ "${SKIP_AWS}" == "true" ]] && { echo "Skipping Kubernetes/AWS runtime validation because --skip-aws was provided."; exit 0; }
kubectl get namespace "${NAMESPACE}" >/dev/null
kubectl get configmap horizon-enterprise-config -n "${NAMESPACE}" >/dev/null
kubectl get secret horizon-enterprise-license-defaults -n "${NAMESPACE}" >/dev/null
kubectl get pods -n "${NAMESPACE}"
echo "Validation completed."
