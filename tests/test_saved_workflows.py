import tempfile
import unittest
from agentic_web.engine.workflows.saved import SavedWorkflows
from agentic_web.engine.skills.registry import SkillRegistry
from agentic_web.engine.schemas import Tool, Step

A, B = 'https://todo.test/', 'https://blog.test/'


class Browser:
    current_url = ''
    def __init__(self): self.calls = []
    def navigate(self, url): self.current_url = url
    def execute_tool(self, tool, arguments):
        self.calls.append((self.current_url, tool['name'], arguments))
        return {'action_result': {'success': True}, 'verification': {},
                'before_snapshot': {}, 'after_snapshot': {}}


class Verifier:
    def __init__(self, fail_at=None): self.count, self.fail_at = 0, fail_at
    def verify_execution(self, **kwargs):
        self.count += 1
        return {'success': self.count != self.fail_at, 'reason': 'verified'}


class SavedWorkflowTests(unittest.TestCase):
    def setup_service(self, directory, verifier=None):
        registry = SkillRegistry(directory + '/skills')
        for url, name in [(A, 'add'), (A, 'complete'), (B, 'write')]:
            registry.register_skill(url, [Tool(name=name, description=name,
                steps=[Step(action='click', selector='#'+name)]).model_dump()])
        browser = Browser()
        service = SavedWorkflows(registry, browser, verifier or Verifier(), directory + '/workflows')
        return service, browser

    def data(self):
        return {'name': '블로그 작성', 'steps': [
            {'url': A, 'tool_name': 'add', 'arguments': {}},
            {'url': B, 'tool_name': 'write', 'arguments': {}},
            {'url': A, 'tool_name': 'complete', 'arguments': {}}]}

    def test_save_reload_and_cross_site_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            service, browser = self.setup_service(directory)
            saved = service.save(self.data())
            reloaded = SavedWorkflows(service.registry, browser, service.verifier, directory+'/workflows')
            self.assertEqual(reloaded.list()[0]['name'], '블로그 작성')
            result = reloaded.run(saved['id'])
            self.assertEqual(result['status'], 'SUCCESS')
            self.assertEqual([(url, name) for url, name, _ in browser.calls], [(A,'add'), (B,'write'), (A,'complete')])

    def test_verification_failure_stops_dependent_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            service, browser = self.setup_service(directory, Verifier(fail_at=2))
            result = service.run(service.save(self.data())['id'])
            self.assertEqual(result['status'], 'FAILED')
            self.assertEqual(len(browser.calls), 2)

    def test_wrong_site_tool_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.setup_service(directory)
            data = self.data(); data['steps'][0]['url'] = B
            with self.assertRaises(ValueError): service.save(data)

    def test_changed_skill_blocks_run_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            service, browser = self.setup_service(directory)
            saved = service.save(self.data())
            service.registry.register_skill(B, [Tool(name='write', description='changed',
                steps=[Step(action='click', selector='#new')]).model_dump()])
            with self.assertRaises(ValueError): service.run(saved['id'])
            self.assertEqual(browser.calls, [])

    def test_login_resume_does_not_repeat_completed_steps(self):
        class LoginVerifier(Verifier):
            def verify_execution(self, **kwargs):
                self.count += 1
                return {'success': self.count != 2, 'status': 'NEEDS_USER_INPUT' if self.count == 2 else 'SUCCESS'}
        with tempfile.TemporaryDirectory() as directory:
            service, browser = self.setup_service(directory, LoginVerifier())
            saved = service.save(self.data())
            result = service.run(saved['id'])
            self.assertEqual(result['status'], 'NEEDS_USER_INPUT')
            resumed = service.run(saved['id'], result['run_id'])
            self.assertEqual(resumed['status'], 'SUCCESS')
            self.assertEqual([name for _, name, _ in browser.calls], ['add','write','write','complete'])

    def test_web_studio_tool_schema_and_required_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            service, browser = self.setup_service(directory)
            service.registry.register_skill(B, [{'name':'publish', 'description':'publish',
                'parameters': {'type':'object','properties':{'title':{'type':'string'}},'required':['title']},
                'steps':[{'op':'fill','selector':'#title','value':'{{title}}'}]}])
            data = {'name':'글 쓰기', 'steps':[{'url':B,'tool_name':'publish','arguments':{}}]}
            with self.assertRaises(ValueError): service.save(data)
            data['steps'][0]['arguments'] = {'title':'글 제목'}
            result = service.run(service.save(data)['id'])
            self.assertEqual(result['status'], 'SUCCESS')
