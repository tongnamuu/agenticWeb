#!/usr/bin/env bash
set -euo pipefail
# Validate the whole file before changing any cloud resources.
bash -n "$0"

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

# Deploy to the configured project; never select an arbitrary account's first project.
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-h2gdm-hack26icn-2680}"
for required_var in GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET FLASK_SECRET_KEY; do
  if [ -z "${!required_var:-}" ]; then
    echo "❌ ${required_var} 설정이 필요합니다. .env에 웹앱용 Google OAuth 설정을 입력하세요."
    exit 1
  fi
done

gcloud config set project "$PROJECT_ID"
echo "🚀 배포 대상 GCP 프로젝트: $PROJECT_ID (리전: $REGION, 서비스: $SERVICE_NAME)"

echo "🔧 Cloud Run / Cloud Build / Artifact Registry / Cloud Firestore API 활성화 중..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  --project "$PROJECT_ID"

echo "🗄️ Cloud Firestore (Native 모드) 데이터베이스 확인 및 생성 중..."
if ! gcloud firestore databases describe --database="(default)" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud firestore databases create \
    --database="(default)" \
    --location="$REGION" \
    --type=firestore-native \
    --project="$PROJECT_ID" || true
fi

# Keep the local callback in .env; deployment has its own explicit callback.
DEPLOY_REDIRECT_URI="${GOOGLE_DEPLOY_REDIRECT_URI:-}"
if [ -z "$DEPLOY_REDIRECT_URI" ]; then
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
  if [[ ! "$PROJECT_NUMBER" =~ ^[0-9]+$ ]]; then
    echo 'GCP 프로젝트 번호를 확인하지 못했습니다.' >&2
    exit 1
  fi
  DEPLOY_REDIRECT_URI="https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app/auth/google/callback"
fi
if [[ "$DEPLOY_REDIRECT_URI" != https://*/auth/google/callback || "$DEPLOY_REDIRECT_URI" == *placeholder* ]]; then
  echo 'GOOGLE_DEPLOY_REDIRECT_URI에 실제 HTTPS 콜백 주소를 설정하세요.' >&2
  exit 1
fi

echo "📦 Cloud Run 컨테이너 빌드 및 배포 시작..."
gcloud run deploy "$SERVICE_NAME" \
  --quiet \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --concurrency 4 \
  --max-instances 1 \
  --set-env-vars="HEADLESS_BROWSER=true,USE_FIRESTORE=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GEMINI_API_KEY=${GEMINI_API_KEY:-},FLASK_SECRET_KEY=${FLASK_SECRET_KEY},GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-},GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET:-},GOOGLE_REDIRECT_URI=${DEPLOY_REDIRECT_URI}"

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
CALLBACK_URI="$DEPLOY_REDIRECT_URI"

echo ""
echo "✅ GCP Cloud Run 배포 완료!"
echo "🌐 서비스 접속 URL: ${SERVICE_URL}"
echo "🔑 Google Cloud Console -> Google Auth Platform (OAuth 클라이언트)의"
echo "   [승인된 리디렉션 URI]에 아래 주소를 추가해주세요:"
echo "   ${CALLBACK_URI}"

echo "👥 모든 Google 계정으로 로그인하려면 Google Auth Platform의 Audience를 External로 설정하세요."
echo "   운영 공개 시 Branding 구성을 완료하고 In production 상태를 확인하세요."
echo "   https://console.cloud.google.com/auth/audience?project=${PROJECT_ID}"
echo "   일반 사용자는 gcloud 로그인이나 GCP 프로젝트 권한이 필요하지 않습니다."
