#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  verify-product-signatures.sh --key KEY [--registry REGISTRY] [--manifest FILE] [--image REPOSITORY:TAG]

Purpose:
  Verify Cosign signatures on Horizon product images before installation or
  client handoff. KEY can be a public key file or an awskms:// URI.
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REGISTRY="426946630837.dkr.ecr.us-east-1.amazonaws.com"
MANIFEST="${PLATFORM_DIR}/release/product-images.tsv"
KEY=""
SINGLE_IMAGE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key) KEY="${2:-}"; shift 2 ;;
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --manifest) MANIFEST="${2:-}"; shift 2 ;;
    --image) SINGLE_IMAGE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ -n "${KEY}" ]] || { echo "--key is required" >&2; exit 1; }
[[ -f "${MANIFEST}" ]] || { echo "Manifest not found: ${MANIFEST}" >&2; exit 1; }
command -v cosign >/dev/null 2>&1 || { echo "cosign is required to verify image signatures" >&2; exit 1; }

verify_one() {
  local repo="$1"
  local tag="$2"
  local image="${REGISTRY}/${repo}:${tag}"
  echo "Verifying ${image}"
  cosign verify --key "${KEY}" "${image}" >/dev/null
  echo "OK ${image}"
}

if [[ -n "${SINGLE_IMAGE}" ]]; then
  verify_one "${SINGLE_IMAGE%:*}" "${SINGLE_IMAGE##*:}"
  exit 0
fi

while IFS=$'\t' read -r component repo tag required; do
  [[ -z "${component}" || "${component}" == \#* ]] && continue
  [[ "${required:-true}" == "true" ]] || continue
  verify_one "${repo}" "${tag}"
done < "${MANIFEST}"

echo "All required Horizon product image signatures verified."
