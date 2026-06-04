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

terraform_plugin_failure() {
  local log_file="$1"
  grep -Eq 'Failed to load plugin schemas|Unrecognized remote plugin message|failed to instantiate provider' "${log_file}"
}

repair_environment_terraform_cache() {
  local backend_file="$1"
  echo "Detected a local Terraform provider plugin cache issue. Rebuilding local provider cache..."
  rm -rf "${ROOT_DIR}/terraform/environment/.terraform/providers"
  terraform -chdir="${ROOT_DIR}/terraform/environment" init -reconfigure -upgrade -backend-config="${backend_file}"
}

run_environment_terraform() {
  local backend_file="$1"
  shift
  local env_lc log_file rc
  env_lc="$(echo "${ENVIRONMENT}" | tr '[:upper:]' '[:lower:]')"
  log_file="${GENERATED_DIR}/terraform-${env_lc}-$(date +%s).log"

  set +e
  terraform -chdir="${ROOT_DIR}/terraform/environment" "$@" 2>&1 | tee "${log_file}"
  rc=${PIPESTATUS[0]}
  set -e

  if [[ ${rc} -ne 0 ]] && terraform_plugin_failure "${log_file}"; then
    repair_environment_terraform_cache "${backend_file}"
    set +e
    terraform -chdir="${ROOT_DIR}/terraform/environment" "$@"
    rc=$?
    set -e
  fi

  return "${rc}"
}

run_existing_resource_checks() {
  echo "== Existing resource preflight =="
  command -v aws >/dev/null || { echo "Missing required command: aws" >&2; exit 1; }
  aws sts get-caller-identity >/dev/null

  local failures=0
  local kind name region source
  local check_command=(ruby "${HELPER}" checks --file "${VALUES_FILE}")
  [[ -n "${ENVIRONMENT}" ]] && check_command+=(--environment "${ENVIRONMENT}")

  while IFS=$'\t' read -r kind name region source; do
    [[ -z "${kind}" || -z "${name}" ]] && continue
    echo "  - ${source}: ${kind} ${name}"
    case "${kind}" in
      s3_bucket)
        aws s3api head-bucket --bucket "${name}" >/dev/null || failures=1
        ;;
      dynamodb_table)
        aws dynamodb describe-table --region "${region}" --table-name "${name}" >/dev/null || failures=1
        ;;
      ecr_repository)
        aws ecr describe-repositories --region "${region}" --repository-names "${name}" >/dev/null || failures=1
        ;;
      eks_cluster)
        aws eks describe-cluster --region "${region}" --name "${name}" >/dev/null || failures=1
        ;;
      iam_role)
        aws iam get-role --role-name "${name}" >/dev/null || failures=1
        ;;
    esac
  done < <("${check_command[@]}")

  if [[ "${failures}" -ne 0 ]]; then
    echo "Existing resource preflight failed. Fix the missing/invalid resources above or update the values file before running Terraform apply." >&2
    exit 1
  fi
}

reconcile_environment_placeholder_secret() {
  local tfvars_file="$1"
  local create_secret_prefix secret_prefix aws_region secret_name describe_output deleted_date resource_address

  create_secret_prefix="$(ruby -rjson -e 'v = JSON.parse(File.read(ARGV[0])); puts v["create_secret_prefix"]' "${tfvars_file}")"
  secret_prefix="$(ruby -rjson -e 'v = JSON.parse(File.read(ARGV[0])); puts v["secret_prefix"].to_s' "${tfvars_file}")"
  aws_region="$(ruby -rjson -e 'v = JSON.parse(File.read(ARGV[0])); puts v["aws_region"].to_s' "${tfvars_file}")"

  [[ "${create_secret_prefix}" == "true" && -n "${secret_prefix}" ]] || return 0

  secret_name="${secret_prefix}/installer-placeholder"
  resource_address='aws_secretsmanager_secret.environment_placeholder[0]'

  set +e
  describe_output="$(aws secretsmanager describe-secret --region "${aws_region}" --secret-id "${secret_name}" 2>&1)"
  local describe_rc=$?
  set -e

  if [[ ${describe_rc} -ne 0 ]]; then
    if grep -q "ResourceNotFoundException" <<< "${describe_output}"; then
      return 0
    fi
    echo "${describe_output}" >&2
    return "${describe_rc}"
  fi

  deleted_date="$(ruby -rjson -e 'secret = JSON.parse(STDIN.read); puts secret["DeletedDate"].to_s' <<< "${describe_output}")"
  if [[ -n "${deleted_date}" ]]; then
    echo "Restoring Secrets Manager placeholder secret scheduled for deletion: ${secret_name}"
    aws secretsmanager restore-secret --region "${aws_region}" --secret-id "${secret_name}" >/dev/null
  fi

  if ! terraform -chdir="${ROOT_DIR}/terraform/environment" state show "${resource_address}" >/dev/null 2>&1; then
    echo "Importing existing Secrets Manager placeholder secret into Terraform state: ${secret_name}"
    terraform -chdir="${ROOT_DIR}/terraform/environment" import -var-file="${tfvars_file}" "${resource_address}" "${secret_name}"
  fi
}

