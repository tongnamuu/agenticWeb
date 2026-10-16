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
"""

from flask import Blueprint, jsonify, render_template, request, g, redirect, url_for, current_app
from werkzeug.local import LocalProxy

browser_session = LocalProxy(lambda: g.services.browser)
orchestrator = LocalProxy(lambda: g.services.orchestrator)
saved_workflows = LocalProxy(lambda: g.services.workflows)
from agentic_web.config import MAX_ROOT_RETRY_COUNT, MODEL_ROLES
from agentic_web.engine.skills.registry import URLMatcher

routes = Blueprint('studio', __name__)


@routes.before_request
def serialize_browser_requests():
    if not g.user:
        return
    g.services = current_app.extensions['user_services'].for_user(g.user['id'])
    browser_paths = { '/api/execute', '/api/call-tool', '/api/state', '/api/delete-skill', '/api/rename-skill'}
    if request.path in browser_paths or (request.path.startswith('/api/workflows/') and request.path.endswith('/run')):
        if not g.services.lock.acquire(blocking=False):
            return jsonify(ok=False, error='다른 브라우저 작업이 실행 중입니다. 완료 후 다시 시도하세요.'), 409
        g.browser_request_locked = True


@routes.teardown_request
def release_browser_request_lock(error):
    if getattr(g, 'browser_request_locked', False):
        g.browser_request_locked = False
        g.services.lock.release()



@routes.route('/api/workflows', methods=['GET', 'POST'])
def api_workflows():
    try:
        if request.method == 'GET':
            return jsonify(ok=True, workflows=saved_workflows.list())
        return jsonify(ok=True, workflow=saved_workflows.save(request.get_json() or {}))
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify(ok=False, error=str(exc)), 400


@routes.route('/api/workflows/<identifier>/run', methods=['POST'])
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

@routes.route("/")
def index():
    if not g.user:
        return redirect(url_for("auth.login"))
    return render_template("index.html", user=g.user)


# =====================================================================
# API Routes
# =====================================================================

@routes.route("/api/skills", methods=["GET"])
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


@routes.route("/api/analyze", methods=["POST"])
def api_analyze_url():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "URL을 입력해주세요."}), 400
    try:
        url = URLMatcher.normalize_url(url)
        job = current_app.extensions['analysis_jobs'].start(
            g.user['id'], g.services, url, bool(data.get('force_resynthesize', False)))
        return jsonify(ok=True, job=job), 202
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400


@routes.route('/api/analysis-jobs/<job_id>')
def api_analysis_status(job_id):
    job=current_app.extensions['analysis_jobs'].get(g.user['id'],job_id)
    if job is None:
        return jsonify(ok=False,error='분석 요청을 찾을 수 없습니다.'),404
    return jsonify(ok=True,job=job)


@routes.route('/workflows')
def workflows_page():
    if not g.user:
        return redirect(url_for('auth.login'))
    return render_template('workflows.html',user=g.user)


@routes.route("/api/execute", methods=["POST"])
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


@routes.route("/api/call-tool", methods=["POST"])
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

    required = tool_def.get('parameters', {}).get('required', [])
    missing = [key for key in required if not isinstance(arguments, dict) or not str(arguments.get(key, '')).strip()]
    if missing:
        return jsonify(ok=False, error='필수 입력값이 없습니다: ' + ', '.join(missing)), 400

    attempts = []
    exec_out = None
    for attempt in range(2):
        # A technical verification failure retries observation, never blindly repeats a mutation.
        if exec_out is None or verification.get('failure_code') == 'GOAL_NOT_MET':
            exec_out = browser_session.execute_tool(tool_def, arguments)
        step_record = {
            'tool_name': tool_name, 'display_name': tool_def.get('display_name', tool_name),
            'args': arguments, 'operations': tool_def.get('steps', []),
            'action_result': exec_out['action_result'], 'verification': exec_out['verification'],
        }
        verification = orchestrator.verify_execution(
            user_prompt=f"{tool_def.get('description', tool_name)}. 요청 인자: {arguments}",
            before_snapshot=exec_out['before_snapshot'], after_snapshot=exec_out['after_snapshot'],
            steps=[step_record], browser_session=browser_session,
        )
        attempts.append({'attempt_number': attempt+1, 'is_retry': bool(attempt),
                         'steps': [step_record], 'verification': verification})
        if verification.get('success') or verification.get('status') == 'NEEDS_USER_INPUT':
            break
        if not verification.get('failure_code', '').startswith('VERIFICATION_') and verification.get('failure_code') != 'GOAL_NOT_MET':
            break
    return jsonify(ok=verification.get('success', False), tool_name=tool_name,
                   arguments=arguments, execution=exec_out, verification=verification,
                   attempts=attempts, retry_count=len(attempts)-1)


@routes.route('/api/rename-skill', methods=['POST'])
def api_rename_skill():
    data = request.get_json(silent=True) or {}
    try:
        skill = orchestrator.registry.rename_skill(data.get('url', ''), data.get('name', ''))
        return jsonify(ok=True, skill=skill)
    except KeyError:
        return jsonify(ok=False, error='등록된 URL이 없습니다.'), 404
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400


@routes.route("/api/delete-skill", methods=["POST"])
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


@routes.route("/api/state", methods=["GET"])
def api_state():
    dom_state = browser_session.inspect_page()
    return jsonify({"ok": True, "state": dom_state})
