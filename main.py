import argparse
import json
from itertools import chain
from datetime import datetime
from pathlib import Path
from skill_registry import URLMatcher


def parse_jobs(argv=None):
    parser = argparse.ArgumentParser(description='Gemini URL skill browser agent')
    parser.add_argument('url', nargs='?')
    parser.add_argument('task', nargs='?')
    parser.add_argument('--job', nargs=2, action='append', metavar=('URL', 'TASK'),
                        help='URL과 작업 쌍. 여러 번 지정할 수 있습니다.')
    parser.add_argument('--task', dest='workflow_task', help='여러 URL의 스킬을 조합할 전체 작업')
    parser.add_argument('--site', action='append', default=[], help='전체 작업에서 사용할 추가 URL')
    parser.add_argument('--once', action='store_true', help='입력한 작업들만 실행하고 종료')
    parser.add_argument('--profile', default='.browser-profile')
    args = parser.parse_args(argv)
    if bool(args.url) != bool(args.task):
        parser.error('URL과 작업을 함께 입력하세요.')
    jobs = ([{'url': args.url, 'task': args.task}] if args.url else [])
    jobs += [{'url': url, 'task': task} for url, task in (args.job or [])]
    for job in jobs:
        try:
            raw_url = (job['url'] or '').strip()
            parts = raw_url.split(maxsplit=1)
            url_token = parts[0] if parts else ''
            extra_note = parts[1].strip() if len(parts) > 1 else ''
            job['url'] = URLMatcher.normalize_url(url_token)
            if extra_note and job['task'].strip():
                job['task'] = f"{job['task'].strip()} ({extra_note})"
            elif extra_note and not job['task'].strip():
                job['task'] = extra_note
            if not job['url'] or not job['task'].strip():
                raise ValueError('URL과 작업은 비어 있을 수 없습니다.')
        except ValueError as exc:
            parser.error(str(exc))
    if args.workflow_task:
        try:
            sites = [URLMatcher.normalize_url(u) for u in args.site]
        except ValueError as exc:
            parser.error(str(exc))
        if not args.workflow_task.strip():
            parser.error('전체 작업은 비어 있을 수 없습니다.')
        jobs.append({'task': args.workflow_task, 'urls': sites})
    elif args.site:
        parser.error('--site는 --task와 함께 사용하세요.')
    return args, jobs


def interactive_jobs(registry=None):
    while True:
        try:
            raw_url = input('\n대상 URL (/task: 사이트 통합 작업, /skills: 학습 목록, /quit: 종료): ').strip()
            if not raw_url:
                continue
            cmd = raw_url.lower()
            if cmd in {'/quit', 'quit', '/exit', 'exit', 'q'}:
                return
            if cmd in {'/task', 'task'}:
                task = input('여러 사이트에서 수행할 전체 작업: ').strip()
                if task.lower() in {'/quit', 'quit', '/exit', 'exit'}:
                    return
                if task:
                    yield {'task': task, 'urls': []}
                continue
            if cmd in {'/skills', 'skills'}:
                for skill in registry.list_all_skills() if registry else []:
                    print(f"{skill['normalized_url']} | v{skill['version']} | "
                          + ', '.join(t['name'] for t in skill['tools']))
                continue
            parts = raw_url.split(maxsplit=1)
            url = URLMatcher.normalize_url(parts[0])
            extra_note = parts[1].strip() if len(parts) > 1 else ''
            task = input('원하는 작업: ').strip()
            if task.lower() in {'/quit', 'quit', '/exit', 'exit'}:
                return
            if extra_note and task:
                task = f"{task} ({extra_note})"
            elif extra_note and not task:
                task = extra_note
            if not url or not task:
                print('URL과 작업을 모두 입력하세요.')
                continue
            yield {'url': url, 'task': task}
        except ValueError as exc:
            print(f'잘못된 URL: {exc}')
        except (EOFError, KeyboardInterrupt):
            return


def main():
    args, jobs = parse_jobs()
    # Lazy imports allow input parsing without loading browser/API dependencies.
    from site_agent import RootAgent
    from browser import Browser
    from gemini import Gemini
    from workflow import WorkflowAgent

    model = Gemini()
    browser = Browser(profile=args.profile)
    agent = RootAgent(model, browser)
    workflow = WorkflowAgent(model, browser, agent.registry, worker=agent)
    failed = False
    try:
        print('에이전트 실행 중: URL별 기능은 자동 저장되며 재시작 후에도 유지됩니다.')
        requests = iter(jobs) if args.once else chain(jobs, interactive_jobs(agent.registry))
        for job in requests:
            result = (agent.run(job['url'], job['task']) if 'url' in job
                      else workflow.run(job['task'], job.get('urls')))
            result['request'] = job
            skills = agent.registry.list_all_skills()
            result['knowledge'] = {'urls': len(skills), 'tools': sum(len(s['tools']) for s in skills)}
            Path('runs').mkdir(exist_ok=True)
            report = Path('runs') / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k: v for k, v in result.items() if k != 'events'}, ensure_ascii=False, indent=2))
            print(f'실행 기록: {report}')
            failed |= result['status'] != 'success'
        return 1 if failed else 0
    finally:
        browser.close()


if __name__ == '__main__':
    raise SystemExit(main())
