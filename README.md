# Gemini Browser Skill Agent

웹 화면에서 URL을 등록하고 사이트별 스킬을 생성·조회하며, 별도 워크플로우 화면에서 선택한 스킬들을 워크플로우로 저장하고 실행하는 에이전트입니다. 모든 모델 호출은 Gemini를 사용합니다.

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

`.env`의 `GEMINI_API_KEY` 또는 `GOOGLE_API_KEY`를 읽습니다. 대상 URL은 환경변수가 아니라 웹 요청마다 입력받습니다. 모델은 `agentic_web/config.py`의 역할별 설정과 `GEMINI_ROOT_MODEL`, `GEMINI_SYNTHESIZER_MODEL`, `GEMINI_EXECUTOR_MODEL`로 지정합니다.

## 사용

1. 웹 화면에서 사이트 URL을 입력하고 **URL 스킬 판별/추출**을 누릅니다.
2. **등록된 사이트와 스킬**에서 사이트별 기능을 확인합니다. 같은 URL은 기존 스킬을 재사용하고, 새로운 기능은 기존 기능을 보존하며 추가합니다.
3. 기능의 **+ 단계 추가**로 워크플로우를 구성합니다. 입력값을 채우고 **위로 / 아래로**로 실행 순서를 정합니다.
4. 이름을 입력해 **워크플로우 저장**을 누릅니다. 저장 목록에서 **불러오기**로 수정하거나 **실행**으로 실행합니다.

예를 들어 `할 일 추가 → 블로그 글쓰기 → 할 일 완료`를 서로 다른 URL의 스킬로 구성할 수 있습니다. 동일한 사이트로 돌아가거나 같은 스킬을 여러 번 추가할 수 있습니다. 단계 입력값은 직접 지정하며, 앞 단계 결과를 변수로 자동 연결하는 편집 기능은 아직 없습니다.

실행기는 각 단계의 URL에 속한 도구만 사용합니다. 경로·쿼리·해시 라우트까지 정확히 구분합니다. 단계별 결과를 검증하고, 실패하면 후속 단계는 중단합니다. 로그인 요청은 브라우저에서 처리하고 **로그인·입력 완료 후 재개**를 누릅니다. 재개 정보는 현재 서버 프로세스에서 유지됩니다.

저장 후 도구 정의가 바뀌면 워크플로우 재저장을 요청합니다. 이미 성공한 변경은 검증이 불확실하다고 무조건 재실행하지 않습니다. 자연어 작업 실행 경로는 실패 시 스킬을 보수해 한 번 재시도합니다.

## 저장과 현재 범위

현재 스킬은 서버 디스크의 `skills_store/`, 워크플로우는 `workflows_store/`에 JSON으로 저장됩니다. **외부 DB 저장은 아직 구현되지 않았습니다.** 로컬 실행 시 로컬 디스크에 저장되며, 배포 환경에서 영속 디스크 없이 사용하면 재배포 시 데이터가 유실될 수 있습니다. 브라우저 프로필·키·실행 기록·저장 데이터는 Git에서 제외합니다.

Google 사용자 ID를 기준으로 스킬·워크플로우 저장소와 브라우저 세션을 분리합니다. 신규 사용자는 빈 목록에서 시작하며 공용 데이터나 테스트 데이터를 복사하지 않습니다. 실행 서비스·분석 작업 상태는 서버 프로세스 메모리에 있으므로 다중 워커 간 공유와 서버 재시작 후 실행 재개는 지원하지 않습니다. 웹 UI·API는 `agentic_web/web/`, 브라우저와 에이전트 실행은 `agentic_web/engine/`에서 관리합니다. 엔진은 Flask나 웹 화면에 의존하지 않습니다.

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

사용자 ID에 따른 로컬 저장소·브라우저 격리는 연결했습니다. **DB 저장은 아직 구현되지 않았습니다.** 배포 시 영속 저장소와 다중 인스턴스 실행 상태 관리를 추가해야 합니다.

사이트 분석(`/`)과 워크플로우 편집(`/workflows`)은 별도 화면입니다. 분석은 스킬만 등록하고 워크플로우는 생성하지 않습니다. 분석 버튼을 누르면 접수·페이지 열기·Gemini 분석·등록 단계를 경과 시간과 함께 표시하며 실패 이유도 표시합니다. 데모 페이지는 `tests/fixtures/`에만 두고 테스트 실행은 임시 저장소를 사용합니다.

결과 확인은 `engine/agents/result_verifier.py`의 독립 에이전트가 담당합니다. 실행기의 성공 플래그는 작업 완료 근거로 사용하지 않습니다. 변경 작업은 페이지를 다시 열어 저장된 결과를 읽고, 요청별 조건과 실제 관찰 문자열을 대조합니다. Gemini 검증 실패·누락된 근거·조회만 수행한 변경 요청은 성공 처리하지 않습니다. 작성 페이지처럼 URL이 바뀌면 해당 URL의 스킬을 다시 선택합니다.
