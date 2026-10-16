"""
Interactive CLI Runner for the URL-Gated Web Skill Multi-Agent System.

Supports:
1. Interactive URL & Goal entry
2. Automatic Per-URL Skill synthesis & Registry lookup
3. Root Agent Post-Execution Verification + 1x Automatic Retry Loop
4. Interactive Human-in-the-Loop (HITL) prompts in terminal when:
   - Site login is required (`LOGIN_REQUIRED`: allows direct browser login or CLI ID/PW entry)
   - Required tool parameters are missing (`MISSING_PARAMETERS`)
"""

import argparse
import getpass
import json
import sys
from typing import Any, Dict, Optional

from agent import browser_session, orchestrator
from config import TARGET_URL


def print_banner() -> None:
    print("=" * 78)
    print(" 🌐 Gemini URL-Skill Web Agent System (Per-URL Skill Registry + Root Verifier)")
    print("=" * 78)


def handle_hitl_prompt(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    When `result['status'] == 'NEEDS_USER_INPUT'`, prompt the user in the terminal
    and return `user_inputs` dict to resume execution.
    """
    req = result.get("user_input_request") or {}
    input_type = req.get("input_type", "LOGIN_REQUIRED")
    title = req.get("title", "추가 입력 요청")
    prompt_msg = req.get("message") or result.get("summary") or "추가 입력이 필요합니다."

    print("\n" + "!" * 78)
    print(f" [{title} - {input_type}]")
    print(f" 안내: {prompt_msg}")
    print("!" * 78)

    if input_type == "LOGIN_REQUIRED":
        print("\n로그인 방식을 선택하세요:")
        print("  [1] 터미널에서 아이디/비밀번호 입력하여 자동 로그인 수행")
        print("  [2] 열린 Chrome 브라우저 창에서 직접 로그인 완료 후 계속하기")
        print("  [q] 작업 취소")
        choice = input("선택 (1/2/q, 기본값 1): ").strip().lower() or "1"
        if choice == "q":
            return None
        if choice == "2":
            input(">> 브라우저에서 로그인을 마친 후 Enter 키를 누르세요...")
            return {"browser_login_completed": True}
        username = input("아이디(또는 이메일): ").strip()
        password = getpass.getpass("비밀번호: ").strip()
        return {
            "username": username,
            "password": password,
            "browser_login_completed": False,
        }

    # MISSING_PARAMETERS
    fields = req.get("fields") or [{"name": "text", "label": "추가 입력값"}]
    extra_params: Dict[str, Any] = {}
    print("\n누락된 파라미터를 입력해주세요:")
    for f in fields:
        f_name = f.get("name", "text")
        f_label = f.get("label", f_name)
        val = input(f" - {f_label} ({f_name}): ").strip()
        if val:
            extra_params[f_name] = val
    if not extra_params:
        return None
    return extra_params


def print_execution_report(result: Dict[str, Any]) -> None:
    skill_pkg = result.get("skill_package") or {}
    print("\n" + "-" * 78)
    print(f" 📌 최종 상태 (Status)     : {result.get('status')}")
    print(f" 🔗 정규화 URL             : {result.get('normalized_url')}")
    print(f" 🧩 선택된 스킬 ID         : {skill_pkg.get('skill_id')} (기존 스킬 재사용: {result.get('skill_reused')})")
    print(f" 🔄 루트 재시도 횟수       : {result.get('retry_count')} / {result.get('max_retry_count')}")
    print(f" 💬 루트 에이전트 요약     :\n{result.get('summary')}")
    print("-" * 78 + "\n")


def run_single_goal(url: str, goal: str) -> Dict[str, Any]:
    print(f"\n▶ 실행 시작 | URL: {url} | 목표: {goal}")
    result = orchestrator.run_task(
        target_url=url,
        user_prompt=goal,
        browser_session=browser_session,
    )

    # Loop while agent requests HITL user input (e.g., login or missing parameter)
    while result.get("status") == "NEEDS_USER_INPUT":
        user_inputs = handle_hitl_prompt(result)
        if not user_inputs:
            print("사용자가 입력을 취소했습니다.")
            break
        print("\n▶ 유저 입력을 반영하여 작업을 재개합니다...")
        result = orchestrator.run_task(
            target_url=url,
            user_prompt=goal,
            browser_session=browser_session,
            user_inputs=user_inputs,
        )

    print_execution_report(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini URL-Skill Web Agent CLI")
    parser.add_argument("--url", type=str, default="", help="Target website URL")
    parser.add_argument("--goal", type=str, default="", help="Natural language goal to execute")
    parser.add_argument("--headed", action="store_true", help="Open visible Chrome window")
    args = parser.parse_args()

    if args.headed:
        browser_session.start(headless=False)

    print_banner()

    if args.url and args.goal:
        run_single_goal(args.url, args.goal)
        return

    current_url = args.url or TARGET_URL
    while True:
        try:
            print(f"\n현재 등록된 URL 스킬 수: {len(orchestrator.registry.list_all_skills())}")
            url_in = input(f"대상 URL 입력 (Enter 시 기본값 '{current_url}', 종료 'q'): ").strip()
            if url_in.lower() == "q":
                break
            if url_in:
                current_url = url_in

            goal_in = input("수행할 작업(Goal) 입력 (스킬 목록 보기 'skills', 종료 'q'): ").strip()
            if goal_in.lower() == "q":
                break
            if goal_in.lower() == "skills":
                print(json.dumps(orchestrator.registry.list_all_skills(), ensure_ascii=False, indent=2))
                continue
            if not goal_in:
                continue

            run_single_goal(current_url, goal_in)
        except (KeyboardInterrupt, EOFError):
            print("\n종료합니다.")
            sys.exit(0)


if __name__ == "__main__":
    main()
