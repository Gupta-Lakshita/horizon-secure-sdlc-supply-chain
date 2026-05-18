#!/usr/bin/env bash
set -euo pipefail

PHASE="platform"
VALUES_FILE=""
ENVIRONMENT=""
DRY_RUN="false"
AUTO_APPROVE="false"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
HELPER="${SCRIPT_DIR}/values-helper.rb"
GENERATED_DIR="${ROOT_DIR}/.generated"

usage() {
  echo "Usage: $0 --phase <state|infra|platform|all> -f <client-values.yaml> [--environment ENV] [--dry-run] [--auto-approve]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="${2:-}"; shift 2 ;;
    -f|--file) VALUES_FILE="${2:-}"; shift 2 ;;
    -e|--environment) ENVIRONMENT="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN="true"; shift ;;
    --auto-approve) AUTO_APPROVE="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -f "${VALUES_FILE}" ]] || { usage; exit 1; }
for cmd in ruby terraform helm; do command -v "${cmd}" >/dev/null || { echo "Missing required command: ${cmd}" >&2; exit 1; }; done
mkdir -p "${GENERATED_DIR}"

if [[ -n "${ENVIRONMENT}" ]]; then
  ruby "${HELPER}" validate --file "${VALUES_FILE}" --environment "${ENVIRONMENT}"
else
  ruby "${HELPER}" validate --file "${VALUES_FILE}"
fi

write_env_files() {
  [[ -n "${ENVIRONMENT}" ]] || { echo "--environment is required for infra phase" >&2; exit 1; }
  local env_lc backend_file tfvars_file
  env_lc="$(echo "${ENVIRONMENT}" | tr '[:upper:]' '[:lower:]')"
  backend_file="${GENERATED_DIR}/backend-${env_lc}.hcl"
  tfvars_file="${GENERATED_DIR}/${env_lc}.auto.tfvars.json"
  ruby "${HELPER}" backend-config --file "${VALUES_FILE}" --scope environment --environment "${ENVIRONMENT}" > "${backend_file}"
  ruby "${HELPER}" tfvars --file "${VALUES_FILE}" --environment "${ENVIRONMENT}" > "${tfvars_file}"
  echo "${backend_file}|${tfvars_file}"
}

run_state() {
  echo "== Terraform state backend phase =="
  local tfvars_file="${GENERATED_DIR}/state-backend.auto.tfvars.json"
  ruby "${HELPER}" state-tfvars --file "${VALUES_FILE}" > "${tfvars_file}"
  [[ "${DRY_RUN}" == "true" ]] && { echo "Dry-run: would initialize and plan Terraform state backend resources."; echo "Generated: ${tfvars_file}"; return; }
  terraform -chdir="${ROOT_DIR}/terraform/bootstrap" init
  terraform -chdir="${ROOT_DIR}/terraform/bootstrap" plan -var-file="${tfvars_file}"
  if [[ "${AUTO_APPROVE}" == "true" ]]; then
    terraform -chdir="${ROOT_DIR}/terraform/bootstrap" apply -auto-approve -var-file="${tfvars_file}"
  else
    echo "Plan completed. Re-run with --auto-approve to apply state backend resources."
  fi
}

run_infra() {
  echo "== Infrastructure phase =="
  local files backend_file tfvars_file
  files="$(write_env_files)"
  backend_file="${files%%|*}"
  tfvars_file="${files##*|}"
  ruby "${HELPER}" plan --file "${VALUES_FILE}" --environment "${ENVIRONMENT}"
  [[ "${DRY_RUN}" == "true" ]] && { echo "Dry-run: would run Terraform init/plan for ${ENVIRONMENT}."; echo "Generated backend config: ${backend_file}"; echo "Generated tfvars: ${tfvars_file}"; return; }
  terraform -chdir="${ROOT_DIR}/terraform/environment" init -reconfigure -backend-config="${backend_file}"
  terraform -chdir="${ROOT_DIR}/terraform/environment" plan -var-file="${tfvars_file}"
  if [[ "${AUTO_APPROVE}" == "true" ]]; then
    terraform -chdir="${ROOT_DIR}/terraform/environment" apply -auto-approve -var-file="${tfvars_file}"
  else
    echo "Plan completed. Re-run with --auto-approve to apply ${ENVIRONMENT} infrastructure."
  fi
}

run_platform() {
  echo "== Platform phase =="
  local namespace release
  namespace="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path installer.namespace)"
  release="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path installer.releaseName)"
  namespace="${namespace:-horizon-platform}"
  release="${release:-horizon-ai-devsecops}"
  if [[ "${DRY_RUN}" == "true" ]]; then
    helm template "${release}" "${ROOT_DIR}/helm/horizon-platform" --namespace "${namespace}" --values "${VALUES_FILE}" >/dev/null
    echo "Dry-run: Helm template rendered successfully for release ${release} in namespace ${namespace}."
    return
  fi
  command -v kubectl >/dev/null || { echo "Missing required command: kubectl" >&2; exit 1; }
  kubectl create namespace "${namespace}" --dry-run=client -o yaml | kubectl apply -f -
  helm upgrade --install "${release}" "${ROOT_DIR}/helm/horizon-platform" --namespace "${namespace}" --values "${VALUES_FILE}"
}

case "${PHASE}" in
  state) run_state ;;
  infra) run_infra ;;
  platform) run_platform ;;
  all) run_state; run_infra; run_platform ;;
  *) usage; exit 1 ;;
esac
