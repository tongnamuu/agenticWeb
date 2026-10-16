from threading import Event, Lock
from types import SimpleNamespace
import unittest
from agentic_web.web.analysis_jobs import AnalysisJobs


class AnalysisJobTests(unittest.TestCase):
    def test_progress_owner_isolation_and_no_workflow_creation(self):
        waiting,release=Event(),Event()
        class Analyzer:
            def analyze_and_register_url_skill(self, **kwargs):
                kwargs['progress']('Gemini 분석 중')
                waiting.set();release.wait(2)
                return {'skill_package':{'normalized_url':'http://blog.test/','tools':[]}}
        services=SimpleNamespace(lock=Lock(),browser=object(),orchestrator=Analyzer())
        jobs=AnalysisJobs()
        try:
            job=jobs.start('alice',services,'http://blog.test/')
            self.assertTrue(waiting.wait(2))
            self.assertEqual(jobs.get('alice',job['id'])['message'],'Gemini 분석 중')
            self.assertIsNone(jobs.get('bob',job['id']))
            with self.assertRaises(ValueError): jobs.start('alice',services,'http://other.test/')
            release.set();jobs.executor.shutdown(wait=True)
            self.assertEqual(jobs.get('alice',job['id'])['status'],'SUCCESS')
            self.assertTrue(services.lock.acquire(blocking=False));services.lock.release()
        finally:
            release.set();jobs.executor.shutdown(wait=True)

    def test_browser_error_is_visible_and_unlocks(self):
        class Analyzer:
            def analyze_and_register_url_skill(self, **kwargs): raise RuntimeError('Chrome 실행 실패')
        services=SimpleNamespace(lock=Lock(),browser=object(),orchestrator=Analyzer())
        jobs=AnalysisJobs()
        job=jobs.start('alice',services,'http://blog.test/')
        jobs.executor.shutdown(wait=True)
        result=jobs.get('alice',job['id'])
        self.assertEqual(result['status'],'FAILED')
        self.assertIn('Chrome 실행 실패',result['message'])
        self.assertTrue(services.lock.acquire(blocking=False));services.lock.release()
