#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  verify-product-images.sh [--registry REGISTRY] [--region REGION] [--online]

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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --online) ONLINE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

IMAGES=(
  "horizon/frontend:1.4.25"
  "horizon/backend:1.4.32"
  "horizon/license-management-service:0.1.7"
  "horizon/jenkins:1.0.8"
  "horizon/sonarqube:10.4-community"
  "horizon/trivy-scanner:1.1.2"
  "horizon/opa-scanner:1.0.1"
  "horizon/self-service-password:1.7.3-ltb"
)

echo "== Horizon product image manifest =="
for image in "${IMAGES[@]}"; do
  printf '%s/%s\n' "${REGISTRY}" "${image}"
done

if [[ "${ONLINE}" != "true" ]]; then
  echo
  echo "Offline manifest rendered. Re-run with --online to verify tags in ECR."
  exit 0
fi

echo
echo "== Verifying images in ECR =="
for image in "${IMAGES[@]}"; do
  repo="${image%:*}"
  tag="${image##*:}"
  if aws ecr describe-images \
    --region "${REGION}" \
    --repository-name "${repo}" \
    --image-ids imageTag="${tag}" >/dev/null; then
    echo "OK ${REGISTRY}/${image}"
  else
    echo "MISSING ${REGISTRY}/${image}" >&2
    exit 1
  fi
done

echo "All expected Horizon product images exist."

