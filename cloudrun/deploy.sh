#!/usr/bin/env bash
set -euo pipefail

BASE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$BASE"
if [[ ! -f cloudrun/.env ]]; then
  echo 'Copia cloudrun/.env.example a cloudrun/.env y completa proyecto, región y bucket.' >&2
  exit 1
fi
source cloudrun/.env
: "${PROJECT_ID:?Falta PROJECT_ID}" "${REGION:?Falta REGION}"
: "${DATASET:?Falta DATASET}"
STORAGE_BACKEND=${STORAGE_BACKEND:-gcs}
if [[ "$STORAGE_BACKEND" != sftp && "$STORAGE_BACKEND" != gcs ]]; then
  echo 'STORAGE_BACKEND debe ser sftp o gcs.' >&2; exit 1
fi
if [[ "$STORAGE_BACKEND" == gcs ]]; then : "${BUCKET:?Falta BUCKET}"; fi
BUCKET=${BUCKET:-unused}
SFTP_PASSWORD_SECRET=${SFTP_PASSWORD_SECRET:-iess-sftp-password}
REPOSITORY=${REPOSITORY:-iess}
SERVICE_ACCOUNT_NAME=${SERVICE_ACCOUNT_NAME:-iess-worker}
IMAGE_TAG=${IMAGE_TAG:-v1}
PARALLELISM=${PARALLELISM:-2}
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/worker:${IMAGE_TAG}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
STAGING="cloudrun/staging/${DATASET}"
PHASE=${2:-companias}
if [[ "$PHASE" != companias && "$PHASE" != personas ]]; then
  echo 'La fase debe ser companias o personas.' >&2
  exit 1
fi
PYTHON=${IESS_PYTHON:-.venv/bin/python}

render() {
  "$PYTHON" -m cloudrun.render --manifest "$STAGING/manifest_${PHASE}.json" \
    --bucket "$BUCKET" --image "$IMAGE" --service-account "$SERVICE_ACCOUNT" \
    --storage "$STORAGE_BACKEND" --password-secret "$SFTP_PASSWORD_SECRET" \
    --parallelism "$PARALLELISM" --output "cloudrun/rendered/${PHASE}.yaml"
  "$PYTHON" -m cloudrun.render --manifest "$STAGING/manifest_${PHASE}.json" \
    --bucket "$BUCKET" --image "$IMAGE" --service-account "$SERVICE_ACCOUNT" \
    --storage "$STORAGE_BACKEND" --password-secret "$SFTP_PASSWORD_SECRET" \
    --pilot --output "cloudrun/rendered/${PHASE}-piloto.yaml"
}

case "${1:-help}" in
  setup)
    gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
      cloudbuild.googleapis.com storage.googleapis.com iam.googleapis.com --project "$PROJECT_ID"
    if [[ "$STORAGE_BACKEND" == sftp ]]; then
      gcloud services enable secretmanager.googleapis.com --project "$PROJECT_ID"
      "$PYTHON" -m hostinger.admin init
    fi
    if ! gcloud artifacts repositories describe "$REPOSITORY" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
      gcloud artifacts repositories create "$REPOSITORY" --repository-format docker --location "$REGION" --project "$PROJECT_ID"
    fi
    if [[ "$STORAGE_BACKEND" == gcs ]] && ! gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT_ID" >/dev/null 2>&1; then
      gcloud storage buckets create "gs://$BUCKET" --location "$REGION" --project "$PROJECT_ID" \
        --uniform-bucket-level-access --public-access-prevention
    fi
    if ! gcloud iam service-accounts describe "$SERVICE_ACCOUNT" --project "$PROJECT_ID" >/dev/null 2>&1; then
      gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" --display-name 'Consultas IESS por lotes' --project "$PROJECT_ID"
    fi
    if [[ "$STORAGE_BACKEND" == gcs ]]; then
      gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
        --member "serviceAccount:$SERVICE_ACCOUNT" --role roles/storage.objectUser --project "$PROJECT_ID"
    else
      "$PYTHON" -m hostinger.admin secret --project "$PROJECT_ID" --service-account "$SERVICE_ACCOUNT" --secret-name "$SFTP_PASSWORD_SECRET"
    fi
    ;;
  build)
    gcloud builds submit . --tag "$IMAGE" --project "$PROJECT_ID"
    ;;
  ci)
    "$PYTHON" -m cloudrun.build_job --phase "$PHASE" --mode "${3:-pilot}"
    ;;
  prepare)
    "$PYTHON" -m cloudrun.prepare --dataset "$DATASET" --output "$STAGING"
    ;;
  upload)
    test -f "$STAGING/manifest_companias.json"
    test -f "$STAGING/manifest_personas.json"
    if [[ "$STORAGE_BACKEND" == sftp ]]; then
      "$PYTHON" -m hostinger.admin upload --directory "$STAGING" --dataset "$DATASET"
    else
      gcloud storage rsync "$STAGING" "gs://$BUCKET/runs/$DATASET" --recursive --project "$PROJECT_ID"
    fi
    ;;
  render)
    render
    ;;
  pilot)
    render
    gcloud run jobs replace "cloudrun/rendered/${PHASE}-piloto.yaml" --region "$REGION" --project "$PROJECT_ID"
    gcloud run jobs execute "iess-${PHASE}-piloto" --region "$REGION" --project "$PROJECT_ID" --wait
    ;;
  deploy)
    render
    gcloud run jobs replace "cloudrun/rendered/${PHASE}.yaml" --region "$REGION" --project "$PROJECT_ID"
    ;;
  run)
    gcloud run jobs execute "iess-${PHASE}" --region "$REGION" --project "$PROJECT_ID"
    ;;
  status)
    gcloud run jobs executions list --job "iess-${PHASE}" --region "$REGION" --project "$PROJECT_ID"
    "$PYTHON" -m cloudrun.control status --storage "$STORAGE_BACKEND" --bucket "$BUCKET"
    ;;
  collect)
    if [[ "$STORAGE_BACKEND" == sftp ]]; then
      URI="sftp:///runs/$DATASET/manifest_${PHASE}.json"
    else
      URI="gs://$BUCKET/runs/$DATASET/manifest_${PHASE}.json"
    fi
    "$PYTHON" -m cloudrun.collect --manifest-uri "$URI" --output cloudrun/downloads --publish
    ;;
  *)
    echo 'Uso: bash cloudrun/deploy.sh {setup|build|ci|prepare|upload|render|pilot|deploy|run|status|collect} [companias|personas] [pilot|full]'
    ;;
esac
