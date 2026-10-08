#!/usr/bin/env bash
set -euo pipefail

: "${IMAGE_TAG:?set IMAGE_TAG to the image tag to deploy}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAMESPACE=opsfleet

output() { terraform -chdir="$ROOT/infra/terraform/main" output -raw "$1"; }

PROJECT=opsfleet-demo-56d0
SQL_IP="$(output sql_private_ip)"
BASE_DOMAIN="$(output base_domain)"
APP_GSA="$(output app_gsa_email)"
BIFROST_GSA="$(output bifrost_gsa_email)"
ESO_GSA="$(output eso_gsa_email)"
ARTIFACTS_BUCKET="$(output artifacts_bucket)"
LANGFUSE_BUCKET="$(output langfuse_bucket)"
IMAGE_REPO="$(output artifact_repo)/opsfleet-agent"

source "$ROOT/infra/helm/versions.env"

helm repo add external-secrets https://charts.external-secrets.io --force-update >/dev/null
helm repo add temporal https://go.temporal.io/helm-charts --force-update >/dev/null
helm repo update >/dev/null

helm upgrade --install external-secrets external-secrets/external-secrets \
  --version "$ESO_CHART_VERSION" -n external-secrets --create-namespace \
  -f "$ROOT/infra/helm/values/external-secrets.yaml" \
  --set "serviceAccount.annotations.iam\.gke\.io/gcp-service-account=$ESO_GSA" \
  --wait --timeout 10m

common=(
  --set "gcpProject=$PROJECT"
  --set "sqlHost=$SQL_IP"
  --set "baseDomain=$BASE_DOMAIN"
  --set "appGsaEmail=$APP_GSA"
  --set "bifrostGsaEmail=$BIFROST_GSA"
  --set "artifactsBucket=$ARTIFACTS_BUCKET"
  --set "langfuseBucket=$LANGFUSE_BUCKET"
  --set "image.repository=$IMAGE_REPO"
  --set "image.tag=$IMAGE_TAG"
)

helm upgrade --install opsfleet-secrets "$ROOT/infra/helm/opsfleet" -n "$NAMESPACE" --create-namespace \
  "${common[@]}" --set components.workloads=false --wait --timeout 10m

helm upgrade --install temporal temporal/temporal --version "$TEMPORAL_CHART_VERSION" -n "$NAMESPACE" \
  -f "$ROOT/infra/helm/values/temporal.yaml" \
  --set "server.config.persistence.datastores.default.sql.connectAddr=$SQL_IP:5432" \
  --set "server.config.persistence.datastores.visibility.sql.connectAddr=$SQL_IP:5432" \
  --wait --timeout 15m

helm upgrade --install opsfleet "$ROOT/infra/helm/opsfleet" -n "$NAMESPACE" \
  "${common[@]}" --set components.secrets=false --wait --timeout 20m

kubectl -n "$NAMESPACE" rollout status deploy/api --timeout 5m
kubectl -n "$NAMESPACE" exec deploy/api -- python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/readyz', timeout=10).read().decode())"
echo "UI: https://ui.$BASE_DOMAIN  API: https://api.$BASE_DOMAIN  Auth: https://auth.$BASE_DOMAIN  Langfuse: https://langfuse.$BASE_DOMAIN"
