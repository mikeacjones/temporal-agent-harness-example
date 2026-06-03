#!/usr/bin/env bash
#
# Configure broad S3 expiration for claim-check payloads and artifact bytes.
# Workflows checkpoint or close before this window, so replay never depends on
# objects older than the lifecycle horizon.

set -euo pipefail

REGION="${AWS_REGION:-us-west-1}"
BUCKET="${SIMPLE_CHAT_S3_BUCKET:-michaelj-agent-harness-claimcheck-429214323166}"
DAYS="${SIMPLE_CHAT_S3_LIFECYCLE_DAYS:-30}"

if [[ -z "${BUCKET}" ]]; then
  echo "SIMPLE_CHAT_S3_BUCKET is empty; skipping lifecycle configuration."
  exit 0
fi

CONFIG_FILE="$(mktemp)"
trap 'rm -f "${CONFIG_FILE}"' EXIT

cat > "${CONFIG_FILE}" <<EOF
{
  "Rules": [
    {
      "ID": "ExpireAgentHarnessObjectsAfter${DAYS}Days",
      "Status": "Enabled",
      "Filter": {
        "Prefix": ""
      },
      "Expiration": {
        "Days": ${DAYS}
      },
      "AbortIncompleteMultipartUpload": {
        "DaysAfterInitiation": 7
      }
    }
  ]
}
EOF

echo ">> Configuring ${DAYS}-day S3 lifecycle on ${BUCKET}"
aws s3api put-bucket-lifecycle-configuration \
  --region "${REGION}" \
  --bucket "${BUCKET}" \
  --lifecycle-configuration "file://${CONFIG_FILE}"
