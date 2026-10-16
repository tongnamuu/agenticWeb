import tempfile
import unittest
from agentic_web.engine.schemas import Tool, Step
from agentic_web.engine.skills.registry import SkillRegistry


class GrowthTests(unittest.TestCase):
    def test_additive_capabilities_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SkillRegistry(directory)
            def tool(name):
                return Tool(name=name, description=name,
                            steps=[Step(action='click', selector='#'+name)]).model_dump()
            registry.register_skill('https://a.test/', [tool('add')])
            registry.register_skill('https://a.test/', [tool('delete')])
            registry.register_skill('https://b.test/', [tool('search')])
            loaded = SkillRegistry(directory)
            package, _ = loaded.resolve_skill_by_url('https://a.test/')
            self.assertEqual({t['name'] for t in package['tools']}, {'add', 'delete'})
            self.assertEqual(package['version'], 2)
            self.assertEqual(len(loaded.list_all_skills()), 2)
