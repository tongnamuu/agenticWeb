"""Single-site worker used by the cross-site root and CLI."""
from agentic_web.engine.schemas import Synthesis, Decision, Verdict
from agentic_web.engine.skills.registry import SkillRegistry, URLMatcher

SYNTHESIZE = '''Analyze visible page controls and create reusable tools for this URL and task.
Use declarative Playwright selectors and allowed steps only, never Python/JS code.
Parameters are strings referenced with {{parameter_name}} in selector/value.
Parameterize task-specific values. Create separate tools for meaningful capabilities.
Every tool remains on its exact bound URL. Do not create authentication tools.
Correct existing tools by name and preserve unrelated capabilities.
'''


class RootAgent:
    def __init__(self, model, browser, registry=None, human=None):
        self.model, self.browser = model, browser
        self.registry = registry if registry is not None else SkillRegistry()
        self.human = human or self.console_input
        self.events, self.context = [], []

    @staticmethod
    def console_input(kind, message):
        print(f'\n[{kind}] {message}')
        if kind == 'login':
            print('열린 브라우저에서 직접 로그인/CAPTCHA를 완료하세요. 비밀번호를 입력하지 마세요.')
        return input('완료 후 Enter / 필요한 답변 입력 (취소: /cancel): ')

    def request_input(self, kind, message):
        answer = self.human(kind, message)
        if answer is None or answer.strip() == '/cancel':
            raise InterruptedError('사용자가 작업을 취소했습니다.')
        self.context.append({'kind': kind, 'answer': answer if kind != 'login' else 'Manual login completed'})

    def synthesize(self, url, task, snapshot, repair=False):
        existing, _ = self.registry.resolve_skill_by_url(url)
        result = self.model.ask('skill_synthesizer', SYNTHESIZE,
            {'task': task, 'page': snapshot, 'existing_tools': existing['tools'] if existing else [],
             'history': self.events, 'user_context': self.context}, Synthesis)
        return self.registry.register_skill(url, [t.model_dump() for t in result.tools],
            site_title=snapshot['title'], synthesized_by=self.model.last_model, is_repair=repair)

    def verify(self, task, before):
        return self.model.ask('root_verifier',
            'Independently verify ALL requirements of this subtask using observable current state. '
            'Tool success is insufficient. Include concrete evidence and actual output titles/IDs/URLs '
            'needed by later workflow steps. Use input/login if human action is needed.',
            {'task': task, 'before': before, 'after': self.browser.snapshot(),
             'history': self.events, 'user_context': self.context}, Verdict)

    def run(self, url, task, workflow_context=None):
        self.events, self.context = [], []
        if workflow_context:
            self.context.append({'workflow': workflow_context})
        attempt = 0
        try:
            self.browser.open(URLMatcher.normalize_url(url))
            before = self.browser.snapshot()
            for attempt in (1, 2):
                for _ in range(30):
                    page = self.browser.snapshot()
                    if page.get('password_visible'):
                        self.request_input('login', '로그인이 필요한 페이지입니다.')
                        continue
                    active_url = page['url']
                    tools, package, routing = self.registry.get_tools_strictly_for_url(active_url)
                    self.events.append({'routing': routing, 'attempt': attempt})
                    if not tools:
                        tools = self.synthesize(active_url, task, page)['tools']
                    decision = self.model.ask('skill_executor',
                        'Choose ONE available tool to advance the subtask, or done for independent verification. '
                        'Use input/login for missing information/authentication, expand for missing capabilities. '
                        'Never repeat completed mutations. Arguments must exactly match tool parameters.',
                        {'task': task, 'page': page, 'tools': tools, 'history': self.events[-20:],
                         'user_context': self.context}, Decision)
                    if decision.kind in ('login', 'input'):
                        self.request_input(decision.kind, decision.message)
                        continue
                    if decision.kind == 'expand':
                        self.synthesize(active_url, task, page, repair=True)
                        continue
                    if decision.kind == 'done':
                        break
                    try:
                        tool = next((t for t in tools if t['name'] == decision.tool_name), None)
                        if tool is None:
                            raise ValueError('Unregistered tool')
                        result = self.browser.execute_tool(tool, {a.name: a.value for a in decision.arguments})
                        self.registry.increment_usage(active_url)
                        self.events.append({'tool': tool['name'], 'result': result})
                    except Exception as exc:
                        self.events.append({'tool_error': str(exc)})
                        break
                verdict = self.verify(task, before)
                for _ in range(5):
                    if verdict.status not in ('login', 'input'):
                        break
                    self.request_input(verdict.status, verdict.reason)
                    verdict = self.verify(task, before)
                self.events.append({'verification': verdict.model_dump(), 'attempt': attempt})
                if verdict.status == 'success':
                    return self.result('success', attempt, verdict.reason, verdict.evidence)
                if attempt == 1:
                    page = self.browser.snapshot()
                    self.synthesize(page['url'], task, page, repair=True)
            return self.result('failure', attempt, verdict.reason, verdict.evidence)
        except InterruptedError as exc:
            return self.result('cancelled', attempt, str(exc))
        except Exception as exc:
            return self.result('failure', attempt, str(exc))

    def result(self, status, attempts, reason, evidence=None):
        return {'status': status, 'attempts': attempts, 'reason': reason,
                'evidence': evidence or [], 'url': self.browser.current_url, 'events': self.events}
