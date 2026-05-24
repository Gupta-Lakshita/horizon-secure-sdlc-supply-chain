#!/usr/bin/env bash
set -euo pipefail

VALUES_FILE=""
ENVIRONMENT=""
SKIP_AWS="false"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
GENERATED_DIR="${ROOT_DIR}/.generated"

usage() {
  cat <<USAGE
Usage: $0 -f <client-values.yaml> --environment <DEV|QA|STAGE|PROD> [--skip-aws]

Runs the non-destructive readiness checks a trial client should complete before
installing or exposing Horizon Relevance to application teams.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--file) VALUES_FILE="${2:-}"; shift 2 ;;
    -e|--environment) ENVIRONMENT="${2:-}"; shift 2 ;;
    --skip-aws) SKIP_AWS="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -f "${VALUES_FILE}" && -n "${ENVIRONMENT}" ]] || { usage; exit 1; }
mkdir -p "${GENERATED_DIR}"

env_lc="$(echo "${ENVIRONMENT}" | tr '[:upper:]' '[:lower:]')"
report_file="${GENERATED_DIR}/trial-readiness-${env_lc}.txt"

run_step() {
  local name="$1"
  shift
  echo
  echo "== ${name} =="
  "$@"
}

{
  echo "Horizon Relevance Trial Readiness Report"
  echo "Values file: ${VALUES_FILE}"
  echo "Environment: ${ENVIRONMENT}"
  echo "Generated at: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  if [[ "${SKIP_AWS}" == "true" ]]; then
    run_step "Values and desired-state preflight" "${SCRIPT_DIR}/preflight.sh" -f "${VALUES_FILE}" --environment "${ENVIRONMENT}" --dry-run --skip-aws
  else
    run_step "Values, desired-state, and AWS preflight" "${SCRIPT_DIR}/preflight.sh" -f "${VALUES_FILE}" --environment "${ENVIRONMENT}" --dry-run
  fi

  run_step "Terraform state backend dry-run" "${SCRIPT_DIR}/install.sh" --phase state -f "${VALUES_FILE}" --dry-run
  run_step "Environment infrastructure dry-run" "${SCRIPT_DIR}/install.sh" --phase infra --environment "${ENVIRONMENT}" -f "${VALUES_FILE}" --dry-run
  run_step "Platform Helm render dry-run" "${SCRIPT_DIR}/install.sh" --phase platform -f "${VALUES_FILE}" --dry-run
  run_step "Environment Catalog payload dry-run" "${SCRIPT_DIR}/install.sh" --phase catalog --environment "${ENVIRONMENT}" -f "${VALUES_FILE}" --dry-run
  run_step "Post-install validation dry-run" "${SCRIPT_DIR}/validate.sh" -f "${VALUES_FILE}" --environment "${ENVIRONMENT}" --skip-aws

  echo
  echo "Trial readiness checks completed."
} | tee "${report_file}"

echo
echo "Readiness report written to ${report_file}"
