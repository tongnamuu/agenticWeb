import tempfile
import unittest
from agentic_web.engine.agents.orchestrator import RootOrchestratorAgent, SkillSynthesizerAgent
from agentic_web.engine.skills.registry import SkillRegistry

class Client:
    has_key=False
    def __init__(self, *args): pass

def tool(name, params):
    return {'name':name,'description':name,'parameters':{'type':'object','properties':{},'required':[]},'steps':[{'op':'read_state'}]}

class DiscoveryTests(unittest.TestCase):
    def test_empty_todo_list_dynamic_controls_use_actual_source_bindings(self):
        source='''const todoList = document.querySelector(".todolist");
        const completeList = document.querySelector(".completelist");
        function addtoFinished(text) {
          const li = document.createElement("li");
          const delBtn = document.createElement("button"); delBtn.innerHTML = "❌";
          const finBtn = document.createElement("button"); finBtn.innerHTML = "◀";
          li.appendChild(delBtn); li.appendChild(finBtn); completeList.appendChild(li);
        }
        function addtoPending(text) {
          const li = document.createElement("li");
          const finBtn = document.createElement("button"); finBtn.innerHTML = "✅";
          li.appendChild(finBtn); todoList.appendChild(li);
        }'''
        snapshot={'url':'https://todo.test/','lists':[
            {'selector':'ul.todolist','heading':'Pending','items':[]},
            {'selector':'ul.completelist','heading':'Finished','items':[]}],
            'dynamicHints':[{'src':'src/todolist.js','snippet':source}]}
        tools=SkillSynthesizerAgent(Client({}))._heuristic_dom_to_tools(snapshot)
        complete=next(t for t in tools if t['name']=='complete_todo_item')
        self.assertEqual(complete['steps'][0]['list_selector'],'ul.todolist')
        restore=next(t for t in tools if t['name']=='restore_todo_item')
        self.assertEqual(restore['steps'][0]['list_selector'],'ul.completelist')

    def test_linked_writer_discovery_pauses_for_login_then_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=RootOrchestratorAgent(registry=SkillRegistry(directory))
            class Browser:
                current_url=''
                logged_in=False
                mutations=0
                def navigate(self,url):
                    self.current_url=url
                    if url.endswith('/write') and not self.logged_in: self.current_url='https://blog.test/login'
                    return self.inspect_page()
                def inspect_page(self):
                    return {'screenshot':'','snapshot':{'url':self.current_url,'title':'Blog',
                        'links':[{'href':'/write','text':'Write a Post'}] if self.current_url=='https://blog.test/' else [],
                        'inputs':[{'type':'text','name':'name'},{'type':'password'}] if self.current_url.endswith('/login') else [],
                        'buttons':[{'text':'Login'}],
                        'authState':{'login_wall_detected':False}}}
            browser=Browser()
            root.synthesizer.synthesize_tools=lambda **kwargs: ([tool('read_page',[])],'test')
            result=root.analyze_site('https://blog.test/',browser)
            self.assertEqual(result['status'],'NEEDS_USER_INPUT')
            self.assertFalse(root.registry.has_skill_for_url('https://blog.test/login'))
            browser.logged_in=True
            result=root.analyze_site('https://blog.test/',browser)
            self.assertEqual(result['status'],'SUCCESS')
            self.assertEqual(len(result['analyzed_skills']),2)
            self.assertTrue(root.registry.has_skill_for_url('https://blog.test/write'))
            self.assertEqual(browser.current_url,'https://blog.test/')
