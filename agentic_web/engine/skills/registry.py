import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from agentic_web.config import SKILLS_STORE_DIR
from agentic_web.engine.schemas import Tool
from agentic_web.engine.storage import (
    FIRESTORE_SKILLS_COLLECTION,
    ScopedFirestoreCollection,
    resolve_store_scope,
)


def is_user_tool(tool):
    name = tool.get('name', '').lower()
    if name.startswith(('open_page_', 'navigate_to_', 'login', 'sign_in', 'signup', 'register_account')):
        return False
    schema = tool.get('parameters') or {}
    params = schema.get('properties', {}) if isinstance(schema, dict) else {p['name']:p for p in schema}
    if any('password' in key.lower() for key in params):
        return False
    operations = [step.get('op', step.get('action')) for step in tool.get('steps', [])]
    return not (operations and 'navigate' in operations and all(op in ('navigate','wait','read','read_state') for op in operations))


def validate_tool(tool):
    # The web studio uses JSON Schema parameters and CDP op steps;
    # the Playwright worker uses parameter lists and action steps.
    if isinstance(tool.get('parameters'), dict):
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{0,63}', tool.get('name', '')):
            raise ValueError('Invalid tool name')
        steps = tool.get('steps')
        allowed = {'read', 'read_state', 'extract', 'fill', 'select', 'click',
                   'click_item_button', 'submit', 'navigate', 'wait'}
        if not isinstance(steps, list) or not steps or any(s.get('op') not in allowed for s in steps):
            raise ValueError('Invalid browser tool steps')
    else:
        Tool.model_validate(tool)


def atomic_json(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix='.writing-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class URLMatcher:
    """
    Strictly normalizes and compares URLs to determine whether a registered Skill
    should be invoked or if a new Skill must be synthesized for the URL.
    """

    @staticmethod
    def normalize_url(raw_url: str) -> str:
        """Canonicalize HTTP(S) origin only; preserve all route semantics exactly."""
        if not raw_url or not raw_url.strip():
            return ""
        u = raw_url.strip().split()[0]
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
            u = "https://" + u
        parsed = urlparse(u)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not hostname
            or (hostname != "localhost" and "." not in hostname and ":" not in hostname)
        ):
            raise ValueError("Only absolute HTTP(S) URLs are supported")
        if parsed.username or parsed.password:
            raise ValueError("Credentials must not be embedded in URLs")
        scheme = parsed.scheme.lower()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = parsed.port
        netloc = hostname if port is None or (scheme, port) in {("http", 80), ("https", 443)} else f"{hostname}:{port}"
        # Preserve path, params, query order/blank values, and hash routes exactly.
        return urlunparse((scheme, netloc, parsed.path or "/", parsed.params, parsed.query, parsed.fragment))

    @classmethod
    def is_exact_url_match(cls, url_a: str, url_b: str) -> bool:
        """Returns True iff both URLs normalize to the exact same canonical URL."""
        norm_a = cls.normalize_url(url_a)
        norm_b = cls.normalize_url(url_b)
        return bool(norm_a and norm_b and norm_a == norm_b)

    @classmethod
    def url_to_skill_id(cls, raw_url: str) -> str:
        """Generates a human-readable, filesystem-safe Skill ID for a normalized URL."""
        norm = cls.normalize_url(raw_url)
        if not norm:
            return "skill_unknown"
        parsed = urlparse(norm)
        host_slug = re.sub(r"[^a-z0-9]+", "_", (parsed.netloc or "local").lower()).strip("_")
        path_slug = re.sub(r"[^a-z0-9]+", "_", (parsed.path or "root").lower()).strip("_") or "root"
        short_hash = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
        return f"skill_{host_slug}_{path_slug}_{short_hash}"


