#!/bin/bash
# DELEG8 - deploy to Google Cloud Run (API + web console), on free-tier settings.
#
#   export PROJECT_ID=your-gcp-project
#   export DATABASE_URL='postgresql://USER:PASS@HOST/DB?sslmode=require'   # Neon / Supabase
#   export GEMINI_API_KEY=...                                              # aistudio.google.com/apikey (free)
#   ./deploy/deploy.sh
#
# Re-running is safe: secrets are only created once, services are updated in place.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${DATABASE_URL:?set DATABASE_URL (a free Neon/Supabase Postgres URL)}"
: "${GEMINI_API_KEY:?set GEMINI_API_KEY (free key from aistudio.google.com/apikey)}"
REGION="${REGION:-us-central1}"          # a free-tier Cloud Run region
REPO="${REPO:-deleg8}"
API_SERVICE="${API_SERVICE:-deleg8-api}"
WEB_SERVICE="${WEB_SERVICE:-deleg8-web}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

gcloud config set project "$PROJECT_ID" >/dev/null
echo "==> Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
    artifactregistry.googleapis.com secretmanager.googleapis.com

echo "==> Artifact Registry repo"
gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 || \
    gcloud artifacts repositories create "$REPO" --repository-format=docker --location "$REGION"
REGISTRY="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO"

put_secret() {  # name value  (create once, or add a new version when the value changed)
    local name="$1" value="$2"
    if gcloud secrets describe "$name" >/dev/null 2>&1; then
        [ "$(gcloud secrets versions access latest --secret "$name")" = "$value" ] || \
            printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=-
    else
        printf '%s' "$value" | gcloud secrets create "$name" --data-file=-
    fi
}
gen() { python3 -c "import secrets; print(secrets.token_urlsafe(48))"; }

echo "==> Secrets"
# Generated once and then kept: rotating ENCRYPTION_KEY makes stored user keys unreadable.
gcloud secrets describe deleg8-secret-key >/dev/null 2>&1 || put_secret deleg8-secret-key "$(gen)"
gcloud secrets describe deleg8-encryption-key >/dev/null 2>&1 || put_secret deleg8-encryption-key "$(gen)"
put_secret deleg8-database-url "$DATABASE_URL"
put_secret deleg8-gemini-key "$GEMINI_API_KEY"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUNTIME_SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"
for s in deleg8-secret-key deleg8-encryption-key deleg8-database-url deleg8-gemini-key; do
    gcloud secrets add-iam-policy-binding "$s" \
        --member "serviceAccount:$RUNTIME_SA" --role roles/secretmanager.secretAccessor >/dev/null
done

echo "==> Building the API image (first build takes ~10 min)"
API_IMAGE="$REGISTRY/api:latest"
BUILD_CONFIG="$(mktemp)"
cat > "$BUILD_CONFIG" <<YAML
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "Dockerfile.api", "-t", "$API_IMAGE", "."]
images: ["$API_IMAGE"]
timeout: 1800s
YAML
gcloud builds submit --config "$BUILD_CONFIG" .

SECRETS="DATABASE_URL=deleg8-database-url:latest,DELEG8_SECRET_KEY=deleg8-secret-key:latest,DELEG8_ENCRYPTION_KEY=deleg8-encryption-key:latest,GEMINI_API_KEY=deleg8-gemini-key:latest"

echo "==> Deploying the API"
# max-instances=1: playground sessions and the vector index live in this process's
# memory/disk, so exactly one instance must serve every request.
gcloud run deploy "$API_SERVICE" --image "$API_IMAGE" --region "$REGION" \
    --allow-unauthenticated --memory 2Gi --cpu 1 --cpu-boost \
    --min-instances 0 --max-instances 1 --timeout 300 \
    --set-secrets "$SECRETS" \
    --set-env-vars "DELEG8_ENV=production,DELEG8_DISPATCH_MODE=inprocess,DELEG8_LLM_PROVIDER=gemini,DELEG8_TENANT_DIR=/app/data/tenants"
API_URL="$(gcloud run services describe "$API_SERVICE" --region "$REGION" --format 'value(status.url)')"

echo "==> Building the web console against $API_URL"
WEB_IMAGE="$REGISTRY/web:latest"
gcloud builds submit web --config deploy/cloudbuild-web.yaml \
    --substitutions "_IMAGE=$WEB_IMAGE,_API_URL=$API_URL"

echo "==> Deploying the web console"
gcloud run deploy "$WEB_SERVICE" --image "$WEB_IMAGE" --region "$REGION" \
    --allow-unauthenticated --memory 512Mi --cpu 1 \
    --min-instances 0 --max-instances 2 --port 3000
WEB_URL="$(gcloud run services describe "$WEB_SERVICE" --region "$REGION" --format 'value(status.url)')"

echo "==> Pointing the API at the console (CORS + public URL)"
gcloud run services update "$API_SERVICE" --region "$REGION" \
    --update-env-vars "DELEG8_CORS_ORIGINS=$WEB_URL,DELEG8_BASE_URL=$API_URL"

cat <<DONE

Deployed.
  Console (give this to the judges): $WEB_URL/judges
  API health:                        $API_URL/api/v1/health
  API docs:                          $API_URL/docs

Google sign-in (optional): add  $API_URL/api/v1/auth/oauth/google/callback
as an authorised redirect URI on the OAuth client, then set GOOGLE_SSO_CLIENT_ID/SECRET.
DONE
