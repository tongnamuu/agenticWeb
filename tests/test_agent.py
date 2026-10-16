import tempfile
import unittest
from agentic_web.engine.agents.site import RootAgent
from agentic_web.engine.schemas import Decision, Verdict, Synthesis, Tool, Step
from agentic_web.engine.skills.registry import SkillRegistry, URLMatcher

URL = 'https://example.com/todos'
TOOL = Tool(name='add', description='Add todo', steps=[Step(action='click', selector='#add')])


class FakeBrowser:
    current_url = URL
    def open(self, url): self.current_url = url
    def snapshot(self): return {'url': self.current_url, 'title': 'Todo', 'text': 'task', 'password_visible': False}
    def execute_tool(self, tool, args):
        return {'action_result': 'clicked', 'verification': 'pending'}


class FakeModel:
    last_model = 'gemini-test'
    def __init__(self, verdicts, decisions=None):
        self.verdicts = iter(verdicts)
        self.decisions = iter(decisions or [Decision(kind='done', message='done')]*2)
        self.synthesis_count = 0
    def ask(self, role, instruction, payload, schema):
        if schema is Synthesis:
            self.synthesis_count += 1
            return Synthesis(tools=[TOOL])
        if schema is Decision: return next(self.decisions)
        return next(self.verdicts)


class Tests(unittest.TestCase):
    def test_strict_url(self):
        for suffix in ['/','?a=','?a=1','?utm_source=x','#/other','/index.html']:
            self.assertFalse(URLMatcher.is_exact_url_match(URL, URL+suffix))
        self.assertTrue(URLMatcher.is_exact_url_match('HTTPS://EXAMPLE.COM:443/todos', URL))
        self.assertEqual(URLMatcher.normalize_url('http://168.110.118.148/ 블로그 글쓰기 사이트야'), 'http://168.110.118.148/')
        for bad in ['file:///tmp/test','javascript:alert(1)','https://user:pass@example.com','quit']:
            with self.assertRaises(ValueError): URLMatcher.normalize_url(bad)

    def test_gemini_developer_api_schema_has_no_additional_properties(self):
        import json
        from agentic_web.engine.schemas import WorkflowDecision
        for model_cls in (Synthesis, Decision, Verdict, WorkflowDecision):
            schema_json = json.dumps(model_cls.model_json_schema())
            self.assertNotIn('additionalProperties', schema_json)

    def test_persistence_and_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SkillRegistry(directory)
            registry.register_skill(URL, [TOOL.model_dump()])
            registry = SkillRegistry(directory)
            self.assertTrue(registry.has_skill_for_url(URL))
            self.assertFalse(registry.has_skill_for_url(URL+'#other'))
            self.assertEqual(registry.get_tools_strictly_for_url(URL+'/')[0], [])

    def test_retry_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            model = FakeModel([Verdict(status='failure', reason='missing')]*2)
            result = RootAgent(model, FakeBrowser(), SkillRegistry(directory)).run(URL, 'add task')
            self.assertEqual(result['attempts'], 2)
            self.assertEqual(result['status'], 'failure')
            self.assertEqual(model.synthesis_count, 2)

    def test_human_resume_and_success(self):
        with tempfile.TemporaryDirectory() as directory:
            model = FakeModel([Verdict(status='success', reason='Task visible', evidence=['task'])], [
                Decision(kind='input', message='What title?'),
                Decision(kind='call', tool_name='add', message='add'),
                Decision(kind='done', message='done')])
            prompts = []
            def human(kind, message):
                prompts.append(message)
                return 'task'
            result = RootAgent(model, FakeBrowser(), SkillRegistry(directory), human).run(URL, 'add task')
            self.assertEqual(result['status'], 'success')
            self.assertEqual(result['attempts'], 1)
            self.assertEqual(prompts, ['What title?'])

    def test_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            model = FakeModel([], [Decision(kind='login', message='Login')])
            result = RootAgent(model, FakeBrowser(), SkillRegistry(directory), lambda *a:'/cancel').run(URL,'task')
            self.assertEqual(result['status'], 'cancelled')


if __name__ == '__main__': unittest.main()
