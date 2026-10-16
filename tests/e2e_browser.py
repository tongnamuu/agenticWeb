"""
End-to-End Verification Suite for the Gemini URL-Gated Web Skill Multi-Agent System.

Verifies all 5 core user requirements against real Chrome CDP + Flask test server:
1. [Per-URL Skill Synthesis & Dynamic Registration]
   - Input a To-Do List URL -> Analyze DOM -> Synthesize reusable tools -> Persist in `skills_store/`.
2. [Strict URL-Based Skill Selection & Reuse]
   - Calling the same URL again reuses the registered skill (`skill_reused == True`).
   - Calling a different URL (`/demo-blog`) strictly isolates tools (`URLMatcher`) and blocks cross-URL execution (`URL_MISMATCH_BLOCKED`).
3. [Root Agent Post-Execution Verification & 1x Automatic Retry Loop]
   - Inject a broken CSS selector into a registered skill -> Attempt 1 fails DOM verification -> Root Agent triggers 1x retry loop (`retry_count == 1`), repairs the skill, and Attempt 2 succeeds (`SUCCESS`).
4. [Human-in-the-Loop (HITL) Login / User Input Request]
   - Navigate to login-protected URL (`/demo-protected`) -> Root Agent detects login wall and returns `status == "NEEDS_USER_INPUT"` (`LOGIN_REQUIRED`) -> Provide user login credentials -> Logs in, extracts post-login skill, and completes task (`SUCCESS`).
"""

import threading
import time
from werkzeug.serving import make_server

import tempfile
from flask import Flask
from agentic_web.engine.browser.cdp import BrowserSession
from agentic_web.engine.agents.orchestrator import RootOrchestratorAgent
from agentic_web.engine.skills.registry import SkillRegistry
from tests.fixtures.demo_pages import DEMO_BLOG_HTML, DEMO_PROTECTED_HTML

app = Flask(__name__)
_test_store = tempfile.TemporaryDirectory(prefix='agenticweb-e2e-')
browser_session = BrowserSession(headless=True)
orchestrator = RootOrchestratorAgent(registry=SkillRegistry(_test_store.name))
app.add_url_rule('/demo-blog', 'demo_blog', lambda: DEMO_BLOG_HTML)
app.add_url_rule('/demo-protected', 'demo_protected', lambda: DEMO_PROTECTED_HTML)


TEST_TODO_HTML = """<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><title>Smart ToDo List</title></head>
<body>
  <h1>Smart ToDo List</h1>
  <div>
    <input id="todo-input" type="text" placeholder="할 일을 입력하세요" />
    <button id="add-btn" onclick="addTodo()">추가</button>
  </div>
  <ul id="todo-list">
    <li class="todo-item"><span>기존 샘플 할 일</span><button class="delete-btn" onclick="this.parentElement.remove()">삭제</button></li>
  </ul>
  <script>
    function addTodo() {
      const inp = document.getElementById('todo-input');
      const v = inp.value.trim();
      if (!v) return;
      const li = document.createElement('li');
      li.className = 'todo-item';
      li.innerHTML = '<span>' + v + '</span><button class="delete-btn" onclick="this.parentElement.remove()">삭제</button>';
      document.getElementById('todo-list').appendChild(li);
      inp.value = '';
    }
  </script>
</body>
</html>"""


@app.route("/demo-todo")
def demo_todo():
    return TEST_TODO_HTML


class BackgroundServer(threading.Thread):
    def __init__(self, flask_app, port: int = 5059):
        super().__init__(daemon=True)
        self.server = make_server("127.0.0.1", port, flask_app, threaded=True)
        self.ctx = flask_app.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def stop(self):
        self.server.shutdown()


