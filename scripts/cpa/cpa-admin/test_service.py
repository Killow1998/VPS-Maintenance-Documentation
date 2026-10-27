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
from service import COOKIE, Server


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
    c = {"management_key": "test-admin-only"}

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


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.failures = [], set()
        calls, failures = self.calls, self.failures
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append({"model": data["model"], "key": self.headers.get("Authorization")})
                code = 429 if data["model"].split('/')[0] in failures else 200
                body = b'data: {"type":"message_stop"}\n\n' if data.get("stream") else b'{"ok":true}'
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "text/event-stream" if data.get("stream") else "application/json")
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
        con.request("POST" if payload else "GET", path, payload, headers)
        res = con.getresponse()
        result = res.status, res.read(), dict(res.getheaders())
        con.close()
        return result

    def model(self, model="claude-test", **extra):
        return self.request('/v1/messages', {"model": model, "messages": [], **extra}, {"Authorization": "Bearer test-client"})

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


if __name__ == '__main__': unittest.main()
