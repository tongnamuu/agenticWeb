import tempfile
import unittest
from unittest.mock import Mock, patch
from agentic_web.engine.browser.remote import control
from agentic_web.engine.services import UserServicePool
from agentic_web.web import create_app


class RemoteBrowserTests(unittest.TestCase):
    def test_only_supported_input_reaches_cdp(self):
        browser = Mock()
        control(browser, {'action':'click', 'x':10, 'y':20})
        self.assertEqual(browser._send_cdp.call_count, 2)
        browser.reset_mock()
        for payload in [{'action':'evaluate','script':'bad'}, {'action':'click','x':float('nan'),'y':0},
                        {'action':'key','key':'unsupported'}, {'action':'text','text':None}]:
            with self.assertRaises(ValueError): control(browser, payload)
        browser._send_cdp.assert_not_called()
        control(browser, {'action':'text','text':'hello'})
        browser._send_cdp.assert_called_once_with('Input.insertText', {'text':'hello'})

    def test_remote_screen_is_authenticated_private_and_user_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app({'TESTING':True, 'SECRET_KEY':'test'})
            pool = UserServicePool(directory+'/skills', directory+'/workflows')
            app.extensions['user_services'] = pool
            with app.test_client() as client:
                self.assertEqual(client.get('/api/browser/frame').status_code,401)
                self.assertEqual(client.get('/browser-session').status_code,302)
                for user in ('alice','bob'):
                    with client.session_transaction() as session:
                        session['user'] = {'id':user}
                        session['csrf_token'] = 'csrf'
                    services = pool.for_user(user)
                    with patch('agentic_web.engine.browser.remote.frame', side_effect=lambda browser: {'url':user,'screenshot':''} if browser._get_current_object() is services.browser else self.fail('wrong user browser')):
                        response = client.get('/api/browser/frame')
                        self.assertEqual(response.json['url'],user)
                        self.assertEqual(response.headers['Cache-Control'],'no-store')
                    self.assertEqual(client.post('/api/browser/control',json={'action':'text','text':'secret'}).status_code,403)
                    with patch('agentic_web.engine.browser.remote.control') as controller:
                        response = client.post('/api/browser/control',json={'action':'key','key':'Tab'},headers={'X-CSRF-Token':'csrf'})
                        self.assertEqual(response.status_code,200)
                        controller.assert_called_once()
                    page = client.get('/browser-session')
                    self.assertEqual(page.headers['X-Frame-Options'],'SAMEORIGIN')
                    self.assertEqual(page.headers['Cache-Control'],'no-store')