def run_all_verifications():
    port = 5059
    srv = BackgroundServer(app, port=port)
    srv.start()
    time.sleep(0.8)

    todo_url = f"http://127.0.0.1:{port}/demo-todo"
    blog_url = f"http://127.0.0.1:{port}/demo-blog"
    protected_url = f"http://127.0.0.1:{port}/demo-protected"

    # Clean any previous test skills for these local test URLs
    orchestrator.registry.delete_skill_by_url(todo_url)
    orchestrator.registry.delete_skill_by_url(blog_url)
    orchestrator.registry.delete_skill_by_url(protected_url)

    try:
        print("\n=== [TEST 1] 첫 번째 URL(ToDoList) 입력 → 페이지 분석 → 스킬 생성 및 실행 ===")
        res1 = orchestrator.run_task(
            target_url=todo_url,
            user_prompt="우유 사기 할 일 목록에 추가해줘",
            browser_session=browser_session,
        )
        skill_id_1 = res1.get("skill_package", {}).get("skill_id")
        print(f"Status: {res1['status']} | Skill ID: {skill_id_1} | Reused: {res1.get('skill_reused')}")
        assert res1["status"] == "SUCCESS", f"Expected SUCCESS, got {res1.get('summary')}"
        assert res1["skill_reused"] is False, "First run on URL should synthesize a new skill"

        print("\n=== [TEST 2] 동일 URL 재호출 → URL 일치 판별 후 기존 스킬 즉시 재사용 ===")
        res2 = orchestrator.run_task(
            target_url=todo_url,
            user_prompt="운동 가기 할 일 목록에 추가해줘",
            browser_session=browser_session,
        )
        skill_id_2 = res2.get("skill_package", {}).get("skill_id")
        print(f"Status: {res2['status']} | Skill ID: {skill_id_2} | Reused: {res2.get('skill_reused')}")
        assert res2["status"] == "SUCCESS", f"Expected SUCCESS on reuse, got {res2.get('summary')}"
        assert res2["skill_reused"] is True, "Second run on same normalized URL must reuse existing skill"
        assert skill_id_1 == skill_id_2

        print("\n=== [TEST 3] 엄격한 URL 기반 스킬 분리 및 교차 URL 호출 차단 검증 ===")
        todo_tools, _, _ = orchestrator.registry.get_tools_strictly_for_url(todo_url)
        blog_tools_before, _, _ = orchestrator.registry.get_tools_strictly_for_url(blog_url)
        assert len(todo_tools) > 0
        assert len(blog_tools_before) == 0, "Unanalyzed URL must NOT inherit tools from another URL"

        # Navigate browser to blog_url, then attempt to invoke todo_url's tool -> must be blocked by URL guard
        browser_session.navigate(blog_url)
        cross_exec = browser_session.execute_tool(todo_tools[0], {})
        failure_code = cross_exec["action_result"].get("failure_code")
        print(f"Cross-URL Guard Result: {failure_code} ({cross_exec['action_result'].get('failure_reason')})")
        assert cross_exec["action_result"].get("success") is False
        assert failure_code == "URL_MISMATCH_BLOCKED"

        print("\n=== [TEST 4] 루트 에이전트 실행 실패 감지 및 1회 자동 재시도(Retry Loop) 검증 ===")
        # Intentionally corrupt the selector of todo_url's add tool so Attempt 1 fails DOM check
        corrupted_skill, _ = orchestrator.registry.resolve_skill_by_url(todo_url)
        assert corrupted_skill is not None
        for t in corrupted_skill["tools"]:
            if "add" in t["name"] or "submit" in t["name"]:
                for step in t.get("steps", []):
                    if step.get("op") in ("fill", "click", "submit"):
                        step["selector"] = "#non-existent-broken-selector-999"
                        step["placeholder"] = ""
                        step["button_text"] = ""
        orchestrator.registry.register_skill(
            target_url=todo_url,
            tools=corrupted_skill["tools"],
            site_title=corrupted_skill.get("site_title", ""),
            synthesized_by="corrupted_for_retry_test",
            is_repair=True,
        )

        res_retry = orchestrator.run_task(
            target_url=todo_url,
            user_prompt="책 읽기 할 일 목록에 추가해줘",
            browser_session=browser_session,
        )
        print(
            f"Retry Test Status: {res_retry['status']} | Retry Count: {res_retry['retry_count']} "
            f"| Attempts: {len(res_retry['attempts'])}"
        )
        assert res_retry["retry_count"] == 1, f"Expected 1 retry, got {res_retry['retry_count']}"
        assert len(res_retry["attempts"]) == 2, "Should have Attempt 1 (FAILED) and Attempt 2 (SUCCESS)"
        assert res_retry["attempts"][0]["verification"]["status"] == "FAILED"
        assert res_retry["status"] == "SUCCESS", f"Expected SUCCESS after 1x retry repair, got {res_retry.get('summary')}"

        print("\n=== [TEST 5] 로그인 필요 사이트 감지 → 유저 입력(HITL) 요청 → 로그인 후 작업 완수 ===")
        res_login_req = orchestrator.run_task(
            target_url=protected_url,
            user_prompt="방화벽 정책 검토 할 일 목록에 추가해줘",
            browser_session=browser_session,
        )
        print(
            f"Step 5-A (Before Login) Status: {res_login_req['status']} "
            f"| Input Type: {res_login_req.get('user_input_request', {}).get('input_type')} "
            f"| Summary: {res_login_req.get('summary')}"
        )
        assert res_login_req["status"] == "NEEDS_USER_INPUT"
        assert res_login_req["user_input_request"]["input_type"] == "LOGIN_REQUIRED"

        # Now provide user credentials (HITL response) and resume
        res_login_done = orchestrator.run_task(
            target_url=protected_url,
            user_prompt="방화벽 정책 검토 할 일 목록에 추가해줘",
            browser_session=browser_session,
            user_inputs={
                "username": "security_admin",
                "password": "pass1234",
            },
        )
        print(
            f"Step 5-B (After User Login Input) Status: {res_login_done['status']} "
            f"| Summary:\n{res_login_done.get('summary')}"
        )
        assert res_login_done["status"] == "SUCCESS", f"Expected SUCCESS after HITL login, got {res_login_done.get('summary')}"

        print("\n🎉 모든 5대 핵심 요구사항 E2E 검증 통과 완료!")
    finally:
        orchestrator.registry.delete_skill_by_url(todo_url)
        orchestrator.registry.delete_skill_by_url(protected_url)
        browser_session.close()
        srv.stop()
        _test_store.cleanup()


if __name__ == "__main__":
    run_all_verifications()
