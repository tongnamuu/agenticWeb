import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from joserfc import jwt
from joserfc.jwk import RSAKey
from agentic_web.web import create_app


class GoogleAuthTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({'TESTING':True, 'SECRET_KEY':'test-secret',
            'GOOGLE_CLIENT_ID':'test-client', 'GOOGLE_CLIENT_SECRET':'test-client-secret'})
        self.client = self.app.test_client()
        with self.app.app_context():
            self.provider = self.app.extensions['google_oauth'].create_client('google')
        self.metadata = {'issuer':'https://accounts.google.com',
            'authorization_endpoint':'https://accounts.google.com/o/oauth2/v2/auth',
            'token_endpoint':'https://oauth2.googleapis.com/token',
            'jwks_uri':'https://www.googleapis.com/oauth2/v3/certs',
            'id_token_signing_alg_values_supported':['RS256']}

    def test_anonymous_requests_are_blocked(self):
        self.assertEqual(self.client.get('/').status_code,302)
        for path in ['/api/skills','/api/workflows','/api/state']:
            self.assertEqual(self.client.get(path).status_code,401)
        self.assertEqual(self.client.post('/api/execute',json={'user_id':'fake'}).status_code,401)
        self.assertEqual(self.client.get('/auth/me').status_code,401)
        self.assertIn('Google 계정으로 로그인',self.client.get('/auth/login').text)

    def test_login_has_state_nonce_pkce_and_fixed_callback(self):
        with self.client.session_transaction() as session: session['csrf_token']='csrf'
        with patch.object(self.provider,'load_server_metadata',return_value=self.metadata):
            response=self.client.post('/auth/google',data={'csrf_token':'csrf'},headers={'Host':'evil.test'})
        # Host changes cookies' scope, so an unrelated Host must fail CSRF.
        self.assertEqual(response.status_code,403)
        with patch.object(self.provider,'load_server_metadata',return_value=self.metadata):
            response=self.client.post('/auth/google',data={'csrf_token':'csrf'})
        query=parse_qs(urlsplit(response.location).query)
        self.assertTrue(query['state'][0]);self.assertTrue(query['nonce'][0])
        self.assertEqual(query['code_challenge_method'],['S256'])
        self.assertEqual(query['redirect_uri'],['http://127.0.0.1:5055/auth/google/callback'])
        self.assertEqual(set(query['scope'][0].split()),{'openid','email','profile'})

    def test_forged_state_never_exchanges_tokens(self):
        with patch.object(self.provider,'fetch_access_token') as fetch:
            self.assertEqual(self.client.get('/auth/google/callback?code=fake&state=forged').status_code,400)
            fetch.assert_not_called()

    def callback(self, audience='test-client', nonce='expected', expired=False, wrong_signature=False):
        key=RSAKey.generate_key(2048)
        claims={'iss':'https://accounts.google.com','sub':'google-user-123','aud':audience,
            'iat':int(time.time())-1000,'exp':int(time.time())+300 if not expired else int(time.time())-500,
            'nonce':nonce,'email':'test@example.com','email_verified':True,'name':'Test'}
        token=jwt.encode({'alg':'RS256','kid':'test-key'},claims,key)
        with self.client.session_transaction() as session:
            self.provider.framework.set_state_data(session,'valid-state',{'nonce':'expected','redirect_uri':self.app.config['GOOGLE_REDIRECT_URI']})
        public=(RSAKey.generate_key(2048) if wrong_signature else key).as_dict(private=False);public['kid']='test-key'
        with patch.object(self.provider,'load_server_metadata',return_value=self.metadata), \
             patch.object(self.provider,'fetch_jwk_set',return_value={'keys':[public]}), \
             patch.object(self.provider,'fetch_access_token',return_value={'id_token':token}):
            return self.client.get('/auth/google/callback?code=code&state=valid-state')

    def test_verified_login_stores_identity_without_tokens(self):
        self.assertEqual(self.callback().status_code,302)
        with self.client.session_transaction() as session:
            self.assertEqual(session['user']['id'],'google-user-123')
            self.assertNotIn('id_token',session);self.assertNotIn('access_token',session)
            csrf=session['csrf_token']
        self.assertEqual(self.client.get('/auth/me').json['user']['email'],'test@example.com')
        self.assertEqual(self.client.post('/api/analyze',json={}).status_code,403)
        self.assertEqual(self.client.post('/api/analyze',json={},headers={'X-CSRF-Token':csrf}).status_code,400)
        self.assertEqual(self.client.get('/auth/logout').status_code,405)
        self.assertEqual(self.client.post('/auth/logout',data={'csrf_token':csrf}).status_code,302)
        self.assertEqual(self.client.get('/auth/me').status_code,401)

    def test_wrong_audience_nonce_and_expired_token_rejected(self):
        for kwargs in [{'audience':'another-client'},{'nonce':'wrong'},{'expired':True},{'wrong_signature':True}]:
            self.assertEqual(self.callback(**kwargs).status_code,400)
            self.assertEqual(self.client.get('/auth/me').status_code,401)

    def test_missing_configuration_disables_login(self):
        app=create_app({'TESTING':True,'SECRET_KEY':'test','GOOGLE_CLIENT_ID':'','GOOGLE_CLIENT_SECRET':''})
        with app.test_client() as client:
            self.assertIn('disabled',client.get('/auth/login').text)
            with client.session_transaction() as session: session['csrf_token']='csrf'
            self.assertEqual(client.post('/auth/google',data={'csrf_token':'csrf'}).status_code,503)

    def test_callback_cannot_be_replayed(self):
        self.assertEqual(self.callback().status_code,302)
        self.assertEqual(self.client.get('/auth/google/callback?code=code&state=valid-state').status_code,400)

    def test_https_callback_sets_secure_cookie(self):
        app=create_app({'SECRET_KEY':'test','GOOGLE_CLIENT_ID':'id','GOOGLE_CLIENT_SECRET':'secret',
                        'GOOGLE_REDIRECT_URI':'https://agent.example.com/auth/google/callback'})
        self.assertTrue(app.config['SESSION_COOKIE_SECURE'])
        self.assertTrue(app.config['SESSION_COOKIE_HTTPONLY'])
        self.assertEqual(app.config['SESSION_COOKIE_SAMESITE'],'Lax')

    def test_nonlocal_http_callback_is_rejected(self):
        with self.assertRaises(ValueError):
            create_app({'SECRET_KEY':'test','GOOGLE_REDIRECT_URI':'http://evil.test/auth/google/callback'})
