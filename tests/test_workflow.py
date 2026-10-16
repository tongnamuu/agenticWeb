import tempfile
import unittest
from agentic_web.engine.schemas import WorkflowDecision, Verdict, Tool, Step
from agentic_web.engine.skills.registry import SkillRegistry
from agentic_web.engine.agents.workflow import WorkflowAgent

TODO = 'https://todo.test/'
BLOG = 'https://blog.test/'


class Model:
    def __init__(self, decisions): self.decisions = iter(decisions)
    def ask(self, role, instruction, payload, schema):
        if schema is WorkflowDecision:
            return next(self.decisions)
        return Verdict(status='success', reason='Ordered verified steps', evidence=['Todo added, blog posted, same todo completed'])


class Browser:
    current_url = TODO
    def snapshot(self): return {'url': self.current_url, 'text': 'visible result'}


class Worker:
    def __init__(self, browser, fail=False):
        self.calls = []
        self.browser = browser
        self.fail = fail
    def run(self, url, task, workflow_context=None):
        self.calls.append((url, task, workflow_context))
        self.browser.current_url = url
        failed = self.fail and url == BLOG
        return {'status': 'failure' if failed else 'success', 'reason': 'failed' if failed else 'verified',
                'evidence': ['todo-id:42' if url == TODO else 'post-url:https://blog.test/posts/7']}


def sequence():
    return [WorkflowDecision(kind='execute', url=TODO, task='Add blog task', message='add'),
            WorkflowDecision(kind='execute', url=BLOG, task='Write blog', message='write'),
            WorkflowDecision(kind='execute', url=TODO, task='Complete todo 42', message='complete'),
            WorkflowDecision(kind='done', message='done')]


class WorkflowTests(unittest.TestCase):
    def registry(self, directory):
        registry = SkillRegistry(directory)
        for url, name in [(TODO,'add'), (BLOG,'write')]:
            registry.register_skill(url, [Tool(name=name, description=name,
                steps=[Step(action='click', selector='button')]).model_dump()])
        return registry

    def test_cross_site_order_and_result_passing(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = Browser()
            worker = Worker(browser)
            result = WorkflowAgent(Model(sequence()), browser, self.registry(directory), worker=worker).run('add, write, complete')
            self.assertEqual(result['status'], 'success')
            self.assertEqual([c[0] for c in worker.calls], [TODO, BLOG, TODO])
            context = worker.calls[2][2]
            self.assertEqual(len(context['previous_steps']), 2)
            self.assertIn('post-url:', context['previous_steps'][1]['result']['evidence'][0])

    def test_failed_blog_blocks_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = Browser()
            worker = Worker(browser, fail=True)
            result = WorkflowAgent(Model(sequence()), browser, self.registry(directory), worker=worker).run('add, write, complete')
            self.assertEqual(result['status'], 'failure')
            self.assertEqual(len(worker.calls), 2)

    def test_unregistered_url_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = Browser()
            worker = Worker(browser)
            decisions = [WorkflowDecision(kind='execute', url='https://unknown.test/', task='write', message='write'),
                         WorkflowDecision(kind='input', message='URL?')]
            result = WorkflowAgent(Model(decisions), browser, self.registry(directory),
                human=lambda *a:'/cancel', worker=worker).run('write')
            self.assertEqual(result['status'], 'cancelled')
            self.assertEqual(worker.calls, [])
