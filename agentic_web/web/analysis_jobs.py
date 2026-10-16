"""Short HTTP requests with observable background analysis progress."""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import time
from uuid import uuid4
import copy


class AnalysisJobs:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='site-analysis')
        self.jobs, self.lock = {}, Lock()

    def start(self, owner, services, url, repair=False):
        if not services.lock.acquire(blocking=False):
            raise ValueError('다른 브라우저 작업이 실행 중입니다. 완료 후 다시 시도하세요.')
        job_id = uuid4().hex
        with self.lock:
            self.jobs = {key:value for key,value in self.jobs.items() if value['status']=='RUNNING' or time()-value['created_at']<1800}
            self.jobs[job_id] = {'id':job_id,'owner':owner,'status':'RUNNING', 'message':'페이지를 여는 중입니다.', 'created_at':time()}
        try:
            self.executor.submit(self._run,job_id,services,url,repair)
        except Exception:
            services.lock.release()
            raise
        return self.get(owner,job_id)

    def update(self, job_id, **values):
        with self.lock: self.jobs[job_id].update(values)

    def _run(self, job_id, services, url, repair):
        try:
            result=services.orchestrator.analyze_and_register_url_skill(
                target_url=url,browser_session=services.browser,force_resynthesize=repair,
                progress=lambda message:self.update(job_id,message=message))
            self.update(job_id,status='SUCCESS',message='사이트 분석과 스킬 등록을 완료했습니다.',result={
                'reused':result.get('skill_reused',False),'normalized_url':result['skill_package']['normalized_url'],
                'skill':result['skill_package']})
        except Exception as exc:
            # Keep credentials and raw provider URLs out of user-visible errors.
            message=str(exc)
            import re
            message=re.sub(r'([?&]key=)[^\s&]+',r'\1[redacted]',message)
            self.update(job_id,status='FAILED',message='사이트 분석 실패: '+message[:500])
        finally:
            services.lock.release()

    def get(self, owner, job_id):
        with self.lock:
            job=self.jobs.get(job_id)
            if not job or job['owner']!=owner: return None
            return copy.deepcopy({k:v for k,v in job.items() if k!='owner'})
