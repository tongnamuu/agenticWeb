"""Observable workflow execution without holding HTTP connections open."""
from .analysis_jobs import AnalysisJobs
from time import time
from uuid import uuid4


class WorkflowJobs(AnalysisJobs):
    def start(self, owner, services, identifier, run_id=None):
        workflow = services.workflows.get(identifier)
        with self.lock:
            active = next((job['id'] for job in self.jobs.values() if job['owner'] == owner and job['status'] == 'RUNNING' and job.get('workflow_id') == identifier), None)
        if active:
            return self.get(owner, active)
        if not services.lock.acquire(blocking=False):
            raise ValueError('다른 브라우저 작업이 실행 중입니다.')
        job_id = uuid4().hex
        with self.lock:
            self.jobs = {key: value for key, value in self.jobs.items()
                         if value['status'] == 'RUNNING' or time() - value['created_at'] < 1800}
            self.jobs[job_id] = dict(id=job_id, owner=owner, workflow_id=identifier, status='RUNNING',
                                    message='워크플로우 시작', created_at=time(),
                                    result=dict(status='RUNNING', workflow=workflow, next_step=0, results=[]))
        try:
            self.executor.submit(self._execute, job_id, services, identifier, run_id)
        except Exception:
            services.lock.release()
            self.update(job_id, status='FAILED', message='실행을 시작하지 못했습니다.')
            raise
        return self.get(owner, job_id)

    def _execute(self, job_id, services, identifier, run_id):
        try:
            result = services.workflows.run(identifier, run_id, progress=lambda message:
                                            self.update(job_id, message=message),
                                            on_update=lambda result: self.update(job_id, result=result))
            self.update(job_id, status=result['status'], message=result['message'], result=result)
        except Exception:
            message = '실행 중 오류가 발생했습니다. 사이트와 스킬 상태를 확인하세요.'
            with self.lock:
                self.jobs[job_id]['result'].update(status='FAILED', message=message)
                self.jobs[job_id].update(status='FAILED', message=message)
        finally:
            services.lock.release()
