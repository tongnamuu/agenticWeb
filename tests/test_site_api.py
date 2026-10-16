import tempfile
import unittest
from agentic_web.web import create_app
from agentic_web.engine.services import UserServicePool


class SiteApiTests(unittest.TestCase):
    def test_new_user_empty_analysis_does_not_create_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app({'TESTING':True,'SECRET_KEY':'test'})
            pool=UserServicePool(directory+'/skills',directory+'/workflows')
            app.extensions['user_services']=pool
            with app.test_client() as client:
                with client.session_transaction() as session:
                    session['user']={'id':'alice','name':'Alice','email':'alice@example.com'}
                    session['csrf_token']='csrf'
                self.assertEqual(client.get('/api/skills').json['skills'],[])
                self.assertEqual(client.get('/api/workflows').json['workflows'], [])
                services=pool.for_user('alice')
                def analyze(**kwargs):
                    kwargs['progress']('분석 중')
                    return {'skill_package':{'normalized_url':'http://blog.test/','tools':[]}}
                services.orchestrator.analyze_site=analyze
                response=client.post('/api/analyze',json={'url':'http://blog.test/'},headers={'X-CSRF-Token':'csrf'})
                self.assertEqual(response.status_code,202)
                job_id=response.json['job']['id']
                app.extensions['analysis_jobs'].executor.shutdown(wait=True)
                self.assertEqual(client.get('/api/analysis-jobs/'+job_id).json['job']['status'],'SUCCESS')
                self.assertEqual(client.get('/api/workflows').json['workflows'], [])
                with client.session_transaction() as session:
                    session['user']={'id':'bob','name':'Bob','email':'bob@example.com'}
                self.assertEqual(client.get('/api/analysis-jobs/'+job_id).status_code,404)
                self.assertEqual(client.get('/api/skills').json['skills'],[])

    def test_direct_tool_retries_verification_without_duplicating_add(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app({'TESTING':True,'SECRET_KEY':'test'})
            pool=UserServicePool(directory+'/skills',directory+'/workflows')
            app.extensions['user_services']=pool
            services=pool.for_user('alice')
            url='https://example.com/todo'
            tool={'name':'add_todo_item','description':'할 일 추가',
                  'parameters':{'type':'object','properties':{'text':{'type':'string'}},'required':['text']},
                  'steps':[{'op':'submit','selector':'form'}]}
            services.orchestrator.registry.register_skill(url,[tool])
            class FakeBrowser:
                current_url=url
                calls=0
                def capture_screenshot(self):return ''
                def inspect_page(self):return {'snapshot':{'url':url,'authState':{}}}
                def execute_tool(self,tool,args):
                    self.calls+=1
                    return {'before_snapshot':{'url':url},'after_snapshot':{'url':url},
                            'action_result':{'success':True},'verification':{'verified':True}}
            browser=FakeBrowser(); services.browser=browser
            checks=[]
            def verify(**kwargs):
                checks.append(kwargs)
                return {'success':len(checks)==2,'status':'SUCCESS' if len(checks)==2 else 'FAILED',
                        'failure_code':None if len(checks)==2 else 'VERIFICATION_MODEL_FAILED','reason':'확인'}
            services.orchestrator.verify_execution=verify
            with app.test_client() as client:
                with client.session_transaction() as session:
                    session['user']={'id':'alice'};session['csrf_token']='csrf'
                response=client.post('/api/call-tool',json={'url':url,'tool_name':'add_todo_item','arguments':{'text':'물 마시기'}},headers={'X-CSRF-Token':'csrf'})
                self.assertTrue(response.json['ok'])
                self.assertEqual(response.json['retry_count'],1)
                self.assertEqual(browser.calls,1)
                self.assertEqual(checks[0]['steps'][0]['operations'],tool['steps'])
                response=client.post('/api/call-tool',json={'url':url,'tool_name':'add_todo_item','arguments':{}},headers={'X-CSRF-Token':'csrf'})
                self.assertEqual(response.status_code,400)
                self.assertEqual(browser.calls,1)

    def test_url_name_persists_after_reanalysis_and_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app({'TESTING':True,'SECRET_KEY':'test'})
            pool=UserServicePool(directory+'/skills',directory+'/workflows')
            app.extensions['user_services']=pool
            registry=pool.for_user('alice').orchestrator.registry
            url='https://example.com/todo'
            tool={'name':'read_page','description':'페이지 조회','steps':[{'op':'read_state'}]}
            registry.register_skill(url,[tool],site_title='Todo')
            with app.test_client() as client:
                with client.session_transaction() as session:
                    session['user']={'id':'alice'};session['csrf_token']='csrf'
                response=client.post('/api/rename-skill',json={'url':url,'name':'내 할 일'},headers={'X-CSRF-Token':'csrf'})
                self.assertEqual(response.json['skill']['skill_name'],'내 할 일')
                registry.register_skill(url,[tool],site_title='Changed title',is_repair=True)
                from agentic_web.engine.skills.registry import SkillRegistry
                self.assertEqual(SkillRegistry(registry.store_dir).resolve_skill_by_url(url)[0]['skill_name'],'내 할 일')
                response=client.post('/api/rename-skill',json={'url':url,'name':'  '},headers={'X-CSRF-Token':'csrf'})
                self.assertEqual(response.status_code,400)
                with client.session_transaction() as session:
                    session['user']={'id':'bob'}
                self.assertEqual(client.post('/api/rename-skill',json={'url':url,'name':'Bob'},headers={'X-CSRF-Token':'csrf'}).status_code,404)

    def test_login_browser_opens_users_session_and_checks_oauth_return(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            app = create_app({'TESTING': True, 'SECRET_KEY': 'test'})
            pool = UserServicePool(directory+'/skills', directory+'/workflows')
            app.extensions['user_services'] = pool
            browser = Mock()
            pool.for_user('alice').browser = browser
            with app.test_client() as client:
                with client.session_transaction() as session:
                    session['user'] = {'id': 'alice'}
                    session['csrf_token'] = 'csrf'
                headers = {'X-CSRF-Token': 'csrf'}
                from unittest.mock import patch
                with patch.dict('os.environ', {'K_SERVICE':'agentic-web'}):
                    self.assertEqual(client.post('/api/login-browser', headers=headers).json['view_url'], '/browser-session')
                    browser.show_for_login.assert_not_called()
                with patch.dict('os.environ', {'K_SERVICE':''}), patch('sys.platform', 'darwin'):
                    self.assertEqual(client.post('/api/login-browser', headers=headers).json['mode'], 'native')
                    browser.show_for_login.assert_called_once_with()
                for url, auth, expected in [
                    ('https://github.com/login', True, False),
                    ('https://blog.test/login', False, False),
                    ('https://blog.test/', True, True),
                ]:
                    browser.evaluate_js.return_value = {'url': url, 'authState': {'is_authenticated': auth}}
                    response = client.post('/api/login-status', json={'url':'https://blog.test/'}, headers=headers)
                    self.assertEqual(response.json['authenticated'], expected)
                browser.navigate.assert_not_called()
