import tempfile
import unittest
from unittest.mock import patch, Mock
from agentic_web.engine.agents.orchestrator import RootOrchestratorAgent, GeminiClient
from agentic_web.engine.skills.registry import SkillRegistry

URL='https://tongnamuu.github.io/ToDoList/'
def tool(name, selector='form'):
    return {'name':name,'description':name,'steps':[{'op':'submit','selector':selector}]}

class RenewalTests(unittest.TestCase):
    def test_repeat_analysis_updates_same_package_and_adds_missing_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=SkillRegistry(directory)
            root=RootOrchestratorAgent(registry=registry)
            class Browser:
                current_url=''
                def navigate(self,url):self.current_url=url;return self.inspect_page()
                def inspect_page(self):return {'screenshot':'','snapshot':{'url':URL,'title':'Todo','links':[]}}
            calls=[]
            def synthesize(**kwargs):
                calls.append(kwargs)
                return ([tool('add_todo_item'),tool('delete_item')] if len(calls)==1 else
                        [tool('add_todo_item','form.todoform'),tool('complete_todo_item')]),'test'
            root.synthesizer.synthesize_tools=synthesize
            browser=Browser()
            first=root.analyze_site(URL,browser)
            registry.rename_skill(URL,'내 할 일')
            second=root.analyze_site(URL,browser)
            package=second['skill_package']
            self.assertEqual(first['skill_package']['skill_id'],package['skill_id'])
            self.assertEqual(package['version'],2)
            self.assertEqual(package['skill_name'],'내 할 일')
            self.assertEqual(len(registry.list_all_skills()),1)
            self.assertEqual({t['name'] for t in package['tools']},{'add_todo_item','delete_item','complete_todo_item'})
            self.assertEqual(second['refresh_summary']['added'],['complete_todo_item'])
            self.assertEqual(second['refresh_summary']['updated'],['add_todo_item'])
            self.assertEqual(len(calls[1]['existing_tools']),2)
            persisted=SkillRegistry(directory).resolve_skill_by_url(URL)[0]
            self.assertEqual(persisted['tools'],package['tools'])

    def test_repair_retains_unrelated_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=SkillRegistry(directory)
            registry.register_skill(URL,[tool('add_todo_item'),tool('complete_todo_item')])
            pkg=registry.register_skill(URL,[tool('add_todo_item','form.fixed')],is_repair=True)
            self.assertEqual({t['name'] for t in pkg['tools']},{'add_todo_item','complete_todo_item'})

    def test_argon_request_uses_thinking_and_preserves_function_history(self):
        response=Mock(status_code=200)
        response.json.return_value={'candidates':[{'content':{'parts':[{'text':'OK'}]}}]}
        history=[{'role':'model','parts':[{'functionCall':{'name':'read','args':{}},'thoughtSignature':'signature'}]}]
        with patch('agentic_web.engine.agents.orchestrator.get_api_key',return_value='test-key'), patch(
            'agentic_web.engine.agents.orchestrator.requests.post',return_value=response) as post:
            result=GeminiClient().generate('skill_executor',history,json_mode=True)
        self.assertEqual(result['_model_used'],'gemini-4-argon')
        payload=post.call_args.kwargs['json']
        self.assertNotIn('temperature',payload['generationConfig'])
        self.assertEqual(payload['generationConfig']['thinkingConfig']['thinkingLevel'],'high')
        self.assertEqual(payload['contents'],history)
        self.assertGreater(post.call_args.kwargs['timeout'][1],800)
