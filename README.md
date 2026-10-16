# Gemini Browser Skill Agent

웹 화면에서 URL을 분석하고 사이트별 스킬을 생성·조회·실행하는 에이전트입니다. 모든 모델 호출은 Gemini를 사용합니다.

## 실행

프로그램 진입점은 **`app.py` 하나**입니다. 웹 화면과 API 서버를 함께 실행합니다.

Python 3.11 이상과 Google Chrome이 필요합니다.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# .env가 없으면 .env.example을 복사하고 API 키를 설정합니다.
python app.py
```

브라우저에서 http://127.0.0.1:5055 에 접속합니다. 배포 시 WSGI 대상은 `app:app`입니다. 개발용 Flask 서버를 외부에 공개하는 대신 배포 환경의 WSGI 서버를 사용해야 합니다.

`.env`의 `GEMINI_API_KEY` 또는 `GOOGLE_API_KEY`를 읽습니다. 대상 URL은 환경변수가 아니라 웹 요청마다 입력받습니다. 기본 모델은 결과 판정에 `gemini-3.8-flash`, 분석·실행에 `gemini-4-argon`(high thinking)을 사용하며 구형 모델로 자동 전환하지 않습니다. 모델은 `agentic_web/config.py`의 역할별 설정과 `GEMINI_ROOT_MODEL`, `GEMINI_SYNTHESIZER_MODEL`, `GEMINI_EXECUTOR_MODEL`로 지정합니다.

## 사용

1. URL을 입력하고 **사이트 분석 · 갱신**을 누릅니다. 같은 URL은 기존 기능을 보존하며 갱신합니다.
2. 로그인 요청이 나오면 **로그인 브라우저로 이동 · 완료 후 자동 재개**를 누릅니다. 웹앱에 표시되는 실행기 브라우저 화면에서 직접 로그인하면 상태를 확인하고 분석을 이어갑니다. 로컬과 Cloud Run 모두 같은 화면을 사용합니다. OAuth 팝업은 화면 상단 탭 목록에서 선택하고, 로그인 후 원래 사이트 탭으로 돌아옵니다. 로그인 제공자가 자동화 브라우저를 차단하는 경우까지 우회하지는 않습니다.
3. 사이트 카드 아래에서 스킬의 입력값을 채우고 실행합니다. 호출할 때는 경로·쿼리·해시까지 정확히 일치하는 URL의 도구만 선택합니다.
4. 독립 결과 확인 에이전트가 저장된 결과를 다시 읽어 성공 여부를 판단합니다. 검증이 불확실한 변경을 무조건 중복 실행하지 않습니다.

워크플로우 탭(`/workflows`)에서는 등록된 스킬을 단계로 추가하고 입력값·실행 순서를 정해 저장합니다. 서로 다른 사이트의 스킬도 연결할 수 있으며, 분석만으로 워크플로우가 자동 생성되지는 않습니다. 저장 목록에서 실행하면 현재 단계와 검증 결과를 표시합니다. 로그인 대기는 실행기 브라우저에서 처리한 뒤 해당 단계부터 재개하고, 실패하면 후속 단계를 실행하지 않습니다. 스킬 정의가 바뀌면 워크플로우를 다시 저장해야 합니다.

워크플로우 정의는 사용자별로 저장하며 Firestore 활성화 시 `agentic_web_workflows` 컬렉션을 사용합니다. 실행 상태와 재개 정보는 서버 메모리에 있어 재시작하면 사라집니다. 단계 간 결과를 변수로 전달하는 기능은 아직 없으며, 각 단계의 입력값을 직접 지정합니다. 분석 중에는 테스트 글이나 할 일을 생성하지 않습니다.

## 저장과 현재 범위

DB는 GCP 프로젝트 `h2gdm-hack26icn-2680`의 Cloud Firestore를 사용할 예정입니다. 연결 코드는 `agentic_web/engine/storage.py`, 설정은 `agentic_web/config.py`에 있습니다.

- 활성화: `USE_FIRESTORE=true`
- 프로젝트: `GOOGLE_CLOUD_PROJECT=h2gdm-hack26icn-2680` 또는 `FIRESTORE_PROJECT_ID`
- 데이터베이스: `FIRESTORE_DATABASE=(default)`
- 스킬 컬렉션: `agentic_web_skills`

Google 사용자 ID의 해시를 기준으로 저장 범위와 브라우저 세션을 분리합니다. 신규 사용자는 빈 목록에서 시작합니다. Firestore는 Google 기본 인증 자격 증명을 사용합니다. `deploy.sh`는 배포 프로젝트의 DB 생성 및 활성화를 시도하지만, 실제 연결과 저장 성공은 배포 환경에서 별도로 확인해야 합니다.

현재 구현은 로컬 JSON 미러를 유지하며, Firestore 비활성화 또는 연결 실패 시 로컬 파일로 동작합니다. 로컬 `.env`에 DB 설정이 없다면 Firestore에 연결된 것이 아닙니다. Cloud Run에서는 자동 활성화됩니다. 배포 프로젝트는 `GOOGLE_CLOUD_PROJECT`로 명시하세요.

분석 작업과 브라우저는 서버 프로세스 메모리에 있으므로 다중 워커 공유와 재시작 후 작업 재개는 지원하지 않습니다. 키·브라우저 프로필·사용자 저장 데이터는 Git에서 제외합니다.

## 패키지 구조

```text
app.py                         # 유일한 서버 실행 진입점
agentic_web/
  config.py                    # 프로젝트 설정과 저장 경로
  web/
    __init__.py                # Flask 앱 생성
    routes.py                  # 사용자 요청과 API 응답
    demo_pages.py              # 데모 페이지
    templates/                 # 웹 화면
    static/                    # 웹 화면 JavaScript
  engine/
    runtime.py                 # 브라우저·에이전트 연결
    schemas.py                 # 실행 데이터 모델
    browser/
      cdp.py                   # 웹 서버가 사용하는 Chrome 제어
      playwright.py            # Playwright 브라우저 백엔드
    agents/
      orchestrator.py          # 스킬 생성·실행·검증
      site.py                  # 사이트 단위 실행
      workflow.py              # 사이트 간 작업 조율
      gemini.py                # Gemini SDK 클라이언트
    skills/
      registry.py              # URL별 스킬 등록·조회
    workflows/
      saved.py                 # 저장 워크플로우 실행