class SkillRegistry:
    """
    Persistent & Dynamic Per-URL Skill Registry.
    - Stores synthesized Skills per normalized URL on disk (`skills_store/`).
    - Dynamically registers newly synthesized Skills into the Agent system as they are created.
    - Enforces strict URL-based routing: ONLY the Skill whose `normalized_url` matches the
      active target URL is activated for tool calling.
    """

    def __init__(
        self,
        store_dir: str = SKILLS_STORE_DIR,
        index_path: Optional[str] = None,
        use_firestore: Optional[bool] = None,
    ):
        self.store_dir = store_dir
        self.index_path = index_path or os.path.join(store_dir, "registry_index.json")
        os.makedirs(self.store_dir, exist_ok=True)
        scope = resolve_store_scope(self.store_dir, SKILLS_STORE_DIR)
        self._firestore = ScopedFirestoreCollection(
            FIRESTORE_SKILLS_COLLECTION,
            scope=scope,
            use_firestore=use_firestore,
        )
        # Map: normalized_url -> skill_package dict
        self._skills_by_url: Dict[str, Dict[str, Any]] = {}
        self._load_all_from_disk()

    def _ingest_skill_data(self, data: Dict[str, Any]) -> Optional[str]:
        norm_url = URLMatcher.normalize_url(data.get("target_url", ""))
        if norm_url and isinstance(data.get("tools"), list):
            data["tools"] = [tool for tool in data["tools"] if is_user_tool(tool)]
            for tool in data["tools"]:
                validate_tool(tool)
                tool["bound_normalized_url"] = norm_url
            data["normalized_url"] = norm_url
            self._skills_by_url[norm_url] = data
            return norm_url
        return None

    def _load_all_from_disk(self) -> None:
        self._skills_by_url.clear()
        if os.path.exists(self.store_dir):
            for fname in sorted(os.listdir(self.store_dir)):
                if not fname.endswith(".json") or fname == "registry_index.json":
                    continue
                fpath = os.path.join(self.store_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._ingest_skill_data(data)
                except Exception as e:
                    print(f"[SkillRegistry] Warning: Failed to load {fpath}: {e}")

        if self._firestore.enabled:
            remote_ids = set()
            for remote_pkg in self._firestore.list_documents():
                try:
                    norm_url = self._ingest_skill_data(remote_pkg)
                    skill_id = remote_pkg.get("skill_id") or (URLMatcher.url_to_skill_id(norm_url) if norm_url else "")
                    if norm_url and skill_id:
                        remote_ids.add(skill_id)
                        atomic_json(os.path.join(self.store_dir, f"{skill_id}.json"), remote_pkg)
                except Exception as e:
                    print(f"[SkillRegistry] Warning: Failed to ingest Firestore skill: {e}")

            for pkg in self._skills_by_url.values():
                sid = pkg.get("skill_id")
                if sid and sid not in remote_ids:
                    self._firestore.set_document(sid, pkg)

        self._save_index()

    def _save_index(self) -> None:
        index_Summary = {
            "total_skills": len(self._skills_by_url),
            "updated_at": datetime.now().isoformat(),
            "skills": [
                {
                    "skill_id": pkg.get("skill_id"),
                    "skill_name": pkg.get("skill_name"),
                    "target_url": pkg.get("target_url"),
                    "normalized_url": norm_url,
                    "tool_count": len(pkg.get("tools", [])),
                    "tool_names": [t.get("name") for t in pkg.get("tools", [])],
                    "version": pkg.get("version", 1),
                    "synthesized_by": pkg.get("synthesized_by", ""),
                    "updated_at": pkg.get("updated_at", ""),
                    "usage_count": pkg.get("usage_count", 0),
                }
                for norm_url, pkg in self._skills_by_url.items()
            ],
        }
        try:
            atomic_json(self.index_path, index_Summary)
        except Exception as e:
            print(f"[SkillRegistry] Warning: Failed to write index: {e}")

    def resolve_skill_by_url(self, target_url: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        Strictly checks whether a reusable Skill exists for `target_url`.
        Returns `(skill_package_or_None, normalized_url)`.
        """
        norm_url = URLMatcher.normalize_url(target_url)
        if not norm_url:
            return None, ""
        skill = self._skills_by_url.get(norm_url)
        return skill, norm_url

    def has_skill_for_url(self, target_url: str) -> bool:
        skill, _ = self.resolve_skill_by_url(target_url)
        return skill is not None and len(skill.get("tools", [])) > 0

    def register_skill(
        self,
        target_url: str,
        tools: List[Dict[str, Any]],
        site_title: str = "",
        synthesized_by: str = "gemini",
        skill_name: Optional[str] = None,
        description: Optional[str] = None,
        is_repair: bool = False,
    ) -> Dict[str, Any]:
        """
        Dynamically registers (or updates/repairs) a Skill Package for `target_url`
        and persists it to `skills_store/<skill_id>.json`.
        """
        tools = [tool for tool in tools if is_user_tool(tool)]
        norm_url = URLMatcher.normalize_url(target_url)
        if not norm_url or not tools:
            raise ValueError("A skill needs a valid URL and tools")
        for tool in tools:
            validate_tool(tool)
        if len({t["name"] for t in tools}) != len(tools):
            raise ValueError("Tool names must be unique")
        skill_id = URLMatcher.url_to_skill_id(norm_url)
        existing = self._skills_by_url.get(norm_url)
        now_iso = datetime.now().isoformat()

        # Renewal and repair update matching names while retaining capabilities absent from this page state.
        if existing:
            new_names = {t["name"] for t in tools}
            merged = {
                t["name"]: t
                for t in existing["tools"]
                if is_user_tool(t)
            }
            merged.update({t["name"]: t for t in tools})
            tools = list(merged.values())
        version = existing.get("version", 1) + 1 if existing else 1
        created_at = existing.get("created_at", now_iso) if existing else now_iso
        usage_count = existing.get("usage_count", 0) if existing else 0

        parsed = urlparse(norm_url)
        default_name = skill_name or (existing or {}).get("custom_name") or site_title or f"{parsed.netloc}{parsed.path} 전용 스킬"
        default_desc = description or f"URL [{norm_url}] 전용 브라우저 제어 스킬 패키지 ({len(tools)}개 Tool 포함)"

        # Stamp each tool with its bound normalized_url so it can never be called on a mismatched URL
        stamped_tools: List[Dict[str, Any]] = []
        for t in tools:
            t_copy = dict(t)
            t_copy["bound_normalized_url"] = norm_url
            t_copy["skill_id"] = skill_id
            stamped_tools.append(t_copy)

        skill_package: Dict[str, Any] = {
            "skill_id": skill_id,
            "skill_name": default_name,
            "custom_name": (existing or {}).get("custom_name"),
            "description": default_desc,
            "target_url": target_url,
            "normalized_url": norm_url,
            "site_title": site_title,
            "synthesized_by": synthesized_by,
            "version": version,
            "created_at": created_at,
            "updated_at": now_iso,
            "usage_count": usage_count,
            "tools": stamped_tools,
        }

        skill_file = os.path.join(self.store_dir, f"{skill_id}.json")
        atomic_json(skill_file, skill_package)
        self._skills_by_url[norm_url] = skill_package
        self._save_index()
        self._firestore.set_document(skill_id, skill_package)
        return skill_package

    def rename_skill(self, target_url: str, name: str) -> Dict[str, Any]:
        name = name.strip() if isinstance(name, str) else ''
        if not name or len(name) > 100:
            raise ValueError('이름은 1~100자로 입력하세요.')
        pkg, _ = self.resolve_skill_by_url(target_url)
        if pkg is None:
            raise KeyError('등록된 URL이 없습니다.')
        updated = dict(pkg, skill_name=name, custom_name=name, updated_at=datetime.now().isoformat())
        atomic_json(os.path.join(self.store_dir, f"{pkg['skill_id']}.json"), updated)
        self._skills_by_url[pkg['normalized_url']] = updated
        self._save_index()
        self._firestore.set_document(pkg['skill_id'], updated)
        return updated

    def increment_usage(self, target_url: str) -> None:
        norm_url = URLMatcher.normalize_url(target_url)
        pkg = self._skills_by_url.get(norm_url)
        if not pkg:
            return
        pkg["usage_count"] = pkg.get("usage_count", 0) + 1
        pkg["last_used_at"] = datetime.now().isoformat()
        skill_file = os.path.join(self.store_dir, f"{pkg['skill_id']}.json")
        try:
            atomic_json(skill_file, pkg)
            self._save_index()
        except Exception:
            pass
        self._firestore.set_document(pkg["skill_id"], pkg)

    def delete_skill_by_url(self, target_url: str) -> bool:
        norm_url = URLMatcher.normalize_url(target_url)
        pkg = self._skills_by_url.pop(norm_url, None)
        if not pkg:
            return False
        skill_file = os.path.join(self.store_dir, f"{pkg['skill_id']}.json")
        if os.path.exists(skill_file):
            try:
                os.remove(skill_file)
            except Exception:
                pass
        self._save_index()
        self._firestore.delete_document(pkg["skill_id"])
        return True

    def list_all_skills(self) -> List[Dict[str, Any]]:
        """Returns all registered URL Skills across the agent system."""
        return list(self._skills_by_url.values())

    def list_sites(self):
        groups = {}
        for package in self.list_all_skills():
            parsed = urlparse(package['normalized_url'])
            if re.search(r'(?:^|/)(?:login|signin|sign-in)(?:/|$)', parsed.path, re.I):
                continue
            origin = f'{parsed.scheme}://{parsed.netloc}'
            groups.setdefault(origin, []).append(package)
        sites = []
        for origin, packages in groups.items():
            packages.sort(key=lambda pkg: (len(urlparse(pkg['normalized_url']).path), pkg['normalized_url']))
            main = packages[0]
            name = next((pkg['custom_name'] for pkg in packages if pkg.get('custom_name')), main['skill_name'])
            sites.append({'origin':origin,'name':name,'representative_url':main['normalized_url'],
                          'packages':packages})
        return sites

    def get_tools_strictly_for_url(self, target_url: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
        """
        Strict URL Gatekeeper:
        Checks if `target_url` matches a registered Skill in the registry.
        Returns `(matched_tools, matched_skill_pkg, routing_decision_log)`.
        Tools belonging to other URLs are strictly excluded.
        """
        norm_target = URLMatcher.normalize_url(target_url)
        all_registered_urls = list(self._skills_by_url.keys())
        matched_pkg = self._skills_by_url.get(norm_target)

        if matched_pkg:
            routing_decision = {
                "url_matched": True,
                "Requested_url": target_url,
                "normalized_url": norm_target,
                "matched_skill_id": matched_pkg["skill_id"],
                "matched_skill_name": matched_pkg["skill_name"],
                "skill_version": matched_pkg.get("version", 1),
                "activated_tools": [t["name"] for t in matched_pkg.get("tools", [])],
                "isolated_other_skills_count": max(0, len(all_registered_urls) - 1),
                "reason": f"정확한 URL 매칭 성공 ({norm_target}) → 기존 등록된 Skill '{matched_pkg['skill_id']}' 재사용",
            }
            return matched_pkg.get("tools", []), matched_pkg, routing_decision

        routing_decision = {
            "url_matched": False,
            "Requested_url": target_url,
            "normalized_url": norm_target,
            "matched_skill_id": None,
            "matched_skill_name": None,
            "skill_version": 0,
            "activated_tools": [],
            "isolated_other_skills_count": len(all_registered_urls),
            "reason": f"등록된 Skill 중 URL ({norm_target})과 일치하는 항목 없음 → 페이지 분석 후 신규 Skill 생성 및 등록 필요",
        }
        return [], None, routing_decision

    def build_adk_callable_tools_for_url(
        self,
        target_url: str,
        browser_session: Any,
        execution_log_sink: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Callable]:
        """
        Converts the URL-matched Skill's declarative tools into live Python callables
        compatible with Google ADK (`LlmAgent(tools=[...])`) and Gemini Function Calling.
        Enforces a strict runtime URL check before executing each tool.
        """
        tools, skill_pkg, _ = self.get_tools_strictly_for_url(target_url)
        if not tools or not skill_pkg:
            return []

        bound_norm_url = skill_pkg["normalized_url"]
        callables: List[Callable] = []

        for tool_def in tools:
            fn_name = re.sub(r"[^a-zA-Z0-9_]", "_", tool_def.get("name", "tool"))
            fn_desc = tool_def.get("description", "Browser action tool")

            def _make_tool_fn(t_def: Dict[str, Any], expected_norm_url: str) -> Callable:
                def dynamic_skill_tool(**kwargs: Any) -> Dict[str, Any]:
                    current_norm = URLMatcher.normalize_url(browser_session.current_url)
                    if current_norm != expected_norm_url:
                        return {
                            "success": False,
                            "failure_code": "URL_MISMATCH_BLOCKED",
                            "failure_reason": (
                                f"Skill URL 불일치 차단: 도구 '{t_def['name']}'은(는) "
                                f"'{expected_norm_url}' 전용이지만 현재 브라우저 URL은 '{current_norm}'입니다."
                            ),
                        }
                    exec_out = browser_session.execute_tool(t_def, kwargs)
                    if execution_log_sink is not None:
                        execution_log_sink.append(
                            {
                                "tool_name": t_def["name"],
                                "display_name": t_def.get("display_name", t_def["name"]),
                                "args": kwargs,
                                "action_result": exec_out["action_result"],
                                "verification": exec_out["verification"],
                            }
                        )
                    return {
                        "result": exec_out["action_result"],
                        "verification": exec_out["verification"],
                    }

                dynamic_skill_tool.__name__ = re.sub(r"[^a-zA-Z0-9_]", "_", t_def.get("name", "tool"))
                dynamic_skill_tool.__doc__ = (
                    f"{t_def.get('description', '')}\n"
                    f"Bound strictly to URL: {expected_norm_url}\n"
                    f"Parameters: {json.dumps(t_def.get('parameters', {}), ensure_ascii=False)}"
                )
                return dynamic_skill_tool

            callables.append(_make_tool_fn(tool_def, bound_norm_url))

        return callables
