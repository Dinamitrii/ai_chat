"""Isolated tests. Never call a live AI service or touch the production database."""
import base64
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sandbox = tempfile.TemporaryDirectory()
os.environ['AI_DATA_DIR'] = sandbox.name
spec = importlib.util.spec_from_file_location('portal_test', Path(__file__).with_name('app.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.app.config['TESTING'] = True


class PortalTests(unittest.TestCase):
    def setUp(self):
        with m.app.app_context():
            c = m.db()
            for table in ['generated_images', 'usage_events', 'messages', 'facts', 'conversations', 'sessions', 'payment_events', 'users', 'auth_attempts']:
                c.execute('DELETE FROM ' + table)
        self.client = m.app.test_client()
        self.register(self.client, 'a@example.com')

    def post(self, path, data=None, client=None):
        client = client or self.client
        with client.session_transaction() as s:
            csrf = s['csrf']
        return client.post(path, json=data or {}, headers={'X-CSRF-Token': csrf})

    def register(self, client, email):
        client.get('/register')
        with client.session_transaction() as s:
            csrf = s['csrf']
        r = client.post('/register', data={'email': email, 'password': 'long-test-password', 'csrf': csrf, 'plan': 'paid'})
        self.assertEqual(r.status_code, 302)

    def event(self, kind, amount):
        with m.app.app_context():
            m.db().execute("INSERT INTO usage_events(user_id,kind,status,charged) VALUES(1,?,'success',?)", (kind, amount))

    def mock_llama(self, path, data, timeout=30):
        if path == '/apply-template':
            return {'prompt': 'rendered prompt'}
        if path == '/tokenize':
            return {'tokens': list(range(100))}
        self.assertLessEqual(data['max_tokens'] + 116, 1500)
        return {'choices': [{'message': {'content': 'Здравей!'}}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}

    def test_auth_csrf_hash_and_server_plan(self):
        self.assertEqual(self.client.get('/api/state').json['plan'], 'free')
        self.assertEqual(self.client.post('/api/reset', json={}).status_code, 403)
        with m.app.app_context():
            h = m.db().execute('SELECT password_hash FROM users').fetchone()[0]
            self.assertTrue(h.startswith('scrypt:'))
            self.assertNotIn('long-test-password', h)
        self.assertEqual(self.post('/logout').status_code, 302)
        self.assertEqual(self.client.get('/api/state').status_code, 401)

    def test_chat_usage_memory_reset_and_isolation(self):
        self.post('/api/facts', {'content': 'Казвам се Иван.'})
        with patch.object(m, 'llama_post', side_effect=self.mock_llama) as backend:
            self.assertEqual(self.post('/api/chat', {'message': 'Здравей'}).status_code, 200)
            sent = [c for c in backend.call_args_list if c.args[0] == '/v1/chat/completions'][0].args[1]
            self.assertIn('Иван', sent['messages'][0]['content'])
        s = self.client.get('/api/state').json
        self.assertEqual(s['usage']['chat']['used'], 120)
        self.assertEqual(len(s['messages']), 2)
        other = m.app.test_client()
        self.register(other, 'b@example.com')
        self.assertEqual(other.get('/api/state').json['messages'], [])
        self.post('/api/facts', {'delete_id': s['facts'][0]['id']}, client=other)
        self.assertEqual(len(self.client.get('/api/state').json['facts']), 1)
        self.post('/api/reset')
        s = self.client.get('/api/state').json
        self.assertEqual(s['messages'], [])
        self.assertEqual(s['usage']['chat']['used'], 120)
        self.assertEqual(len(s['facts']), 1)

    def test_exhausted_chat_does_not_call_backend(self):
        self.event('chat', 1500)
        with patch.object(m, 'llama_post') as p:
            self.assertEqual(self.post('/api/chat', {'message': 'Hello'}).status_code, 402)
            p.assert_not_called()

    def test_exact_remaining_restricts_reply(self):
        self.event('chat', 1300)
        with patch.object(m, 'llama_post', side_effect=self.mock_llama) as p:
            self.assertEqual(self.post('/api/chat', {'message': 'Hello'}).status_code, 200)
            sent = [c for c in p.call_args_list if c.args[0] == '/v1/chat/completions'][0].args[1]
            self.assertEqual(sent['max_tokens'], 84)
        self.assertEqual(self.client.get('/api/state').json['usage']['chat']['used'], 1420)

    def test_tokenizer_unavailable_is_not_charged(self):
        with patch.object(m, 'llama_post', side_effect=m.requests.ConnectionError()):
            self.assertEqual(self.post('/api/chat', {'message': 'Hello'}).status_code, 502)
        self.assertEqual(self.client.get('/api/state').json['usage']['chat']['used'], 0)

    def test_timeout_retains_reservation_and_operator_reconciliation(self):
        def backend(path, data, timeout=30):
            if path == '/v1/chat/completions':
                raise m.requests.Timeout()
            return self.mock_llama(path, data, timeout)
        with patch.object(m, 'llama_post', side_effect=backend):
            self.assertEqual(self.post('/api/chat', {'message': 'Hello'}).status_code, 502)
        self.assertEqual(self.client.get('/api/state').json['usage']['chat']['used'], 1500)
        with m.app.app_context():
            event = m.db().execute('SELECT id FROM usage_events').fetchone()[0]
        result = m.app.test_cli_runner().invoke(args=['reconcile-usage', str(event), '--prompt-tokens', '100', '--completion-tokens', '3', '--reason', 'Verified test backend logs'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.client.get('/api/state').json['usage']['chat']['used'], 103)

    def test_images_five_successes_then_paywall(self):
        out = io.BytesIO()
        m.Image.new('RGB', (512, 512)).save(out, format='PNG')
        class Response:
            def raise_for_status(self): pass
            def json(self): return {'images': [base64.b64encode(out.getvalue()).decode()]}
        with patch.object(m.requests, 'post', return_value=Response()) as backend:
            for _ in range(5):
                r = self.post('/api/generate', {'prompt': 'a tree', 'width': 1024, 'batch_size': 99})
                self.assertEqual(r.status_code, 200, r.json)
            self.assertEqual(self.post('/api/generate', {'prompt': 'a tree'}).status_code, 402)
            self.assertEqual(backend.call_count, 5)
            self.assertEqual(backend.call_args.kwargs['json']['width'], 512)
            self.assertEqual(backend.call_args.kwargs['json']['batch_size'], 1)
        url = r.json['image_url']
        self.assertEqual(self.client.get(url).status_code, 200)
        other = m.app.test_client()
        self.register(other, 'b@example.com')
        self.assertEqual(other.get(url).status_code, 404)
        self.post('/api/reset')
        self.assertEqual(self.client.get('/api/state').json['usage']['image']['used'], 5)

    def test_image_failure_does_not_charge(self):
        with patch.object(m.requests, 'post', side_effect=m.requests.Timeout()):
            self.assertEqual(self.post('/api/generate', {'prompt': 'tree'}).status_code, 502)
        self.assertEqual(self.client.get('/api/state').json['usage']['image']['used'], 0)

    def test_payments_fail_closed(self):
        for _ in range(2):
            self.assertEqual(self.client.post('/webhooks/payment', json={'user_id': 1, 'paid': True}).status_code, 503)
        self.assertEqual(self.post('/api/checkout').status_code, 503)
        self.assertEqual(self.client.get('/api/state').json['plan'], 'free')

    def test_parallel_lock_blocks_before_backend(self):
        with m.locked('user-1'), patch.object(m, 'llama_post') as backend:
            self.assertEqual(self.post('/api/chat', {'message': 'Hello'}).status_code, 409)
            self.assertEqual(self.post('/api/reset').status_code, 409)
            backend.assert_not_called()

    def test_paid_limit_configuration(self):
        with m.app.app_context():
            m.db().execute("UPDATE users SET plan='paid'")  # Test fixture, never a public route.
        self.event('chat', 2000)
        self.assertIsNone(self.client.get('/api/state').json['usage']['chat']['limit'])
        with patch.object(m, 'PAID_CHAT', 2000):
            self.assertEqual(self.post('/api/chat', {'message': 'Hi'}).status_code, 402)

    def test_ui_and_session_relogin_persistence(self):
        for path in ['/', '/upgrade']:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200)
            self.assertIn("script-src 'nonce-", r.headers['Content-Security-Policy'])
        self.post('/api/facts', {'content': '<script>alert(1)</script>'})
        with self.client.session_transaction() as s:
            old_auth = s['auth']
        self.post('/logout')
        with m.app.app_context():
            self.assertIsNone(m.db().execute('SELECT * FROM sessions WHERE token_hash=?', (m.digest(old_auth),)).fetchone())
        self.client.get('/login')
        with self.client.session_transaction() as s:
            csrf = s['csrf']
        r = self.client.post('/login', data={'email': 'a@example.com', 'password': 'long-test-password', 'csrf': csrf})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.client.get('/api/state').json['facts']), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