tests/                         # 단위·통합 테스트
```

설정은 프로젝트 루트의 `.env`를 읽고, 저장 경로도 루트의 기존 디렉터리를 유지합니다. 패키지를 옮기면서 저장 데이터를 이동하거나 초기화하지 않습니다.

## 검증

```sh
python -m unittest discover -s tests -v
```

단위 테스트는 URL 격리, 스킬 누적, 워크플로우 저장·재실행, 사이트 왕복 순서, 실패 후 종속 단계 중단, 로그인 후 재개를 검증합니다. 실제 사이트와 Gemini 응답을 포함한 통합 실행 성공까지 보장하지는 않습니다.

실제 Chrome을 사용하는 별도 통합 테스트는 `python -m tests.e2e_browser`로 실행합니다.

## Google 로그인

Google Auth Platform에서 OAuth 동의 화면을 설정하고 **웹 애플리케이션** 유형의 OAuth 클라이언트를 생성합니다. 승인된 리디렉션 URI는 로컬 실행 시 다음과 같습니다.

```text
http://127.0.0.1:5055/auth/google/callback
```

발급한 클라이언트 정보를 `.env`에 설정합니다. Gemini API 키와는 별도입니다.

```dotenv
GOOGLE_CLIENT_ID=발급한-클라이언트-ID
GOOGLE_CLIENT_SECRET=발급한-클라이언트-시크릿
GOOGLE_REDIRECT_URI=http://127.0.0.1:5055/auth/google/callback
FLASK_SECRET_KEY=충분히-긴-임의의-비밀값
```

세션 키는 `python -c 'import secrets; print(secrets.token_hex(32))'`로 생성할 수 있습니다. 서버 인스턴스 간 같은 키를 유지해야 합니다. 배포 시 콜백을 실제 HTTPS 도메인으로 변경하고 Google 콘솔에도 동일한 주소를 등록합니다. HTTPS 콜백 설정에서는 Secure 쿠키를 사용합니다.

로그인 전에는 작업 화면과 `/api/` 요청을 사용할 수 없습니다. 로그인은 `openid email profile`만 요청하고, Authlib가 state·nonce·서명·발급자·대상·만료를 검증합니다. 로그인 세션에는 Google 사용자 ID(`sub`)·이름·이메일만 저장하며 Google 토큰은 저장하지 않습니다. 변경 API와 로그아웃 요청에는 CSRF 토큰이 필요합니다.

사용자별 저장 범위와 브라우저 세션을 분리합니다. 다중 인스턴스 실행 상태 공유는 아직 지원하지 않습니다.

사이트 분석과 스킬 실행은 `/` 화면에서 제공합니다. 분석 버튼을 누르면 접수·페이지 열기·Gemini 분석·등록 단계를 경과 시간과 함께 표시하며 실패 이유도 표시합니다. 데모 페이지는 `tests/fixtures/`에만 두고 테스트 실행은 임시 저장소를 사용합니다.

결과 확인은 `engine/agents/result_verifier.py`의 독립 에이전트가 담당합니다. 실행기의 성공 플래그는 작업 완료 근거로 사용하지 않습니다. 변경 작업은 페이지를 다시 열어 저장된 결과를 읽고, 요청별 조건과 실제 관찰 문자열을 대조합니다. Gemini 검증 실패·누락된 근거·조회만 수행한 변경 요청은 성공 처리하지 않습니다. 작성 페이지처럼 URL이 바뀌면 해당 URL의 스킬을 다시 선택합니다.

### 모든 사용자의 Google 로그인

일반 사용자는 웹사이트의 Google 로그인 버튼을 이용합니다. gcloud 로그인과 GCP 프로젝트 권한은 배포 관리자에게만 필요합니다. 앱은 특정 이메일이나 조직으로 사용자를 제한하지 않으며 Google의 검증된 사용자 ID로 데이터를 분리합니다.

Google Auth Platform의 **Audience → External**을 사용하고, 운영 공개 시 Branding 구성을 완료한 뒤 **In production** 상태를 확인하세요. `openid email profile`만 요청하는 로그인은 Testing 상태에서도 테스트 사용자 목록 제한의 예외 대상입니다. 조직 계정은 해당 Workspace 관리자의 앱 허용 정책에 영향을 받을 수 있습니다.

공식 안내: https://developers.google.com/identity/protocols/oauth2/production-readiness/overview

Cloud Run 배포에는 웹 애플리케이션 OAuth 클라이언트의 `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, 고정된 `FLASK_SECRET_KEY`가 필요합니다. `deploy.sh`는 누락 시 중단하고, 배포 후 서비스 주소의 `/auth/google/callback`을 설정합니다. 출력된 콜백 주소를 Google OAuth 클라이언트의 승인된 리디렉션 URI에도 등록해야 합니다. 이 콘솔 설정은 Git 푸시로 변경되지 않습니다.

### 로컬과 Cloud Run 콜백

동일한 웹 OAuth 클라이언트의 승인된 리디렉션 URI에 다음 주소를 각각 등록합니다.

- `http://127.0.0.1:5055/auth/google/callback`
- `https://agentic-web-800515339243.asia-northeast3.run.app/auth/google/callback`

로컬 `.env`의 `GOOGLE_REDIRECT_URI`는 로컬 주소로 유지합니다. `deploy.sh`는 별도의 `GOOGLE_DEPLOY_REDIRECT_URI`를 Cloud Run의 `GOOGLE_REDIRECT_URI`에 전달합니다. 미설정 시 서비스 이름·프로젝트 번호·리전으로 배포 콜백을 구성합니다. 임시 placeholder 주소는 사용하지 않습니다. OAuth 설정의 콜백 등록은 환경변수 설정과 별개입니다.

결과 판정은 새로고침 후 관찰과 실행 내역을 Gemini 3.8 Flash에 전달해 결정합니다. 문자열 완전 일치로 모델 판정을 덮어쓰지 않습니다. 모델 호출·응답 형식 오류는 작업 실패와 구분해 판정 오류로 표시합니다.
