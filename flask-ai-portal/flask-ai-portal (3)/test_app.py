"""Isolated tests. Never call a live AI service or touch the production database."""
import base64
import importlib.util
import io
import json
import hmac
import hashlib
import time
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

sandbox = tempfile.TemporaryDirectory()
os.environ['AI_DATA_DIR'] = sandbox.name
for key in ('STRIPE_SECRET_KEY','STRIPE_WEBHOOK_SECRET','STRIPE_PRICE_ID'):
    os.environ[key] = ''
spec = importlib.util.spec_from_file_location('portal_test', Path(__file__).with_name('app.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.app.config['TESTING'] = True


class PortalTests(unittest.TestCase):
    def setUp(self):
        with m.app.app_context():
            c = m.db()
            for table in ['billing_accounts', 'generated_images', 'usage_events', 'messages', 'facts', 'conversations', 'sessions', 'payment_events', 'users', 'auth_attempts']:
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
        with m.locked('user-1-chat'), patch.object(m, 'llama_post') as backend:
            self.assertEqual(self.post('/api/chat', {'message': 'Hello'}).status_code, 409)
            self.assertEqual(self.post('/api/reset').status_code, 409)
            backend.assert_not_called()

    def test_chat_during_active_image_generation(self):
        started, release = threading.Event(), threading.Event()
        out = io.BytesIO()
        m.Image.new('RGB', (512, 512)).save(out, format='PNG')
        class Response:
            def raise_for_status(self): pass
            def json(self): return {'images': [base64.b64encode(out.getvalue()).decode()]}
        def slow_image(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise AssertionError('Image test was not released')
            return Response()
        parallel_client = m.app.test_client()
        with self.client.session_transaction() as session:
            credentials = dict(session)
        with parallel_client.session_transaction() as session:
            session.update(credentials)
        with patch.object(m.requests, 'post', side_effect=slow_image), patch.object(m, 'llama_post', side_effect=self.mock_llama), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.post, '/api/generate', {'prompt': 'tree'})
            try:
                self.assertTrue(started.wait(3))
                chat = self.post('/api/chat', {'message': 'Hello'}, client=parallel_client)
                self.assertEqual(chat.status_code, 200, chat.json)
                self.assertFalse(future.done(), 'Image must still be running while chat succeeds')
                duplicate = self.post('/api/generate', {'prompt': 'second'}, client=parallel_client)
                self.assertEqual(duplicate.status_code, 409)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=3).status_code, 200)
        stats = self.client.get('/api/state').json['usage']
        self.assertEqual(stats['chat']['used'], 120)
        self.assertEqual(stats['image']['used'], 1)

    def test_long_history_preparation_is_bounded(self):
        with m.app.app_context():
            cid = m.db().execute('SELECT id FROM conversations WHERE user_id=1').fetchone()[0]
            m.db().executemany('INSERT INTO messages(conversation_id,role,content) VALUES(?,?,?)',
                              [(cid, role, 'old message') for _ in range(50) for role in ('user', 'assistant')])
        with patch.object(m, 'count_prompt', side_effect=lambda messages: len(messages) * 100) as counter, patch.object(m, 'llama_post', side_effect=self.mock_llama):
            response = self.post('/api/chat', {'message': 'Hello'})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertLessEqual(counter.call_count, 7)
            self.assertIn('generation_seconds', response.json['timings'])
        self.assertEqual(len(self.client.get('/api/state').json['messages']), 102)

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


class StripeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in [('STRIPE_KEY', 'sk_test_fake'), ('STRIPE_WEBHOOK', 'whsec_test_secret'),
                            ('STRIPE_PRICE', 'price_month'), ('STRIPE_LIVE', False)]:
            self.stack.enter_context(patch.object(m, name, value))
        with m.app.app_context():
            for table in ['billing_accounts','generated_images','usage_events','messages','facts','conversations','sessions','payment_events','users','auth_attempts']:
                m.db().execute('DELETE FROM ' + table)
            m.db().execute("INSERT INTO users(id,email,password_hash) VALUES(1,'stripe@example.com','unused')")
            m.db().execute("INSERT INTO conversations(user_id) VALUES(1)")
            m.db().execute("INSERT INTO billing_accounts(user_id,customer_id,customer_key) VALUES(1,'cus_test','customer-key')")
            m.db().execute('INSERT INTO sessions VALUES(?,?,?)', (m.digest('test-session'), 1, int(time.time())+3600))
        self.client = m.app.test_client()
        with self.client.session_transaction() as s:
            s.update(auth='test-session', csrf='csrf')
        self.price = dict(id='price_month', active=True, livemode=False, type='recurring', unit_amount=1000,
                          currency='eur', recurring={'interval':'month','interval_count':1,'usage_type':'licensed'})
        self.sub = dict(id='sub_test', customer='cus_test', livemode=False, metadata={'portal_user':'1'},
                        items={'data':[{'quantity':1,'price':{'id':'price_month'}}]}, status='active',
                        latest_invoice='in_test', current_period_end=int(time.time())+86400)
        self.invoice = dict(id='in_test', customer='cus_test', subscription='sub_test', livemode=False,
                            status='paid', paid_out_of_band=False, currency='eur', amount_paid=1000,
                            charge=dict(id='ch_test', livemode=False, paid=True, captured=True, disputed=False,
                                        amount_refunded=0, currency='eur', amount=1000))
        self.stack.enter_context(patch.object(m.stripe.Price,'retrieve',return_value=self.price))
        listing = self.stack.enter_context(patch.object(m.stripe.Subscription,'list'))
        listing.return_value.auto_paging_iter.side_effect = lambda: iter([self.sub])
        self.stack.enter_context(patch.object(m.stripe.Invoice,'retrieve',return_value=self.invoice))

    def webhook(self, event_id='evt_test', event_type='invoice.paid', live=False, signature=True):
        raw = json.dumps({'id':event_id,'object':'event','type':event_type,'livemode':live,
                          'data':{'object':{'customer':'cus_test'}}}).encode()
        now = int(time.time())
        signed = str(now).encode()+b'.'+raw
        digest = hmac.new(b'whsec_test_secret', signed, hashlib.sha256).hexdigest()
        return self.client.post('/webhooks/payment',data=raw,content_type='application/json',
                                headers={'Stripe-Signature':f't={now},v1={digest if signature else "invalid"}'})

    def plan(self):
        with m.app.app_context():
            return m.db().execute('SELECT plan FROM users WHERE id=1').fetchone()[0]

    def post(self, url):
        return self.client.post(url, headers={'X-CSRF-Token':'csrf'})

    def test_valid_signed_payment_and_duplicate(self):
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(self.plan(), 'paid')
        self.assertTrue(self.webhook().json['duplicate'])
        with m.app.app_context():
            self.assertEqual(m.db().execute('SELECT COUNT(*) FROM payment_events').fetchone()[0],1)

    def test_invalid_signature_and_live_mismatch(self):
        self.assertEqual(self.webhook(signature=False).status_code,400)
        self.assertEqual(self.webhook(live=True).status_code,400)
        self.assertEqual(self.plan(),'free')

    def test_unpaid_invoice_never_grants(self):
        self.invoice['status']='open'
        self.invoice['amount_paid']=0
        self.assertEqual(self.webhook().status_code,200)
        self.assertEqual(self.plan(),'free')

    def test_refund_revokes_and_stale_event_cannot_restore(self):
        self.webhook()
        self.invoice['charge']['amount_refunded']=1000
        self.assertEqual(self.webhook('evt_refund','charge.refunded').status_code,200)
        self.assertEqual(self.plan(),'free')
        self.webhook('evt_old_invoice','invoice.paid')
        self.assertEqual(self.plan(),'free')

    def test_cancellation_and_local_expiration(self):
        self.webhook()
        self.sub['status']='canceled'
        self.webhook('evt_cancel','customer.subscription.deleted')
        self.assertEqual(self.plan(),'free')
        self.sub['status']='active'
        self.webhook('evt_renew')
        with m.app.app_context():
            m.db().execute('UPDATE billing_accounts SET valid_until=?',(int(time.time())-1,))
        self.client.get('/api/state')
        self.assertEqual(self.plan(),'free')

    def test_wrong_price_and_amount_never_grant(self):
        self.sub['items']['data'][0]['price']['id']='price_other'
        self.webhook()
        self.assertEqual(self.plan(),'free')
        self.sub['items']['data'][0]['price']['id']='price_month'
        self.invoice['amount_paid']=1
        self.webhook('evt_small')
        self.assertEqual(self.plan(),'free')

    def test_success_redirect_alone_does_not_grant(self):
        self.assertEqual(self.client.get('/upgrade?checkout=success').status_code,200)
        self.assertEqual(self.plan(),'free')

    def test_checkout_uses_server_price_and_reuses_open_session(self):
        self.sub['status']='canceled'
        with patch.object(m.stripe.checkout.Session,'create',return_value={'id':'cs_test','url':'https://checkout.stripe.com/test'}) as create, patch.object(m.stripe.checkout.Session,'retrieve',return_value={'status':'open','url':'https://checkout.stripe.com/test'}):
            self.assertEqual(self.post('/api/checkout').status_code,303)
            self.assertEqual(self.post('/api/checkout').status_code,303)
            self.assertEqual(create.call_count,1)
            self.assertEqual(create.call_args.kwargs['line_items'],[{'price':'price_month','quantity':1}])
            self.assertTrue(create.call_args.kwargs['idempotency_key'])
        self.assertEqual(self.plan(),'free')

    def test_sync_failure_does_not_acknowledge_event(self):
        with patch.object(m.stripe.Invoice,'retrieve',side_effect=m.stripe.APIConnectionError('offline')):
            self.assertEqual(self.webhook().status_code,502)
        with m.app.app_context():
            self.assertEqual(m.db().execute('SELECT COUNT(*) FROM payment_events').fetchone()[0],0)



if __name__ == '__main__':
    unittest.main(verbosity=2)
