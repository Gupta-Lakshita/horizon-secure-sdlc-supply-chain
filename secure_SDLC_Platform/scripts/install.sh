#!/usr/bin/env bash
set -euo pipefail

PHASE="platform"
VALUES_FILE=""
ENVIRONMENT=""
DRY_RUN="false"
AUTO_APPROVE="false"
CATALOG_SYNC_INSECURE_FLAG="false"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
HELPER="${SCRIPT_DIR}/values-helper.rb"
GENERATED_DIR="${ROOT_DIR}/.generated"

usage() {
  echo "Usage: $0 --phase <state|infra|platform|catalog|all> -f <client-values.yaml> [--environment ENV] [--dry-run] [--auto-approve] [--insecure-catalog-sync]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="${2:-}"; shift 2 ;;
    -f|--file) VALUES_FILE="${2:-}"; shift 2 ;;
    -e|--environment) ENVIRONMENT="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN="true"; shift ;;
    --auto-approve) AUTO_APPROVE="true"; shift ;;
    --insecure-catalog-sync) CATALOG_SYNC_INSECURE_FLAG="true"; shift ;;
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

catalog_backend_base_url() {
  local explicit host path scheme url
  explicit="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path catalogSync.backendUrl)"
  [[ -z "${explicit}" ]] && explicit="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path installer.backendUrl)"
  [[ -z "${explicit}" ]] && explicit="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path domain.platformHosts.backendUrl)"
  if [[ -n "${explicit}" ]]; then
    echo "${explicit%/}"
    return
  fi

  host="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path domain.platformHosts.frontendHost)"
  path="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path domain.platformHosts.backendPath)"
  scheme="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path domain.platformHosts.scheme)"
  [[ -z "${scheme}" ]] && scheme="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path domain.scheme)"
  [[ -z "${scheme}" ]] && scheme="https"
  [[ -z "${path}" ]] && path="/pipeline/api"
  [[ "${path}" != /* ]] && path="/${path}"

  if [[ -z "${host}" ]]; then
    url="http://localhost:8000"
  elif [[ "${host}" =~ ^https?:// ]]; then
    url="${host}${path}"
  else
    url="${scheme}://${host}${path}"
  fi
  echo "${url%/}"
}

catalog_tls_verify() {
  local tls_verify
  tls_verify="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path catalogSync.tlsVerify)"
  if [[ "${CATALOG_SYNC_INSECURE:-false}" == "true" || "${CATALOG_SYNC_INSECURE_FLAG}" == "true" || "${tls_verify}" == "false" ]]; then
    echo "false"
  else
    echo "true"
  fi
}

catalog_ca_bundle() {
  local ca_bundle
  ca_bundle="${CATALOG_SYNC_CA_BUNDLE:-}"
  [[ -z "${ca_bundle}" ]] && ca_bundle="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path catalogSync.caBundlePath)"
  echo "${ca_bundle}"
}

write_catalog_payload() {
  local env_lc payload_file output_file terraform_output_file files backend_file
  env_lc="all"
  [[ -n "${ENVIRONMENT}" ]] && env_lc="$(echo "${ENVIRONMENT}" | tr '[:upper:]' '[:lower:]')"
  payload_file="${GENERATED_DIR}/catalog-${env_lc}.json"
  terraform_output_file=""

  if [[ -n "${ENVIRONMENT}" && "${DRY_RUN}" != "true" ]]; then
    files="$(write_env_files)"
    backend_file="${files%%|*}"
    output_file="${GENERATED_DIR}/terraform-output-${env_lc}.json"
    if terraform -chdir="${ROOT_DIR}/terraform/environment" init -reconfigure -backend-config="${backend_file}" >/dev/null 2>&1 &&
       terraform -chdir="${ROOT_DIR}/terraform/environment" output -json > "${output_file}" 2>/dev/null; then
      terraform_output_file="${output_file}"
      echo "Terraform outputs loaded: ${output_file}" >&2
    else
      echo "Warning: Terraform outputs are unavailable; catalog sync will use values file data." >&2
    fi
  fi

  if [[ -n "${ENVIRONMENT}" ]]; then
    if [[ -n "${terraform_output_file}" ]]; then
      ruby "${HELPER}" catalog-payload --file "${VALUES_FILE}" --environment "${ENVIRONMENT}" --terraform-output "${terraform_output_file}" > "${payload_file}"
    else
      ruby "${HELPER}" catalog-payload --file "${VALUES_FILE}" --environment "${ENVIRONMENT}" > "${payload_file}"
    fi
  else
    ruby "${HELPER}" catalog-payload --file "${VALUES_FILE}" > "${payload_file}"
  fi
  echo "${payload_file}"
}

run_catalog() {
  echo "== Environment Catalog sync phase =="
  command -v curl >/dev/null || { echo "Missing required command: curl" >&2; exit 1; }
  local payload_file base_url endpoint curl_args tls_verify ca_bundle curl_output curl_status
  payload_file="$(write_catalog_payload)"
  base_url="$(catalog_backend_base_url)"
  if [[ "${base_url}" == */environment-catalog ]]; then
    endpoint="${base_url}"
  else
    endpoint="${base_url%/}/environment-catalog"
  fi

  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "Dry-run: would upsert Environment Catalog via ${endpoint}"
    echo "Generated payload: ${payload_file}"
    cat "${payload_file}"
    return
  fi

  curl_args=(--fail --show-error --silent -X POST "${endpoint}" -H "Content-Type: application/json" --data-binary "@${payload_file}")
  [[ -n "${CATALOG_SYNC_TOKEN:-}" ]] && curl_args+=(-H "Authorization: Bearer ${CATALOG_SYNC_TOKEN}")
  tls_verify="$(catalog_tls_verify)"
  ca_bundle="$(catalog_ca_bundle)"
  if [[ "${tls_verify}" == "false" ]]; then
    echo "Warning: catalogSync.tlsVerify=false. Skipping TLS certificate verification for catalog sync." >&2
    curl_args+=(--insecure)
  elif [[ -n "${ca_bundle}" ]]; then
    curl_args+=(--cacert "${ca_bundle}")
  fi

  set +e
  curl_output="$(curl "${curl_args[@]}" 2>&1)"
  curl_status=$?
  set -e
  if [[ ${curl_status} -ne 0 ]]; then
    echo "${curl_output}" >&2
    if [[ ${curl_status} -eq 60 ]]; then
      cat >&2 <<EOF

Catalog sync failed because curl could not verify the backend TLS certificate.
Enterprise fix: install a trusted ACM/public certificate chain or set catalogSync.caBundlePath to the client CA bundle.
Internal demo workaround: set catalogSync.tlsVerify: false in the values file, pass --insecure-catalog-sync, or run with CATALOG_SYNC_INSECURE=true.
EOF
    fi
    exit "${curl_status}"
  fi
  echo "${curl_output}"
  echo
  echo "Environment Catalog synced through ${endpoint}"
}

case "${PHASE}" in
  state) run_state ;;
  infra) run_infra ;;
  platform) run_platform ;;
  catalog) run_catalog ;;
  all) run_state; run_infra; run_platform; run_catalog ;;
  *) usage; exit 1 ;;
esac
