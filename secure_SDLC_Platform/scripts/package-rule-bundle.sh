#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  package-rule-bundle.sh --source DIR --version VERSION [--name NAME] [--output-dir DIR] [--signing-key KEY] [--yes]

Purpose:
  Package Horizon policy/rule/template content into a versioned bundle with a
  checksum manifest and optional Cosign blob signature. This lets Horizon keep
  crown-jewel rules private while distributing verifiable bundles to licensed
  client-hosted Jenkins runtimes.

Examples:
  bash secure_SDLC_Platform/scripts/package-rule-bundle.sh \
    --source /path/to/rules --version 2026.05.23 --signing-key awskms://arn:aws:kms:...
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_DIR=""
VERSION=""
NAME="horizon-rules"
OUTPUT_DIR="${PLATFORM_DIR}/.generated/rule-bundles"
SIGNING_KEY=""
YES=false

checksum() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE_DIR="${2:-}"; shift 2 ;;
    --version) VERSION="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:-}"; shift 2 ;;
    --signing-key) SIGNING_KEY="${2:-}"; shift 2 ;;
    --yes) YES=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ -d "${SOURCE_DIR}" ]] || { echo "--source must be an existing directory" >&2; exit 1; }
[[ -n "${VERSION}" ]] || { echo "--version is required" >&2; exit 1; }

mkdir -p "${OUTPUT_DIR}"
BUNDLE="${OUTPUT_DIR}/${NAME}-${VERSION}.tar.gz"
MANIFEST="${OUTPUT_DIR}/${NAME}-${VERSION}.manifest.json"
SIGNATURE="${BUNDLE}.sig"
CREATED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

tar \
  --exclude='.git' \
  --exclude='.DS_Store' \
  --exclude='*.pem' \
  --exclude='*.key' \
  --exclude='*.p12' \
  --exclude='*.kubeconfig' \
  -C "$(dirname "${SOURCE_DIR}")" \
  -czf "${BUNDLE}" \
  "$(basename "${SOURCE_DIR}")"

BUNDLE_SHA="$(checksum "${BUNDLE}")"

cat > "${MANIFEST}" <<JSON
{
  "bundle_name": "${NAME}",
  "version": "${VERSION}",
  "created_at": "${CREATED_AT}",
  "sha256": "${BUNDLE_SHA}",
  "signature": "$(basename "${SIGNATURE}")",
  "signature_algorithm": "$(if [[ -n "${SIGNING_KEY}" ]]; then echo "cosign"; else echo "none"; fi)",
  "source_policy": "private-horizon-distribution"
}
JSON

if [[ -n "${SIGNING_KEY}" ]]; then
  command -v cosign >/dev/null 2>&1 || { echo "cosign is required when --signing-key is set" >&2; exit 1; }
  if [[ "${YES}" == "true" ]]; then
    cosign sign-blob --yes --key "${SIGNING_KEY}" --output-signature "${SIGNATURE}" "${BUNDLE}"
  else
    cosign sign-blob --key "${SIGNING_KEY}" --output-signature "${SIGNATURE}" "${BUNDLE}"
  fi
else
  : > "${SIGNATURE}"
fi

echo "Wrote ${BUNDLE}"
echo "Wrote ${MANIFEST}"
echo "Wrote ${SIGNATURE}"
