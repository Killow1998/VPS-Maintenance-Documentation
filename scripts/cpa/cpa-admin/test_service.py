import copy
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from authentication import Authentication, digest
import authentication
from server import PolicyError, Store
from service import COOKIE, Server, SSE_BUFFER_LIMIT, SSECompletionGate, expected_stream_choices


class SSECompletionGateTest(unittest.TestCase):
    def test_split_crlf_events_preserve_incremental_output_and_completion(self):
        gate = SSECompletionGate()
        start = b'event: delta\r\ndata: {"text":"x"}\r\n\r\n'
        end = b'event: message_stop\r\ndata: {}\r\n\r\n: ping\r\n\r\n'
        output = []
        for byte in start + end:
            output.extend(gate.feed(bytes([byte])))
        self.assertEqual(b''.join(output), start)
        self.assertEqual(gate.finish(), end)

    def test_terminal_formats_and_invalid_payloads(self):
        for event in (b'data: {"type":"message_stop"}\n\n', b'data: [DONE]\n\n',
                      b'event: response.completed\n\n',
                      b'data: {"choices":[{"finish_reason":"stop"}]}\n\n'):
            with self.subTest(event=event):
                gate = SSECompletionGate()
                self.assertEqual(gate.feed(event), [])
                self.assertEqual(gate.finish(), event)
        for event in (b'data: {"choices":null}\n\n', b'data: not-json\n\n'):
            self.assertEqual(SSECompletionGate().feed(event), [event])

    def test_event_buffer_is_bounded(self):
        with self.assertRaises(ValueError):
            SSECompletionGate().feed(b'x' * (SSE_BUFFER_LIMIT + 1))

    def test_multichoice_waits_for_all_choices_before_holding_completion(self):
        gate = SSECompletionGate(expected_choices=2)
        first = b'data: {"choices":[{"index":0,"finish_reason":"stop"}]}\n\n'
        progress = b'data: {"choices":[{"index":1,"delta":{"content":"still running"}}]}\n\n'
        last = b'data: {"choices":[{"index":1,"finish_reason":"stop"}]}\n\n'
        self.assertEqual(gate.feed(first), [first])
        self.assertEqual(gate.feed(progress), [progress])
        self.assertEqual(gate.feed(last), [])
        self.assertEqual(gate.finish(), last)

    def test_batch_completions_count_all_prompts(self):
        self.assertEqual(expected_stream_choices('/v1/completions', {'prompt': ['a', 'b'], 'n': 2}), 4)
        self.assertEqual(expected_stream_choices('/v1/completions', {'prompt': [1, 2, 3], 'n': 2}), 2)
        self.assertEqual(expected_stream_choices('/v1/completions', {'prompt': [[1], [2]], 'n': 1}), 2)
        self.assertIsNone(expected_stream_choices('/v1/completions', {'prompt': [], 'n': 1}))

    def test_more_than_128_choices_are_tracked(self):
        self.assertEqual(expected_stream_choices('/v1/completions', {'prompt': ['a'] * 129}), 129)
        gate = SSECompletionGate(expected_choices=129)
        for index in range(129):
            gate.feed(('data: {"choices":[{"index":%d,"finish_reason":"stop"}]}\n\n' % index).encode())
        gate.feed(b'data: [DONE]\n\n')
        self.assertTrue(gate.completed)


class AuthenticationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")
        self.auth = Authentication(self.store)

    def test_password_login_revocation_and_no_plaintext(self):
        password = "fixture-password-12345"
        self.auth.set_password("admin", password)
        token = self.auth.login({"username": "admin", "password": password}, "fixture")
        self.assertEqual(self.auth.actor(token), "web:admin")
        self.assertNotIn(password.encode(), self.store.path.read_bytes())
        self.auth.logout(token)
        with self.assertRaises(PolicyError): self.auth.actor(token)

    def test_ticket_is_one_use_and_expires(self):
        ticket = self.auth.ticket()
        session = self.auth.login({"ticket": ticket}, "fixture")
        self.assertEqual(self.auth.actor(session), "ssh-admin")
        with self.assertRaises(PolicyError): self.auth.login({"ticket": ticket}, "fixture")
        expired = self.auth.ticket()
        with self.store.db() as con:
            con.execute("UPDATE tickets SET expires=0 WHERE hash=?", (digest(expired),))
        with self.assertRaises(PolicyError): self.auth.login({"ticket": expired}, "fixture")

    def test_password_reset_invalidates_sessions(self):
        token = self.auth.login({"ticket": self.auth.ticket()}, "fixture")
        self.auth.set_password("admin", "long-fixture-password")
        with self.assertRaises(PolicyError): self.auth.actor(token)

    def test_renamed_admin_reset_revokes_legacy_accounts_sessions_and_tickets(self):
        password = "fixture-password-old"
        self.auth.set_password("old_admin", password)
        with self.store.db() as con:
            con.execute("INSERT INTO administrators SELECT 'legacy_admin',salt,password_hash FROM administrators")
        tokens = [self.auth.login({"username": name, "password": password}, "fixture")
                  for name in ("old_admin", "legacy_admin")]
        tokens.append(self.auth.login({"ticket": self.auth.ticket()}, "fixture"))
        ticket = self.auth.ticket()
        before = self.store.snapshot()
        self.auth.set_password("new_admin", "fixture-password-new")
        for token in tokens:
            with self.assertRaises(PolicyError): self.auth.actor(token)
        for name in ("old_admin", "legacy_admin"):
            with self.assertRaises(PolicyError):
                self.auth.login({"username": name, "password": password}, "fixture")
        with self.assertRaises(PolicyError): self.auth.login({"ticket": ticket}, "fixture")
        token = self.auth.login({"username": "new_admin", "password": "fixture-password-new"}, "fixture")
        self.assertEqual(self.auth.actor(token), "web:new_admin")
        with self.store.db() as con:
            self.assertEqual(con.execute("SELECT name FROM administrators").fetchall(), [("new_admin",)])
        self.assertEqual(self.store.snapshot(), before)

    def test_same_username_reset_rejects_old_password(self):
        self.auth.set_password("admin", "fixture-password-old")
        self.auth.set_password("admin", "fixture-password-new")
        with self.assertRaises(PolicyError):
            self.auth.login({"username": "admin", "password": "fixture-password-old"}, "fixture")
        token = self.auth.login({"username": "admin", "password": "fixture-password-new"}, "fixture")
        self.assertEqual(self.auth.actor(token), "web:admin")

    def test_invalid_reset_preserves_current_login(self):
        self.auth.set_password("admin", "fixture-password-old")
        token = self.auth.login({"username": "admin", "password": "fixture-password-old"}, "fixture")
        for name, password in (("bad name", "fixture-password-new"), ("new_admin", "short")):
            with self.assertRaises(PolicyError): self.auth.set_password(name, password)
        self.assertEqual(self.auth.actor(token), "web:admin")

    def test_failed_reset_rolls_back_old_credentials_and_sessions(self):
        self.auth.set_password("admin", "fixture-password-old")
        token = self.auth.login({"username": "admin", "password": "fixture-password-old"}, "fixture")
        with self.store.db() as con:
            con.executescript("CREATE TRIGGER reject_reset BEFORE INSERT ON administrators "
                              "WHEN NEW.name='blocked' BEGIN SELECT RAISE(ABORT,'fixture'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.auth.set_password("blocked", "fixture-password-new")
        self.assertEqual(self.auth.actor(token), "web:admin")
        token = self.auth.login({"username": "admin", "password": "fixture-password-old"}, "fixture")
        self.assertEqual(self.auth.actor(token), "web:admin")

    def test_reset_during_password_validation_leaves_no_old_session(self):
        self.auth.set_password("admin", "fixture-password-old")
        hashing, resetting, reset_done = threading.Event(), threading.Event(), threading.Event()
        original_hash = authentication.password_hash
        tokens, errors = [], []

        def paused_hash(password, salt):
            if password == "fixture-password-old":
                hashing.set()
                if not resetting.wait(5): raise RuntimeError("reset did not start")
                reset_done.wait(1)
            result = original_hash(password, salt)
            if password == "fixture-password-new": resetting.set()
            return result

        def login():
            try:
                tokens.append(self.auth.login({"username": "admin", "password": "fixture-password-old"}, "fixture"))
            except Exception as error:
                errors.append(error)

        def reset():
            try:
                self.auth.set_password("replacement", "fixture-password-new")
            except Exception as error:
                errors.append(error)
            finally:
                reset_done.set()

        with patch.object(authentication, "password_hash", side_effect=paused_hash):
            worker = threading.Thread(target=login)
            worker.start()
            self.assertTrue(hashing.wait(5))
            resetter = threading.Thread(target=reset)
            resetter.start()
            worker.join(10)
            resetter.join(10)
        self.assertFalse(worker.is_alive())
        self.assertFalse(resetter.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(tokens), 1)
        with self.assertRaises(PolicyError): self.auth.actor(tokens[0])


class FakeBackend:
    config = {"origin": "https://control.example", "active": True}
    c = {"management_key": "test-admin-only", "hmac_secret": "synthetic-routing-hmac"}

    def __init__(self, path, upstream):
        self.store = Store(path)
        self.host, self.port = "127.0.0.1", upstream.server_port
        self.lock = threading.RLock()
        self.fresh = True
        self.quota_keys = []
        self.models = {"account-a": {"claude-test": {}}, "account-b": {"claude-test": {}}}
        with self.store.db() as con:
            state = self.store.snapshot(con)
            for a in state["accounts"]:
                a.update(route_ready=True, prefix=a["id"])
            con.execute("UPDATE policy SET data=?", (json.dumps({k: state[k] for k in ("clients", "accounts")}),))

    def require_fresh(self):
        if not self.fresh: raise PolicyError(503, "fixture stale")

    def authenticate(self, key):
        self.require_fresh()
        if key not in ("test-client", "test-other"): raise PolicyError(401, "fixture invalid")
        return "client-a" if key == "test-client" else "client-b"

    def allowed(self, identity):
        self.require_fresh()
        s = self.store.snapshot()
        c = next(c for c in s["clients"] if c["id"] == identity)
        return [a for a in s["accounts"] if c["enabled"] and a["id"] in c["account_ids"]]

    def state(self):
        return dict(self.store.snapshot(), mode="live", enforcement="enforced")

    def save(self, data, actor):
        return self.store.save(data, actor)

    def quota(self, allowed, *, key=None):
        self.quota_keys.append(key)
        return {"accounts": [{"id": a} for a in sorted(allowed)]}

    def account_quota(self):
        return {"accounts": self.store.snapshot()["accounts"], "query_status": "ok"}

    def quota_action(self, data, actor):
        self.last_quota_action = (data, actor)
        return {"status": "ok", "quota": self.account_quota()}


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.failures, self.statuses, self.session_headers = [], set(), {}, []
        self.broken_errors, self.empty_successes, self.truncated = set(), set(), set()
        self.delayed_sse = {}
        self.stream_events = {}
        calls, failures, statuses, session_headers = self.calls, self.failures, self.statuses, self.session_headers
        broken_errors, empty_successes, truncated = self.broken_errors, self.empty_successes, self.truncated
        delayed_sse = self.delayed_sse
        stream_events = self.stream_events
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append({"model": data["model"], "key": self.headers.get("Authorization")})
                session_headers.append(self.headers.get('X-Session-ID'))
                account = data["model"].split('/')[0]
                code = statuses.get(account, 429 if account in failures else 200)
                body = stream_events.get(account, b'data: {"type":"message_stop"}\n\n') if data.get("stream") else b'{"ok":true}'
                if account in truncated:
                    self.send_response(code)
                    self.send_header('Content-Type', 'text/event-stream' if data.get('stream') else 'application/json')
                    self.send_header('Retry-After', '173')
                    self.send_header('Content-Length', '100')
                    self.end_headers()
                    self.wfile.write(body if data.get('stream') else b'{')
                    self.close_connection = True
                    return
                if account in broken_errors:
                    self.send_response(code)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Retry-After', '173')
                    self.send_header('Transfer-Encoding', 'chunked')
                    self.end_headers()
                    self.wfile.write(b'8\r\nx')
                    self.close_connection = True
                    return
                if account in empty_successes:
                    body = b''
                if code == 200 and data.get('stream') and account in delayed_sse:
                    sent, release = delayed_sse[account]
                    self.send_response(code)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Transfer-Encoding', 'chunked')
                    self.end_headers()
                    chunk = b'event: content_block_delta\ndata: {"delta":"ok"}\n\n' + body
                    self.wfile.write(('%x\r\n' % len(chunk)).encode() + chunk + b'\r\n')
                    self.wfile.flush()
                    sent.set()
                    release.wait(5)
                    self.wfile.write(b'0\r\n\r\n')
                    return
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "text/event-stream" if data.get("stream") else "application/json")
                if code == 429: self.send_header("Retry-After", "173")
                self.end_headers()
                self.wfile.write(body)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.temp = tempfile.TemporaryDirectory()
        self.backend = FakeBackend(Path(self.temp.name) / "control.db", self.upstream)
        self.server = Server(("127.0.0.1", 0), self.backend)
        for server in (self.upstream, self.server):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in (self.server, self.upstream):
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None, raw=None):
        con = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json", **(headers or {})}
        payload = raw if raw is not None else json.dumps(body) if body is not None else None
        try:
            con.request("POST" if payload else "GET", path, payload, headers)
            res = con.getresponse()
            return res.status, res.read(), dict(res.getheaders())
        finally:
            con.close()

    def model(self, model="claude-test", **extra):
        return self.request('/v1/messages', {"model": model, "messages": [], **extra}, {"Authorization": "Bearer test-client"})

    def session_model(self, session='one', key='test-other', **extra):
        return self.request('/v1/messages', {'model': 'claude-test', 'messages': [], **extra},
                            {'Authorization': 'Bearer ' + key, **({'X-Session-ID': session} if session else {})})

    def test_session_stays_on_one_upstream_and_only_new_sessions_rotate(self):
        for _ in range(3): self.assertEqual(self.session_model()[0], 200)
        self.assertEqual([c['model'] for c in self.calls], ['account-a/claude-test'] * 3)
        self.assertEqual(self.session_headers, ['one'] * 3)
        self.assertEqual(self.session_model('two', stream=True)[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-b/claude-test')
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-a/claude-test')

    def test_quota_failover_is_sticky_after_original_recovers(self):
        self.assertEqual(self.session_model()[0], 200)
        self.failures.add('account-a')
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual([c['model'] for c in self.calls[-2:]], ['account-a/claude-test', 'account-b/claude-test'])
        self.failures.clear()
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-b/claude-test')

    def test_transient_failover_keeps_primary_and_missing_id_does_not_rotate(self):
        self.assertEqual(self.session_model(None)[0], 200)
        self.statuses['account-a'] = 503
        self.assertEqual(self.session_model(None)[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-b/claude-test')
        self.statuses.clear()
        self.assertEqual(self.session_model(None)[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-a/claude-test')

    def test_temporarily_missing_primary_migrates_only_after_success(self):
        self.assertEqual(self.session_model()[0], 200)
        del self.backend.models['account-a']['claude-test']
        self.statuses['account-b'] = 503
        self.assertEqual(self.session_model()[0], 503)
        self.backend.models['account-a']['claude-test'] = {}
        self.statuses.clear()
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-a/claude-test')
        del self.backend.models['account-a']['claude-test']
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-b/claude-test')
        self.backend.models['account-a']['claude-test'] = {}
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-b/claude-test')

    def test_claude_metadata_binds_same_session_without_headers(self):
        for _ in range(3):
            self.assertEqual(self.session_model(None, metadata={'user_id': json.dumps({'session_id': 'claude-session'})})[0], 200)
        self.assertEqual([c['model'] for c in self.calls], ['account-a/claude-test'] * 3)

    def test_backup_quota_error_does_not_turn_primary_transient_error_into_migration(self):
        state = self.backend.store.snapshot()
        state['accounts'][2]['enabled'] = True
        state['clients'][1]['account_ids'].append('account-c')
        with self.backend.store.db() as con:
            con.execute('UPDATE policy SET data=?', (json.dumps({k: state[k] for k in ('clients', 'accounts')}),))
        self.backend.models['account-c'] = {'claude-test': {}}
        self.statuses.update({'account-a': 503, 'account-b': 429})
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-c/claude-test')
        self.statuses.clear()
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-a/claude-test')

    def test_binding_write_failure_does_not_discard_successful_generation(self):
        with patch.object(self.server.router, 'succeeded', side_effect=sqlite3.OperationalError('synthetic')):
            with self.assertLogs('service', level='WARNING') as logs:
                self.assertEqual(self.session_model()[0], 200)
        self.assertIn('session_binding_save_failed: OperationalError', logs.output[0])
        self.assertNotIn('test-other', logs.output[0])

    def test_revoked_session_account_cannot_override_policy(self):
        self.assertEqual(self.session_model()[0], 200)
        state = self.backend.store.snapshot()
        state['clients'][1]['account_ids'] = ['account-b']
        self.backend.store.save({'expected_revision': state['revision'], 'clients': [
            {k: c[k] for k in ('id', 'enabled', 'account_ids')} for c in state['clients']]})
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-b/claude-test')
        self.failures.add('account-b')
        before = len(self.calls)
        self.assertEqual(self.session_model()[0], 429)
        self.assertEqual(len(self.calls), before + 1)

    def test_backup_revoked_after_primary_429_preserves_upstream_error(self):
        self.assertEqual(self.session_model()[0], 200)
        self.statuses['account-a'] = 429
        original = self.backend.allowed
        count = [0]
        def allowed(identity):
            count[0] += 1
            result = original(identity)
            return [a for a in result if count[0] < 3 or a['id'] != 'account-b']
        before = len(self.calls)
        with patch.object(self.backend, 'allowed', side_effect=allowed):
            code, _, headers = self.session_model()
        self.assertEqual(code, 429)
        self.assertEqual(headers['Retry-After'], '173')
        self.assertEqual([c['model'] for c in self.calls[before:]], ['account-a/claude-test'])

    def test_broken_429_body_preserves_status_and_retry_after(self):
        self.assertEqual(self.session_model()[0], 200)
        self.statuses['account-a'] = 429
        self.broken_errors.add('account-a')
        original = self.backend.allowed
        count = [0]
        def allowed(identity):
            count[0] += 1
            return [a for a in original(identity) if count[0] < 3 or a['id'] != 'account-b']
        with patch.object(self.backend, 'allowed', side_effect=allowed):
            code, body, headers = self.session_model()
        self.assertEqual(code, 429)
        self.assertEqual(headers['Retry-After'], '173')
        self.assertIn('error', json.loads(body))

    def test_truncated_429_body_is_not_forwarded_as_complete_json(self):
        self.assertEqual(self.session_model()[0], 200)
        self.statuses['account-a'] = 429
        self.truncated.add('account-a')
        original = self.backend.allowed
        count = [0]
        def allowed(identity):
            count[0] += 1
            return [a for a in original(identity) if count[0] < 3 or a['id'] != 'account-b']
        with patch.object(self.backend, 'allowed', side_effect=allowed):
            code, body, headers = self.session_model()
        self.assertEqual(code, 429)
        self.assertEqual(headers['Retry-After'], '173')
        self.assertIn('error', json.loads(body))

    def test_primary_revoked_after_selection_migrates_only_after_backup_success(self):
        self.assertEqual(self.session_model()[0], 200)
        original = self.backend.allowed
        count = [0]
        def allowed(identity):
            count[0] += 1
            result = original(identity)
            return [a for a in result if count[0] == 1 or a['id'] != 'account-a']
        before = len(self.calls)
        with patch.object(self.backend, 'allowed', side_effect=allowed):
            self.assertEqual(self.session_model()[0], 200)
        self.assertEqual([c['model'] for c in self.calls[before:]], ['account-b/claude-test'])
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-b/claude-test')

    def test_backup_with_empty_success_body_does_not_migrate(self):
        self.assertEqual(self.session_model()[0], 200)
        self.empty_successes.add('account-b')
        original = self.backend.allowed
        count = [0]
        def allowed(identity):
            count[0] += 1
            return [a for a in original(identity) if count[0] == 1 or a['id'] != 'account-a']
        with patch.object(self.backend, 'allowed', side_effect=allowed):
            self.assertEqual(self.session_model(stream=True)[0], 200)
        self.empty_successes.clear()
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-a/claude-test')

    def test_truncated_backup_response_is_not_completed_or_migrated(self):
        self.assertEqual(self.session_model()[0], 200)
        self.statuses['account-a'] = 429
        self.truncated.add('account-b')
        with self.assertRaises(http.client.IncompleteRead) as caught:
            self.session_model(stream=True)
        self.assertNotIn(b'message_stop', caught.exception.partial)
        self.truncated.clear()
        self.statuses.clear()
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-a/claude-test')

    def test_completed_backup_is_bound_before_client_sees_end_of_stream(self):
        self.assertEqual(self.session_model()[0], 200)
        self.failures.add('account-a')
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = self.server.router.succeeded
        errors = []
        def delayed(selection, account, **kwargs):
            if account == 'account-b':
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test wait')
            return original(selection, account, **kwargs)
        def request():
            try:
                self.assertEqual(self.session_model(stream=True)[0], 200)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        with patch.object(self.server.router, 'succeeded', side_effect=delayed):
            thread = threading.Thread(target=request)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(finished.wait(.5))
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.failures.clear()
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-b/claude-test')

    def test_terminal_sse_event_waits_for_complete_backup_and_binding(self):
        self.assertEqual(self.session_model()[0], 200)
        self.failures.add('account-a')
        sent, release, received_terminal = threading.Event(), threading.Event(), threading.Event()
        self.delayed_sse['account-b'] = sent, release
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        errors = []
        def receive():
            try:
                conn.request('POST', '/v1/messages', json.dumps({'model': 'claude-test', 'messages': [], 'stream': True}),
                             {'Content-Type': 'application/json', 'Authorization': 'Bearer test-other', 'X-Session-ID': 'one'})
                res = conn.getresponse()
                self.assertEqual(res.readline(), b'event: content_block_delta\n')
                self.assertEqual(res.readline(), b'data: {"delta":"ok"}\n')
                self.assertEqual(res.readline(), b'\n')
                self.assertIn(b'message_stop', res.readline())
                received_terminal.set()
                res.read()
            except Exception as error:
                errors.append(error)
            finally:
                conn.close()
        thread = threading.Thread(target=receive)
        thread.start()
        try:
            self.assertTrue(sent.wait(5))
            self.assertFalse(received_terminal.wait(.4))
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertTrue(received_terminal.is_set())
        self.failures.clear()
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-b/claude-test')

    def test_batch_prompt_migration_passes_total_choices_to_sse_gate(self):
        self.assertEqual(self.session_model()[0], 200)
        self.failures.add('account-a')
        with patch('service.SSECompletionGate', wraps=SSECompletionGate) as gate:
            status, _, _ = self.request('/v1/completions', {'model': 'claude-test', 'prompt': ['a', 'b'],
                'n': 1, 'stream': True}, {'Authorization': 'Bearer test-other', 'X-Session-ID': 'one'})
        self.assertEqual(status, 200)
        gate.assert_called_once_with(2)

    def test_excessive_stream_choice_count_is_rejected_before_upstream(self):
        status, _, _ = self.session_model(stream=True, n=4097)
        self.assertEqual(status, 400)
        self.assertEqual(self.calls, [])
        for prompt in (None, [], {'invalid': True}):
            with self.subTest(prompt=prompt):
                body = {'model': 'claude-test', 'n': 4097, 'stream': True}
                if prompt is not None:
                    body['prompt'] = prompt
                status, _, _ = self.request('/v1/completions', body,
                    {'Authorization': 'Bearer test-other', 'X-Session-ID': 'one'})
                self.assertEqual(status, 400)
                self.assertEqual(self.calls, [])

    def test_stream_limit_does_not_change_nonstream_requests(self):
        status, _, _ = self.request('/v1/completions',
            {'model': 'claude-test', 'prompt': 'ok', 'n': 4097, 'stream': False},
            {'Authorization': 'Bearer test-other', 'X-Session-ID': 'one'})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.calls), 1)

    def test_sse_error_does_not_commit_backup_binding(self):
        self.assertEqual(self.session_model()[0], 200)
        self.failures.add('account-a')
        self.stream_events['account-b'] = b'event: error\ndata: {"type":"error"}\n\n'
        self.assertEqual(self.session_model(stream=True)[0], 200)
        self.failures.clear()
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-a/claude-test')

    def test_sse_error_payload_and_late_error_do_not_migrate(self):
        for event in (b'data: {"error":{"message":"synthetic"}}\n\ndata: [DONE]\n\n',
                      b'data: {"choices":[{"index":0,"finish_reason":"stop"}]}\n\n'
                      b'event: error\ndata: {"type":"error"}\n\n'):
            with self.subTest(event=event):
                session = str(len(self.calls))
                self.assertEqual(self.session_model(session)[0], 200)
                primary = self.calls[-1]['model'].split('/')[0]
                backup = 'account-b' if primary == 'account-a' else 'account-a'
                self.failures.add(primary)
                self.stream_events[backup] = event
                self.assertEqual(self.session_model(session, stream=True)[0], 200)
                self.failures.clear()
                self.assertEqual(self.session_model(session)[0], 200)
                self.assertEqual(self.calls[-1]['model'].split('/')[0], primary)
                self.stream_events.clear()

    def test_incomplete_batch_stream_does_not_migrate(self):
        self.assertEqual(self.session_model()[0], 200)
        self.failures.add('account-a')
        self.stream_events['account-b'] = b'data: {"choices":[{"index":0,"finish_reason":"stop"}]}\n\n'
        status, _, _ = self.request('/v1/completions', {'model': 'claude-test', 'prompt': ['a', 'b'],
            'n': 1, 'stream': True}, {'Authorization': 'Bearer test-other', 'X-Session-ID': 'one'})
        self.assertEqual(status, 200)
        self.failures.clear()
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-a/claude-test')

    def test_batch_done_does_not_hide_missing_choice(self):
        self.assertEqual(self.session_model()[0], 200)
        self.failures.add('account-a')
        self.stream_events['account-b'] = (b'data: {"choices":[{"index":0,"finish_reason":"stop"}]}\n\n'
                                            b'data: [DONE]\n\n')
        status, _, _ = self.request('/v1/completions', {'model': 'claude-test', 'prompt': ['a', 'b'],
            'n': 1, 'stream': True}, {'Authorization': 'Bearer test-other', 'X-Session-ID': 'one'})
        self.assertEqual(status, 200)
        self.failures.clear()
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-a/claude-test')

    def test_cr_terminated_late_error_does_not_migrate(self):
        self.assertEqual(self.session_model()[0], 200)
        self.failures.add('account-a')
        self.stream_events['account-b'] = (b'data: {"choices":[{"index":0,"finish_reason":"stop"}]}\n\n'
                                            b'event: error\rdata: {"type":"error"}\r\r')
        self.assertEqual(self.session_model(stream=True)[0], 200)
        self.failures.clear()
        self.assertEqual(self.session_model()[0], 200)
        self.assertEqual(self.calls[-1]['model'], 'account-a/claude-test')

    def test_gateway_uses_authorized_account_and_preserves_key_for_metering(self):
        self.assertEqual(self.model()[0], 200)
        self.assertEqual(self.calls, [{"model": "account-a/claude-test", "key": "Bearer test-client"}])

    def test_prefix_injection_conflicting_headers_and_duplicate_model_are_rejected(self):
        self.assertEqual(self.model("account-b/claude-test")[0], 400)
        self.assertEqual(self.request('/v1/messages', {"model": "claude-test"},
            {"Authorization": "Bearer test-client", "x-api-key": "different"})[0], 401)
        self.assertEqual(self.request('/v1/messages', headers={"Authorization": "Bearer test-client"},
            raw='{"model":"claude-test","model":"account-b/claude-test"}')[0], 400)
        self.assertEqual(self.calls, [])

    def test_error_does_not_escape_and_two_allowed_accounts_can_retry(self):
        self.failures.add("account-a")
        self.assertEqual(self.model()[0], 429)
        self.assertEqual(len(self.calls), 1)
        state = self.backend.state()
        state["clients"][0]["account_ids"].append("account-b")
        self.backend.store.save({"expected_revision": state["revision"], "clients": [
            {k: c[k] for k in ("id", "enabled", "account_ids")} for c in state["clients"]]})
        self.server.sequence = 0
        self.assertEqual(self.model()[0], 200)
        self.assertEqual(self.calls[-1]["model"], "account-b/claude-test")

    def test_streaming_default_deny_stale_and_quota_scope(self):
        code, body, _ = self.model(stream=True)
        self.assertEqual(code, 200)
        self.assertIn(b'message_stop', body)
        self.assertEqual(self.request('/v1/models')[0], 401)
        q = self.request('/v1/quota', headers={"Authorization": "Bearer test-client"})
        self.assertEqual(json.loads(q[1])["accounts"], [{"id": "account-a"}])
        self.assertEqual(self.backend.quota_keys, ["test-client"])
        self.backend.fresh = False
        self.assertEqual(self.model()[0], 503)

    def test_public_quota_forwards_current_key_with_its_own_acl(self):
        for key, accounts in (("test-client", ["account-a"]), ("test-other", ["account-a", "account-b"])):
            result = self.request('/v1/quota', headers={"Authorization": "Bearer " + key})
            self.assertEqual(result[0], 200)
            self.assertEqual(json.loads(result[1])["accounts"], [{"id": a} for a in accounts])
        self.assertEqual(self.backend.quota_keys, ["test-client", "test-other"])

    def test_admin_login_cookie_csrf_and_internal_boundary(self):
        self.assertEqual(self.request('/admin/api/state')[0], 401)
        self.assertEqual(self.request('/admin/api/quota')[0], 401)
        self.assertEqual(self.request('/admin/api/quota', headers={"Authorization": "Bearer test-client"})[0], 401)
        self.assertEqual(self.request('/internal/state')[0], 401)
        ticket = self.server.authentication.ticket()
        self.assertEqual(self.request('/admin/api/login', {"ticket": ticket})[0], 403)
        code, body, headers = self.request('/admin/api/login', {"ticket": ticket}, {"Origin": self.backend.config["origin"]})
        self.assertEqual(code, 200)
        cookie = headers["Set-Cookie"]
        for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/admin/"):
            self.assertIn(flag, cookie)
        session = cookie.split(';', 1)[0]
        self.assertEqual(self.request('/admin/api/quota', headers={"Cookie": session})[0], 200)
        self.assertEqual(self.request('/admin/api/state', headers={"Cookie": session})[0], 200)
        self.assertEqual(self.request('/admin/api/policies', {}, {"Cookie": session, "Origin": "https://evil.example"})[0], 403)
        reset = {"action": "redeem", "account_id": "account-a", "grant_id": "synthetic", "operation_id": "synthetic"}
        self.assertEqual(self.request('/admin/api/quota', reset, {"Authorization": "Bearer test-client"})[0], 401)
        self.assertEqual(self.request('/admin/api/quota', reset, {"Cookie": session, "Origin": "https://evil.example"})[0], 403)
        self.assertFalse(hasattr(self.backend, "last_quota_action"))
        self.assertEqual(self.request('/admin/api/quota', reset, {"Cookie": session, "Origin": self.backend.config["origin"]})[0], 200)
        self.assertEqual(self.backend.last_quota_action, (reset, "ssh-admin"))


if __name__ == '__main__': unittest.main()
