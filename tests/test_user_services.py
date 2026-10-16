from pathlib import Path
import tempfile
import unittest
from agentic_web.engine.services import UserServicePool
from agentic_web.engine.skills.registry import SkillRegistry
from agentic_web.engine.schemas import Tool, Step


class UserIsolationTests(unittest.TestCase):
    def test_first_use_empty_and_users_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            # Legacy global data must never become a new user's initial data.
            tool=Tool(name='add',description='add',steps=[Step(action='click',selector='#add')]).model_dump()
            SkillRegistry(str(root/'skills')).register_skill('http://legacy.test/',[tool])
            pool=UserServicePool(root/'skills',root/'workflows')
            alice,bob=pool.for_user('alice'),pool.for_user('bob')
            self.assertEqual(alice.orchestrator.registry.list_all_skills(),[])
            self.assertEqual(alice.workflows.list(),[])
            alice.orchestrator.registry.register_skill('http://blog.test/',[tool])
            self.assertEqual(bob.orchestrator.registry.list_all_skills(),[])
            self.assertIsNot(alice.browser,bob.browser)
            self.assertIsNot(alice.lock,bob.lock)
            restarted=UserServicePool(root/'skills',root/'workflows')
            self.assertEqual(len(restarted.for_user('alice').orchestrator.registry.list_all_skills()),1)
            self.assertEqual(restarted.for_user('bob').orchestrator.registry.list_all_skills(),[])
