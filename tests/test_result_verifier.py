import json
import unittest
from agentic_web.engine.agents.result_verifier import ResultVerificationAgent
from agentic_web.engine.agents.orchestrator import RootOrchestratorAgent


class Client:
    has_key=True
    def __init__(self, verdict): self.verdict=verdict;self.calls=0
    def generate(self, **kwargs):
        self.calls+=1
        return {'_model_used':'gemini-test','candidates':[{'content':{'parts':[{'text':json.dumps(self.verdict)}]}}]}


class Browser:
    def __init__(self, text): self.text=text;self.reloaded=False
    def navigate(self,url):
        self.reloaded=True
        return {'snapshot':{'url':url,'bodyText':self.text}}


def step(name='write_post'):
    return {'tool_name':name,'args':{},'operations':[{'op':'read_state' if name=='get_page_state' else 'submit'}],
            'action_result':{'success':True},'verification':{'verified':True}}


def verdict(success=True,evidence='물마시자',category=True):
    return {'is_success':success,'is_mutation':True,'reason':'확인', 'checks':[
        {'requirement':'제목','satisfied':True,'evidence':evidence},
        {'requirement':'카테고리','satisfied':category,'evidence':'유머'}]}


class ResultVerifierTests(unittest.TestCase):
    def verify(self, model, browser, steps=None):
        return ResultVerificationAgent(model).verify('제목은 물마시자, 카테고리 유머로 글 작성',{},
            {'url':'http://blog.test/post/2','bodyText':'물마시자 유머'},steps or [step()],browser)

    def test_read_only_tool_never_satisfies_write_request(self):
        model=Client(verdict(False))
        result=self.verify(model,Browser('물마시자 유머'),[step('get_page_state')])
        self.assertFalse(result['success']);self.assertEqual(model.calls,1)

    def test_model_failure_is_not_overridden_by_executor_success(self):
        self.assertFalse(self.verify(Client(verdict(False)),Browser('물마시자 유머'))['success'])

    def test_unpersisted_dom_change_is_not_success(self):
        model = Client(verdict(False))
        self.assertFalse(self.verify(model,Browser('아직 게시글이 없습니다'))['success'])
        self.assertEqual(model.calls, 1)

    def test_missing_category_fails_even_if_title_exists(self):
        self.assertFalse(self.verify(Client(verdict(category=False)),Browser('물마시자 유머'))['success'])

    def test_persisted_evidence_and_all_conditions_required(self):
        browser=Browser('물마시자 유머 저장된 본문')
        self.assertTrue(self.verify(Client(verdict()),browser)['success'])
        self.assertTrue(browser.reloaded)

    def test_unavailable_model_cannot_report_success(self):
        client=Client(verdict());client.has_key=False
        self.assertFalse(self.verify(client,Browser('물마시자 유머'))['success'])

    def test_missing_readback_and_empty_checks_fail(self):
        self.assertFalse(self.verify(Client(verdict()),None)['success'])
        v=verdict();v['checks']=[]
        self.assertFalse(self.verify(Client(v),Browser('물마시자 유머'))['success'])

    def test_root_delegates_to_independent_verifier(self):
        root=RootOrchestratorAgent()
        class Checker:
            def verify(self,*args,**kwargs): return {'success':False,'status':'FAILED','reason':'independent result'}
        root.result_verifier=Checker()
        self.assertEqual(root.verify_execution('write',{}, {},[step()])['reason'],'independent result')

class VerificationErrorTests(unittest.TestCase):
    def test_readback_failure_has_distinct_code(self):
        class BrokenBrowser:
            def navigate(self, url): raise ConnectionError('secret must not leak')
        result=ResultVerificationAgent(Client(verdict())).verify('글 작성',{},
            {'url':'http://blog.test/'},[step()],BrokenBrowser())
        self.assertEqual(result['failure_code'],'VERIFICATION_READBACK_FAILED')
        self.assertNotIn('secret',json.dumps(result))

    def test_model_failure_is_distinct_from_readback(self):
        class BrokenClient:
            has_key=True
            def generate(self, **kwargs): raise RuntimeError('api_key=secret')
        result=ResultVerificationAgent(BrokenClient()).verify('글 작성',{},
            {'url':'http://blog.test/'},[step()],Browser('saved'))
        self.assertEqual(result['failure_code'],'VERIFICATION_MODEL_FAILED')
        self.assertNotIn('secret',json.dumps(result))

    def test_invalid_model_json_is_distinct(self):
        class BadClient:
            has_key=True
            def generate(self, **kwargs): return {'candidates':[]}
        result=ResultVerificationAgent(BadClient()).verify('글 작성',{},
            {'url':'http://blog.test/'},[step()],Browser('saved'))
        self.assertEqual(result['failure_code'],'VERIFICATION_RESPONSE_FAILED')

    def test_paraphrased_evidence_does_not_override_model_success(self):
        v = verdict(evidence='인사말 Hello hello!가 새로고침 후에도 표시됨')
        result = ResultVerificationAgent(Client(v)).verify('이름 hello 설정', {},
            {'url':'https://todo.test/'}, [step('set_username')], Browser('Hello hello!'))
        self.assertTrue(result['success'])
        self.assertEqual(result['status'], 'SUCCESS')

    def test_contradictory_model_response_is_error_not_task_failure(self):
        v = verdict(category=False)
        result = ResultVerificationAgent(Client(v)).verify('글 작성', {},
            {'url':'https://blog.test/'}, [step()], Browser('유머'))
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(result['failure_code'], 'VERIFICATION_RESPONSE_FAILED')
