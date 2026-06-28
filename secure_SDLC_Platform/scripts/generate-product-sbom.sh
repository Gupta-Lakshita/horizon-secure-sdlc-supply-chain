#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  generate-product-sbom.sh [--registry REGISTRY] [--manifest FILE] [--output-dir DIR] [--image REPOSITORY:TAG]

Purpose:
  Generate SBOM evidence for Horizon product images before granting client pull
  access. The script prefers Trivy CycloneDX and falls back to Syft CycloneDX.

Examples:
  bash secure_SDLC_Platform/scripts/generate-product-sbom.sh
  bash secure_SDLC_Platform/scripts/generate-product-sbom.sh --image horizon/backend:1.4.34
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REGISTRY="426946630837.dkr.ecr.us-east-1.amazonaws.com"
MANIFEST="${PLATFORM_DIR}/release/product-images.tsv"
OUTPUT_DIR="${PLATFORM_DIR}/.generated/sbom"
SINGLE_IMAGE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --manifest) MANIFEST="${2:-}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:-}"; shift 2 ;;
    --image) SINGLE_IMAGE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ -f "${MANIFEST}" ]] || { echo "Manifest not found: ${MANIFEST}" >&2; exit 1; }
mkdir -p "${OUTPUT_DIR}"

generate_one() {
  local component="$1"
  local repo="$2"
  local tag="$3"
  local image="${REGISTRY}/${repo}:${tag}"
  local safe_component="${component//\//-}"

  echo "Generating SBOM for ${image}"
  if command -v trivy >/dev/null 2>&1; then
    trivy image --format cyclonedx --output "${OUTPUT_DIR}/${safe_component}-${tag}.cyclonedx.json" "${image}"
    echo "Wrote ${OUTPUT_DIR}/${safe_component}-${tag}.cyclonedx.json"
    return
  fi

  if command -v syft >/dev/null 2>&1; then
    syft packages "${image}" -o "cyclonedx-json=${OUTPUT_DIR}/${safe_component}-${tag}.cyclonedx.json"
    echo "Wrote ${OUTPUT_DIR}/${safe_component}-${tag}.cyclonedx.json"
    return
  fi

  echo "Neither trivy nor syft is installed. Install one of them to generate SBOM evidence." >&2
  exit 1
}

if [[ -n "${SINGLE_IMAGE}" ]]; then
  component="${SINGLE_IMAGE%%:*}"
  component="${component##*/}"
  repo="${SINGLE_IMAGE%:*}"
  tag="${SINGLE_IMAGE##*:}"
  generate_one "${component}" "${repo}" "${tag}"
  exit 0
fi

while IFS=$'\t' read -r component repo tag required; do
  [[ -z "${component}" || "${component}" == \#* ]] && continue
  generate_one "${component}" "${repo}" "${tag}"
done < "${MANIFEST}"

echo "SBOM generation completed."
