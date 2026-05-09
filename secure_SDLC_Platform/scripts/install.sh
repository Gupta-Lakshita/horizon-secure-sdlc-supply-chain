#!/usr/bin/env bash
set -euo pipefail

PHASE="platform"
VALUES_FILE=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  echo "Usage: $0 --phase <infra|platform|all> -f <client-values.yaml>"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase)
      PHASE="${2:-}"
      shift 2
      ;;
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

run_infra() {
  echo "== Infrastructure phase =="
  echo "This skeleton initializes Terraform modules. Review backend configuration before applying in a client account."
  terraform -chdir="${ROOT_DIR}/terraform/bootstrap" init
  terraform -chdir="${ROOT_DIR}/terraform/bootstrap" plan
  echo "Run terraform apply only after client approval."
}

run_platform() {
  echo "== Platform phase =="
  NAMESPACE="$(grep -A5 '^installer:' "${VALUES_FILE}" | awk '/namespace:/ {print $2; exit}' | tr -d '\"')"
  RELEASE="$(grep -A5 '^installer:' "${VALUES_FILE}" | awk '/releaseName:/ {print $2; exit}' | tr -d '\"')"
  NAMESPACE="${NAMESPACE:-horizon-platform}"
  RELEASE="${RELEASE:-horizon-ai-devsecops}"

  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
  helm upgrade --install "${RELEASE}" "${ROOT_DIR}/helm/horizon-platform" \
    --namespace "${NAMESPACE}" \
    --values "${VALUES_FILE}"
}

case "${PHASE}" in
  infra)
    run_infra
    ;;
  platform)
    run_platform
    ;;
  all)
    run_infra
    run_platform
    ;;
  *)
    usage
    exit 1
    ;;
esac

