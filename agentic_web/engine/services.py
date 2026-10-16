"""User-scoped engines. No default sites or skills are seeded."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock
from agentic_web.config import SKILLS_STORE_DIR, WORKFLOWS_STORE_DIR
from agentic_web.engine.browser.cdp import BrowserSession
from agentic_web.engine.agents.orchestrator import RootOrchestratorAgent
from agentic_web.engine.skills.registry import SkillRegistry
from agentic_web.engine.workflows.saved import SavedWorkflows
import os


@dataclass
class UserServices:
    browser: BrowserSession
    orchestrator: RootOrchestratorAgent
    workflows: SavedWorkflows
    lock: Lock


class UserServicePool:
    def __init__(self, skills_root=SKILLS_STORE_DIR, workflows_root=WORKFLOWS_STORE_DIR):
        self.skills_root, self.workflows_root = Path(skills_root), Path(workflows_root)
        self.users, self.lock = {}, Lock()

    def for_user(self, user_id):
        if not isinstance(user_id, str) or not user_id:
            raise ValueError('Authenticated user ID required')
        key = sha256(user_id.encode()).hexdigest()
        with self.lock:
            if key not in self.users:
                registry = SkillRegistry(str(self.skills_root/'users'/key))
                browser = BrowserSession(headless=os.environ.get('HEADLESS_BROWSER','true').lower() in ('1','true','yes'))
                orchestrator = RootOrchestratorAgent(registry=registry)
                workflows = SavedWorkflows(registry, browser, orchestrator, str(self.workflows_root/'users'/key))
                self.users[key] = UserServices(browser, orchestrator, workflows, Lock())
            return self.users[key]
