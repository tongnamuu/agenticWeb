"""
GCP Cloud Firestore persistence adapter for SkillRegistry and SavedWorkflows.

Stores per-user and global skills and workflows as native JSON-compatible documents
in Cloud Firestore collections (`agentic_web_skills` and `agentic_web_workflows`),
while maintaining local filesystem mirror/fallback for offline development and tests.
"""
import os
import re
import sys
from typing import Any, Dict, List, Optional

from agentic_web.config import (
    FIRESTORE_DATABASE,
    FIRESTORE_PROJECT_ID,
    FIRESTORE_SKILLS_COLLECTION,
    FIRESTORE_WORKFLOWS_COLLECTION,
)

_firestore_client: Any = None
_firestore_init_failed: bool = False


def is_firestore_enabled(use_firestore: Optional[bool] = None) -> bool:
    """Returns True if Cloud Firestore persistence should be active."""
    if use_firestore is not None:
        return bool(use_firestore)
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return False
    env_flag = os.environ.get("USE_FIRESTORE", "").strip().lower()
    if env_flag in ("false", "0", "no"):
        return False
    if env_flag in ("true", "1", "yes"):
        return True
    # Auto-enable when deployed on Google Cloud Run (K_SERVICE is always set by Cloud Run)
    return bool(os.environ.get("K_SERVICE"))


def get_firestore_client() -> Optional[Any]:
    """Lazily initializes and returns a singleton google.cloud.firestore.Client."""
    global _firestore_client, _firestore_init_failed
    if _firestore_client is not None:
        return _firestore_client
    if _firestore_init_failed:
        return None
    try:
        from google.cloud import firestore  # type: ignore[import-untyped]

        kwargs: Dict[str, Any] = {}
        project_id = (
            os.environ.get("FIRESTORE_PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or FIRESTORE_PROJECT_ID
        )
        if project_id:
            kwargs["project"] = project_id
        database = os.environ.get("FIRESTORE_DATABASE") or FIRESTORE_DATABASE
        if database and database != "(default)":
            kwargs["database"] = database

        _firestore_client = firestore.Client(**kwargs)
        return _firestore_client
    except Exception as exc:
        _firestore_init_failed = True
        print(f"[Firestore] Warning: Firestore client unavailable, using local disk fallback ({exc})")
        return None


def resolve_store_scope(target_dir: str, root_dir: str) -> Optional[str]:
    """
    Computes a stable scope key (e.g. 'global' or 'users/<sha256>') when `target_dir`
    is `root_dir` or a subdirectory of `root_dir`. Returns None for external temp dirs.
    """
    abs_target = os.path.abspath(target_dir)
    abs_root = os.path.abspath(root_dir)
    if abs_target == abs_root:
        return "global"
    if abs_target.startswith(abs_root + os.sep):
        rel = os.path.relpath(abs_target, abs_root).replace(os.sep, "/")
        return rel
    return None


class ScopedFirestoreCollection:
    """
    Wraps a Firestore collection (`agentic_web_skills` or `agentic_web_workflows`)
    with automatic per-user / global scope isolation (`_scope` field + prefixed doc ID).
    """

    def __init__(
        self,
        collection_name: str,
        scope: Optional[str],
        use_firestore: Optional[bool] = None,
    ):
        self.collection_name = collection_name
        self.scope = scope or ("global" if use_firestore else None)
        self.enabled = bool(self.scope) and is_firestore_enabled(use_firestore)

    def _doc_key(self, doc_id: str) -> str:
        safe_scope = re.sub(r"[^a-zA-Z0-9_-]+", "_", self.scope or "global").strip("_")
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", doc_id)
        return f"{safe_scope}__{safe_id}"

    def _collection(self) -> Optional[Any]:
        if not self.enabled:
            return None
        client = get_firestore_client()
        if client is None:
            return None
        return client.collection(self.collection_name)

    def list_documents(self) -> List[Dict[str, Any]]:
        """Loads all documents belonging to `self.scope` from Firestore."""
        col = self._collection()
        if col is None:
            return []
        try:
            try:
                from google.cloud.firestore_v1.base_query import FieldFilter  # type: ignore[import-untyped]
                query = col.where(filter=FieldFilter("_scope", "==", self.scope))
            except ImportError:
                query = col.where("_scope", "==", self.scope)
            results: List[Dict[str, Any]] = []
            for snap in query.stream():
                data = snap.to_dict()
                if isinstance(data, dict):
                    data.pop("_scope", None)
                    results.append(data)
            return results
        except Exception as exc:
            print(f"[Firestore] Warning: Failed to list {self.collection_name} (scope={self.scope}): {exc}")
            return []

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Fetches a single document by ID within `self.scope`."""
        col = self._collection()
        if col is None:
            return None
        try:
            snap = col.document(self._doc_key(doc_id)).get()
            if not snap.exists:
                return None
            data = snap.to_dict()
            if isinstance(data, dict):
                data.pop("_scope", None)
                return data
            return None
        except Exception as exc:
            print(f"[Firestore] Warning: Failed to get {doc_id} from {self.collection_name}: {exc}")
            return None

    def set_document(self, doc_id: str, data: Dict[str, Any]) -> bool:
        """Upserts a document by ID within `self.scope`."""
        col = self._collection()
        if col is None:
            return False
        try:
            payload = dict(data)
            payload["_scope"] = self.scope
            col.document(self._doc_key(doc_id)).set(payload)
            return True
        except Exception as exc:
            print(f"[Firestore] Warning: Failed to save {doc_id} to {self.collection_name}: {exc}")
            return False

    def delete_document(self, doc_id: str) -> bool:
        """Deletes a document by ID within `self.scope`."""
        col = self._collection()
        if col is None:
            return False
        try:
            col.document(self._doc_key(doc_id)).delete()
            return True
        except Exception as exc:
            print(f"[Firestore] Warning: Failed to delete {doc_id} from {self.collection_name}: {exc}")
            return False


__all__ = [
    "FIRESTORE_SKILLS_COLLECTION",
    "FIRESTORE_WORKFLOWS_COLLECTION",
    "ScopedFirestoreCollection",
    "get_firestore_client",
    "is_firestore_enabled",
    "resolve_store_scope",
]
