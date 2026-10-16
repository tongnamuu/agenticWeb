import json
import re
import time
from urllib.parse import urljoin, urlparse
from typing import Any, Dict, List, Optional, Tuple

import requests

from agentic_web.config import MAX_ROOT_RETRY_COUNT, MODEL_ROLES, GEMINI_REQUEST_TIMEOUT, get_api_key, get_model_candidates
from agentic_web.engine.skills.registry import SkillRegistry, URLMatcher, is_user_tool
from agentic_web.engine.browser.auth import requires_login
from agentic_web.engine.agents.result_verifier import ResultVerificationAgent


class GeminiClient:
    """
    Role-aware Gemini API client supporting optimal model selection per agent role
    (`root_verifier`, `skill_synthesizer`, `skill_executor`) with automatic fallback.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = get_api_key(api_key or "")

    def set_api_key(self, api_key: str) -> None:
        self.api_key = api_key.strip()

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def generate(
        self,
        role: str,
        contents: List[Dict[str, Any]],
        system_instruction: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        json_mode: bool = False,
        temperature: float = 0.1,
    ) -> Dict[str, Any]:
        self.api_key = get_api_key(self.api_key)
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY not configured")

        candidates = get_model_candidates(role)
        last_err = None
        deadline = time.monotonic() + GEMINI_REQUEST_TIMEOUT

        for model in candidates:
            endpoints = [
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                f"https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent",
            ]
            payload: Dict[str, Any] = {"contents": contents}
            if system_instruction:
                payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
            if tools:
                payload["tools"] = tools
            gen_cfg: Dict[str, Any] = (
                {'thinkingConfig': {'thinkingLevel': MODEL_ROLES.get(role, {}).get('thinking_level', 'high')},
                 'maxOutputTokens': 65536}
                if model.startswith(('gemini-3', 'gemini-4')) else {'temperature': temperature})
            if json_mode:
                gen_cfg["responseMimeType"] = "application/json"
            payload["generationConfig"] = gen_cfg

            for url in endpoints:
                if time.monotonic() >= deadline:
                    break
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('Gemini 응답 대기 시간이 초과됐습니다.')
                    resp = requests.post(url, headers={"x-goog-api-key": self.api_key}, json=payload, timeout=(5, remaining))
                    if resp.status_code == 200:
                        data = resp.json()
                        data["_model_used"] = model
                        data["_role"] = role
                        return data
                    last_err = f"{model} HTTP {resp.status_code}"
                    if resp.status_code not in (401, 403):
                        break
                except Exception as e:
                    last_err = f"{model} error: {type(e).__name__}"

        raise RuntimeError(f"Gemini call failed for role '{role}': {last_err}")


class SkillSynthesizerAgent:
    """
    [Agent 1] Page Analyzer & Per-URL Skill Synthesizer Agent
    Model Role: `skill_synthesizer` (Primary: `gemini-4-argon`)
    Analyzes a website's DOM snapshot and synthesizes reusable per-URL Skill Tools.
    Also repairs/updates a URL's Skill during the Root Agent's 1x retry loop if Attempt 1 fails.
    """

    def __init__(self, gemini_client: GeminiClient):
        self.client = gemini_client

    def synthesize_tools(
        self,
        snapshot: Dict[str, Any],
        failure_feedback: Optional[str] = None,
        existing_tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """
        Synthesizes reusable browser tools for the given page snapshot.
        If `failure_feedback` is provided (during Root Agent retry loop), repairs selectors/steps.
        """
        heuristic_tools = self._heuristic_dom_to_tools(snapshot)

        if self.client.has_key:
            try:
                llm_tools, model_used = self._synthesize_with_gemini(snapshot, failure_feedback, existing_tools)
                if llm_tools:
                    merged = self._merge_and_validate_tools(llm_tools, heuristic_tools)
                    return merged, f"Gemini ({model_used})"
            except Exception as e:
                print(f"[SkillSynthesizerAgent] Gemini fallback to DOM analyzer: {e}")

        return heuristic_tools, "DOM Structural Synthesizer (Deterministic + Heuristic)"

    def _synthesize_with_gemini(
        self,
        snapshot: Dict[str, Any],
        failure_feedback: Optional[str] = None,
        existing_tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        system_prompt = """당신은 특정 URL의 웹페이지 DOM 구조를 분석하여 이후에도 계속 재사용할 수 있는 'URL 전용 에이전트 스킬(Function Tools)'을 생성하는 Skill Synthesizer Agent입니다.

각 도구는 브라우저 실행기가 수행할 수 있는 기본 스텝(`steps` 배열)으로 정의됩니다.
사용 가능한 `op`:
1. "read_state": 현재 페이지의 목록, 게시글, 입력창, 상태 정보를 읽습니다.
   예: { "op": "read_state" }
2. "fill": 입력창(input, textarea, contenteditable)에 값을 입력합니다. 파라미터 치환은 "{{파라미터명}}" 형식을 사용합니다.
   예: { "op": "fill", "selector": "#todoInput", "value": "{{text}}" }
3. "select": <select> 드롭다운에서 옵션을 선택합니다.
   예: { "op": "select", "selector": "#category", "value": "{{category}}" }
4. "submit": 폼을 제출하거나 입력창에서 Enter를 누릅니다.
   예: { "op": "submit", "selector": "input.getinput", "form_selector": "form.todoform" }
5. "click": 특정 버튼을 클릭합니다.
   예: { "op": "click", "selector": "button.publish-btn", "button_text": "발행" }
6. "click_item_button": 목록(ul, ol, tbody, article 등)에서 특정 텍스트(`item_text`)를 가진 항목 안의 버튼(`button_text`)을 클릭합니다.
   예: { "op": "click_item_button", "list_selector": "ul.todolist", "item_text": "{{text}}", "button_text": "✅" }

설계 규칙:
- 여러 입력 필드를 채운 후 제출/클릭해야 하는 기능(예: 로그인, 게시글 작성, 할 일 추가)은 하나의 도구 안에 순서대로 `fill`/`select` 스텝과 마지막 `submit`/`click` 스텝을 묶으세요.
- 로그인이 필요한 동작인지 여부를 `"requires_auth": true/false`로 명시하세요.
- 단순 링크 이동, 로그인, 회원가입은 사용자 스킬이 아닙니다. navigate_to_*, open_page_*, login 도구를 생성하지 마세요. 작성·추가·완료 같은 실제 작업만 생성하세요.
- 현재 페이지 상태를 조회하는 `get_page_state` 도구를 항상 포함하세요.
- existingTools는 기존 등록 기능입니다. 같은 기능은 기존 name과 파라미터 이름을 유지해 갱신하고, 빠진 기능은 새 도구로 추가하세요. 현재 화면에 없다는 이유로 기존 기능을 삭제하지 마세요.
- `dynamicHints`(스크립트 코드)에 동적으로 생성되는 목록 버튼(예: ✅, ❌, ◀, 삭제, 완료 등)이 있으면 `click_item_button` 도구를 반드시 생성하세요.

