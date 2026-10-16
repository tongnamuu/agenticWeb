"""Web application assembly; engine modules do not depend on Flask."""
import os
import secrets
from datetime import timedelta
from urllib.parse import urlsplit
from flask import Flask
from agentic_web import config  # Load the project-root .env before reading settings.


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('FLASK_SECRET_KEY'),
        GOOGLE_CLIENT_ID=os.environ.get('GOOGLE_CLIENT_ID', ''),
        GOOGLE_CLIENT_SECRET=os.environ.get('GOOGLE_CLIENT_SECRET', ''),
        GOOGLE_REDIRECT_URI=os.environ.get('GOOGLE_REDIRECT_URI', 'http://127.0.0.1:5055/auth/google/callback'),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    if test_config:
        app.config.update(test_config)
    if not app.secret_key:
        if app.config['GOOGLE_CLIENT_ID'] or app.config['GOOGLE_CLIENT_SECRET']:
            raise RuntimeError('Google 로그인을 사용하려면 FLASK_SECRET_KEY를 설정하세요.')
        app.secret_key = secrets.token_hex(32)
    callback = urlsplit(app.config['GOOGLE_REDIRECT_URI'])
    if not callback.netloc or callback.username or callback.password or callback.query or callback.fragment or callback.path != '/auth/google/callback':
        raise ValueError('GOOGLE_REDIRECT_URI는 /auth/google/callback 경로의 절대 URL이어야 합니다.')
    if callback.scheme != 'https' and not (callback.scheme == 'http' and callback.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('배포 환경의 GOOGLE_REDIRECT_URI는 HTTPS여야 합니다.')
    app.config['SESSION_COOKIE_SECURE'] = callback.scheme == 'https'
    from .auth import init_auth
    init_auth(app)
    from agentic_web.engine.services import UserServicePool
    from .analysis_jobs import AnalysisJobs
    app.extensions['user_services'] = UserServicePool()
    app.extensions['analysis_jobs'] = AnalysisJobs()
    @app.context_processor
    def model_context():
        return {'model_roles': config.MODEL_ROLES}

    from .routes import routes
    app.register_blueprint(routes)
    return app
