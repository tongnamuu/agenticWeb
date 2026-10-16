import tempfile
import unittest
from threading import Event
from agentic_web.web import create_app
from agentic_web.engine.services import UserServicePool
from tests.test_saved_workflows import Browser, Verifier


class WorkflowApiTests(unittest.TestCase):
    def test_save_async_execute_and_user_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app({'TESTING': True, 'SECRET_KEY': 'test'})
            pool = UserServicePool(directory+'/skills', directory+'/workflows')
            app.extensions['user_services'] = pool
            services = pool.for_user('alice')
            services.workflows.browser = Browser()
            services.workflows.verifier = Verifier()
            tool = {'name':'add', 'description':'add', 'parameters':{'type':'object', 'properties':{}}, 'steps':[{'op':'click','selector':'#add'}]}
            services.orchestrator.registry.register_skill('https://todo.test/', [tool])
            with app.test_client() as client:
                with client.session_transaction() as session:
                    session['user'] = {'id':'alice'}
                    session['csrf_token'] = 'csrf'
                headers = {'X-CSRF-Token':'csrf'}
                data = {'name':'할 일', 'steps':[{'url':'https://todo.test/', 'tool_name':'add', 'arguments':{}}]}
                self.assertEqual(client.post('/api/workflows', json=data).status_code, 403)
                saved = client.post('/api/workflows', json=data, headers=headers).json['workflow']
                response = client.post('/api/workflows/'+saved['id']+'/run', json={}, headers=headers)
                self.assertEqual(response.status_code, 202)
                job_id = response.json['job']['id']
                app.extensions['workflow_jobs'].executor.shutdown(wait=True)
                job = client.get('/api/workflow-jobs/'+job_id).json['job']
                self.assertEqual(job['status'], 'SUCCESS')
                self.assertEqual(len(services.workflows.browser.calls), 1)
                self.assertTrue(services.lock.acquire(blocking=False))
                services.lock.release()
                with client.session_transaction() as session:
                    session['user'] = {'id':'bob'}
                self.assertEqual(client.get('/api/workflows').json['workflows'], [])
                self.assertEqual(client.get('/api/workflow-jobs/'+job_id).status_code, 404)
                self.assertEqual(client.post('/api/workflows/'+saved['id']+'/run', json={}, headers=headers).status_code, 409)
                endpoint = '/api/workflows/'+saved['id']+'/delete'
                self.assertEqual(client.post(endpoint, headers=headers).status_code, 404)
                with client.session_transaction() as session:
                    session['user'] = {'id':'alice'}
                self.assertEqual(client.post(endpoint).status_code, 403)
                services.lock.acquire()
                try:
                    self.assertEqual(client.post(endpoint, headers=headers).status_code, 409)
                finally:
                    services.lock.release()
                self.assertEqual(client.post(endpoint, headers=headers).status_code, 200)
                self.assertEqual(client.get('/api/workflows').json['workflows'], [])
                self.assertTrue(services.orchestrator.registry.list_all_skills())


    def test_browser_busy_prevents_workflow_start(self):
        from types import SimpleNamespace
        from threading import Lock
        from unittest.mock import Mock
        from agentic_web.web.workflow_jobs import WorkflowJobs
        services = SimpleNamespace(lock=Lock(), workflows=Mock())
        services.lock.acquire()
        jobs = WorkflowJobs()
        try:
            with self.assertRaises(ValueError):
                jobs.start('alice', services, 'workflow')
            services.workflows.run.assert_not_called()
        finally:
            services.lock.release()
            jobs.executor.shutdown(wait=True)

    def test_verification_progress_and_exception_have_explicit_status(self):
        from types import SimpleNamespace
        from threading import Lock
        from tests.test_saved_workflows import SavedWorkflowTests
        from agentic_web.web.workflow_jobs import WorkflowJobs
        entered, release = Event(), Event()
        class SlowVerifier:
            def verify_execution(self, **kwargs):
                entered.set()
                release.wait(2)
                raise RuntimeError('provider failed')
        with tempfile.TemporaryDirectory() as directory:
            service, browser = SavedWorkflowTests().setup_service(directory, SlowVerifier())
            workflow = service.save(SavedWorkflowTests().data())
            services = SimpleNamespace(lock=Lock(), workflows=service)
            jobs = WorkflowJobs()
            try:
                job = jobs.start('alice', services, workflow['id'])
                self.assertTrue(entered.wait(2))
                duplicate = jobs.start('alice', services, workflow['id'])
                self.assertEqual(duplicate['id'], job['id'])
                self.assertEqual(len(browser.calls), 1)
                running = jobs.get('alice', job['id'])
                self.assertEqual(running['status'], 'RUNNING')
                self.assertEqual(running['result']['phase'], 'VERIFYING')
                self.assertEqual(len(running['result']['workflow']['steps']), 3)
                release.set()
                jobs.executor.shutdown(wait=True)
                failed = jobs.get('alice', job['id'])
                self.assertEqual(failed['status'], 'FAILED')
                self.assertEqual(failed['result']['status'], 'FAILED')
                self.assertEqual(len(browser.calls), 1)
            finally:
                release.set()
                jobs.executor.shutdown(wait=True)
