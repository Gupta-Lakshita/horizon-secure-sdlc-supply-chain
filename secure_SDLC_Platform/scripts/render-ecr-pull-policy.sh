#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  render-ecr-pull-policy.sh --principal-arn ARN [--repository NAME] [--expires-at ISO8601] [--output FILE]

Purpose:
  Render a least-privilege Horizon private ECR repository policy that grants
  a licensed client principal read-only image pull access.

Examples:
  bash secure_SDLC_Platform/scripts/render-ecr-pull-policy.sh \
    --principal-arn arn:aws:iam::111122223333:role/ClientEcrPullRole \
    --repository horizon/backend

  bash secure_SDLC_Platform/scripts/render-ecr-pull-policy.sh \
    --principal-arn arn:aws:iam::111122223333:root \
    --output /tmp/horizon-ecr-pull-policy.json
USAGE
}

PRINCIPAL_ARN=""
REPOSITORY="*"
OUTPUT=""
EXPIRES_AT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --principal-arn) PRINCIPAL_ARN="${2:-}"; shift 2 ;;
    --repository) REPOSITORY="${2:-}"; shift 2 ;;
    --expires-at) EXPIRES_AT="${2:-}"; shift 2 ;;
    --output) OUTPUT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ -n "${PRINCIPAL_ARN}" ]] || { echo "--principal-arn is required" >&2; exit 1; }

CONDITION=""
if [[ -n "${EXPIRES_AT}" ]]; then
  CONDITION=$(cat <<JSON
      ,
      "Condition": {
        "DateLessThan": {
          "aws:CurrentTime": "${EXPIRES_AT}"
        }
      }
JSON
)
fi

POLICY=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "HorizonLicensedClientReadOnlyPull",
      "Effect": "Allow",
      "Principal": {
        "AWS": "${PRINCIPAL_ARN}"
      },
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:DescribeImages",
        "ecr:GetDownloadUrlForLayer"
      ]
${CONDITION}
    }
  ]
}
JSON
)

if [[ -n "${OUTPUT}" ]]; then
  printf '%s\n' "${POLICY}" > "${OUTPUT}"
  echo "Wrote ECR pull policy for ${REPOSITORY} to ${OUTPUT}"
else
  printf '%s\n' "${POLICY}"
fi
