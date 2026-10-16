"""
Flask Web Studio & REST API Server for the Gemini URL-Gated Web Skill Multi-Agent System.

Endpoints:
- GET  /                    : Main Agent Studio UI
- GET  /api/skills          : List all per-URL registered skills + model configuration
- POST /api/analyze         : Analyze a URL (or reuse existing URL skill) and register in SkillRegistry
- POST /api/execute         : Run the full RootOrchestratorAgent pipeline:
                              (URL skill lookup/synthesis -> HITL check -> Executor -> Root Verification -> 1x Retry Loop)
- POST /api/call-tool       : Directly invoke a registered URL tool with strict URL gating
- POST /api/delete-skill    : Remove a URL skill from the registry
- GET  /api/state           : Inspect current live browser state
- GET  /demo-blog           : Built-in Blog/Board test page (for multi-URL skill isolation tests)
- GET  /demo-protected      : Built-in Login-Protected Workspace test page (for HITL Login Request tests)
"""

from flask import Flask, jsonify, render_template, request, g
from threading import Lock

from agent import browser_session, orchestrator
from config import MAX_ROOT_RETRY_COUNT, MODEL_ROLES
from skill_registry import URLMatcher

app = Flask(__name__)
from saved_workflows import SavedWorkflows
saved_workflows = SavedWorkflows(orchestrator.registry, browser_session, orchestrator)
_browser_request_lock = Lock()


@app.before_request
def serialize_browser_requests():
    browser_paths = {'/api/analyze', '/api/execute', '/api/call-tool', '/api/state', '/api/delete-skill'}
    if request.path in browser_paths or (request.path.startswith('/api/workflows/') and request.path.endswith('/run')):
        if not _browser_request_lock.acquire(blocking=False):
            return jsonify(ok=False, error='다른 브라우저 작업이 실행 중입니다. 완료 후 다시 시도하세요.'), 409
        g.browser_request_locked = True


@app.teardown_request
def release_browser_request_lock(error):
    if getattr(g, 'browser_request_locked', False):
        g.browser_request_locked = False
        _browser_request_lock.release()



@app.route('/api/workflows', methods=['GET', 'POST'])
def api_workflows():
    try:
        if request.method == 'GET':
            return jsonify(ok=True, workflows=saved_workflows.list())
        return jsonify(ok=True, workflow=saved_workflows.save(request.get_json() or {}))
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.route('/api/workflows/<identifier>/run', methods=['POST'])
def api_run_workflow(identifier):
    try:
        data = request.get_json(silent=True) or {}
        result = saved_workflows.run(identifier, data.get('run_id'))
        return jsonify(ok=result['status'] == 'SUCCESS', **result)
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, status='FAILED', error=str(exc)), 500



# =====================================================================
# Built-in Test Web Pages (for Multi-URL Isolation & HITL Login Testing)
# =====================================================================

