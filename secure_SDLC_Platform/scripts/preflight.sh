#!/usr/bin/env bash
set -euo pipefail

VALUES_FILE=""
ENVIRONMENT=""
DRY_RUN="false"
SKIP_AWS="false"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
HELPER="${SCRIPT_DIR}/values-helper.rb"
GENERATED_DIR="${ROOT_DIR}/.generated"

usage() {
  cat <<USAGE
Usage: $0 -f <client-values.yaml> [--environment <DEV|QA|STAGE|PROD>] [--dry-run] [--skip-aws]
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--file) VALUES_FILE="${2:-}"; shift 2 ;;
    -e|--environment) ENVIRONMENT="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN="true"; shift ;;
    --skip-aws) SKIP_AWS="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -f "${VALUES_FILE}" ]] || { usage; exit 1; }
command -v ruby >/dev/null || { echo "Missing required command: ruby" >&2; exit 1; }
mkdir -p "${GENERATED_DIR}"

echo "== Horizon Enterprise Installer Preflight =="
echo "Values file: ${VALUES_FILE}"
[[ -n "${ENVIRONMENT}" ]] && echo "Environment: ${ENVIRONMENT}"
[[ "${DRY_RUN}" == "true" ]] && echo "Mode: dry-run/read-only"

if [[ -n "${ENVIRONMENT}" ]]; then
  ruby "${HELPER}" validate --file "${VALUES_FILE}" --environment "${ENVIRONMENT}"
  ruby "${HELPER}" plan --file "${VALUES_FILE}" --environment "${ENVIRONMENT}"
  ruby "${HELPER}" catalog-payload --file "${VALUES_FILE}" --environment "${ENVIRONMENT}" > "${GENERATED_DIR}/preflight-catalog-$(echo "${ENVIRONMENT}" | tr '[:upper:]' '[:lower:]').json"
else
  ruby "${HELPER}" validate --file "${VALUES_FILE}"
  ruby "${HELPER}" plan --file "${VALUES_FILE}"
  ruby "${HELPER}" catalog-payload --file "${VALUES_FILE}" > "${GENERATED_DIR}/preflight-catalog-all.json"
fi

if [[ "${SKIP_AWS}" == "true" ]]; then
  echo "Skipping AWS/Kubernetes checks because --skip-aws was provided."
  echo "Preflight passed locally."
  exit 0
fi

for cmd in aws kubectl helm terraform; do
  command -v "${cmd}" >/dev/null || { echo "Missing required command: ${cmd}" >&2; exit 1; }
done

CALLER_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

role_account_id_from_arn() {
  local role_arn="$1"
  echo "${role_arn}" | cut -d: -f5
}

role_arn_for_check_source() {
  local source="$1"
  local env_name role_key

  case "${source}" in
    accessModel.jenkins.runtimeRole)
      ruby "${HELPER}" get --file "${VALUES_FILE}" --path accessModel.jenkins.runtimeRole.roleArn
      ;;
    accessModel.backend.validationRole)
      ruby "${HELPER}" get --file "${VALUES_FILE}" --path accessModel.backend.validationRole.roleArn
      ;;
    *.iam.deployRole|*.iam.sourceRole|*.iam.targetRole)
      env_name="${source%%.iam.*}"
      role_key="${source##*.iam.}"
      ruby "${HELPER}" get-env --file "${VALUES_FILE}" --environment "${env_name}" --path "iam.${role_key}.roleArn"
      ;;
    *)
      echo ""
      ;;
  esac
}

should_validate_iam_role_in_current_account() {
  local source="$1"
  local role_arn role_account_id

  role_arn="$(role_arn_for_check_source "${source}")"
  role_account_id="$(role_account_id_from_arn "${role_arn}")"

  if [[ -n "${role_account_id}" && "${role_account_id}" != "${CALLER_ACCOUNT_ID}" ]]; then
    echo "    skipping cross-account IAM role validation; role account=${role_account_id}, current account=${CALLER_ACCOUNT_ID}"
    return 1
  fi

  return 0
}

if [[ -n "${ENVIRONMENT}" ]]; then
  CHECK_COMMAND=(ruby "${HELPER}" checks --file "${VALUES_FILE}" --environment "${ENVIRONMENT}")
else
  CHECK_COMMAND=(ruby "${HELPER}" checks --file "${VALUES_FILE}")
fi

failures=0
while IFS=$'\t' read -r kind name region source; do
  [[ -z "${kind}" || -z "${name}" ]] && continue
  echo "  - ${source}: ${kind} ${name}"
  case "${kind}" in
    s3_bucket)
      aws s3api head-bucket --bucket "${name}" >/dev/null || failures=$((failures + 1))
      ;;
    dynamodb_table)
      aws dynamodb describe-table --region "${region}" --table-name "${name}" >/dev/null || failures=$((failures + 1))
      ;;
    ecr_repository)
      aws ecr describe-repositories --region "${region}" --repository-names "${name}" >/dev/null || failures=$((failures + 1))
      ;;
    eks_cluster)
      aws eks describe-cluster --region "${region}" --name "${name}" >/dev/null || failures=$((failures + 1))
      ;;
    iam_role)
      if should_validate_iam_role_in_current_account "${source}"; then
        aws iam get-role --role-name "${name}" >/dev/null || failures=$((failures + 1))
      fi
      ;;
  esac
done < <("${CHECK_COMMAND[@]}")

if [[ "${failures}" -ne 0 ]]; then
  echo "Preflight failed: ${failures} existing resource check(s) failed." >&2
  echo "Fix the missing resource, update the values file, or mark the resource state as provision/disabled before installing." >&2
  exit 1
fi

echo "Preflight passed."
