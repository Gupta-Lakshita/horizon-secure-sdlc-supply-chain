#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  verify-product-images.sh [--registry REGISTRY] [--region REGION] [--manifest FILE] [--online]

Purpose:
  Print the Horizon product image release manifest. With --online, verify the
  expected tags exist in Horizon private ECR.

Notes:
  - This script does not grant client pull access.
  - Use render-ecr-pull-policy.sh to generate repository policies.
USAGE
}

REGISTRY="426946630837.dkr.ecr.us-east-1.amazonaws.com"
REGION="us-east-1"
ONLINE=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
MANIFEST="${PLATFORM_DIR}/release/product-images.tsv"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --manifest) MANIFEST="${2:-}"; shift 2 ;;
    --online) ONLINE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ -f "${MANIFEST}" ]] || { echo "Manifest not found: ${MANIFEST}" >&2; exit 1; }

echo "== Horizon product image manifest =="
printf 'Manifest: %s\n\n' "${MANIFEST}"
while IFS=$'\t' read -r component repo tag required; do
  [[ -z "${component}" || "${component}" == \#* ]] && continue
  printf '%-28s %s/%s:%s required=%s\n' "${component}" "${REGISTRY}" "${repo}" "${tag}" "${required:-true}"
done < "${MANIFEST}"

if [[ "${ONLINE}" != "true" ]]; then
  echo
  echo "Offline manifest rendered. Re-run with --online to verify tags in ECR."
  exit 0
fi

echo
echo "== Verifying images in ECR =="
while IFS=$'\t' read -r component repo tag required; do
  [[ -z "${component}" || "${component}" == \#* ]] && continue
  if aws ecr describe-images \
    --region "${REGION}" \
    --repository-name "${repo}" \
    --image-ids imageTag="${tag}" >/dev/null; then
    echo "OK ${REGISTRY}/${repo}:${tag}"
  else
    if [[ "${required:-true}" == "true" ]]; then
      echo "MISSING ${REGISTRY}/${repo}:${tag}" >&2
      exit 1
    fi
    echo "OPTIONAL-MISSING ${REGISTRY}/${repo}:${tag}"
  fi
done < "${MANIFEST}"

echo "All expected Horizon product images exist."