DEMO_BLOG_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>DevNotes 블로그 보드</title>
  <style>
    body { font-family: -apple-system, sans-serif; max-width: 680px; margin: 36px auto; padding: 0 20px; background: #f8fafc; color: #0f172a; }
    .card { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 18px; }
    input, select, textarea { width: 100%; box-sizing: border-box; padding: 10px; margin-bottom: 10px; border: 1px solid #cbd5e1; border-radius: 8px; }
    button { padding: 10px 16px; border: none; border-radius: 8px; background: #2563eb; color: white; font-weight: 600; cursor: pointer; }
    button.del-btn { background: #ef4444; padding: 6px 12px; font-size: 12px; }
    ul { list-style: none; padding: 0; }
    li { display: flex; justify-content: space-between; align-items: center; padding: 12px; border-bottom: 1px solid #e2e8f0; }
  </style>
</head>
<body>
  <h1>📝 DevNotes 기술 메모 보드</h1>
  <div class="card">
    <input id="note-title" type="text" placeholder="메모 제목을 입력하세요" />
    <select id="note-category">
      <option value="AI">AI</option>
      <option value="Web">Web</option>
      <option value="Cloud">Cloud</option>
    </select>
    <button id="publish-note-btn" onclick="publishNote()">메모 등록</button>
  </div>
  <div class="card">
    <h3>등록된 메모 목록</h3>
    <ul id="note-list">
      <li class="note-item"><span>[AI] Gemini 2.5 URL Skill 아키텍처 정리</span><button class="del-btn" onclick="this.parentElement.remove()">삭제</button></li>
    </ul>
  </div>
  <script>
    function publishNote() {
      const titleInput = document.getElementById('note-title');
      const catSelect = document.getElementById('note-category');
      const val = titleInput.value.trim();
      if (!val) return;
      const li = document.createElement('li');
      li.className = 'note-item';
      li.innerHTML = '<span>[' + catSelect.value + '] ' + val + '</span><button class="del-btn" onclick="this.parentElement.remove()">삭제</button>';
      document.getElementById('note-list').appendChild(li);
      titleInput.value = '';
    }
  </script>
</body>
</html>"""


DEMO_PROTECTED_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>사내 보안 업무 포털 (로그인 필요)</title>
  <style>
    body { font-family: -apple-system, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 20px; background: #0f172a; color: #f8fafc; }
    .box { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 24px; margin-bottom: 20px; }
    input { width: 100%; box-sizing: border-box; padding: 10px 12px; margin-bottom: 12px; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: white; }
    button { padding: 10px 18px; border-radius: 8px; border: none; background: #6366f1; color: white; font-weight: 600; cursor: pointer; }
    li { display: flex; justify-content: space-between; align-items: center; padding: 10px; border-bottom: 1px solid #334155; }
  </style>
</head>
<body>
  <h1>🔒 사내 보안 승인 작업 시스템</h1>
  <div id="app-root"></div>
  <script>
    function renderApp() {
      const root = document.getElementById('app-root');
      const loggedInUser = sessionStorage.getItem('portal_user');
      if (!loggedInUser) {
        root.innerHTML = `
          <div class="box login-wall" id="loginGate" data-requires-auth="true">
            <h2>로그인이 필요한 서비스입니다</h2>
            <p style="color:#94a3b8;font-size:14px;">업무 티켓을 등록하거나 처리하려면 먼저 사내 계정으로 로그인해주세요.</p>
            <form onsubmit="doLogin(event)">
              <input id="login-username" name="username" type="text" placeholder="아이디 또는 이메일 (예: admin)" />
              <input id="login-password" name="password" type="password" placeholder="비밀번호 (아무 값이나 입력)" />
              <button id="login-submit-btn" type="submit">로그인</button>
            </form>
          </div>
        `;
      } else {
        const items = JSON.parse(sessionStorage.getItem('portal_tickets') || '["보안 감사 로그 점검"]');
        const listHtml = items.map((t, idx) =>
          `<li class="ticket-item"><span>${t}</span><button onclick="removeTicket(${idx})" style="background:#ef4444;padding:5px 10px;font-size:12px;">삭제</button></li>`
        ).join('');
        root.innerHTML = `
          <div class="box" data-authenticated="true">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
              <span>👤 접속 계정: <strong>${loggedInUser}</strong></span>
              <button onclick="doLogout()" style="background:#475569;padding:6px 12px;font-size:12px;">로그아웃</button>
            </div>
            <input id="ticket-input" type="text" placeholder="할 일을 입력하세요" />
            <button id="add-ticket-btn" onclick="addTicket()">추가</button>
          </div>
          <div class="box">
            <h3>내 보안 업무 티켓 목록</h3>
            <ul id="ticket-list" style="list-style:none;padding:0;">${listHtml}</ul>
          </div>
        `;
      }
    }
    function doLogin(e) {
      e.preventDefault();
      const u = document.getElementById('login-username').value.trim();
      const p = document.getElementById('login-password').value.trim();
      if (!u || !p) return;
      sessionStorage.setItem('portal_user', u);
      renderApp();
    }
    function doLogout() {
      sessionStorage.removeItem('portal_user');
      renderApp();
    }
    function addTicket() {
      const inp = document.getElementById('ticket-input');
      const v = inp.value.trim();
      if (!v) return;
      const items = JSON.parse(sessionStorage.getItem('portal_tickets') || '["보안 감사 로그 점검"]');
      items.push(v);
      sessionStorage.setItem('portal_tickets', JSON.stringify(items));
      renderApp();
    }
    function removeTicket(idx) {
      const items = JSON.parse(sessionStorage.getItem('portal_tickets') || '["보안 감사 로그 점검"]');
      items.splice(idx, 1);
      sessionStorage.setItem('portal_tickets', JSON.stringify(items));
      renderApp();
    }
    renderApp();
  </script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/demo-blog")
def demo_blog():
    return DEMO_BLOG_HTML


@app.route("/demo-protected")
def demo_protected():
    return DEMO_PROTECTED_HTML


# =====================================================================
# API Routes
# =====================================================================

@app.route("/api/skills", methods=["GET"])
def api_list_skills():
    url_query = (request.args.get("url") or "").strip()
    skills = orchestrator.registry.list_all_skills()
    matched_skill = None
    norm_query = ""
    if url_query:
        try:
            matched_skill, norm_query = orchestrator.registry.resolve_skill_by_url(url_query)
        except Exception:
            norm_query = url_query

    return jsonify(
        {
            "ok": True,
            "total": len(skills),
            "skills": skills,
            "query_url": url_query,
            "normalized_query_url": norm_query,
            "matched_skill_for_query_url": matched_skill,
            "models": MODEL_ROLES,
            "max_retry_count": MAX_ROOT_RETRY_COUNT,
        }
    )


@app.route("/api/analyze", methods=["POST"])
def api_analyze_url():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "URL을 입력해주세요."}), 400
    force_resynthesize = bool(data.get("force_resynthesize", False))

    result = orchestrator.analyze_and_register_url_skill(
        target_url=url,
        browser_session=browser_session,
        force_resynthesize=force_resynthesize,
    )
    return jsonify(
        {
            "ok": True,
            "status": "READY",
            "reused": result.get("skill_reused", False),
            "normalized_url": result.get("skill_package", {}).get("normalized_url", ""),
            "skill": result.get("skill_package"),
            **result,
            "all_skills": orchestrator.registry.list_all_skills(),
        }
    )


@app.route("/api/execute", methods=["POST"])
def api_execute_goal():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    user_goal = (data.get("goal") or data.get("prompt") or "").strip()
    user_provided_input = data.get("user_provided_input") or data.get("user_inputs")

    if not url:
        return jsonify({"ok": False, "error": "URL을 입력해주세요."}), 400
    if not user_goal:
        return jsonify({"ok": False, "error": "수행할 작업 명령(goal)을 입력해주세요."}), 400

    result = orchestrator.run_task(
        target_url=url,
        user_prompt=user_goal,
        browser_session=browser_session,
        user_inputs=user_provided_input,
    )
    return jsonify(
        {
            "ok": result.get("status") == "SUCCESS",
            "final_message": result.get("summary", ""),
            **result,
            "all_skills": orchestrator.registry.list_all_skills(),
        }
    )


@app.route("/api/call-tool", methods=["POST"])
def api_call_tool():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    tool_name = (data.get("tool_name") or "").strip()
    arguments = data.get("arguments") or {}

    if not url or not tool_name:
        return jsonify({"ok": False, "error": "url과 tool_name이 모두 필요합니다."}), 400

    tools, skill_pkg, _ = orchestrator.registry.get_tools_strictly_for_url(url)
    if not tools or not skill_pkg:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"해당 URL({URLMatcher.normalize_url(url)})에 등록된 스킬이 없습니다.",
                }
            ),
            404,
        )

    tool_def = next((t for t in tools if t.get("name") == tool_name), None)
    if not tool_def:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"툴 '{tool_name}'은(는) URL({URLMatcher.normalize_url(url)})의 스킬에 존재하지 않습니다.",
                }
            ),
            404,
        )

    # Ensure browser is on the expected URL
    if URLMatcher.normalize_url(browser_session.current_url) != skill_pkg["normalized_url"]:
        browser_session.navigate(url)

    exec_out = browser_session.execute_tool(tool_def, arguments)
    step_record = {
        "tool_name": tool_name,
        "display_name": tool_def.get("display_name", tool_name),
        "args": arguments,
        "action_result": exec_out["action_result"],
        "verification": exec_out["verification"],
    }
    verification = orchestrator.verify_execution(
        user_prompt=f"직접 스킬 툴 호출: {tool_name}({arguments})",
        before_snapshot=exec_out["before_snapshot"],
        after_snapshot=exec_out["after_snapshot"],
        steps=[step_record],
    )
    return jsonify(
        {
            "ok": verification.get("success", False),
            "tool_name": tool_name,
            "arguments": arguments,
            "execution": exec_out,
            "verification": verification,
        }
    )


@app.route("/api/delete-skill", methods=["POST"])
def api_delete_skill():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    deleted = orchestrator.registry.delete_skill_by_url(url)
    return jsonify(
        {
            "ok": deleted,
            "deleted_url": URLMatcher.normalize_url(url) if url else "",
            "all_skills": orchestrator.registry.list_all_skills(),
        }
    )


@app.route("/api/state", methods=["GET"])
def api_state():
    dom_state = browser_session.inspect_page()
    return jsonify({"ok": True, "state": dom_state})


if __name__ == "__main__":
    print(f"🚀 Gemini URL-Skill Web Agent Studio starting on http://127.0.0.1:5055")
    app.run(host="127.0.0.1", port=5055, debug=False, threaded=True)