role_name_from_arn() {
  local role_arn="$1"
  echo "${role_arn##*/}"
}

account_id_from_arn() {
  local role_arn="$1"
  echo "${role_arn}" | cut -d: -f5
}

write_irsa_trust_policy() {
  local output_file="$1"
  local provider_arn="$2"
  local issuer="$3"
  local service_account_namespace="$4"
  local service_account_name="$5"

  ruby -rjson -e '
    provider_arn, issuer, namespace, service_account = ARGV
    puts JSON.pretty_generate({
      "Version" => "2012-10-17",
      "Statement" => [
        {
          "Sid" => "AllowHorizonServiceAccountIRSA",
          "Effect" => "Allow",
          "Principal" => { "Federated" => provider_arn },
          "Action" => "sts:AssumeRoleWithWebIdentity",
          "Condition" => {
            "StringEquals" => {
              "#{issuer}:aud" => "sts.amazonaws.com",
              "#{issuer}:sub" => "system:serviceaccount:#{namespace}:#{service_account}"
            }
          }
        }
      ]
    })
  ' "${provider_arn}" "${issuer}" "${service_account_namespace}" "${service_account_name}" > "${output_file}"
}

write_deploy_role_trust_policy() {
  local output_file="$1"
  shift

  ruby -rjson -e '
    principals = ARGV.reject { |value| value.to_s.empty? }.uniq
    puts JSON.pretty_generate({
      "Version" => "2012-10-17",
      "Statement" => [
        {
          "Sid" => "AllowHorizonRuntimeAssumeRole",
          "Effect" => "Allow",
          "Principal" => { "AWS" => principals },
          "Action" => "sts:AssumeRole"
        }
      ]
    })
  ' "$@" > "${output_file}"
}

reconcile_irsa_role_trust() {
  local role_arn="$1"
  local service_account_namespace="$2"
  local service_account_name="$3"
  local issuer="$4"
  local label="$5"
  [[ -n "${role_arn}" && -n "${service_account_namespace}" && -n "${service_account_name}" ]] || return 0

  local account_id role_name provider_arn policy_file
  account_id="$(account_id_from_arn "${role_arn}")"
  role_name="$(role_name_from_arn "${role_arn}")"
  provider_arn="arn:aws:iam::${account_id}:oidc-provider/${issuer}"
  policy_file="$(mktemp "${TMPDIR:-/tmp}/horizon-irsa-trust.XXXXXX.json")"

  write_irsa_trust_policy "${policy_file}" "${provider_arn}" "${issuer}" "${service_account_namespace}" "${service_account_name}"
  echo "Reconciling ${label} IRSA trust on ${role_name} for ${service_account_namespace}/${service_account_name}"
  aws iam update-assume-role-policy --role-name "${role_name}" --policy-document "file://${policy_file}" >/dev/null
  rm -f "${policy_file}"
}

reconcile_deploy_role_trust() {
  local jenkins_role_arn="$1"
  local backend_role_arn="$2"
  [[ -n "${jenkins_role_arn}" || -n "${backend_role_arn}" ]] || return 0

  local role_arn role_name policy_file
  while IFS= read -r role_arn; do
    [[ -n "${role_arn}" ]] || continue
    role_name="$(role_name_from_arn "${role_arn}")"
    policy_file="$(mktemp "${TMPDIR:-/tmp}/horizon-deploy-trust.XXXXXX.json")"
    write_deploy_role_trust_policy "${policy_file}" "${jenkins_role_arn}" "${backend_role_arn}"
    echo "Reconciling deploy role trust on ${role_name} for Horizon runtime roles"
    aws iam update-assume-role-policy --role-name "${role_name}" --policy-document "file://${policy_file}" >/dev/null
    rm -f "${policy_file}"
  done < <(ruby "${HELPER}" deploy-role-arns --file "${VALUES_FILE}" | sort -u)
}

