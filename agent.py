"""
Google ADK (Agent Development Kit) Entrypoint & Singleton Runtime Export.

Exposes:
- `browser_session`: Singleton `BrowserSession` (Chrome CDP controller)
- `orchestrator`: Singleton `RootOrchestratorAgent`
- `root_agent`: Google ADK `LlmAgent` (when `google-adk` is installed) with dynamically attached per-URL tools
"""

import os
from typing import Any, Dict, Optional

from agents import RootOrchestratorAgent
from browser_controller import BrowserSession
from config import MODEL_ROLES
from skill_registry import URLMatcher

# Singleton BrowserSession & RootOrchestratorAgent
HEADLESS_DEFAULT = os.environ.get("HEADLESS_BROWSER", "true").lower() in ("1", "true", "yes")
browser_session = BrowserSession(headless=HEADLESS_DEFAULT)
orchestrator = RootOrchestratorAgent()


def execute_web_goal_with_url_skill(
    url: str,
    user_goal: str,
    username: str = "",
    password: str = "",
    browser_login_completed: bool = False,
) -> Dict[str, Any]:
    """
    웹사이트 URL과 사용자의 목표(user_goal)를 입력받아:
    1. 정규화된 URL 기준으로 등록된 스킬이 있는지 엄격히 판별하고(없으면 페이지를 분석해 스킬 생성 및 레지스트리 등록),
    2. 로그인이나 추가 사용자 입력이 필요하면 NEEDS_USER_INPUT 상태를 반환해 유저에게 요청하며,
    3. 해당 URL 전용 스킬/툴만 실행기로 실행한 뒤,
    4. 루트 에이전트가 실행 전후 DOM 상태를 검증하여 실패 시 자동으로 1회 재시도(스킬 복구 + 재실행) 루프를 수행합니다.
    """
    user_inputs: Optional[Dict[str, Any]] = None
    if username or password or browser_login_completed:
        user_inputs = {
            "username": username,
            "password": password,
            "browser_login_completed": browser_login_completed,
        }

    result = orchestrator.run_task(
        target_url=url,
        user_prompt=user_goal,
        browser_session=browser_session,
        user_inputs=user_inputs,
    )
    _sync_dynamic_adk_tools(url)
    return result


def analyze_url_and_extract_skill(
    url: str,
    force_resynthesize: bool = False,
) -> Dict[str, Any]:
    """
    특정 URL을 방문하여 DOM 구조를 분석하고, 재사용 가능한 URL 전용 스킬/툴을 추출해 레지스트리에 추가합니다.
    이미 동일 URL에 대한 스킬이 존재하고 force_resynthesize=False 이면 기존 스킬을 즉시 재사용합니다.
    """
    res = orchestrator.analyze_and_register_url_skill(
        target_url=url,
        browser_session=browser_session,
        force_resynthesize=force_resynthesize,
    )
    _sync_dynamic_adk_tools(url)
    return res


def list_registered_url_skills() -> Dict[str, Any]:
    """현재 스킬 레지스트리에 저장된 모든 URL별 스킬과 툴 목록을 반환합니다."""
    skills = orchestrator.registry.list_all_skills()
    return {
        "total_url_skills": len(skills),
        "skills": [
            {
                "skill_id": s.get("skill_id"),
                "skill_name": s.get("skill_name"),
                "normalized_url": s.get("normalized_url"),
                "version": s.get("version"),
                "tools": [t.get("name") for t in s.get("tools", [])],
            }
            for s in skills
        ],
    }


def check_if_url_has_skill(url: str) -> Dict[str, Any]:
    """입력한 URL에 매칭되는 전용 스킬이 레지스트리에 존재하는지 URL로 정확히 판별합니다."""
    skill, norm_url = orchestrator.registry.resolve_skill_by_url(url)
    return {
        "input_url": url,
        "normalized_url": norm_url,
        "has_registered_skill": skill is not None,
        "skill_id": skill.get("skill_id") if skill else None,
        "tool_names": [t.get("name") for t in skill.get("tools", [])] if skill else [],
    }


root_agent = None
try:
    from google.adk.agents import LlmAgent  # type: ignore

    root_agent = LlmAgent(
        name="url_skill_web_root_agent",
        model=str(MODEL_ROLES["root_verifier"]["primary"]),
        description=(
            "웹사이트 URL별 재사용 스킬을 자동 생성·등록하고, URL 일치 여부를 엄격히 판별해 "
            "브라우저 작업을 수행한 뒤 성공/실패 검증 및 1회 자동 재시도, 로그인 등 유저 입력(HITL)을 처리하는 루트 에이전트"
        ),
        instruction=f"""당신은 URL 기반 웹 브라우저 제어 및 스킬 합성 루트 에이전트입니다.
타겟 URL은 매 요청에서 사용자가 지정합니다.

핵심 원칙:
1. 유저가 특정 URL과 원하는 작업을 요청하면 `execute_web_goal_with_url_skill` 도구를 호출하세요.
2. 스킬 호출 여부는 반드시 정규화된 URL 일치 여부(`check_if_url_has_skill`)로만 판별합니다. 다른 URL의 스킬을 절대 교차 호출하지 마세요.
3. 실행 결과 `status == "NEEDS_USER_INPUT"` 이면(예: 로그인이 필요한 경우 또는 필수 입력값이 누락된 경우), 유저에게 로그인 또는 추가 정보를 명확히 요청하세요.
4. 실행 후에는 루트 검증기(Verifier)의 판정(`root_verification.status`)과 1회 자동 재시도(`retry_count`) 결과를 유저에게 투명하게 보고하세요.
""",
        tools=[
            execute_web_goal_with_url_skill,
            analyze_url_and_extract_skill,
            list_registered_url_skills,
            check_if_url_has_skill,
        ],
    )
except Exception:
    root_agent = None


def _sync_dynamic_adk_tools(url: str) -> None:
    """When a new URL skill is created, dynamically append its URL-guarded callable tools to ADK root_agent."""
    if root_agent is None:
        return
    try:
        existing_names = {getattr(fn, "__name__", "") for fn in root_agent.tools}
        callable_tools = orchestrator.registry.build_adk_callable_tools_for_url(
            target_url=url,
            browser_session=browser_session,
        )
        for fn in callable_tools:
            if getattr(fn, "__name__", "") not in existing_names:
                root_agent.tools.append(fn)
    except Exception:
        pass
