#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  sign-product-images.sh --key KEY [--registry REGISTRY] [--manifest FILE] [--image REPOSITORY:TAG] [--yes]

Purpose:
  Sign Horizon product images with Cosign before releasing them to licensed
  clients. KEY can be an awskms:// URI, env://COSIGN_PRIVATE_KEY, or a key file.

Examples:
  bash secure_SDLC_Platform/scripts/sign-product-images.sh \
    --key awskms://arn:aws:kms:us-east-1:426946630837:key/example --yes

  bash secure_SDLC_Platform/scripts/sign-product-images.sh \
    --key env://COSIGN_PRIVATE_KEY --image horizon/backend:1.4.34 --yes
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REGISTRY="426946630837.dkr.ecr.us-east-1.amazonaws.com"
MANIFEST="${PLATFORM_DIR}/release/product-images.tsv"
KEY=""
SINGLE_IMAGE=""
YES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key) KEY="${2:-}"; shift 2 ;;
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --manifest) MANIFEST="${2:-}"; shift 2 ;;
    --image) SINGLE_IMAGE="${2:-}"; shift 2 ;;
    --yes) YES=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ -n "${KEY}" ]] || { echo "--key is required" >&2; exit 1; }
[[ -f "${MANIFEST}" ]] || { echo "Manifest not found: ${MANIFEST}" >&2; exit 1; }
command -v cosign >/dev/null 2>&1 || { echo "cosign is required to sign images" >&2; exit 1; }

sign_one() {
  local repo="$1"
  local tag="$2"
  local image="${REGISTRY}/${repo}:${tag}"
  echo "Signing ${image}"
  if [[ "${YES}" == "true" ]]; then
    cosign sign --yes --key "${KEY}" "${image}"
  else
    cosign sign --key "${KEY}" "${image}"
  fi
}

if [[ -n "${SINGLE_IMAGE}" ]]; then
  sign_one "${SINGLE_IMAGE%:*}" "${SINGLE_IMAGE##*:}"
  exit 0
fi

while IFS=$'\t' read -r component repo tag required; do
  [[ -z "${component}" || "${component}" == \#* ]] && continue
  [[ "${required:-true}" == "true" ]] || continue
  sign_one "${repo}" "${tag}"
done < "${MANIFEST}"

echo "Image signing completed."
