#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-agentic-web}"
REGION="${REGION:-asia-northeast3}"

cd "$(dirname "$0")"

# Ensure Homebrew paths are in PATH
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME/google-cloud-sdk/bin:$PATH"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "📦 gcloud CLI가 설치되어 있지 않아 Homebrew로 설치합니다..."
  brew install --cask google-cloud-sdk
  export PATH="/opt/homebrew/share/google-cloud-sdk/bin:/usr/local/share/google-cloud-sdk/bin:$PATH"
fi

# Check gcloud authentication
ACTIVE_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || true)"
if [ -z "$ACTIVE_ACCOUNT" ]; then
  echo "🔐 Google Cloud 계정 로그인이 필요합니다. 브라우저에서 로그인해주세요..."
  gcloud auth login
fi

# Load local .env if present
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# Automatically detect GCP Project from GOOGLE_CLIENT_ID prefix (800515339243) or gcloud config
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-}"
if [ -z "$PROJECT_ID" ] && [ -n "${GOOGLE_CLIENT_ID:-}" ]; then
  PROJECT_NUMBER="${GOOGLE_CLIENT_ID%%-*}"
  if [[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]]; then
    PROJECT_ID="$(gcloud projects describe "$PROJECT_NUMBER" --format='value(projectId)' 2>/dev/null || true)"
  fi
fi

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
fi

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  PROJECT_ID="$(gcloud projects list --format='value(projectId)' --limit=1 2>/dev/null || true)"
fi

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "❌ GCP 프로젝트를 찾을 수 없습니다. 'gcloud config set project [PROJECT_ID]' 실행 후 다시 시도하세요."
  exit 1
fi

gcloud config set project "$PROJECT_ID"
echo "🚀 배포 대상 GCP 프로젝트: $PROJECT_ID (리전: $REGION, 서비스: $SERVICE_NAME)"

if [ -z "${FLASK_SECRET_KEY:-}" ]; then
  FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
fi

echo "🔧 Cloud Run / Cloud Build / Artifact Registry API 활성화 중..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --project "$PROJECT_ID"

INITIAL_REDIRECT_URI="${GOOGLE_REDIRECT_URI:-}"
if [[ -z "$INITIAL_REDIRECT_URI" || "$INITIAL_REDIRECT_URI" == http://127.0.0.1* || "$INITIAL_REDIRECT_URI" == http://localhost* ]]; then
  INITIAL_REDIRECT_URI="https://placeholder.run.app/auth/google/callback"
fi

echo "📦 Cloud Run 컨테이너 빌드 및 배포 시작..."
gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --concurrency 4 \
  --max-instances 1 \
  --set-env-vars="HEADLESS_BROWSER=true,GEMINI_API_KEY=${GEMINI_API_KEY:-},FLASK_SECRET_KEY=${FLASK_SECRET_KEY},GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-},GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET:-},GOOGLE_REDIRECT_URI=${INITIAL_REDIRECT_URI}"

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
CALLBACK_URI="${SERVICE_URL}/auth/google/callback"

if [ "$INITIAL_REDIRECT_URI" != "$CALLBACK_URI" ]; then
  echo "🔗 서비스 URL 확정됨 ($SERVICE_URL) -> GOOGLE_REDIRECT_URI 자동 업데이트 중..."
  gcloud run services update "$SERVICE_NAME" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --update-env-vars="GOOGLE_REDIRECT_URI=${CALLBACK_URI}"
fi

echo ""
echo "✅ GCP Cloud Run 배포 완료!"
echo "🌐 서비스 접속 URL: ${SERVICE_URL}"
echo "🔑 Google Cloud Console -> Google Auth Platform (OAuth 클라이언트)의"
echo "   [승인된 리디렉션 URI]에 아래 주소를 추가해주세요:"
echo "   ${CALLBACK_URI}"
