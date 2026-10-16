"""Cross-site root orchestration over strictly URL-scoped skill execution."""
from copy import deepcopy
from site_agent import RootAgent
from schemas import WorkflowDecision, Verdict
from skill_registry import SkillRegistry, URLMatcher


class WorkflowAgent:
    def __init__(self, model, browser, registry=None, human=None, worker=None):
        self.model = model
        self.browser = browser
        self.registry = registry if registry is not None else SkillRegistry()
        self.human = human or RootAgent.console_input
        self.worker = worker or RootAgent(model, browser, self.registry, self.human)

    def run(self, task, urls=None):
        history, answers = [], []
        attempt = 1
        try:
            explicit_urls = {URLMatcher.normalize_url(u) for u in (urls or [])}
            for attempt in (1, 2):
                for _ in range(30):
                    # Refresh every turn: newly synthesized capabilities become visible immediately.
                    catalog = [{'url': s['normalized_url'], 'version': s['version'],
                                'tools': [{'name': t['name'], 'description': t['description'],
                                           'parameters': t.get('parameters', [])} for t in s['tools']]}
                               for s in self.registry.list_all_skills()]
                    allowed = explicit_urls | {s['url'] for s in catalog}
                    decision = self.model.ask('root_verifier',
                        'You are the cross-site root orchestrator. Choose ONE next subtask and its exact URL '
                        'from the catalog/allowed URLs, or ask for missing information. Decompose the full task '
                        'into ordered dependent subtasks. For example: add todo on A, write blog on B, '
                        'then complete that SAME todo on A only after blog verification succeeds. '
                        'Carry actual titles, IDs, URLs and results from history into the next subtask. '
                        'Never invent output IDs. Never repeat a successful mutation. If unsure, inspect '
                        'the state through a read-only subtask. Use done only when all requirements are met. '
                        'Missing site URLs must be requested from the user; do not guess URLs.',
                        {'task': task, 'catalog': catalog, 'allowed_urls': sorted(allowed),
                         'history': history, 'user_answers': answers, 'attempt': attempt}, WorkflowDecision)
                    if decision.kind == 'input':
                        answer = self.human('input', decision.message)
                        if answer is None or answer.strip() == '/cancel':
                            return self.result('cancelled', attempt, '사용자 취소', history)
                        answers.append(answer)
                        # A URL explicitly supplied by the user authorizes discovering a new site.
                        if answer.strip().startswith(('http://', 'https://')) and ' ' not in answer.strip():
                            explicit_urls.add(URLMatcher.normalize_url(answer))
                        continue
                    if decision.kind == 'done':
                        break
                    url = URLMatcher.normalize_url(decision.url)
                    if url not in allowed or not decision.task.strip():
                        history.append({'routing_error': 'Unregistered/unprovided URL or empty subtask',
                                        'url': url})
                        continue
                    context = {'overall_task': task, 'previous_steps': deepcopy(history), 'user_answers': list(answers)}
                    # Context is supplied separately; subtask verification remains scoped to this step.
                    outcome = self.worker.run(url, decision.task, workflow_context=context)
                    observation = self.browser.snapshot()
                    history.append({'url': url, 'task': decision.task,
                                    'result': {k: v for k, v in outcome.items() if k != 'events'},
                                    'observation': observation})
                    if outcome['status'] != 'success':
                        # Stop dependencies immediately: completing the todo after failed posting is forbidden.
                        return self.result(outcome['status'], attempt, outcome['reason'], history)
                verdict = self.model.ask('root_verifier',
                    'Independently verify the ENTIRE cross-site task using the ordered verified step results '
                    'and observations. Verify required ordering and shared object identity across sites. '
                    'An individual step succeeding is insufficient. Return success with concrete evidence '
                    'only if all requirements are met. Failure triggers one correction pass; preserve '
                    'already successful mutations. Use input/login if more human information is required.',
                    {'task': task, 'history': history, 'user_answers': answers}, Verdict)
                if verdict.status == 'success':
                    return self.result('success', attempt, verdict.reason, history, verdict.evidence)
                if verdict.status in ('input', 'login'):
                    answer = self.human(verdict.status, verdict.reason)
                    if answer is None or answer.strip() == '/cancel':
                        return self.result('cancelled', attempt, '사용자 취소', history)
                    answers.append(answer)
                history.append({'workflow_verification': verdict.model_dump()})
            return self.result('failure', attempt, verdict.reason, history, verdict.evidence)
        except Exception as exc:
            return self.result('failure', attempt, str(exc), history)

    @staticmethod
    def result(status, attempts, reason, history, evidence=None):
        return {'status': status, 'attempts': attempts, 'reason': reason,
                'evidence': evidence or [], 'events': history}