응답 JSON 스키마(배열):
[
  {
    "name": "snake_case_tool_name",
    "display_name": "한국어 도구 이름",
    "description": "이 도구가 수행하는 작업 설명",
    "requires_auth": false,
    "parameters": {
      "type": "object",
      "properties": {
        "param_name": { "type": "string", "description": "파라미터 설명" }
      },
      "required": ["param_name"]
    },
    "steps": [
      { "op": "fill", "selector": "...", "value": "{{param_name}}" },
      { "op": "submit", "selector": "...", "form_selector": "..." }
    ],
    "verification": {
      "type": "dom_change",
      "description": "실행 후 기대되는 DOM 상태 변화 설명"
    }
  }
]"""

        compact_snapshot = {
            "url": snapshot.get("url"),
            "title": snapshot.get("title"),
            "authState": snapshot.get("authState"),
            "forms": snapshot.get("forms", []),
            "inputs": snapshot.get("inputs", []),
            "buttons": snapshot.get("buttons", []),
            "links": snapshot.get("links", []),
            "lists": snapshot.get("lists", []),
            "headings": snapshot.get("headings", []),
            "dynamicHints": snapshot.get("dynamicHints", []),
            "existingTools": existing_tools or [],
        }

        user_msg = (
            "다음 웹페이지 DOM 스냅샷을 분석하여 이 URL에서 재사용 가능한 에이전트 스킬 도구(JSON 배열)를 생성하세요:\n"
            + json.dumps(compact_snapshot, ensure_ascii=False, indent=2)
        )
        if failure_feedback:
            user_msg += (
                "\n\n[중요 - 직전 실행 실패 피드백 (재시도 루프 보수 요청)]\n"
                f"{failure_feedback}\n"
                "위 실패 원인을 해결할 수 있도록 셀렉터(selector)와 스텝(steps) 구성을 정확히 보수하여 도구를 다시 생성하세요."
            )

        res = self.client.generate(
            role="skill_synthesizer",
            contents=[{"role": "user", "parts": [{"text": user_msg}]}],
            system_instruction=system_prompt,
            json_mode=True,
            temperature=0.1,
        )
        raw_text = (
            res.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "[]")
        )
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict) and "tools" in parsed:
            parsed = parsed["tools"]
        if not isinstance(parsed, list):
            parsed = []
        return parsed, res.get("_model_used", "gemini-4-argon")

    @staticmethod
    def _merge_and_validate_tools(
        llm_tools: List[Dict[str, Any]],
        heuristic_tools: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        valid_tools: List[Dict[str, Any]] = []
        seen_names = set()
        for t in llm_tools:
            if not isinstance(t, dict) or not t.get("name") or not t.get("steps"):
                continue
            name = re.sub(r"[^a-zA-Z0-9_]", "_", str(t["name"])).lower()
            t["name"] = name
            if "parameters" not in t or not isinstance(t["parameters"], dict):
                t["parameters"] = {"type": "object", "properties": {}, "required": []}
            valid_tools.append(t)
            seen_names.add(name)

        # Ensure essential heuristic tools (e.g. dynamic list item buttons or get_page_state) are not missed
        for ht in heuristic_tools:
            if ht["name"] not in seen_names:
                valid_tools.append(ht)
                seen_names.add(ht["name"])

        return [tool for tool in valid_tools if is_user_tool(tool)]

    def _heuristic_dom_to_tools(self, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        inputs = [i for i in snapshot.get("inputs", []) if i.get("visible", True)]
        buttons = snapshot.get("buttons", [])
        lists = snapshot.get("lists", [])
        dynamic_hints = snapshot.get("dynamicHints", [])
        auth_state = snapshot.get("authState", {})

        # 1. Always provide page state reader
        tools.append(
            {
                "name": "get_page_state",
                "display_name": "현재 페이지 데이터 조회",
                "description": "현재 URL 페이지의 목록, 게시글, 입력창 및 로그인 상태를 조회합니다.",
                "requires_auth": False,
                "parameters": {"type": "object", "properties": {}, "required": []},
                "steps": [{"op": "read_state"}],
                "verification": {"type": "read_only", "description": "페이지 상태 읽기 확인"},
            }
        )

        # 2. Group inputs by inline button or form
        grouped_inputs: Dict[str, List[Dict[str, Any]]] = {}
        for inp in inputs:
            if inp.get("inline_button_selector"):
                key = f"__inline__:{inp['inline_button_selector']}"
            else:
                key = inp.get("form_selector") or "__standalone__"
            grouped_inputs.setdefault(key, []).append(inp)

        def infer_param_name(inp: Dict[str, Any], idx: int) -> Tuple[str, str]:
            raw = " ".join(
                [
                    inp.get("name", ""),
                    inp.get("label", ""),
                    inp.get("placeholder", ""),
                    inp.get("selector", ""),
                ]
            ).lower()
            if inp.get("type") == "password" or any(k in raw for k in ["pass", "비밀번호", "암호"]):
                return "password", inp.get("label") or inp.get("placeholder") or "비밀번호"
            if any(k in raw for k in ["user", "id", "email", "아이디", "이메일", "계정", "로그인"]):
                if "todo" not in raw and "post" not in raw and "title" not in raw:
                    return "username", inp.get("label") or inp.get("placeholder") or "사용자 아이디/이메일"
            if any(k in raw for k in ["title", "제목", "subject"]):
                return "title", inp.get("label") or inp.get("placeholder") or "제목"
            if any(k in raw for k in ["category", "카테고리", "분류", "priority", "중요도"]):
                return "category", inp.get("label") or inp.get("placeholder") or "카테고리/분류"
            if any(k in raw for k in ["content", "body", "본문", "내용", "글", "post", "editor", "memo", "메모"]) or inp.get(
                "tag"
            ) in ("textarea", "contenteditable"):
                return "content", inp.get("label") or inp.get("placeholder") or "본문/내용"
            if any(k in raw for k in ["todo", "할일", "할 일", "task", "item"]):
                return "text", inp.get("label") or inp.get("placeholder") or "할 일 내용"
            if any(k in raw for k in ["name", "이름", "작성자"]):
                return "username", inp.get("label") or inp.get("placeholder") or "이름"
            if any(k in raw for k in ["search", "query", "검색"]):
                return "query", inp.get("label") or inp.get("placeholder") or "검색어"
            clean_name = re.sub(r"[^a-z0-9_]", "", (inp.get("name") or "").lower())
            if clean_name:
                return clean_name, inp.get("label") or inp.get("placeholder") or clean_name
            return (f"value_{idx}" if idx > 0 else "text"), (
                inp.get("label") or inp.get("placeholder") or "입력 텍스트"
            )

        for form_key, form_inputs in grouped_inputs.items():
            is_inline_group = form_key.startswith("__inline__:")
            has_multiline = any(i.get("tag") in ("textarea", "contenteditable") for i in form_inputs)
            has_password = any(i.get("type") == "password" for i in form_inputs)
            if form_key == "__standalone__" and not has_multiline and not has_password and len(form_inputs) > 1:
                batches = [[i] for i in form_inputs]
            else:
                batches = [form_inputs]

            for batch in batches:
                props: Dict[str, Any] = {}
                required: List[str] = []
                steps: List[Dict[str, Any]] = []

                for idx, inp in enumerate(batch):
                    if is_inline_group and inp.get("tag") == "select" and len(batch) > 1:
                        continue
                    p_name, p_desc = infer_param_name(inp, idx)
                    if p_name in props:
                        p_name = f"{p_name}_{idx + 1}"
                    props[p_name] = {"type": "string", "description": p_desc}
                    required.append(p_name)
                    op = "select" if inp.get("tag") == "select" else "fill"
                    steps.append(
                        {
                            "op": op,
                            "selector": inp.get("selector", ""),
                            "placeholder": inp.get("placeholder", ""),
                            "value": f"{{{{{p_name}}}}}",
                        }
                    )

                if is_inline_group:
                    steps.append(
                        {
                            "op": "click",
                            "selector": batch[0].get("inline_button_selector") or "",
                            "button_text": batch[0].get("inline_button_text") or "",
                        }
                    )
                elif form_key != "__standalone__":
                    steps.append(
                        {
                            "op": "submit",
                            "selector": batch[-1].get("selector", ""),
                            "form_selector": form_key,
                        }
                    )
                else:
                    submit_btn = next(
                        (
                            b
                            for b in buttons
                            if any(
                                k in (b.get("text") or "").lower()
                                for k in [
                                    "로그인",
                                    "발행",
                                    "등록",
                                    "작성",
                                    "저장",
                                    "제출",
                                    "게시",
                                    "추가",
                                    "생성",
                                    "login",
                                    "publish",
                                    "submit",
                                    "post",
                                    "save",
                                    "create",
                                    "add",
                                ]
                            )
                        ),
                        None,
                    )
                    if submit_btn:
                        steps.append(
                            {
                                "op": "click",
                                "selector": submit_btn.get("selector", ""),
                                "button_text": submit_btn.get("text", ""),
                            }
                        )
                    else:
                        steps.append({"op": "submit", "selector": batch[-1].get("selector", "")})

                param_set = set(props.keys())
                if "password" in param_set:
                    t_name = "login_site"
                    t_disp = "사이트 로그인"
                    t_desc = "사용자 아이디와 비밀번호를 입력하여 사이트에 로그인합니다."
                    req_auth = False
                elif "title" in param_set or "content" in param_set:
                    t_name = "write_post"
                    t_disp = "게시글/문서 작성 및 등록"
                    t_desc = "제목, 분류, 본문 내용을 입력하고 게시글을 등록/발행합니다."
                    req_auth = bool(auth_state.get("login_wall_detected"))
                elif "username" in param_set and len(param_set) == 1:
                    t_name = "set_username"
                    t_disp = "사용자 이름 등록"
                    t_desc = "이름 입력창에 사용자 이름을 입력하고 제출합니다."
                    req_auth = False
                elif "query" in param_set:
                    t_name = "search_items"
                    t_disp = "키워드 검색"
                    t_desc = "검색어를 입력하고 결과를 조회합니다."
                    req_auth = False
                elif len(batch) == 1:
                    ph = batch[0].get("placeholder") or batch[0].get("label") or "할 일"
                    if any(k in ph.lower() for k in ["todo", "할 일", "할일", "업무", "task"]):
                        t_name = "add_todo_item"
                        t_disp = "할 일(To-Do) 추가"
                        t_desc = f"입력창({ph})에 새로운 할 일을 입력하고 목록에 추가합니다."
                    else:
                        t_name = f"add_item_{len(tools)}"
                        t_disp = f"{ph} 입력 및 추가"
                        t_desc = f"'{ph}' 입력란에 값을 입력하고 제출합니다."
                    req_auth = bool(auth_state.get("login_wall_detected"))
                else:
                    t_name = f"submit_form_{len(tools)}"
                    t_disp = "폼 데이터 입력 및 제출"
                    t_desc = f"{', '.join(props.keys())} 값을 입력하고 폼을 제출합니다."
                    req_auth = bool(auth_state.get("login_wall_detected"))

                tools.append(
                    {
                        "name": t_name,
                        "display_name": t_disp,
                        "description": t_desc,
                        "requires_auth": req_auth,
                        "parameters": {
                            "type": "object",
                            "properties": props,
                            "required": required,
                        },
                        "steps": steps,
                        "verification": {
                            "type": "dom_change",
                            "description": f"{t_disp} 후 페이지 목록 또는 상태 변화 확인",
                        },
                    }
                )

        # 3. Collection / List Item Action Tools (from live items + dynamic JS hints)
        discovered_list_buttons: List[Tuple[str, str, str]] = []
        for lst in lists:
            l_sel = lst.get("selector", "ul")
            l_head = lst.get("heading", l_sel)
            sample_btns: List[str] = []
            for item in lst.get("items", []):
                for bt in item.get("buttons", []):
                    if bt not in sample_btns:
                        sample_btns.append(bt)
            for bt in sample_btns:
                discovered_list_buttons.append((l_sel, l_head, bt))

        if dynamic_hints and lists:
            # Bind dynamic button labels to the collection they are actually appended to.
            # Do not guess a list from source order or register unrelated innerHTML text.
            for hint in dynamic_hints:
                code = hint.get('snippet', '')
                bindings = dict(re.findall(r"([A-Za-z_$][\w$]*)\s*=\s*document\.querySelector\(\s*['\"]([^'\"]+)['\"]\s*\)", code))
                for body in re.split(r'function\s+[A-Za-z_$][\w$]*\s*\([^)]*\)\s*\{', code):
                    labels = re.findall(r"([A-Za-z_$][\w$]*)\.(?:innerHTML|innerText|textContent)\s*=\s*['\"]([^'\"]{1,20})['\"]", body)
                    for list_var, item_var in re.findall(r'([A-Za-z_$][\w$]*)\.appendChild\(\s*([A-Za-z_$][\w$]*)\s*\)', body):
                        selector = bindings.get(list_var)
                        collection = next((lst for lst in lists if selector and (
                            lst.get('selector') == selector or
                            re.sub(r'^[a-zA-Z][\w-]*(?=[.#])', '', lst.get('selector', '')) == selector)), None)
                        if not collection:
                            continue
                        selector = collection['selector']
                        for button_var, label in labels:
                            if not re.search(rf"{re.escape(button_var)}\s*=\s*document\.createElement\(\s*['\"]button['\"]", body):
                                continue
                            if not re.search(rf'{re.escape(item_var)}\.appendChild\(\s*{re.escape(button_var)}\s*\)', body):
                                continue
                            entry = (selector, collection.get('heading', selector), label.strip())
                            if entry not in discovered_list_buttons:
                                discovered_list_buttons.append(entry)

        used_names = {t["name"] for t in tools}
        for l_sel, l_head, bt in discovered_list_buttons:
            if bt in ("✅", "완료", "Complete", "Done"):
                t_name = "complete_todo_item"
                t_disp = f"항목 완료 처리 ({bt})"
                t_desc = f"'{l_head}' 목록에서 지정한 텍스트 항목의 '{bt}' 버튼을 클릭해 완료 상태로 변경합니다."
            elif bt in ("❌", "삭제", "Delete", "Remove"):
                suffix = "_finished" if ("finish" in l_head.lower() or "complete" in l_sel.lower()) else ""
                t_name = f"delete_item{suffix}"
                t_disp = f"항목 삭제 ({l_head} {bt})"
                t_desc = f"'{l_head}' 목록에서 지정한 텍스트 항목의 '{bt}' 버튼을 클릭해 삭제합니다."
            elif bt in ("◀", "복구", "되돌리기", "Restore"):
                t_name = "restore_todo_item"
                t_disp = f"항목 복구 ({bt})"
                t_desc = f"'{l_head}' 목록에서 지정한 항목의 '{bt}' 버튼을 클릭해 이전 목록으로 되돌립니다."
            else:
                t_name = f"click_item_{len(tools)}"
                t_disp = f"목록 항목 '{bt}' 클릭 ({l_head})"
                t_desc = f"'{l_head}' 목록에서 지정한 항목의 '{bt}' 버튼을 클릭합니다."

            if t_name in used_names:
                t_name = f"{t_name}_{len(tools)}"
            used_names.add(t_name)

            tools.append(
                {
                    "name": t_name,
                    "display_name": t_disp,
                    "description": t_desc,
                    "requires_auth": False,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": f"대상 항목의 텍스트 또는 제목 (목록: {l_head})",
                            }
                        },
                        "required": ["text"],
                    },
                    "steps": [
                        {
                            "op": "click_item_button",
                            "list_selector": l_sel,
                            "item_text": "{{text}}",
                            "button_text": bt,
                        }
                    ],
                    "verification": {
                        "type": "dom_change",
                        "description": f"'{l_head}' 목록에서 항목 상태 변화 확인",
                    },
                }
            )

        return [tool for tool in tools if is_user_tool(tool)]


class SkillExecutorAgent:
    """
    [Agent 2] URL-Bound Skill Executor Agent
    Model Role: `skill_executor` (Primary: `gemini-4-argon`)
    Executes user instructions using ONLY the tools belonging to the strictly matched URL Skill.
    Also detects if required user inputs or site login are missing.
    """

    def __init__(self, gemini_client: GeminiClient):
        self.client = gemini_client

    def check_user_input_needed(
        self,
        user_prompt: str,
        snapshot: Dict[str, Any],
        url_tools: List[Dict[str, Any]],
        user_inputs: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Pre-Execution Human-in-the-Loop (HITL) Gate:
        1. Checks if the current page is blocked by a Login Wall (`authState.login_wall_detected`)
           and the user has not yet provided credentials or logged in.
        2. Checks if the user is requesting a login or form submission without providing required parameters.
        """
        provided = user_inputs or {}
        auth_state = snapshot.get("authState", {})

        # Case 1: Login Wall detected on the page
        if auth_state.get("login_wall_detected") and not auth_state.get("is_authenticated"):
            has_creds_in_inputs = bool(provided.get("username") and provided.get("password"))
            # Check if credentials were explicitly written inside the prompt
            cred_match = re.search(
                r"(?:아이디|id|계정|user(?:name)?)\s*[:=은는]?\s*['\"]?([a-zA-Z0-9_@.-]+)['\"]?.*?(?:비밀번호|비번|패스워드|password|pw)\s*[:=은는]?\s*['\"]?([^\s'\"]+)['\"]?",
                user_prompt,
                re.IGNORECASE,
            )
            if cred_match and not has_creds_in_inputs:
                provided["username"] = cred_match.group(1).strip()
                provided["password"] = cred_match.group(2).strip()
                has_creds_in_inputs = True

            if not has_creds_in_inputs and not provided.get("browser_login_completed"):
                return {
                    "requires_user_input": True,
                    "input_type": "LOGIN_REQUIRED",
                    "title": "🔐 사이트 로그인이 필요합니다",
                    "message": (
                        f"현재 페이지({snapshot.get('title') or snapshot.get('url')})는 로그인이 필요한 상태입니다. "
                        "열려 있는 브라우저 창에서 직접 로그인하신 뒤 [로그인 완료 후 재개]를 누르시거나, 아래에 로그인 정보를 입력해 주세요."
                    ),
                    "fields": [
                        {
                            "name": "username",
                            "label": "아이디 / 이메일 (Username)",
                            "type": "text",
                            "placeholder": "로그인 아이디 입력 (예: admin)",
                            "required": True,
                        },
                        {
                            "name": "password",
                            "label": "비밀번호 (Password)",
                            "type": "password",
                            "placeholder": "비밀번호 입력 (예: 1234)",
                            "required": True,
                        },
                    ],
                }

        # Case 2: Prompt asks to add/create/delete something without specifying the required target value
        bare_action_patterns = [
            (r"^\s*(할\s*일|투두|todo)\s*(추가|등록)(해\s*줘|해|하기)?\s*$", "add_todo_item", [{"name": "text", "label": "추가할 할 일 내용", "type": "text", "placeholder": "예: 알고리즘 문제 풀기", "required": True}]),
            (r"^\s*(블로그\s*글|게시글|포스트)\s*(작성|발행|등록|쓰기)(해\s*줘|해)?\s*$", "write_post", [
                {"name": "title", "label": "글 제목", "type": "text", "placeholder": "예: Gemini 에이전트 개발기", "required": True},
                {"name": "content", "label": "글 본문", "type": "textarea", "placeholder": "본문 내용을 입력하세요", "required": True},
            ]),
            (r"^\s*로그인(해\s*줘|하기|해)?\s*$", "login_site", [
                {"name": "username", "label": "아이디 / 이메일", "type": "text", "placeholder": "아이디 입력", "required": True},
                {"name": "password", "label": "비밀번호", "type": "password", "placeholder": "비밀번호 입력", "required": True},
            ]),
        ]
        for pat, tool_name, req_fields in bare_action_patterns:
            if re.match(pat, user_prompt.strip(), re.IGNORECASE):
                missing = [f for f in req_fields if not provided.get(f["name"])]
                if missing:
                    return {
                        "requires_user_input": True,
                        "input_type": "MISSING_PARAMETERS",
                        "title": "✍️ 작업 수행을 위한 추가 입력이 필요합니다",
                        "message": f"'{user_prompt}' 작업을 수행하기 위해 아래 정보를 입력해 주세요.",
                        "target_tool": tool_name,
                        "fields": missing,
                    }

        return None

    def execute_with_url_tools(
        self,
        user_prompt: str,
        browser_session: Any,
        url_tools: List[Dict[str, Any]],
        user_inputs: Optional[Dict[str, Any]] = None,
        retry_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes `user_prompt` using strictly the `url_tools` matched to the current URL.
        """
        tool_map = {t["name"]: t for t in url_tools}
        execution_steps: List[Dict[str, Any]] = []

        # Enrich prompt if user supplied extra HITL inputs (non-password parameters)
        effective_prompt = user_prompt
        if user_inputs:
            extra_pairs = [
                f"{k}=\"{v}\""
                for k, v in user_inputs.items()
                if k not in ("password", "browser_login_completed") and v
            ]
            if extra_pairs and "username" not in user_inputs:
                effective_prompt = f"{user_prompt} (사용자 입력 파라미터: {', '.join(extra_pairs)})"

        if self.client.has_key:
            try:
                return self._execute_via_gemini_function_calling(
                    user_prompt=effective_prompt,
                    browser_session=browser_session,
                    url_tools=url_tools,
                    tool_map=tool_map,
                    execution_steps=execution_steps,
                    retry_context=retry_context,
                )
            except Exception as e:
                if execution_steps:
                    return {"executor_model":"Gemini", "steps":execution_steps,
                            "agent_message":"실행 중 오류가 발생했습니다. 결과 확인이 필요합니다."}
                print("[SkillExecutorAgent] Gemini tool selection failed")

        return self._execute_via_heuristic_planner(
            user_prompt=effective_prompt,
            browser_session=browser_session,
            url_tools=url_tools,
            tool_map=tool_map,
            execution_steps=execution_steps,
        )

    def _execute_via_gemini_function_calling(
        self,
        user_prompt: str,
        browser_session: Any,
        url_tools: List[Dict[str, Any]],
        tool_map: Dict[str, Dict[str, Any]],
        execution_steps: List[Dict[str, Any]],
        retry_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        function_declarations = []
        for t in url_tools:
            params = t.get("parameters", {"type": "object", "properties": {}})
            decl: Dict[str, Any] = {
                "name": t["name"],
                "description": t.get("description", ""),
            }
            if params.get("properties"):
                decl["parameters"] = params
            function_declarations.append(decl)

        current_info = browser_session.inspect_page()
        current_state = {
            "url": current_info["snapshot"].get("url"),
            "normalized_url": current_info["snapshot"].get("normalized_url"),
            "title": current_info["snapshot"].get("title"),
            "lists": current_info["snapshot"].get("lists", []),
            "headings": current_info["snapshot"].get("headings", []),
            "authState": current_info["snapshot"].get("authState", {}),
        }

        system_instruction = (
            "당신은 현재 URL에 정확히 매칭된 Skill 내부의 Tool만 호출하여 브라우저를 제어하는 Skill Executor Agent입니다.\n"
            f"현재 브라우저 상태: {json.dumps(current_state, ensure_ascii=False)}\n"
            "수행 원칙:\n"
            "1. 사용자 요청을 달성하기 위해 제공된 Function Tool을 순서대로 호출하세요.\n"
            "2. 여러 작업(예: 항목 A 추가 후 항목 B 추가, 그다음 항목 A 완료 처리)이 요청된 경우 각 도구를 빠짐없이 호출하세요.\n"
            "3. 만약 도구 실행 결과에서 `LOGIN_REQUIRED` 또는 `PERMISSION_DENIED`가 반환되면 즉시 실행을 멈추고 로그인이 필요하다고 보고하세요.\n"
            "4. 도구 호출이 모두 끝나면 수행한 작업 내역을 한국어로 간결하게 요약하세요."
        )
        if retry_context:
            system_instruction += (
                f"\n\n[주의 - Root Agent 재시도 루프(Retry Loop) 안내]\n{retry_context}\n"
                "보수된 최신 도구와 현재 화면 목록 상태를 참고하여 실패했던 작업을 정확히 다시 수행하세요."
            )

        contents: List[Dict[str, Any]] = [
            {"role": "user", "parts": [{"text": user_prompt}]}
        ]
        model_used = "gemini-4-argon"
        agent_message = ""

        for _ in range(6):
            res = self.client.generate(
                role="skill_executor",
                contents=contents,
                system_instruction=system_instruction,
                tools=[{"functionDeclarations": function_declarations}],
                json_mode=False,
                temperature=0.1,
            )
            model_used = res.get("_model_used", model_used)
            candidate = res.get("candidates", [{}])[0]
            content = candidate.get("content", {})
            parts = content.get("parts", [])
            if not parts:
                break

            contents.append(content)
            function_calls = [p["functionCall"] for p in parts if "functionCall" in p]
            text_parts = [p["text"] for p in parts if "text" in p]
            if text_parts:
                agent_message = "\n".join(text_parts)

            if not function_calls:
                break

            response_parts = []
            hit_auth_wall = False
            for fc in function_calls:
                fn_name = fc["name"]
                fn_args = fc.get("args", {}) or {}
                tool_def = tool_map.get(fn_name)
                if not tool_def:
                    response_parts.append(
                        {
                            "functionResponse": {
                                "name": fn_name,
                                "response": {
                                    "success": False,
                                    "failure_code": "UNREGISTERED_TOOL",
                                    "failure_reason": f"현재 URL 스킬에 존재하지 않는 도구입니다: {fn_name}",
                                },
                            }
                        }
                    )
                    continue

                exec_out = browser_session.execute_tool(tool_def, fn_args)
                step_record = {
                    "tool_name": fn_name,
                    "operations": tool_def.get("steps", []),
                    "display_name": tool_def.get("display_name", fn_name),
                    "args": fn_args,
                    "action_result": exec_out["action_result"],
                    "verification": exec_out["verification"],
                }
                execution_steps.append(step_record)
                response_parts.append(
                    {
                        "functionResponse": {
                            "name": fn_name,
                            "response": {
                                "result": exec_out["action_result"],
                                "verification": exec_out["verification"],
                            },
                        }
                    }
                )
                if not URLMatcher.is_exact_url_match(browser_session.current_url,current_state['url']):
                    return {'executor_model':f'Gemini ({model_used})','steps':execution_steps,
                            'agent_message':agent_message,'url_changed':True}
                if exec_out["action_result"].get("failure_code") in ("LOGIN_REQUIRED", "PERMISSION_DENIED"):
                    hit_auth_wall = True
                    break

            contents.append({"role": "user", "parts": response_parts})
            if hit_auth_wall:
                break

        return {
            "executor_model": f"Gemini ({model_used})",
            "steps": execution_steps,
            "agent_message": agent_message,
        }

    def _execute_via_heuristic_planner(
        self,
        user_prompt: str,
        browser_session: Any,
        url_tools: List[Dict[str, Any]],
        tool_map: Dict[str, Dict[str, Any]],
        execution_steps: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Multi-clause natural language fallback planner when API quota/network is unavailable.
        Handles compound Korean instructions like:
        - "발표 준비를 할 일 목록에 추가하고, 자료 조사를 추가한 뒤 발표 준비는 완료 처리해줘"
        - '제목은 "A", 내용은 "B"로 블로그 글 작성해줘'
        """
        planned_calls: List[Dict[str, Any]] = []

        # Check for blog / multi-field post pattern
        write_tool = next((t for t in url_tools if t["name"] == "write_post" or "title" in t.get("parameters", {}).get("properties", {})), None)
        quoted = re.findall(r"['\"“”‘’]([^'\"“”‘’]+)['\"“”‘’]", user_prompt)
        if write_tool and ("블로그" in user_prompt or "게시글" in user_prompt or "제목" in user_prompt or "발행" in user_prompt or len(quoted) >= 2):
            props = list(write_tool.get("parameters", {}).get("properties", {}).keys())
            args: Dict[str, Any] = {}
            if len(quoted) >= 2:
                if "title" in props:
                    args["title"] = quoted[0]
                if "content" in props:
                    args["content"] = quoted[1]
                for p in props:
                    if p not in args:
                        args[p] = quoted[0]
            else:
                for p in props:
                    args[p] = user_prompt
            planned_calls.append({"name": write_tool["name"], "args": args})
        else:
            # Split compound sentences by '그리고', '한 뒤', '하고', ','
            clauses = [
                c.strip()
                for c in re.split(r"(?:그리고|한\s*뒤|한\s*후|하고\s*,?|,\s*)", user_prompt)
                if c.strip()
            ]
            for clause in clauses:
                c_quoted = re.findall(r"['\"“”‘’]([^'\"“”‘’]+)['\"“”‘’]", clause)
                val = c_quoted[0].strip() if c_quoted else ""
                if not val:
                    cleaned = re.sub(
                        r"\s*(을|를|은|는|이|가)?\s*(할\s*일\s*목록에|목록에|목록에서|할\s*일에)?\s*(추가해\s*줘|추가하고|추가|완료\s*처리해\s*줘|완료해\s*줘|완료|삭제해\s*줘|삭제|복구해\s*줘|복구|조회해\s*줘|조회).*$",
                        "",
                        clause,
                    ).strip()
                    val = cleaned

                if any(k in clause for k in ["조회", "상태", "목록 보여"]):
                    t = tool_map.get("get_page_state")
                    if t:
                        planned_calls.append({"name": t["name"], "args": {}})
                elif any(k in clause for k in ["완료", "체크", "끝내"]):
                    t = next((t for t in url_tools if "complete" in t["name"]), None)
                    if t and val:
                        planned_calls.append({"name": t["name"], "args": {"text": val}})
                elif any(k in clause for k in ["삭제", "지워", "제거"]):
                    t = next((t for t in url_tools if "delete" in t["name"]), None)
                    if t and val:
                        planned_calls.append({"name": t["name"], "args": {"text": val}})
                elif any(k in clause for k in ["복구", "되돌"]):
                    t = next((t for t in url_tools if "restore" in t["name"]), None)
                    if t and val:
                        planned_calls.append({"name": t["name"], "args": {"text": val}})
                elif any(k in clause for k in ["추가", "등록", "입력", "넣어"]):
                    t = next((t for t in url_tools if "add" in t["name"] or "submit" in t["name"]), None)
                    if t and val:
                        p_keys = list(t.get("parameters", {}).get("properties", {}).keys())
                        arg_key = p_keys[0] if p_keys else "text"
                        planned_calls.append({"name": t["name"], "args": {arg_key: val}})

        # Without a matched capability, execute nothing. A random tool or a read-only
        # fallback cannot substitute for the requested mutation.
        for call in planned_calls:
            tool_def = tool_map.get(call["name"])
            if not tool_def:
                continue
            exec_out = browser_session.execute_tool(tool_def, call["args"])
            execution_steps.append(
                {
                    "tool_name": call["name"],
                    "operations": tool_def.get("steps", []),
                    "display_name": tool_def.get("display_name", call["name"]),
                    "args": call["args"],
                    "action_result": exec_out["action_result"],
                    "verification": exec_out["verification"],
                }
            )
            if exec_out["action_result"].get("failure_code") in ("LOGIN_REQUIRED", "PERMISSION_DENIED"):
                break

        return {
            "executor_model": "Rule-Assisted Function Executor (Fallback)",
            "steps": execution_steps,
            "agent_message": "",
        }


class RootOrchestratorAgent:
    """
    [Agent 3 - Root Agent]
    Model Role: `root_verifier` (Primary: `gemini-4-argon`)
    Responsibilities:
    1. Strict URL-Based Skill Resolution:
       - Determines whether to reuse an existing Skill or synthesize a new Skill strictly by URL (`URLMatcher`).
       - Dynamically registers newly synthesized Skills into `SkillRegistry`.
    2. Human-in-the-Loop (HITL) Input & Login Gate:
       - Requests user login or missing parameters when needed, and resumes seamlessly once provided.
    3. Post-Execution Success/Failure Check (`verify_execution`):
       - Evaluates Before/After DOM state + step execution results to return an explicit `SUCCESS` / `FAILED` / `NEEDS_USER_INPUT` verdict.
    4. Automatic 1x Retry Loop (`max_retry = 1`):
       - If Attempt 1 fails verification (e.g., selector mismatch or unverified DOM mutation),
         automatically re-analyzes the live DOM, repairs the URL's Skill in `SkillRegistry`,
         and runs the execution loop 1 more time (Attempt 2).
    """

    def __init__(self, api_key: Optional[str] = None, registry: Optional[SkillRegistry] = None):
        self.client = GeminiClient(api_key=api_key)
        self.registry = registry or SkillRegistry()
        self.synthesizer = SkillSynthesizerAgent(self.client)
        self.executor = SkillExecutorAgent(self.client)
        self.max_retry_count = MAX_ROOT_RETRY_COUNT
        self.result_verifier = ResultVerificationAgent(self.client)

    def set_api_key(self, api_key: str) -> None:
        self.client.set_api_key(api_key)

    def analyze_and_register_url_skill(
        self,
        target_url: str,
        browser_session: Any,
        force_resynthesize: bool = False,
        failure_feedback: Optional[str] = None,
        progress=None,
    ) -> Dict[str, Any]:
        """
        Navigates to `target_url` (if needed), checks `SkillRegistry` strictly by normalized URL,
        and either reuses the existing Skill for that URL or synthesizes & registers a new Skill.
        """
        notify = progress or (lambda message: None)
        notify('브라우저에서 페이지를 여는 중입니다.')
        current_norm = URLMatcher.normalize_url(browser_session.current_url)
        target_norm = URLMatcher.normalize_url(target_url)

        if not browser_session.current_url or current_norm != target_norm:
            page_info = browser_session.navigate(target_url)
        else:
            page_info = browser_session.inspect_page()

        snapshot = page_info["snapshot"]
        screenshot = page_info["screenshot"]
        actual_url = snapshot.get("url") or target_url

        notify('페이지를 읽었습니다. 등록된 스킬과 URL을 비교하는 중입니다.')
        # Strict URL check against SkillRegistry
        existing_tools, existing_pkg, routing_decision = self.registry.get_tools_strictly_for_url(actual_url)

        if existing_pkg and existing_tools and not force_resynthesize:
            return {
                "skill_reused": True,
                "skill_package": existing_pkg,
                "tools": existing_tools,
                "routing_decision": routing_decision,
                "generator_source": f"SkillRegistry Cache HIT ({existing_pkg['skill_id']} v{existing_pkg.get('version', 1)})",
                "snapshot": snapshot,
                "screenshot": screenshot,
            }

        # Cache miss (or forced repair during retry loop) -> Synthesize & Register into SkillRegistry
        notify('Gemini가 페이지를 분석하고 스킬을 추출하는 중입니다.')
        tools, generator_source = self.synthesizer.synthesize_tools(
            snapshot=snapshot,
            failure_feedback=failure_feedback,
            existing_tools=existing_tools,
        )
        previous = {t['name']:t for t in existing_tools}
        notify('기존 기능을 유지하며 새 기능을 병합하는 중입니다.' if existing_pkg else '추출한 스킬을 등록하는 중입니다.')
        skill_pkg = self.registry.register_skill(
            target_url=actual_url,
            tools=tools,
            site_title=snapshot.get("title", ""),
            synthesized_by=generator_source,
            is_repair=bool(failure_feedback),
        )
        strip_binding = lambda tool: {key:value for key,value in tool.items() if key not in ('skill_id','bound_normalized_url')}
        current = {t['name']:t for t in skill_pkg['tools']}
        added = [name for name in current if name not in previous]
        updated = [name for name in current if name in previous and strip_binding(current[name]) != strip_binding(previous[name])]
        refresh_summary = {'mode':'updated' if existing_pkg else 'created', 'added':added, 'updated':updated,
                           'kept':[name for name in previous if name not in updated and name in current],
                           'previous_version':existing_pkg.get('version',0) if existing_pkg else 0,
                           'version':skill_pkg['version']}
        _, _, updated_routing = self.registry.get_tools_strictly_for_url(actual_url)
        if not existing_pkg:
            updated_routing["url_matched"] = False
            updated_routing["reason"] = (
                f"최초 방문 URL ({skill_pkg['normalized_url']}) 분석 완료 → 신규 Skill '{skill_pkg['skill_id']}' 생성 및 에이전트 스킬 레지스트리에 등록됨"
            )
        else:
            updated_routing["reason"] = (
                f"실패 복구 루프(Retry Loop) 또는 재분석 요청으로 URL ({skill_pkg['normalized_url']})의 Skill '{skill_pkg['skill_id']}' (v{skill_pkg['version']}) 업데이트 완료"
            )

        return {
            "skill_reused": False,
            "refresh_summary": refresh_summary,
            "skill_package": skill_pkg,
            "tools": skill_pkg["tools"],
            "routing_decision": updated_routing,
            "generator_source": generator_source,
            "snapshot": snapshot,
            "screenshot": screenshot,
        }

    def analyze_site(self, target_url, browser_session, force_resynthesize=False, progress=None, observation=None):
        """Discover linked capability pages without submitting forms or creating sample data."""
        notify = progress or (lambda message: None)
        origin = urlparse(URLMatcher.normalize_url(target_url))
        queue, visited, results = [target_url], set(), []
        while queue and len(visited) < 6:
            url = queue.pop(0)
            if URLMatcher.normalize_url(url) in visited:
                continue
            notify(f'기능 페이지 확인 중: {url}')
            info = browser_session.navigate(url)
            snapshot = info['snapshot']
            if observation:
                observation({'url':snapshot['url'], 'screenshot':info.get('screenshot','')})
            actual = URLMatcher.normalize_url(snapshot['url'])
            if urlparse(actual).netloc != origin.netloc:
                return {'status':'NEEDS_USER_INPUT','message':'외부 로그인 페이지로 이동했습니다. 브라우저에서 로그인 후 분석을 재개하세요.',
                        'login_url':snapshot['url'], 'analyzed_skills':[r['skill_package'] for r in results],
                'refresh_summaries':[r.get('refresh_summary',{}) for r in results]}
            if requires_login(snapshot):
                return {'status':'NEEDS_USER_INPUT', 'message':'기능 분석을 계속하려면 사이트 로그인이 필요합니다.',
                        'login_url':snapshot['url'], 'analyzed_skills':[r['skill_package'] for r in results],
                'refresh_summaries':[r.get('refresh_summary',{}) for r in results]}
            if actual in visited:
                continue
            visited.add(actual)
            result = self.analyze_and_register_url_skill(
                actual, browser_session, force_resynthesize=True, progress=notify)
            results.append(result)
            for link in snapshot.get('links', []):
                href = urljoin(actual, link.get('href', ''))
                label = (link.get('text') or '') + ' ' + href
                parsed = urlparse(href)
                if parsed.scheme not in ('http','https') or parsed.netloc != origin.netloc:
                    continue
                if re.search(r'logout|sign.?out|delete|remove|publish|register|signup|로그아웃|삭제|회원가입', label, re.I):
                    continue
                if snapshot.get('authState', {}).get('is_authenticated') and re.search(r'login|sign.?in|로그인', label, re.I):
                    continue
                if re.search(r'write|compose|editor|/new(?:[/?#]|$)|/create(?:[/?#]|$)|login|sign.?in|글.?쓰기|작성|로그인', label, re.I):
                    if URLMatcher.normalize_url(href) not in visited:
                        queue.append(href)
        browser_session.navigate(target_url)
        if not results:
            raise ValueError('분석할 페이지를 찾지 못했습니다.')
        return {**results[0], 'status':'SUCCESS',
                'analyzed_skills':[r['skill_package'] for r in results],
                'refresh_summaries':[r.get('refresh_summary',{}) for r in results]}

    def verify_execution(self, user_prompt, before_snapshot, after_snapshot, steps, browser_session=None):
        """Delegate result checking to an agent independent of the executor."""
        return self.result_verifier.verify(user_prompt, before_snapshot, after_snapshot,
                                           steps, browser_session=browser_session)

    def run_task(
        self,
        target_url: str,
        user_prompt: str,
        browser_session: Any,
        user_inputs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        End-to-End Root Agent Orchestration Pipeline:
        1. Navigates to `target_url` and checks `SkillRegistry` strictly by normalized URL.
           - Reuses existing Skill if URL matches, or synthesizes & registers a new Skill if not.
        2. If `user_inputs` contains login credentials (`username` & `password`), performs login first,
           then re-checks the post-login page and synthesizes/loads the post-login Skill for the URL.
        3. Runs Pre-Execution HITL Gate (`check_user_input_needed`):
           - If login or required user input is needed, returns `status="NEEDS_USER_INPUT"` immediately.
        4. Executes Attempt 1 with URL-matched Skill Tools and runs `verify_execution()`.
        5. If Attempt 1 fails verification (and is not waiting on user login):
           - Automatically triggers **1x Retry Loop (`retry_count = 1`)**:
             re-inspects the live DOM, repairs the URL's Skill in `SkillRegistry`,
             re-runs `SkillExecutorAgent` (Attempt 2), and verifies the final outcome!
        """
        provided_inputs = dict(user_inputs or {})

        # Step 1: Ensure browser is on `target_url` and inspect
        current_norm = URLMatcher.normalize_url(browser_session.current_url)
        target_norm = URLMatcher.normalize_url(target_url)
        if not browser_session.current_url or current_norm != target_norm:
            initial_info = browser_session.navigate(target_url)
        else:
            initial_info = browser_session.inspect_page()

        initial_snapshot = initial_info["snapshot"]

        # Step 2: If user just submitted login credentials via HITL modal/CLI, execute login first!
        login_step_record: Optional[Dict[str, Any]] = None
        if provided_inputs.get("username") and provided_inputs.get("password"):
            login_exec = browser_session.perform_direct_login(
                username=str(provided_inputs["username"]),
                password=str(provided_inputs["password"]),
                snapshot=initial_snapshot,
            )
            login_step_record = {
                "tool_name": "login_site",
                "display_name": "사용자 입력 기반 사이트 로그인",
                "args": {"username": provided_inputs["username"], "password": "••••••"},
                "action_result": login_exec["action_result"],
                "verification": login_exec["verification"],
            }
            initial_snapshot = login_exec["after_snapshot"]

            # If login itself failed (e.g., wrong password), request user login input again
            if not login_exec["action_result"].get("success") or initial_snapshot.get("authState", {}).get("login_wall_detected"):
                return {
                    "ok": True,
                    "status": "NEEDS_USER_INPUT",
                    "overall_success": False,
                    "requires_user_input": True,
                    "user_input_request": {
                        "requires_user_input": True,
                        "input_type": "LOGIN_REQUIRED",
                        "title": "🔐 로그인 실패 — 다시 로그인해 주세요",
                        "message": (
                            login_exec["action_result"].get("failure_reason")
                            or "입력하신 아이디 또는 비밀번호로 로그인하지 못했습니다. 다시 확인해 주세요."
                        ),
                        "fields": [
                            {"name": "username", "label": "아이디 / 이메일", "type": "text", "placeholder": "아이디 입력", "required": True},
                            {"name": "password", "label": "비밀번호", "type": "password", "placeholder": "비밀번호 입력", "required": True},
                        ],
                    },
                    "url": initial_snapshot.get("url", target_url),
                    "normalized_url": initial_snapshot.get("normalized_url", target_norm),
                    "attempts": [
                        {
                            "attempt_number": 1,
                            "is_retry": False,
                            "steps": [login_step_record],
                            "verification": {
                                "success": False,
                                "status": "NEEDS_USER_INPUT",
                                "failure_code": "LOGIN_REQUIRED",
                                "reason": login_exec["action_result"].get("failure_reason") or "로그인 실패",
                                "evidence": [],
                            },
                        }
                    ],
                    "retry_count": 0,
                    "max_retry_count": self.max_retry_count,
                    "snapshot": initial_snapshot,
                    "screenshot": login_exec["screenshot"],
                    "all_registered_skills": self.registry.list_all_skills(),
                    "summary": f"⚠️ 로그인 재요청: {login_exec['action_result'].get('failure_reason') or '로그인에 실패했습니다.'}",
                }

            # After successful login, re-synthesize or refresh the Skill for the unlocked page DOM!
            skill_resolution = self.analyze_and_register_url_skill(
                target_url=initial_snapshot.get("url") or target_url,
                browser_session=browser_session,
                force_resynthesize=True,
            )
        elif provided_inputs.get("browser_login_completed"):
            # User logged in directly inside the Chrome window -> re-inspect unlocked page & update skill
            skill_resolution = self.analyze_and_register_url_skill(
                target_url=target_url,
                browser_session=browser_session,
                force_resynthesize=True,
            )
        else:
            # Standard Strict URL Skill Lookup (Cache Hit -> Reuse, Cache Miss -> Synthesize & Register)
            skill_resolution = self.analyze_and_register_url_skill(
                target_url=target_url,
                browser_session=browser_session,
                force_resynthesize=False,
            )

        before_snapshot = skill_resolution["snapshot"]
        url_tools = skill_resolution["tools"]
        skill_pkg = skill_resolution["skill_package"]
        routing_decision = skill_resolution["routing_decision"]

        # Step 3: Pre-Execution HITL Gate (Check if Login or Missing Parameters are needed)
        hitl_request = self.executor.check_user_input_needed(
            user_prompt=user_prompt,
            snapshot=before_snapshot,
            url_tools=url_tools,
            user_inputs=provided_inputs,
        )
        if hitl_request:
            return {
                "ok": True,
                "status": "NEEDS_USER_INPUT",
                "overall_success": False,
                "requires_user_input": True,
                "user_input_request": hitl_request,
                "url": before_snapshot.get("url", target_url),
                "normalized_url": skill_pkg["normalized_url"],
                "skill_package": skill_pkg,
                "routing_decision": routing_decision,
                "tools": url_tools,
                "attempts": [],
                "retry_count": 0,
                "max_retry_count": self.max_retry_count,
                "snapshot": before_snapshot,
                "screenshot": skill_resolution["screenshot"],
                "all_registered_skills": self.registry.list_all_skills(),
                "summary": f"⏸️ [사용자 입력 요청] {hitl_request['title']}: {hitl_request['message']}",
            }

        # Step 4: Execution & Verification Loop (Attempt 1 + up to 1 Automatic Retry Loop on Failure)
        attempts_log: List[Dict[str, Any]] = []
        retry_count = 0
        final_verification: Dict[str, Any] = {}
        final_steps: List[Dict[str, Any]] = []
        executor_model_used = ""

        for attempt_idx in range(1, self.max_retry_count + 2):  # attempt_idx = 1 (initial), 2 (1st retry)
            is_retry = attempt_idx > 1
            retry_context = None

            if is_retry:
                retry_count += 1
                prev_fail_reason = final_verification.get("reason", "이전 실행 검증 실패")
                retry_context = (
                    f"1차 실행 실패 원인: {prev_fail_reason}\n"
                    "현재 페이지의 DOM을 다시 분석하여 URL 스킬의 셀렉터 및 도구를 최신 상태로 보수했습니다."
                )
                # Re-analyze live page DOM and repair the URL's Skill in SkillRegistry
                repaired_res = self.analyze_and_register_url_skill(
                    target_url=browser_session.current_url or target_url,
                    browser_session=browser_session,
                    force_resynthesize=True,
                    failure_feedback=prev_fail_reason,
                )
                before_snapshot = repaired_res["snapshot"]
                url_tools = repaired_res["tools"]
                skill_pkg = repaired_res["skill_package"]
                routing_decision = repaired_res["routing_decision"]

            attempt_steps = []
            for route_turn in range(8):
                exec_out = self.executor.execute_with_url_tools(
                    user_prompt=user_prompt, browser_session=browser_session, url_tools=url_tools,
                    user_inputs=provided_inputs, retry_context=retry_context)
                executor_model_used = exec_out['executor_model']
                attempt_steps.extend(exec_out['steps'])
                if not exec_out.get('url_changed'):
                    break
                next_res = self.analyze_and_register_url_skill(
                    target_url=browser_session.current_url,browser_session=browser_session)
                url_tools=next_res['tools']
                skill_pkg=next_res['skill_package']
                retry_context='이전 실행: '+json.dumps(attempt_steps,ensure_ascii=False)+'. 완료된 변경은 반복하지 마세요.'
            if attempt_idx == 1 and login_step_record:
                attempt_steps = [login_step_record] + attempt_steps

            after_info = browser_session.inspect_page()
            after_snapshot = after_info["snapshot"]

            # Root Agent checks success/failure of this attempt
            verification_report = self.verify_execution(
                user_prompt=user_prompt,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
                steps=attempt_steps,
                browser_session=browser_session,
            )

            attempts_log.append(
                {
                    "attempt_number": attempt_idx,
                    "is_retry": is_retry,
                    "executor_model": executor_model_used,
                    "steps": attempt_steps,
                    "verification": verification_report,
                }
            )
            final_steps = attempt_steps
            final_verification = verification_report

            # If execution hit a login requirement mid-flight, pause and ask the user to log in!
            if verification_report["status"] == "NEEDS_USER_INPUT":
                hitl_login_req = {
                    "requires_user_input": True,
                    "input_type": "LOGIN_REQUIRED",
                    "title": "🔐 작업 수행을 위해 사이트 로그인이 필요합니다",
                    "message": verification_report["reason"],
                    "fields": [
                        {"name": "username", "label": "아이디 / 이메일", "type": "text", "placeholder": "아이디 입력", "required": True},
                        {"name": "password", "label": "비밀번호", "type": "password", "placeholder": "비밀번호 입력", "required": True},
                    ],
                }
                return {
                    "ok": True,
                    "status": "NEEDS_USER_INPUT",
                    "overall_success": False,
                    "requires_user_input": True,
                    "user_input_request": hitl_login_req,
                    "url": after_snapshot.get("url", target_url),
                    "normalized_url": skill_pkg["normalized_url"],
                    "skill_package": skill_pkg,
                    "routing_decision": routing_decision,
                    "tools": url_tools,
                    "steps": final_steps,
                    "attempts": attempts_log,
                    "root_verification": final_verification,
                    "retry_count": retry_count,
                    "max_retry_count": self.max_retry_count,
                    "snapshot": after_snapshot,
                    "screenshot": after_info["screenshot"],
                    "all_registered_skills": self.registry.list_all_skills(),
                    "summary": self._format_final_summary(
                        attempts_log=attempts_log,
                        final_verification=final_verification,
                        routing_decision=routing_decision,
                        final_snapshot=after_snapshot,
                    ),
                }

            # If Root Agent verified SUCCESS, increment skill usage count and stop loop!
            if verification_report["success"]:
                self.registry.increment_usage(after_snapshot.get("url") or target_url)
                break

            # Otherwise, if attempt_idx == 1 and verification failed, loop continues to attempt_idx == 2 (1x Retry Loop)!

        final_info = browser_session.inspect_page()
        overall_success = bool(final_verification.get("success", False))

        return {
            "ok": True,
            "status": "SUCCESS" if overall_success else "FAILED",
            "overall_success": overall_success,
            "requires_user_input": False,
            "user_input_request": None,
            "mode": f"Root({final_verification.get('verifier_model')}) + Executor({executor_model_used})",
            "model_selection": MODEL_ROLES,
            "url": final_info["snapshot"].get("url", target_url),
            "normalized_url": skill_pkg["normalized_url"],
            "skill_reused": skill_resolution["skill_reused"],
            "skill_package": skill_pkg,
            "routing_decision": routing_decision,
            "tools": url_tools,
            "steps": final_steps,
            "attempts": attempts_log,
            "root_verification": final_verification,
            "retry_count": retry_count,
            "max_retry_count": self.max_retry_count,
            "snapshot": final_info["snapshot"],
            "screenshot": final_info["screenshot"],
            "all_registered_skills": self.registry.list_all_skills(),
            "summary": self._format_final_summary(
                attempts_log=attempts_log,
                final_verification=final_verification,
                routing_decision=routing_decision,
                final_snapshot=final_info["snapshot"],
            ),
        }

    @staticmethod
    def _format_final_summary(
        attempts_log: List[Dict[str, Any]],
        final_verification: Dict[str, Any],
        routing_decision: Dict[str, Any],
        final_snapshot: Dict[str, Any],
    ) -> str:
        lines: List[str] = []
        lines.append(f"🔗 [URL 스킬 판별] {routing_decision.get('reason', '')}")

        for att in attempts_log:
            att_no = att["attempt_number"]
            tag = "🔄 2차 실행 (실패 복구 재시도 루프 1/1)" if att["is_retry"] else "▶️ 1차 실행"
            ver = att["verification"]
            status_badge = "✅ 성공" if ver.get("success") else ("🔐 사용자 입력/로그인 필요" if ver.get("status") == "NEEDS_USER_INPUT" else "❌ 실패")
            lines.append(f"\n{tag} — Root 검증 결과: {status_badge}")
            for idx, s in enumerate(att.get("steps", []), 1):
                ar = s["action_result"]
                v = s["verification"]
                if ar.get("success"):
                    ch = ", ".join(v.get("changes", [])) or "확인 완료"
                    lines.append(f"  ✔ {idx}. [{s['display_name']}] {ar.get('message')} (근거: {ch})")
                else:
                    lines.append(
                        f"  ✖ {idx}. [{s['display_name']}] 실패 ({ar.get('failure_code')}) — {ar.get('failure_reason')}"
                    )
            lines.append(f"  ↳ Root 검증 판정 ({ver.get('verifier_model', 'Root')}): {ver.get('reason', '')}")

        lists = final_snapshot.get("lists", [])
        if lists:
            col_strs = [
                f"{lst.get('heading') or lst.get('selector')}({len(lst.get('items', []))}개): {[i['text'] for i in lst.get('items', [])][:5]}"
                for lst in lists
            ]
            lines.append(f"\n📋 [최종 화면 데이터 상태] {' | '.join(col_strs)}")

        return "\n".join(lines)
