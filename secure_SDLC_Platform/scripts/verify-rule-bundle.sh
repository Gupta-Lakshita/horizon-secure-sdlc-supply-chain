#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  verify-rule-bundle.sh --bundle FILE --manifest FILE [--key KEY]

Purpose:
  Verify a Horizon rule/template bundle checksum and, when provided, its Cosign
  signature before installing it into a client-hosted Jenkins runtime.
USAGE
}

BUNDLE=""
MANIFEST=""
KEY=""

checksum() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

json_value() {
  local key="$1"
  sed -n "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "${MANIFEST}" | head -1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle) BUNDLE="${2:-}"; shift 2 ;;
    --manifest) MANIFEST="${2:-}"; shift 2 ;;
    --key) KEY="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ -f "${BUNDLE}" ]] || { echo "--bundle must be an existing file" >&2; exit 1; }
[[ -f "${MANIFEST}" ]] || { echo "--manifest must be an existing file" >&2; exit 1; }

expected="$(json_value sha256)"
actual="$(checksum "${BUNDLE}")"
[[ -n "${expected}" ]] || { echo "Manifest does not include sha256" >&2; exit 1; }

if [[ "${expected}" != "${actual}" ]]; then
  echo "Checksum mismatch for ${BUNDLE}" >&2
  echo "Expected: ${expected}" >&2
  echo "Actual:   ${actual}" >&2
  exit 1
fi

echo "OK checksum ${BUNDLE}"

if [[ -n "${KEY}" ]]; then
  command -v cosign >/dev/null 2>&1 || { echo "cosign is required when --key is set" >&2; exit 1; }
  signature="$(dirname "${BUNDLE}")/$(json_value signature)"
  [[ -f "${signature}" ]] || { echo "Signature file not found: ${signature}" >&2; exit 1; }
  cosign verify-blob --key "${KEY}" --signature "${signature}" "${BUNDLE}" >/dev/null
  echo "OK signature ${BUNDLE}"
fi

echo "Rule bundle verification completed."
