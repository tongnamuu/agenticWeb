import base64
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
import websocket

from agentic_web.engine.skills.registry import URLMatcher


CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

ALLOWED_PRIMITIVES = {
    "read",
    "read_state",
    "extract",
    "fill",
    "select",
    "click",
    "click_item_button",
    "submit",
    "navigate",
    "wait",
}


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _interpolate(template: Any, args: Dict[str, Any]) -> Any:
    """Replaces {{param}} or {param} placeholders with actual argument values."""
    if not isinstance(template, str):
        return template

    def repl(match: Any) -> str:
        key = match.group(1).strip()
        if key in args and args[key] is not None:
            return str(args[key])
        if len(args) == 1:
            return str(next(iter(args.values())))
        return ""

    out = re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", repl, template)
    out = re.sub(r"\{\s*([a-zA-Z0-9_]+)\s*\}", repl, out)
    return out


class BrowserSession:
    """
    Universal Browser Controller via Chrome DevTools Protocol (CDP).
    - Inspects DOM structure, accessibility labels, collections, and authentication/login gates.
    - Enforces strict URL matching before executing any URL-bound Skill Tool.
    - Executes primitive steps (`read_state`, `fill`, `select`, `click`, `click_item_button`, `submit`, `navigate`)
      and compares Before vs. After DOM snapshots to verify whether the action truly succeeded.
    """

    def __init__(self, headless: bool = False):
        self.headless = headless
        self.port: Optional[int] = None
        self.proc: Optional[subprocess.Popen] = None
        self.ws: Optional[websocket.WebSocket] = None
        self.user_data_dir: Optional[str] = None
        self._msg_id = 0
        self._lock = threading.Lock()
        self.current_url: str = ""
        self.last_dialog_message: Optional[str] = None

    def start(self, headless: Optional[bool] = None) -> None:
        if headless is not None and headless != self.headless:
            self.close()
            self.headless = headless

        if self.ws is not None and self.ws.connected:
            return

        env_chrome = os.environ.get("CHROME_BIN", "").strip()
        candidate_paths = ([env_chrome] if env_chrome else []) + CHROME_PATHS
        chrome_bin = next((p for p in candidate_paths if p and os.path.exists(p)), None)
        if not chrome_bin:
            raise RuntimeError("Google Chrome / Chromium binary not found.")

        self.port = _find_free_port()
        if not self.user_data_dir:
            self.user_data_dir = tempfile.mkdtemp(prefix="agentic_web_chrome_")

        args = [
            chrome_bin,
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1280,900",
            "about:blank",
        ]
        if self.headless:
            args.append("--headless=new")

        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        ws_url = None
        for _ in range(40):
            try:
                resp = requests.get(f"http://127.0.0.1:{self.port}/json", timeout=1.0)
                for t in resp.json():
                    if t.get("type") == "page" and "webSocketDebuggerUrl" in t:
                        ws_url = t["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(0.15)

        if not ws_url:
            raise RuntimeError("Failed to connect to Chrome DevTools Protocol.")

        self.ws = websocket.create_connection(ws_url, timeout=15)
        self._send_cdp("Page.enable")
        self._send_cdp("Runtime.enable")
        self._send_cdp("DOM.enable")

    def _send_cdp(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            if not self.ws or not self.ws.connected:
                self.start()
            self._msg_id += 1
            req_id = self._msg_id
            self.ws.send(json.dumps({"id": req_id, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("method") == "Page.javascriptDialogOpening":
                    dlg_params = msg.get("params", {})
                    self.last_dialog_message = dlg_params.get("message", "")
                    self._msg_id += 1
                    self.ws.send(
                        json.dumps(
                            {
                                "id": self._msg_id,
                                "method": "Page.handleJavaScriptDialog",
                                "params": {"accept": True},
                            }
                        )
                    )
                    continue
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(f"CDP error in {method}: {msg['error']}")
                    return msg.get("result", {})

    def evaluate_js(self, expression: str, arg: Any = None) -> Any:
        if arg is not None:
            arg_json = json.dumps(arg, ensure_ascii=False)
            wrapped = f"({expression})({arg_json})"
        else:
            wrapped = f"({expression})()"

        res = self._send_cdp(
            "Runtime.evaluate",
            {
                "expression": wrapped,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in res:
            exc = res["exceptionDetails"]
            raise RuntimeError(f"Browser evaluation error: {exc.get('text', exc)}")
        return res.get("result", {}).get("value")

    def navigate(self, url: str) -> Dict[str, Any]:
        self.start()
        if url.startswith("/") and self.current_url and self.current_url.startswith("http"):
            url = urljoin(self.current_url, url)

        self._send_cdp("Page.navigate", {"url": url})
        for _ in range(30):
            try:
                if self.evaluate_js("() => document.readyState") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.2)
        # SPA shells can finish document loading before their data/UI appears.
        # Do not synthesize a read-only skill from a transient "Loading..." page.
        self.evaluate_js(r"""async () => {
            const deadline = Date.now() + 8000;
            while (Date.now() < deadline) {
                const main = document.querySelector('main, [role="main"]') || document.body;
                const text = (main?.innerText || '').trim();
                const loading = /^(loading[.\s…]*|로드 중[.\s…]*|불러오는 중[.\s…]*)$/i.test(text);
                if (document.readyState === 'complete' && text && !loading) return true;
                await new Promise(resolve => setTimeout(resolve, 200));
            }
            return false;
        }""")

        self.current_url = self.evaluate_js("() => window.location.href") or url
        return self.inspect_page()

    def capture_screenshot(self) -> str:
        try:
            res = self._send_cdp("Page.captureScreenshot", {"format": "png"})
            return res.get("data", "")
        except Exception:
            return ""

    def inspect_page(self) -> Dict[str, Any]:
        """
        Inspects the live DOM of the current page:
        - Forms, inputs, selects, contenteditable editors, buttons, links, lists/collections
        - Authentication/Login detection (`auth_state`): checks if login is required or if a login form is blocking access
        """
        self.start()
        dom_snapshot = self.evaluate_js(
            """async () => {
                function cssPath(el) {
                    if (!el || el.nodeType !== 1) return "";
                    if (el.id && isNaN(Number(el.id))) return "#" + CSS.escape(el.id);
                    const tag = el.tagName.toLowerCase();
                    if (el.getAttribute("contenteditable") === "true") {
                        if (document.querySelectorAll('[contenteditable="true"]').length === 1) {
                            return '[contenteditable="true"]';
                        }
                    }
                    const nameAttr = el.getAttribute("name");
                    if (nameAttr) {
                        const byName = `${tag}[name="${CSS.escape(nameAttr)}"]`;
                        if (document.querySelectorAll(byName).length === 1) return byName;
                    }
                    const ariaLabel = el.getAttribute("aria-label");
                    if (ariaLabel) {
                        const byAria = `${tag}[aria-label="${CSS.escape(ariaLabel)}"]`;
                        if (document.querySelectorAll(byAria).length === 1) return byAria;
                    }
                    const placeholder = el.getAttribute("placeholder");
                    if (placeholder) {
                        const byPh = `${tag}[placeholder="${CSS.escape(placeholder)}"]`;
                        if (document.querySelectorAll(byPh).length === 1) return byPh;
                    }
                    if (el.classList && el.classList.length > 0) {
                        const cls = Array.from(el.classList)
                            .filter(c => c && !c.includes(":") && c !== "not-display" && c !== "hidden")
                            .slice(0, 3)
                            .map(c => CSS.escape(c))
                            .join(".");
                        if (cls) {
                            const candidate = tag + "." + cls;
                            if (document.querySelectorAll(candidate).length === 1) {
                                return candidate;
                            }
                        }
                    }
                    if (el.parentElement && el.parentElement !== document.body) {
                        const parentSelector = cssPath(el.parentElement);
                        if (parentSelector) {
                            const candidate = parentSelector + " " + tag;
                            if (document.querySelectorAll(candidate).length === 1) return candidate;
                        }
                    }
                    return tag;
                }

                function isVisible(el) {
                    if (!el) return false;
                    if (el.classList && (el.classList.contains("not-display") || el.classList.contains("hidden"))) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 || rect.height > 0;
                }

                function getAccessibleName(el) {
                    if (!el) return "";
                    const ariaLabel = el.getAttribute("aria-label");
                    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
                    const labelledBy = el.getAttribute("aria-labelledby");
                    if (labelledBy) {
                        const ref = document.getElementById(labelledBy);
                        if (ref && ref.innerText.trim()) return ref.innerText.trim();
                    }
                    if (el.id) {
                        const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                        if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
                    }
                    const parentLbl = el.closest("label");
                    if (parentLbl && parentLbl.innerText.trim()) return parentLbl.innerText.trim();
                    const titleAttr = el.getAttribute("title");
                    if (titleAttr && titleAttr.trim()) return titleAttr.trim();
                    const placeholder = el.getAttribute("placeholder") || el.getAttribute("data-placeholder");
                    if (placeholder && placeholder.trim()) return placeholder.trim();
                    const prev = el.previousElementSibling;
                    if (prev && ["LABEL", "SPAN", "H1", "H2", "H3", "H4", "P", "TH", "DT"].includes(prev.tagName)) {
                        return prev.innerText.trim().slice(0, 60);
                    }
                    return (el.innerText || el.value || "").trim().slice(0, 60);
                }

                // 1. Inputs
                const fieldNodes = Array.from(
                    document.querySelectorAll('input:not([type="hidden"]), textarea, select, [contenteditable="true"], [role="textbox"], [role="searchbox"], [role="combobox"]')
                ).filter(isVisible);

                const inputs = fieldNodes.map((el, idx) => {
                    const form = el.closest("form");
                    const isContentEditable = el.isContentEditable || el.getAttribute("contenteditable") === "true";
                    const tag = el.tagName.toLowerCase();
                    const options = tag === "select"
                        ? Array.from(el.options).map(o => ({ value: o.value, text: o.text.trim() })).slice(0, 20)
                        : undefined;
                    let inlineBtn = null;
                    if (el.parentElement && el.parentElement.tagName !== "FORM") {
                        const siblingBtns = Array.from(el.parentElement.querySelectorAll(':scope > button, :scope > input[type="button"], :scope > [role="button"]'));
                        if (siblingBtns.length === 1 && siblingBtns[0].getAttribute("type") !== "submit") {
                            inlineBtn = siblingBtns[0];
                        }
                    }
                    const accName = getAccessibleName(el);
                    return {
                        index: idx,
                        tag: isContentEditable ? "contenteditable" : tag,
                        type: isContentEditable ? "richtext" : (el.getAttribute("type") || tag).toLowerCase(),
                        accessible_name: accName,
                        label: accName,
                        placeholder: el.getAttribute("placeholder") || el.getAttribute("data-placeholder") || "",
                        role: el.getAttribute("role") || (isContentEditable ? "textbox" : tag),
                        name: el.getAttribute("name") || el.id || "",
                        value: isContentEditable ? (el.innerText || "").trim().slice(0, 120) : (el.value || "").slice(0, 120),
                        required: Boolean(el.required || el.getAttribute("aria-required") === "true"),
                        disabled: Boolean(el.disabled || el.readOnly),
                        selector: cssPath(el),
                        form_selector: form ? cssPath(form) : null,
                        inline_button_selector: inlineBtn ? cssPath(inlineBtn) : null,
                        inline_button_text: inlineBtn ? getAccessibleName(inlineBtn) : null,
                        options,
                        visible: true
                    };
                });

                // 2. Forms
                const forms = Array.from(document.querySelectorAll("form")).filter(isVisible).map((form, idx) => {
                    const submitBtn = form.querySelector('button[type="submit"], input[type="submit"], button:not([type="button"])');
                    return {
                        index: idx,
                        selector: cssPath(form),
                        accessible_name: getAccessibleName(form),
                        submit_selector: submitBtn ? cssPath(submitBtn) : null,
                        submit_text: submitBtn ? getAccessibleName(submitBtn) : ""
                    };
                });

                // 3. Buttons
                const buttons = Array.from(
                    document.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"]')
                ).filter(isVisible).slice(0, 45).map((el, idx) => {
                    const parentItem = el.closest("li, tr, article, [data-id]");
                    const parentCollection = el.closest("ul, ol, tbody, section, main");
                    const text = (el.innerText || el.value || "").trim().slice(0, 80);
                    const accName = getAccessibleName(el) || text;
                    return {
                        index: idx,
                        tag: el.tagName.toLowerCase(),
                        text: text || accName,
                        accessible_name: accName,
                        role: el.getAttribute("role") || "button",
                        disabled: Boolean(el.disabled),
                        selector: cssPath(el),
                        in_collection_selector: parentCollection ? cssPath(parentCollection) : null,
                        item_context: parentItem ? (parentItem.querySelector("h1,h2,h3,h4,a,span,p,td")?.innerText || "").trim().slice(0, 80) : null
                    };
                });

                // 4. Links
                const links = Array.from(document.querySelectorAll("a[href]"))
                    .filter(isVisible)
                    .slice(0, 35)
                    .map((el, idx) => {
                        const text = (el.innerText || "").trim().slice(0, 80);
                        const accName = getAccessibleName(el) || text;
                        return {
                            index: idx,
                            text: text || accName,
                            accessible_name: accName,
                            href: el.getAttribute("href") || "",
                            selector: cssPath(el)
                        };
                    });

                // 5. Lists / Collections
                const collections = [];
                Array.from(document.querySelectorAll("ul, ol, tbody, [role='list'], [role='table'], [role='grid']")).forEach((container) => {
                    if (!isVisible(container) && container.children.length === 0) return;
                    let prev = container.previousElementSibling;
                    let heading = container.getAttribute("aria-label") || "";
                    while (prev && !heading) {
                        if (/^H[1-6]$/.test(prev.tagName)) {
                            heading = prev.innerText.trim();
                            break;
                        }
                        prev = prev.previousElementSibling;
                    }
                    const childSelector = container.tagName === "TBODY" ? ":scope > tr" : ":scope > li, :scope > article, :scope > div";
                    const children = Array.from(container.querySelectorAll(childSelector)).filter(isVisible);
                    const items = children.slice(0, 25).map((item) => {
                        const titleEl = item.querySelector("h1, h2, h3, h4, a, strong, span, td");
                        const itemBtns = Array.from(item.querySelectorAll("button, a[role='button'], [role='button'], input[type='checkbox']")).map(b => (b.innerText || b.getAttribute("aria-label") || b.type || "").trim()).filter(Boolean);
                        const rawText = (titleEl ? titleEl.innerText : item.innerText || "").trim().replace(/\\s+/g, " ").slice(0, 120);
                        return {
                            id: item.id || item.getAttribute("data-id") || "",
                            text: rawText,
                            buttons: itemBtns
                        };
                    }).filter(i => i.text || i.buttons.length > 0);

                    collections.push({
                        heading: heading || cssPath(container),
                        selector: cssPath(container),
                        item_count: items.length,
                        items: items
                    });
                });

                // 6. Headings & Status Banners
                const headings = Array.from(document.querySelectorAll("h1, h2, h3, [role='status'], [role='alert'], .alert, .error, .toast, .auth-notice, .login-required"))
                    .filter(isVisible)
                    .slice(0, 20)
                    .map(h => ({
                        tag: h.tagName.toLowerCase(),
                        role: h.getAttribute("role") || "",
                        selector: cssPath(h),
                        text: h.innerText.trim().slice(0, 140)
                    }));

                const bodyTextSample = (document.body ? document.body.innerText : "").replace(/\\s+/g, " ").trim().slice(0, 1500);

                // 7. Authentication / Login Gate Detection
                const passwordInputs = inputs.filter(i => i.type === "password" || /비밀번호|암호|password|passwd/i.test(i.label + " " + i.placeholder + " " + i.name));
                const userInputs = inputs.filter(i => i !== passwordInputs[0] && /아이디|이메일|계정|사용자|username|userid|email|login/i.test(i.label + " " + i.placeholder + " " + i.name));
                const loginButtons = buttons.filter(b => /로그인|sign in|log in|인증/i.test(b.text));
                const logoutButtons = buttons.filter(b => /로그아웃|sign out|log out/i.test(b.text));
                const explicitAuthGate = Boolean(document.querySelector('[data-requires-auth="true"]:not([data-authenticated="true"]), #loginGate:not(.hidden), .login-wall:not(.hidden)'));

                const loginWallDetected = (passwordInputs.length > 0 && (userInputs.length > 0 || loginButtons.length > 0)) || explicitAuthGate;

                const authState = {
                    login_wall_detected: loginWallDetected && logoutButtons.length === 0,
                    is_authenticated: logoutButtons.length > 0 || Boolean(document.querySelector('[data-authenticated="true"]')),
                    has_password_field: passwordInputs.length > 0,
                    username_selector: userInputs[0] ? userInputs[0].selector : (inputs[0] && passwordInputs[0] && inputs[0] !== passwordInputs[0] ? inputs[0].selector : null),
                    password_selector: passwordInputs[0] ? passwordInputs[0].selector : null,
                    login_button_selector: loginButtons[0] ? loginButtons[0].selector : null,
                    login_button_text: loginButtons[0] ? loginButtons[0].text : "로그인",
                };

                // 8. Script hints (inline & external same-origin scripts)
                const dynamicHints = [];
                const inlineScripts = Array.from(document.querySelectorAll("script:not([src])")).map(s => s.innerText || "").filter(Boolean);
                for (const code of inlineScripts.slice(0, 4)) {
                    if (code.includes("createElement") || code.includes("addEventListener") || code.includes("innerHTML")) {
                        dynamicHints.push({ src: "inline", snippet: code.slice(0, 2500) });
                    }
                }
                const scriptSrcs = Array.from(document.querySelectorAll("script[src]")).map(s => s.getAttribute("src")).filter(Boolean);
                for (const src of scriptSrcs.slice(0, 4)) {
                    try {
                        const res = await fetch(src);
                        if (res.ok) {
                            const code = await res.text();
                            if (code.includes("createElement") || code.includes("addEventListener") || code.includes("submit")) {
                                dynamicHints.push({ src, snippet: code.slice(0, 2500) });
                            }
                        }
                    } catch (e) {}
                }

                return {
                    url: window.location.href,
                    normalized_url: window.location.origin + window.location.pathname,
                    title: document.title,
                    forms,
                    inputs,
                    buttons,
                    links,
                    lists: collections,
                    headings,
                    bodyTextSample,
                    bodyText: (document.body ? document.body.innerText : "").slice(0, 24000),
                    authState,
                    dynamicHints
                };
            }"""
        )
        if dom_snapshot and dom_snapshot.get("url"):
            self.current_url = dom_snapshot["url"]
            dom_snapshot["normalized_url"] = URLMatcher.normalize_url(self.current_url)

        screenshot = self.capture_screenshot()
        return {
            "snapshot": dom_snapshot,
            "screenshot": screenshot,
        }

    def perform_direct_login(self, username: str, password: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Executes login using credentials provided by the user via Human-in-the-Loop input.
        """
        if not snapshot:
            snapshot = self.inspect_page()["snapshot"]
        auth = snapshot.get("authState", {})
        user_sel = auth.get("username_selector")
        pass_sel = auth.get("password_selector")
        btn_sel = auth.get("login_button_selector")
        btn_txt = auth.get("login_button_text") or "로그인"

        steps: List[Dict[str, Any]] = []
        if user_sel and username:
            steps.append({"op": "fill", "selector": user_sel, "value": username})
        if pass_sel and password:
            steps.append({"op": "fill", "selector": pass_sel, "value": password})
        if btn_sel:
            steps.append({"op": "click", "selector": btn_sel, "button_text": btn_txt})
        elif pass_sel:
            steps.append({"op": "submit", "selector": pass_sel})

        login_tool = {
            "name": "__hitl_login__",
            "display_name": "사이트 로그인 실행",
            "steps": steps,
            "verification": {"type": "dom_change", "description": "로그인 성공 및 인증 상태 전환 확인"},
        }
        return self.execute_tool(login_tool, {"username": username, "password": password})

    def execute_tool(self, tool_def: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes a declarative Skill Tool against the active browser page and verifies DOM changes.
        Also enforces URL matching if `bound_normalized_url` is set on the tool definition.
        """
        self.last_dialog_message = None
        before_info = self.inspect_page()
        before_snapshot = before_info["snapshot"]

        # Strict URL check at tool execution boundary
        bound_url = tool_def.get("bound_normalized_url")
        current_norm_url = URLMatcher.normalize_url(before_snapshot.get("url", self.current_url))
        if bound_url and current_norm_url and bound_url != current_norm_url:
            return {
                "action_result": {
                    "success": False,
                    "failure_code": "URL_MISMATCH_BLOCKED",
                    "failure_reason": (
                        f"URL 불일치로 Skill 호출이 차단되었습니다. "
                        f"Skill 대상 URL: {bound_url} / 현재 브라우저 URL: {current_norm_url}"
                    ),
                    "message": "URL 불일치 차단",
                },
                "verification": {
                    "verified": False,
                    "state_changed": False,
                    "changes": [],
                    "failure_code": "URL_MISMATCH_BLOCKED",
                    "failure_reason": f"Target URL({bound_url}) != Browser URL({current_norm_url})",
                },
                "before_snapshot": before_snapshot,
                "after_snapshot": before_snapshot,
                "screenshot": before_info["screenshot"],
            }

        steps = tool_def.get("steps") or [{"op": "read_state"}]
        verification_spec = tool_def.get("verification") or {}

        step_logs: List[str] = []
        all_ok = True
        failure_code: Optional[str] = None
        failure_reason: Optional[str] = None
        has_mutating_op = False

        for step in steps:
            live_url=self.evaluate_js('() => window.location.href')
            if bound_url and not URLMatcher.is_exact_url_match(live_url,bound_url):
                all_ok=False
                failure_code='URL_MISMATCH_BLOCKED'
                failure_reason='페이지가 이동되어 이전 URL의 도구 실행을 중단했습니다.'
                break
            raw_op = (step.get("op") or step.get("action_type") or "read_state").lower()
            if raw_op not in ALLOWED_PRIMITIVES:
                all_ok = False
                failure_code = "INVALID_PRIMITIVE"
                failure_reason = f"허용되지 않은 브라우저 동작입니다: {raw_op}"
                step_logs.append(failure_reason)
                break

            selector = _interpolate(step.get("selector") or step.get("input_selector") or "", args)
            value = _interpolate(step.get("value", ""), args)
            if not value and step.get("value_param"):
                value = str(args.get(step["value_param"], ""))

            if raw_op in ("read", "read_state", "extract"):
                target_desc = step.get("description") or selector or "현재 화면 요소 및 데이터 목록"
                step_logs.append(f"[읽기] {target_desc} 조회 완료")

            elif raw_op == "wait":
                sec = float(step.get("seconds", 0.5))
                time.sleep(min(sec, 5.0))
                step_logs.append(f"[대기] {sec}초 대기")

            elif raw_op == "navigate":
                target_url = _interpolate(step.get("url") or step.get("value") or "", args)
                if target_url:
                    self.navigate(target_url)
                    step_logs.append(f"[이동] `{target_url}`")

            elif raw_op in ("fill", "select"):
                has_mutating_op = True
                if raw_op == "select":
                    res = self.evaluate_js(
                        """(p) => {
                            const el = document.querySelector(p.selector);
                            if (!el) return { ok: false, code: "ELEMENT_NOT_FOUND", error: `선택 입력창을 찾지 못했습니다 (${p.selector})` };
                            if (el.disabled || el.readOnly) return { ok: false, code: "PERMISSION_DENIED", error: `선택 입력창이 비활성화(권한/로그인 필요) 상태입니다 (${p.selector})` };
                            const opts = Array.from(el.options || []);
                            const match = opts.find(o => o.value === p.value || o.text.trim() === p.value || o.text.includes(p.value));
                            el.value = match ? match.value : p.value;
                            el.dispatchEvent(new Event("change", { bubbles: true }));
                            return { ok: true, selected: el.value };
                        }""",
                        {"selector": selector, "value": value},
                    )
                    if res.get("ok"):
                        step_logs.append(f"[선택] `{selector}` → `{res.get('selected')}`")
                    else:
                        all_ok = False
                        failure_code = res.get("code", "ELEMENT_NOT_FOUND")
                        failure_reason = res.get("error", f"선택 요소를 찾지 못했습니다: {selector}")
                        step_logs.append(failure_reason)
                        break
                else:
                    res = self.evaluate_js(
                        """(p) => {
                            let el = p.selector ? document.querySelector(p.selector) : null;
                            if (!el && p.fallback_placeholder) {
                                el = Array.from(document.querySelectorAll("input, textarea, [contenteditable='true']"))
                                    .find(n => (n.getAttribute("placeholder") || n.getAttribute("aria-label") || "").toLowerCase().includes(p.fallback_placeholder.toLowerCase()));
                            }
                            if (!el) return { ok: false, code: "ELEMENT_NOT_FOUND", error: `입력창을 찾지 못했습니다 (${p.selector || p.fallback_placeholder})` };
                            if (el.disabled || el.readOnly) return { ok: false, code: "PERMISSION_DENIED", error: `입력창이 비활성화되어 권한 또는 로그인이 필요합니다 (${p.selector})` };
                            el.focus();
                            if (el.isContentEditable || el.getAttribute("contenteditable") === "true") {
                                el.innerHTML = `<p>${p.value}</p>`;
                                el.dispatchEvent(new InputEvent("input", { bubbles: true, data: p.value }));
                                el.dispatchEvent(new Event("change", { bubbles: true }));
                            } else if (el.tagName === "SELECT") {
                                const opts = Array.from(el.options || []);
                                const match = opts.find(o => o.value === p.value || o.text.trim() === p.value || o.text.includes(p.value));
                                el.value = match ? match.value : p.value;
                                el.dispatchEvent(new Event("change", { bubbles: true }));
                            } else {
                                const proto = el.tagName === "TEXTAREA"
                                    ? window.HTMLTextAreaElement.prototype
                                    : window.HTMLInputElement.prototype;
                                const nativeSetter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
                                if (nativeSetter) {
                                    nativeSetter.call(el, p.value);
                                } else {
                                    el.value = p.value;
                                }
                                el.dispatchEvent(new Event("input", { bubbles: true }));
                                el.dispatchEvent(new Event("change", { bubbles: true }));
                            }
                            return { ok: true };
                        }""",
                        {
                            "selector": selector,
                            "value": value,
                            "fallback_placeholder": step.get("placeholder", ""),
                        },
                    )
                    if res.get("ok"):
                        preview_val = "••••••" if "pass" in selector.lower() else (value if len(value) <= 40 else value[:37] + "...")
                        step_logs.append(f"[입력] `{selector}` ← `\"{preview_val}\"`")
                    else:
                        all_ok = False
                        failure_code = res.get("code", "ELEMENT_NOT_FOUND")
                        failure_reason = res.get("error", f"입력창을 찾지 못했습니다: {selector}")
                        step_logs.append(failure_reason)
                        break

            elif raw_op in ("submit", "click", "click_item_button"):
                has_mutating_op = True
                if raw_op == "submit":
                    form_sel = _interpolate(step.get("form_selector") or selector, args)
                    res = self.evaluate_js(
                        """(p) => {
                            const target = (p.form_sel ? document.querySelector(p.form_sel) : null) || (p.input_sel ? document.querySelector(p.input_sel) : null);
                            if (!target) return { ok: false, code: "ELEMENT_NOT_FOUND", error: `제출할 입력창/폼을 찾지 못했습니다 (${p.form_sel || p.input_sel})` };
                            const form = target.tagName === "FORM" ? target : target.closest("form");
                            if (form) {
                                const submitBtn = form.querySelector('button[type="submit"], input[type="submit"], button:not([type="button"])');
                                if (submitBtn) {
                                    if (submitBtn.disabled) return { ok: false, code: "PERMISSION_DENIED", error: "제출 버튼이 비활성화 상태입니다 (권한/로그인 확인 필요)." };
                                    submitBtn.click();
                                    return { ok: true, via: "submit_button" };
                                }
                                const ev = new Event("submit", { bubbles: true, cancelable: true });
                                form.dispatchEvent(ev);
                                return { ok: true, via: "form_submit_event" };
                            }
                            target.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
                            target.dispatchEvent(new KeyboardEvent("keypress", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
                            target.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
                            return { ok: true, via: "enter_key" };
                        }""",
                        {"form_sel": form_sel, "input_sel": selector},
                    )
                    if res.get("ok"):
                        step_logs.append(f"[제출] `{form_sel or selector}` 제출 완료")
                        time.sleep(0.35)
                    else:
                        all_ok = False
                        failure_code = res.get("code", "ELEMENT_NOT_FOUND")
                        failure_reason = res.get("error", f"제출 대상을 찾지 못했습니다: {form_sel or selector}")
                        step_logs.append(failure_reason)
                        break
                else:
                    list_selector = _interpolate(step.get("list_selector") or step.get("within_selector") or "", args)
                    match_text = _interpolate(step.get("item_text") or step.get("match_text") or "", args)
                    if not match_text and step.get("match_param"):
                        match_text = str(args.get(step["match_param"], ""))
                    button_text = _interpolate(step.get("button_text") or "", args)

                    if list_selector or (raw_op == "click_item_button"):
                        res = self.evaluate_js(
                            """(p) => {
                                const container = document.querySelector(p.list_selector || "ul, ol, tbody, main");
                                if (!container) return { ok: false, code: "ELEMENT_NOT_FOUND", error: `목록 영역을 찾지 못했습니다 (${p.list_selector})` };
                                const items = Array.from(container.querySelectorAll(":scope > li, :scope > tr, :scope > article, :scope > div"));
                                let matched = items.find(el => {
                                    const t = (el.querySelector("h1,h2,h3,h4,a,strong,span,p,td")?.innerText || el.innerText || "").trim();
                                    return t === p.match_text;
                                });
                                if (!matched && p.match_text) {
                                    matched = items.find(el => (el.innerText || "").includes(p.match_text));
                                }
                                if (!matched) {
                                    return { ok: false, code: "ELEMENT_NOT_FOUND", error: `목록에서 '${p.match_text}' 항목을 찾지 못했습니다.` };
                                }
                                const candidates = Array.from(matched.querySelectorAll("button, a, [role='button'], input[type='button'], input[type='checkbox']"));
                                const btn = p.button_text
                                    ? candidates.find(b => (b.innerText || b.value || b.getAttribute("aria-label") || "").trim().includes(p.button_text))
                                    : candidates[0] || matched;
                                if (!btn) return { ok: false, code: "ELEMENT_NOT_FOUND", error: `'${p.match_text}' 항목 내 '${p.button_text}' 버튼을 찾지 못했습니다.` };
                                if (btn.disabled) return { ok: false, code: "PERMISSION_DENIED", error: `버튼이 비활성화 상태입니다 (${p.button_text})` };
                                btn.click();
                                return { ok: true, clicked: (btn.innerText || p.button_text || btn.tagName).trim() };
                            }""",
                            {
                                "list_selector": list_selector or selector,
                                "match_text": match_text,
                                "button_text": button_text,
                            },
                        )
                    else:
                        res = self.evaluate_js(
                            """(p) => {
                                let el = null;
                                if (p.selector) {
                                    try { el = document.querySelector(p.selector); } catch (e) {}
                                }
                                if (!el && p.button_text) {
                                    const all = Array.from(document.querySelectorAll("button, a, input[type='submit'], input[type='button'], [role='button']"));
                                    el = all.find(b => (b.innerText || b.value || b.getAttribute("aria-label") || "").trim() === p.button_text.trim())
                                      || all.find(b => (b.innerText || b.value || b.getAttribute("aria-label") || "").trim().includes(p.button_text.trim()));
                                }
                                if (!el) return { ok: false, code: "ELEMENT_NOT_FOUND", error: `클릭할 버튼을 찾지 못했습니다 (${p.selector || p.button_text})` };
                                if (el.disabled) return { ok: false, code: "PERMISSION_DENIED", error: `클릭할 버튼이 비활성화되어 권한/로그인이 필요합니다 (${p.selector || p.button_text})` };
                                el.click();
                                return { ok: true, clicked: (el.innerText || el.value || el.getAttribute("aria-label") || p.selector || "").trim().slice(0, 40) };
                            }""",
                            {"selector": selector, "button_text": button_text or match_text},
                        )

                    if res.get("ok"):
                        step_logs.append(f"[클릭] `{res.get('clicked')}` 클릭 완료")
                        time.sleep(0.3)
                    else:
                        all_ok = False
                        failure_code = res.get("code", "ELEMENT_NOT_FOUND")
                        failure_reason = res.get("error", f"클릭할 요소를 찾지 못했습니다: {selector or button_text}")
                        step_logs.append(failure_reason)
                        break

            time.sleep(0.15)

        time.sleep(0.3)
        after_info = self.inspect_page()
        after_snapshot = after_info["snapshot"]
        verification = self._verify_execution_outcome(
            before=before_snapshot,
            after=after_snapshot,
            args=args,
            verification_spec=verification_spec,
            has_mutating_op=has_mutating_op,
            all_steps_ok=all_ok,
            failure_code=failure_code,
            failure_reason=failure_reason,
            dialog_message=self.last_dialog_message,
        )

        final_success = all_ok and verification["verified"]
        if not final_success and not failure_code:
            failure_code = verification.get("failure_code") or "VERIFICATION_FAILED"
            failure_reason = verification.get("failure_reason") or "페이지에서 실제 실행 결과를 확인하지 못했습니다."

        return {
            "action_result": {
                "success": final_success,
                "failure_code": None if final_success else failure_code,
                "failure_reason": None if final_success else failure_reason,
                "message": " → ".join(step_logs) if step_logs else "실행 완료",
                "dialog_message": self.last_dialog_message,
            },
            "verification": verification,
            "before_snapshot": before_snapshot,
            "after_snapshot": after_snapshot,
            "screenshot": after_info["screenshot"],
        }

    def _verify_execution_outcome(
        self,
        before: Dict[str, Any],
        after: Dict[str, Any],
        args: Dict[str, Any],
        verification_spec: Dict[str, Any],
        has_mutating_op: bool,
        all_steps_ok: bool,
        failure_code: Optional[str],
        failure_reason: Optional[str],
        dialog_message: Optional[str],
    ) -> Dict[str, Any]:
        changes: List[str] = []
        if before.get("url") != after.get("url"):
            changes.append(f"[URL 이동] {before.get('url')} → {after.get('url')}")

        before_lists = {
            (lst.get("heading") or lst.get("selector")): [i["text"] for i in lst.get("items", [])]
            for lst in before.get("lists", [])
        }
        after_lists = {
            (lst.get("heading") or lst.get("selector")): [i["text"] for i in lst.get("items", [])]
            for lst in after.get("lists", [])
        }
        for key, after_items in after_lists.items():
            b_items = before_lists.get(key, [])
            if b_items != after_items:
                changes.append(f"[목록 변화: {key}] {len(b_items)}개 → {len(after_items)}개 ({after_items[:5]})")

        before_headings = [h["text"] for h in before.get("headings", [])]
        after_headings = [h["text"] for h in after.get("headings", [])]
        if before_headings != after_headings:
            changes.append(f"[화면 상태/배너 변화] {before_headings} → {after_headings}")

        before_auth = before.get("authState", {}).get("is_authenticated", False)
        after_auth = after.get("authState", {}).get("is_authenticated", False)
        if before_auth != after_auth:
            changes.append(f"[인증 상태 변화] 로그인 여부: {before_auth} → {after_auth}")

        before_body = before.get("bodyTextSample", "")
        after_body = after.get("bodyTextSample", "")

        # 1. Check for login/permission errors in JS alert or newly appeared status banners
        perm_keywords = [
            "권한이 없",
            "로그인이 필요",
            "로그인 후",
            "로그인해",
            "인증이 필요",
            "비밀번호가 일치하지",
            "로그인 실패",
            "unauthorized",
            "forbidden",
            "access denied",
            "permission denied",
            "login required",
        ]
        new_headings = [h["text"] for h in after.get("headings", []) if h["text"] not in before_headings]
        combined_notice = " ".join([dialog_message or ""] + new_headings)
        if any(pk in combined_notice.lower() for pk in perm_keywords):
            return {
                "verified": False,
                "state_changed": len(changes) > 0,
                "changes": changes,
                "failure_code": "LOGIN_REQUIRED",
                "failure_reason": f"로그인이 필요하거나 권한이 없습니다: {combined_notice.strip()[:140]}",
                "current_lists": after_lists,
            }

        # 2. If a primitive step failed directly
        if not all_steps_ok:
            # Check if element wasn't found because a login wall is blocking the page
            if after.get("authState", {}).get("login_wall_detected"):
                return {
                    "verified": False,
                    "state_changed": False,
                    "changes": changes,
                    "failure_code": "LOGIN_REQUIRED",
                    "failure_reason": "현재 페이지에 로그인이 필요하여 요청한 기능 요소에 접근할 수 없습니다.",
                    "current_lists": after_lists,
                }
            return {
                "verified": False,
                "state_changed": len(changes) > 0,
                "changes": changes,
                "failure_code": failure_code or "ELEMENT_NOT_FOUND",
                "failure_reason": failure_reason or "요청한 화면 요소를 찾지 못했습니다.",
                "current_lists": after_lists,
            }

        # 3. Read-only tool verification
        v_type = (verification_spec.get("type") or ("dom_change" if has_mutating_op else "read_only")).lower()
        if not has_mutating_op or v_type == "read_only":
            return {
                "verified": True,
                "state_changed": len(changes) > 0,
                "changes": changes or [f"[읽기 확인] 컬렉션 {len(after_lists)}개, 입력창 {len(after.get('inputs', []))}개 확인"],
                "failure_code": None,
                "failure_reason": None,
                "current_lists": after_lists,
            }

        # 4. Mutating tool verification
        arg_values = [str(v).strip() for k, v in args.items() if v is not None and str(v).strip() and "pass" not in k.lower()]
        matched_args = [
            val
            for val in arg_values
            if val in after_body or any(val in item_txt for items in after_lists.values() for item_txt in items)
        ]
        if matched_args and before_body != after_body:
            changes.append(f"[반영 확인] 화면에서 입력값 확인됨: {matched_args}")

        verified = len(changes) > 0 or bool(matched_args)
        if not verified:
            if after.get("authState", {}).get("login_wall_detected"):
                return {
                    "verified": False,
                    "state_changed": False,
                    "changes": [],
                    "failure_code": "LOGIN_REQUIRED",
                    "failure_reason": "사이트 로그인이 완료되지 않아 작업이 반영되지 않았습니다.",
                    "current_lists": after_lists,
                }
            return {
                "verified": False,
                "state_changed": False,
                "changes": [],
                "failure_code": "VERIFICATION_FAILED",
                "failure_reason": "입력/클릭 동작을 수행했으나 페이지에서 실제 생성·변경 결과를 확인하지 못했습니다.",
                "current_lists": after_lists,
            }

        return {
            "verified": True,
            "state_changed": True,
            "changes": changes,
            "failure_code": None,
            "failure_reason": None,
            "current_lists": after_lists,
        }

    def close(self) -> None:
        with self._lock:
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass
                self.ws = None
            if self.proc:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=3)
                except Exception:
                    pass
                self.proc = None
