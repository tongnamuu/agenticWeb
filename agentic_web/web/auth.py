"""Google OpenID Connect login and request authentication."""
import secrets

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, current_app, g, jsonify, redirect, render_template, request, session, url_for


auth = Blueprint('auth', __name__)


def google_client():
    return current_app.extensions['google_oauth'].create_client('google')


def configured():
    return bool(current_app.config.get('GOOGLE_CLIENT_ID') and current_app.config.get('GOOGLE_CLIENT_SECRET'))


def csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    return session['csrf_token']


def valid_csrf(value):
    expected = session.get('csrf_token')
    return (isinstance(value, str) and isinstance(expected, str) and bool(expected)
            and secrets.compare_digest(value.encode('utf-8'), expected.encode('utf-8')))


def init_auth(app):
    oauth = OAuth(app)
    oauth.register('google',
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile', 'code_challenge_method': 'S256'},
    )
    app.extensions['google_oauth'] = oauth
    app.register_blueprint(auth)
    app.jinja_env.globals['csrf_token'] = csrf_token
    app.before_request(protect_request)
    app.after_request(private_response)


def protect_request():
    g.user = session.get('user')
    if request.path.startswith('/api/'):
        if not g.user:
            return jsonify(ok=False, error='Google 로그인이 필요합니다.', login_url=url_for('auth.login')), 401
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if not valid_csrf(request.headers.get('X-CSRF-Token', '')):
                return jsonify(ok=False, error='요청 인증이 만료되었습니다. 페이지를 새로고침하세요.'), 403


def private_response(response):
    if request.path == '/' or request.path.startswith(('/auth/', '/api/')):
        response.headers['Cache-Control'] = 'no-store'
    return response


@auth.route('/auth/login', methods=['GET'])
def login():
    if session.get('user'):
        return redirect(url_for('studio.index'))
    return render_template('login.html', configured=configured(), error=None)


@auth.route('/auth/google', methods=['POST'])
def google_login():
    if not valid_csrf(request.form.get('csrf_token', '')):
        return render_template('login.html', configured=configured(), error='페이지를 새로고침하세요.'), 403
    if not configured():
        return render_template('login.html', configured=False, error='Google 로그인 설정이 필요합니다.'), 503
    # Fixed redirect URI avoids trusting Host/X-Forwarded-Host headers.
    session.clear()
    try:
        return google_client().authorize_redirect(current_app.config['GOOGLE_REDIRECT_URI'],
                                                   nonce=secrets.token_urlsafe(32), prompt='select_account')
    except Exception:
        session.clear()
        return render_template('login.html', configured=True, error='Google 로그인에 연결하지 못했습니다. 다시 시도하세요.'), 502


@auth.route('/auth/google/callback')
def google_callback():
    if not configured():
        return render_template('login.html', configured=False, error='Google 로그인 설정이 필요합니다.'), 503
    try:
        # Authlib validates state, signature, issuer, audience, expiry and nonce.
        token = google_client().authorize_access_token()
        user = token.get('userinfo')
        if not token.get('id_token') or not user or not user.get('sub') or user.get('email_verified') is not True:
            raise ValueError('Missing verified identity')
        identity = {'id': user['sub'], 'email': user.get('email', ''), 'name': user.get('name') or user.get('email', '')}
    except Exception:
        session.clear()
        return render_template('login.html', configured=True, error='Google 로그인을 확인하지 못했습니다. 다시 로그인하세요.'), 400
    session.clear()
    session.permanent = True
    session['user'] = identity
    csrf_token()
    # OAuth/ID/refresh tokens are never persisted in the browser session.
    return redirect(url_for('studio.index'))


@auth.route('/auth/logout', methods=['POST'])
def logout():
    if not valid_csrf(request.form.get('csrf_token', '')):
        return '잘못된 요청입니다.', 403
    session.clear()
    return redirect(url_for('auth.login'))


@auth.route('/auth/me')
def me():
    if not session.get('user'):
        return jsonify(ok=False, user=None), 401
    return jsonify(ok=True, user=session['user'])
