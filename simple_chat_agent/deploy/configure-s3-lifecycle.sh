#!/usr/bin/env bash
#
# Ensure the shared S3 bucket used for claim-check payloads and artifact bytes
# has a broad object expiration policy. This is intentionally bucket-wide as a
# first-pass safety net for demo data.

set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-1}}"
BUCKET="${SIMPLE_CHAT_S3_BUCKET:-${S3_BUCKET:-michaelj-agent-harness-claimcheck-429214323166}}"
EXPIRATION_DAYS="${SIMPLE_CHAT_S3_EXPIRATION_DAYS:-${S3_EXPIRATION_DAYS:-30}}"
RULE_ID="${SIMPLE_CHAT_S3_EXPIRATION_RULE_ID:-agent-harness-expire-all-objects-after-30-days}"

if [[ ! "${EXPIRATION_DAYS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "S3 expiration days must be a positive integer, got: ${EXPIRATION_DAYS}" >&2
  exit 1
fi

CURRENT="$(mktemp)"
MERGED="$(mktemp)"
ERRORS="$(mktemp)"
trap 'rm -f "${CURRENT}" "${MERGED}" "${ERRORS}"' EXIT

if ! aws s3api get-bucket-lifecycle-configuration \
  --region "${REGION}" \
  --bucket "${BUCKET}" \
  --output json >"${CURRENT}" 2>"${ERRORS}"; then
  if grep -q "NoSuchLifecycleConfiguration" "${ERRORS}"; then
    printf '{"Rules":[]}\n' >"${CURRENT}"
  else
    cat "${ERRORS}" >&2
    exit 1
  fi
fi

python3 - "${CURRENT}" "${MERGED}" "${RULE_ID}" "${EXPIRATION_DAYS}" <<'PY'
import json
import sys

current_path, merged_path, rule_id, expiration_days_raw = sys.argv[1:5]
expiration_days = int(expiration_days_raw)

with open(current_path, "r", encoding="utf-8") as file:
    current = json.load(file)

rules = [
    rule
    for rule in current.get("Rules", [])
    if isinstance(rule, dict) and rule.get("ID") != rule_id
]
rules.append(
    {
        "ID": rule_id,
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "Expiration": {"Days": expiration_days},
        "NoncurrentVersionExpiration": {"NoncurrentDays": expiration_days},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
    }
)

with open(merged_path, "w", encoding="utf-8") as file:
    json.dump({"Rules": rules}, file, indent=2, sort_keys=True)
    file.write("\n")
PY

aws s3api put-bucket-lifecycle-configuration \
  --region "${REGION}" \
  --bucket "${BUCKET}" \
  --lifecycle-configuration "file://${MERGED}"

echo "S3 lifecycle configured: s3://${BUCKET} expires objects after ${EXPIRATION_DAYS} days"
