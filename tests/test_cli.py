import unittest
from main import parse_jobs


class InputTests(unittest.TestCase):
    def test_multiple_urls(self):
        _, jobs = parse_jobs(['--job', 'https://a.test/todo', '추가',
                              '--job', 'https://b.test/', '확인'])
        self.assertEqual([j['url'] for j in jobs], ['https://a.test/todo', 'https://b.test/'])
        self.assertEqual([j['task'] for j in jobs], ['추가', '확인'])

    def test_single_compatible(self):
        _, jobs = parse_jobs(['https://a.test', '확인'])
        self.assertEqual(jobs, [{'url': 'https://a.test/', 'task': '확인'}])

    def test_interactive_mode(self):
        _, jobs = parse_jobs([])
        self.assertEqual(jobs, [])

    def test_once_is_explicit(self):
        args, _ = parse_jobs(['https://a.test', '확인'])
        self.assertFalse(args.once)
        args, _ = parse_jobs(['--once', 'https://a.test', '확인'])
        self.assertTrue(args.once)

    def test_cross_site_task(self):
        _, jobs = parse_jobs(['--task', '할 일 추가 후 글 쓰고 완료',
                              '--site', 'https://todo.test/', '--site', 'https://blog.test/'])
        self.assertNotIn('url', jobs[0])
        self.assertEqual(jobs[0]['urls'], ['https://todo.test/', 'https://blog.test/'])

    def test_inline_url_description(self):
        _, jobs = parse_jobs(['http://168.110.118.148/ 블로그 글쓰기 사이트야', '글쓰기'])
        self.assertEqual(jobs, [{'url': 'http://168.110.118.148/', 'task': '글쓰기 (블로그 글쓰기 사이트야)'}])
