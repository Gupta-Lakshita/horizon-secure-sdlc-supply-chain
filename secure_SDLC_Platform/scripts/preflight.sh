#!/usr/bin/env bash
set -euo pipefail

VALUES_FILE=""
ENVIRONMENT=""
DRY_RUN="false"
SKIP_AWS="false"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="${SCRIPT_DIR}/values-helper.rb"

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

echo "== Horizon Enterprise Installer Preflight =="
echo "Values file: ${VALUES_FILE}"
[[ -n "${ENVIRONMENT}" ]] && echo "Environment: ${ENVIRONMENT}"
[[ "${DRY_RUN}" == "true" ]] && echo "Mode: dry-run/read-only"

if [[ -n "${ENVIRONMENT}" ]]; then
  ruby "${HELPER}" validate --file "${VALUES_FILE}" --environment "${ENVIRONMENT}"
  ruby "${HELPER}" plan --file "${VALUES_FILE}" --environment "${ENVIRONMENT}"
else
  ruby "${HELPER}" validate --file "${VALUES_FILE}"
  ruby "${HELPER}" plan --file "${VALUES_FILE}"
fi

if [[ "${SKIP_AWS}" == "true" ]]; then
  echo "Skipping AWS/Kubernetes checks because --skip-aws was provided."
  echo "Preflight passed locally."
  exit 0
fi

for cmd in aws kubectl helm terraform; do
  command -v "${cmd}" >/dev/null || { echo "Missing required command: ${cmd}" >&2; exit 1; }
done

aws sts get-caller-identity >/dev/null

if [[ -n "${ENVIRONMENT}" ]]; then
  CHECK_COMMAND=(ruby "${HELPER}" checks --file "${VALUES_FILE}" --environment "${ENVIRONMENT}")
else
  CHECK_COMMAND=(ruby "${HELPER}" checks --file "${VALUES_FILE}")
fi

"${CHECK_COMMAND[@]}" | while IFS=$'\t' read -r kind name region source; do
  [[ -z "${kind}" || -z "${name}" ]] && continue
  echo "  - ${source}: ${kind} ${name}"
  case "${kind}" in
    s3_bucket) aws s3api head-bucket --bucket "${name}" >/dev/null ;;
    dynamodb_table) aws dynamodb describe-table --region "${region}" --table-name "${name}" >/dev/null ;;
    ecr_repository) aws ecr describe-repositories --region "${region}" --repository-names "${name}" >/dev/null ;;
    eks_cluster) aws eks describe-cluster --region "${region}" --name "${name}" >/dev/null ;;
    iam_role) aws iam get-role --role-name "${name}" >/dev/null ;;
  esac
done

echo "Preflight passed."
