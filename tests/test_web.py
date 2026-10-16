import ast
from pathlib import Path
import unittest
from agentic_web.web import create_app
from agentic_web.config import BASE_DIR, SKILLS_STORE_DIR, WORKFLOWS_STORE_DIR


class WebPackageTests(unittest.TestCase):
    def test_pages_assets_and_api_survive_package_move(self):
        app = create_app({'TESTING': True, 'SECRET_KEY': 'test-key'})
        with app.test_client() as client:
            with client.session_transaction() as session:
                session['user'] = {'id':'test-user','name':'Test','email':'test@example.com'}
                session['csrf_token'] = 'test-csrf'
            page = client.get('/')
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(b'workflow-catalog', page.data)
            self.assertIn(b'analysis-status', page.data)
            self.assertNotIn(b'createWorkflowFromGoal',page.data)
            self.assertIn(b'data-skill-execute',page.data)
            for path in ['/workflows','/api/workflows','/static/workflows.js']:
                with client.get(path) as response:
                    self.assertEqual(response.status_code,200)
            self.assertEqual(client.get('/api/skills').status_code,200)
            self.assertEqual(client.get('/demo-blog').status_code, 404)
            self.assertEqual(client.get('/demo-protected').status_code, 404)
            self.assertEqual(client.post('/api/analyze', json={}, headers={'X-CSRF-Token':'test-csrf'}).status_code, 400)
            self.assertEqual(client.post('/api/execute', json={'goal':'test'}, headers={'X-CSRF-Token':'test-csrf'}).status_code, 400)

    def test_store_paths_remain_at_project_root(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(Path(BASE_DIR), root)
        self.assertEqual(Path(SKILLS_STORE_DIR), root/'skills_store')
        self.assertEqual(Path(WORKFLOWS_STORE_DIR), root/'workflows_store')

    def test_engine_does_not_import_web_layer(self):
        engine = Path(__file__).resolve().parents[1]/'agentic_web'/'engine'
        for path in engine.rglob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                modules = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                           [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                for module in modules:
                    self.assertFalse(module == 'app' or module.startswith(('flask', 'agentic_web.web')), str(path))
