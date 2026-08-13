#!/usr/bin/env bash
#
# Build the current branch and deploy the isolated TEST stack (frontend + API +
# worker) alongside prod in the same namespace, at
# https://agent-harness-demo.testing.tmprl-demo.cloud
#
# Prereqs: AWS CLI authenticated (ECR + EKS), docker buildx, kubectl.
# Usage (from anywhere): ./simple_chat_agent/deploy/testing/deploy-testing.sh

set -euo pipefail

REGION="us-west-1"
ACCOUNT="429214323166"
REPO="temporal-michaelj-agent-harness-demo"
NAMESPACE="temporal-michaelj-agent-harness-demo"
KUBE_CONTEXT="sa-demo"
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${REPO}"
TAG="testing-$(date +%Y%m%d-%H%M%S)"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "${ROOT}"

echo ">> ECR login (${REGISTRY})"
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

echo ">> Building/pushing ${IMAGE}:${TAG} (linux/amd64)"
docker buildx build --platform linux/amd64 \
  -f simple_chat_agent/Dockerfile \
  -t "${IMAGE}:${TAG}" \
  -t "${IMAGE}:testing" \
  --push .

echo ">> Deploying testing Python sandbox Lambda + IAM"
"${ROOT}/simple_chat_agent/deploy/testing/deploy-python-sandbox-lambda-testing.sh"

echo ">> Applying testing manifests"
kubectl --context "${KUBE_CONTEXT}" apply -f simple_chat_agent/deploy/searxng.yaml
kubectl --context "${KUBE_CONTEXT}" apply -f simple_chat_agent/deploy/testing/redis.yaml

echo ">> Waiting for Redis before changing the application stack"
kubectl --context "${KUBE_CONTEXT}" rollout status deployment/agent-harness-redis-testing -n "${NAMESPACE}" --timeout=300s
kubectl --context "${KUBE_CONTEXT}" apply -f simple_chat_agent/deploy/testing/

echo ">> Setting images + rolling out the test stack"
kubectl --context "${KUBE_CONTEXT}" set image deployment/agent-harness-web-testing web="${IMAGE}:${TAG}" -n "${NAMESPACE}"
kubectl --context "${KUBE_CONTEXT}" set image deployment/agent-harness-api-testing api="${IMAGE}:${TAG}" -n "${NAMESPACE}"
kubectl --context "${KUBE_CONTEXT}" set image deployment/agent-harness-worker-testing worker="${IMAGE}:${TAG}" -n "${NAMESPACE}"
kubectl --context "${KUBE_CONTEXT}" rollout status deployment/agent-harness-web-testing -n "${NAMESPACE}" --timeout=300s
kubectl --context "${KUBE_CONTEXT}" rollout status deployment/agent-harness-api-testing -n "${NAMESPACE}" --timeout=300s
kubectl --context "${KUBE_CONTEXT}" rollout status deployment/agent-harness-worker-testing -n "${NAMESPACE}" --timeout=300s

"${ROOT}/simple_chat_agent/deploy/configure-s3-lifecycle.sh"

echo ">> Done. Test stack on ${IMAGE}:${TAG}"
echo ">> https://agent-harness-demo.testing.tmprl-demo.cloud"
