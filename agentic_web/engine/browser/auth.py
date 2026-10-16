"""Detect login forms without assuming email/username labels."""
import re
from urllib.parse import urlparse


def requires_login(snapshot):
    state = snapshot.get('authState', {})
    if state.get('login_wall_detected'):
        return True
    inputs = [item for item in snapshot.get('inputs', []) if item.get('visible', True)]
    has_password = any(item.get('type') == 'password' for item in inputs)
    if not has_password:
        return False
    path = urlparse(snapshot.get('url', '')).path
    controls = ' '.join(str(item.get('text') or item.get('accessible_name') or '') for item in snapshot.get('buttons', []))
    return bool(re.search(r'(?:^|/)(?:login|signin|sign-in|auth)(?:/|$)', path, re.I)
                or re.search(r'\blog\s*in\b|\bsign\s*in\b|로그인', controls, re.I))