reconcile_platform_iam_trust() {
  local iam_mode reconcile_setting
  iam_mode="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path accessModel.iamMode)"
  reconcile_setting="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path accessModel.reconcileManagedTrustPolicies)"
  if [[ "${iam_mode}" == "validation-only" && "${reconcile_setting}" != "true" ]]; then
    echo "Skipping IAM trust reconciliation because accessModel.iamMode=validation-only."
    return
  fi
  [[ "${reconcile_setting}" != "false" ]] || { echo "Skipping IAM trust reconciliation because accessModel.reconcileManagedTrustPolicies=false."; return; }

  command -v aws >/dev/null || { echo "Missing required command: aws" >&2; exit 1; }

  local namespace cluster_name region issuer_url issuer
  local jenkins_sa_namespace jenkins_sa_name jenkins_role_arn
  local backend_sa_namespace backend_sa_name backend_role_arn

  namespace="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path installer.namespace)"
  namespace="${namespace:-horizon-platform}"

  if [[ -n "${ENVIRONMENT}" ]]; then
    cluster_name="$(ruby "${HELPER}" get-env --file "${VALUES_FILE}" --environment "${ENVIRONMENT}" --path eks.clusterName)"
    region="$(ruby "${HELPER}" get-env --file "${VALUES_FILE}" --environment "${ENVIRONMENT}" --path aws.region)"
  else
    cluster_name=""
    region=""
  fi
  [[ -n "${cluster_name}" ]] || cluster_name="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path platform.cluster.name)"
  [[ -n "${region}" ]] || region="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path platform.region)"
  [[ -n "${cluster_name}" && -n "${region}" ]] || { echo "Skipping IAM trust reconciliation because platform cluster name or region is missing."; return; }

  issuer_url="$(aws eks describe-cluster --region "${region}" --name "${cluster_name}" --query 'cluster.identity.oidc.issuer' --output text)"
  issuer="${issuer_url#https://}"

  jenkins_sa_namespace="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path accessModel.jenkins.serviceAccount.namespace)"
  jenkins_sa_namespace="${jenkins_sa_namespace:-${namespace}}"
  jenkins_sa_name="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path accessModel.jenkins.serviceAccount.name)"
  jenkins_sa_name="${jenkins_sa_name:-jenkins}"
  jenkins_role_arn="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path accessModel.jenkins.runtimeRole.roleArn)"

  backend_sa_namespace="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path accessModel.backend.serviceAccount.namespace)"
  backend_sa_namespace="${backend_sa_namespace:-${namespace}}"
  backend_sa_name="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path accessModel.backend.serviceAccount.name)"
  backend_sa_name="${backend_sa_name:-horizon-backend}"
  backend_role_arn="$(ruby "${HELPER}" get --file "${VALUES_FILE}" --path accessModel.backend.validationRole.roleArn)"

  reconcile_irsa_role_trust "${jenkins_role_arn}" "${jenkins_sa_namespace}" "${jenkins_sa_name}" "${issuer}" "Jenkins runtime"
  reconcile_irsa_role_trust "${backend_role_arn}" "${backend_sa_namespace}" "${backend_sa_name}" "${issuer}" "Backend validation"
  reconcile_deploy_role_trust "${jenkins_role_arn}" "${backend_role_arn}"
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
  local files backend_file tfvars_file env_lc plan_file
  files="$(write_env_files)"
  backend_file="${files%%|*}"
  tfvars_file="${files##*|}"
  env_lc="$(echo "${ENVIRONMENT}" | tr '[:upper:]' '[:lower:]')"
  plan_file="${GENERATED_DIR}/${env_lc}.tfplan"
  ruby "${HELPER}" plan --file "${VALUES_FILE}" --environment "${ENVIRONMENT}"
  [[ "${DRY_RUN}" == "true" ]] && { echo "Dry-run: would run Terraform init/plan for ${ENVIRONMENT}."; echo "Generated backend config: ${backend_file}"; echo "Generated tfvars: ${tfvars_file}"; return; }
  run_existing_resource_checks
  terraform -chdir="${ROOT_DIR}/terraform/environment" init -reconfigure -backend-config="${backend_file}"
  reconcile_environment_placeholder_secret "${tfvars_file}"
  run_environment_terraform "${backend_file}" plan -out="${plan_file}" -var-file="${tfvars_file}"
  if [[ "${AUTO_APPROVE}" == "true" ]]; then
    run_environment_terraform "${backend_file}" apply "${plan_file}"
  else
    echo "Plan completed and saved to ${plan_file}. Re-run with --auto-approve to apply ${ENVIRONMENT} infrastructure."
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
  reconcile_platform_iam_trust
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
