#!/usr/bin/env bash
set -euo pipefail

VALUES_FILE=""
ENVIRONMENT=""
DRY_RUN="false"
CONFIRM=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
HELPER="${SCRIPT_DIR}/values-helper.rb"
GENERATED_DIR="${ROOT_DIR}/.generated"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--file) VALUES_FILE="${2:-}"; shift 2 ;;
    -e|--environment) ENVIRONMENT="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN="true"; shift ;;
    --confirm) CONFIRM="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done

[[ -f "${VALUES_FILE}" && -n "${ENVIRONMENT}" ]] || { echo "Usage: $0 -f <values.yaml> --environment ENV [--dry-run] [--confirm ENV]" >&2; exit 1; }
mkdir -p "${GENERATED_DIR}"
ruby "${HELPER}" validate --file "${VALUES_FILE}" --environment "${ENVIRONMENT}"
echo "== Horizon Enterprise Installer Destroy Plan =="
ruby "${HELPER}" destroy-items --file "${VALUES_FILE}" --environment "${ENVIRONMENT}" || true
env_lc="$(echo "${ENVIRONMENT}" | tr '[:upper:]' '[:lower:]')"
backend_file="${GENERATED_DIR}/backend-${env_lc}.hcl"
tfvars_file="${GENERATED_DIR}/${env_lc}.auto.tfvars.json"
ruby "${HELPER}" backend-config --file "${VALUES_FILE}" --scope environment --environment "${ENVIRONMENT}" > "${backend_file}"
ruby "${HELPER}" tfvars --file "${VALUES_FILE}" --environment "${ENVIRONMENT}" > "${tfvars_file}"
[[ "${DRY_RUN}" == "true" ]] && { echo "Dry-run: would run Terraform destroy for ${ENVIRONMENT}."; exit 0; }
[[ "${CONFIRM}" == "${ENVIRONMENT}" ]] || { echo "Refusing to destroy. Re-run with --confirm ${ENVIRONMENT}." >&2; exit 1; }
terraform -chdir="${ROOT_DIR}/terraform/environment" init -reconfigure -backend-config="${backend_file}"
terraform -chdir="${ROOT_DIR}/terraform/environment" destroy -var-file="${tfvars_file}"
