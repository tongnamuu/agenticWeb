import os
from typing import Dict, List
from dotenv import load_dotenv

load_dotenv(override=False)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_STORE_DIR = os.path.join(BASE_DIR, "skills_store")
REGISTRY_INDEX_PATH = os.path.join(SKILLS_STORE_DIR, "registry_index.json")

os.makedirs(SKILLS_STORE_DIR, exist_ok=True)


def get_api_key(override_key: str = "") -> str:
    """Retrieves the Gemini API Key from argument or environment."""
    if override_key and override_key.strip():
        return override_key.strip()
    load_dotenv(override=False)
    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()


TARGET_URL: str = os.environ.get(
    "TARGET_URL", "https://tongnamuu.github.io/ToDoList/"
).strip()

# Role-specific Gemini model selection:
# - root_verifier: High-precision reasoning for DOM state diff verification & retry diagnosis
# - skill_synthesizer: Deep structural DOM analysis & reusable JSON Schema + step pipeline synthesis
# - skill_executor: Low-latency Function Calling & HITL input detection
MODEL_ROLES: Dict[str, Dict[str, object]] = {
    "root_verifier": {
        "primary": os.environ.get("GEMINI_ROOT_MODEL", "gemini-2.5-pro"),
        "fallbacks": ["gemini-2.5-flash", "gemini-2.0-flash"],
    },
    "skill_synthesizer": {
        "primary": os.environ.get("GEMINI_SYNTHESIZER_MODEL", "gemini-2.5-pro"),
        "fallbacks": ["gemini-2.5-flash", "gemini-2.0-flash"],
    },
    "skill_executor": {
        "primary": os.environ.get("GEMINI_EXECUTOR_MODEL", "gemini-2.5-flash"),
        "fallbacks": ["gemini-2.0-flash", "gemini-2.5-pro"],
    },
}

# Root Agent Retry Loop Policy:
# When post-execution verification detects failure (e.g., selector mismatch or unverified DOM state),
# the Root Agent loops strictly 1 additional time (Attempt 1 -> Verify Fail -> Re-analyze/Repair -> Attempt 2).
MAX_ROOT_RETRY_COUNT: int = 1

def get_model_candidates(role: str) -> List[str]:
    """Returns ordered list of Gemini model candidates for a specific agent role."""
    role_cfg = MODEL_ROLES.get(role, MODEL_ROLES["skill_executor"])
    primary = str(role_cfg["primary"])
    fallbacks = list(role_cfg["fallbacks"])  # type: ignore[arg-type]
    ordered: List[str] = [primary]
    for m in fallbacks:
        if m not in ordered:
            ordered.append(m)
    return ordered
