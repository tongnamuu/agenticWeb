"""Saved, user-selected URL tools with ordered execution and resumable input gates."""
import copy
import hashlib
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4
from agentic_web.config import WORKFLOWS_STORE_DIR
from agentic_web.engine.skills.registry import URLMatcher, atomic_json
from agentic_web.engine.browser.auth import requires_login
from agentic_web.engine.storage import (
    FIRESTORE_WORKFLOWS_COLLECTION,
    ScopedFirestoreCollection,
    resolve_store_scope,
)


def fingerprint(tool):
    return hashlib.sha256(json.dumps(tool, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_arguments(tool, arguments):
    if not isinstance(arguments, dict):
        raise ValueError('단계 입력값은 JSON 객체여야 합니다.')
    schema = tool.get('parameters') or {}
    if isinstance(schema, list):
        props = {p['name']: {'type': 'string'} for p in schema}
        required = list(props)
    else:
        props, required = schema.get('properties', {}), schema.get('required', [])
    missing = [key for key in required if key not in arguments or arguments[key] is None or isinstance(arguments[key], str) and not arguments[key].strip()]
    if missing:
        raise ValueError('필수 입력값: ' + ', '.join(missing))
    if set(arguments) - set(props):
        raise ValueError('정의되지 않은 입력값이 있습니다.')
    types = {'string': str, 'integer': int, 'number': (int, float), 'boolean': bool,
             'object': dict, 'array': list}
    for key, value in arguments.items():
        definition = props[key]
        kind = definition.get('type', 'string')
        expected = types.get(kind)
        if expected and (not isinstance(value, expected) or kind in ('integer', 'number') and isinstance(value, bool)):
            raise ValueError(f'{key}: {kind} 형식으로 입력하세요.')
        if 'enum' in definition and value not in definition['enum']:
            raise ValueError(f'{key}: 허용되지 않은 값입니다.')


class SavedWorkflows:
    def __init__(self, registry, browser, verifier, directory=None, use_firestore=None):
        self.registry, self.browser, self.verifier = registry, browser, verifier
        self.directory = Path(directory or WORKFLOWS_STORE_DIR)
        self.directory.mkdir(parents=True, exist_ok=True)
        scope = resolve_store_scope(str(self.directory), WORKFLOWS_STORE_DIR)
        self._firestore = ScopedFirestoreCollection(
            FIRESTORE_WORKFLOWS_COLLECTION,
            scope=scope,
            use_firestore=use_firestore,
        )
        self.lock = Lock()
        self.runs = {}

    def tool(self, url, name):
        tools, package, _ = self.registry.get_tools_strictly_for_url(url)
        tool = next((t for t in tools if t['name'] == name), None)
        if not package or not tool:
            raise ValueError('등록되지 않은 URL 또는 스킬입니다: ' + name)
        return tool

    def list(self):
        by_id = {}
        for p in sorted(self.directory.glob('*.json')):
            try:
                wf = json.loads(p.read_text(encoding='utf-8'))
                if isinstance(wf, dict) and wf.get('id'):
                    by_id[wf['id']] = wf
            except Exception:
                pass
        if self._firestore.enabled:
            for wf in self._firestore.list_documents():
                wf_id = wf.get('id') if isinstance(wf, dict) else None
                if wf_id:
                    by_id[wf_id] = wf
                    try:
                        atomic_json(str(self.directory / (wf_id + '.json')), wf)
                    except Exception:
                        pass
        return [by_id[k] for k in sorted(by_id)]

    def get(self, identifier):
        if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            raise ValueError('잘못된 워크플로우 ID입니다.')
        if self._firestore.enabled:
            remote = self._firestore.get_document(identifier)
            if isinstance(remote, dict) and remote.get('id') == identifier:
                try:
                    atomic_json(str(self.directory / (identifier + '.json')), remote)
                except Exception:
                    pass
                return remote
        path = self.directory / (identifier + '.json')
        if not path.exists():
            raise ValueError('워크플로우를 찾을 수 없습니다.')
        wf = json.loads(path.read_text(encoding='utf-8'))
        if self._firestore.enabled:
            self._firestore.set_document(identifier, wf)
        return wf

    def save(self, data):
        name = str(data.get('name') or '').strip()
        steps = data.get('steps')
        if not name or not isinstance(steps, list) or not 1 <= len(steps) <= 30:
            raise ValueError('이름과 1~30개의 단계를 지정하세요.')
        prepared = []
        for step in steps:
            url = URLMatcher.normalize_url(step.get('url', ''))
            tool = self.tool(url, step.get('tool_name'))
            arguments = step.get('arguments', {})
            validate_arguments(tool, arguments)
            prepared.append({'url': url, 'tool_name': tool['name'], 'arguments': arguments,
                             'tool_fingerprint': fingerprint(tool)})
        identifier = data.get('id') or uuid4().hex
        if data.get('id'):
            self.get(identifier)
        workflow = {'id': identifier, 'name': name, 'steps': prepared}
        atomic_json(str(self.directory / (identifier + '.json')), workflow)
        self._firestore.set_document(identifier, workflow)
        return workflow

    def run(self, identifier, run_id=None, progress=None, on_update=None):
        notify = progress or (lambda message: None)
        if not self.lock.acquire(blocking=False):
            raise ValueError('다른 워크플로우가 실행 중입니다.')
        try:
            if run_id:
                run = self.runs.get(run_id)
                if not run or run['workflow']['id'] != identifier or run['status'] != 'NEEDS_USER_INPUT':
                    raise ValueError('재개할 실행을 찾을 수 없습니다.')
            else:
                workflow = self.get(identifier)
                # Validate every dependency before making the first mutation.
                for step in workflow['steps']:
                    if fingerprint(self.tool(step['url'], step['tool_name'])) != step['tool_fingerprint']:
                        raise ValueError('스킬이 변경되었습니다. 워크플로우를 다시 저장하세요.')
                run = {'run_id': uuid4().hex, 'workflow': copy.deepcopy(workflow), 'status': 'RUNNING',
                       'next_step': 0, 'results': [], 'message': ''}
                self.runs[run['run_id']] = run
            run['status'] = 'RUNNING'
            def report(phase, message):
                run.update(phase=phase, message=message)
                notify(message)
                if on_update:
                    on_update(copy.deepcopy({key: value for key, value in run.items() if key != 'pending_output'}))
            while run['next_step'] < len(run['workflow']['steps']):
                step = run['workflow']['steps'][run['next_step']]
                report('NAVIGATING', f"{run['next_step'] + 1}/{len(run['workflow']['steps'])}단계 · {step['tool_name']} 페이지 확인 중")
                tool = self.tool(step['url'], step['tool_name'])
                if fingerprint(tool) != step['tool_fingerprint']:
                    raise ValueError('스킬이 변경되어 실행을 중단했습니다. 다시 저장하세요.')
                if not URLMatcher.is_exact_url_match(self.browser.current_url, step['url']):
                    self.browser.navigate(step['url'])
                if not URLMatcher.is_exact_url_match(self.browser.current_url, step['url']):
                    run.update(status='NEEDS_USER_INPUT', message='페이지가 이동되었습니다. 브라우저에서 로그인 후 재개하세요.')
                    return run
                snapshot = self.browser.inspect_page()['snapshot']
                if requires_login(snapshot):
                    run.update(status='NEEDS_USER_INPUT', message='실행기 브라우저에서 로그인하면 현재 단계부터 재개합니다.')
                    return run
                output = run.pop('pending_output', None)
                for attempt in (1, 2):
                    if output is None or not output['action_result'].get('success'):
                        report('EXECUTING', f"{run['next_step'] + 1}단계 · {tool['name']} 브라우저 실행 중 ({attempt}차)")
                        output = self.browser.execute_tool(tool, step['arguments'])
                    record = {'tool_name': tool['name'], 'args': step['arguments'], 'operations': tool.get('steps', []),
                              'action_result': output['action_result'], 'verification': output['verification']}
                    report('VERIFYING', f"{run['next_step'] + 1}단계 · {tool['name']} 저장 결과 검증 중 ({attempt}차)")
                    verdict = self.verifier.verify_execution(
                        user_prompt=f"{tool['description']} 입력값: {json.dumps(step['arguments'], ensure_ascii=False)}",
                        before_snapshot=output['before_snapshot'], after_snapshot=output['after_snapshot'], steps=[record], browser_session=self.browser)
                    run['results'].append({'step': run['next_step'] + 1, 'url': step['url'],
                                           'tool_name': tool['name'], 'attempt': attempt, 'verification': verdict})
                    report('VERIFIED', f"{run['next_step'] + 1}단계 · 검증 결과: {'성공' if verdict.get('success') else '미확인'}")
                    if verdict.get('success'):
                        self.registry.increment_usage(step['url'])
                        break
                    if (verdict.get('status') == 'NEEDS_USER_INPUT' or
                        output['action_result'].get('failure_code') in ('LOGIN_REQUIRED', 'PERMISSION_DENIED')):
                        if output['action_result'].get('success'):
                            run['pending_output'] = output
                        run.update(status='NEEDS_USER_INPUT', message='브라우저에서 로그인 또는 필요한 입력을 완료한 후 재개하세요.')
                        return run
                    # A successful mutation with uncertain verification must not be blindly repeated.
                    if output['action_result'].get('success') and attempt == 2:
                        run.update(status='FAILED', message='변경 결과를 확인하지 못해 중복 실행을 중단했습니다.')
                        return run
                else:
                    run.update(status='FAILED', message=verdict.get('reason', '단계 검증 실패'))
                    return run
                run['next_step'] += 1
            run.update(status='SUCCESS', message='모든 단계를 순서대로 실행하고 검증했습니다.')
            return run
        except Exception:
            if 'run' in locals():
                run.update(status='FAILED', message='실행 중 오류가 발생했습니다. 사이트와 스킬 상태를 확인하세요.')
            raise
        finally:
            self.lock.release()
