"""Exercise acceptance against real gateway/observer servers with synthetic data."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from backend import Backend
from service import Server
import verify_remote as verify

spec = importlib.util.spec_from_file_location("verification_observer", Path(__file__).resolve().parents[1] / "cpa-observer" / "observer.py")
observer_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer_module)


class VerificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.keys = ["synthetic-verification-a", "synthetic-verification-b"]
        self.cpa_calls = []
        fixture = self

        class CPA(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                fixture.cpa_calls.append(self.path)
                data = fixture.keys if self.path == "/v8/management/config/access/api-keys" else {}
                raw = json.dumps(data).encode()
                self.send_response(200 if data else 404)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        def serve(server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            return server.server_port

        cpa_port = serve(ThreadingHTTPServer(("127.0.0.1", 0), CPA))
        self.observer_config = {"cpa_base_url": f"http://127.0.0.1:{cpa_port}",
            "database": str(root / "usage.sqlite"), "hmac_secret": "synthetic-hmac",
            "management_key": "synthetic-management", "prices_file": str(Path(observer_module.__file__).with_name("prices.json"))}
        self.observer = observer_module.Observer(self.observer_config)
        self.observer.backend.valid_key = lambda key: key in self.keys
        self.observer_config["port"] = serve(observer_module.Server(("127.0.0.1", 0), self.observer))
        config_file = root / "observer.json"
        config_file.write_text(json.dumps(self.observer_config))
        self.config = {"observer_config": str(config_file), "database": str(root / "control.sqlite"),
                       "origin": "https://cpa.example.invalid", "active": True}
        backend = Backend(self.config)
        clients = [{"id": backend.identity("client", k), "name": "client-" + backend.identity("client", k)[:10]} for k in self.keys]
        accounts = [{"id": a, "name": a, "enabled": True, "route_ready": True, "prefix": a} for a in ("a", "b")]
        backend.store.sync_inventory(clients, accounts)
        state = backend.store.snapshot()
        backend.store.save({"expected_revision": state["revision"], "clients": [
            {"id": c["id"], "enabled": True, "account_ids": [accounts[i]["id"]]} for i, c in enumerate(clients)]})
        backend.client_keys = {c["id"]: k for c, k in zip(clients, self.keys)}
        backend.models = {a: {"claude-fixture": {}} for a in ("a", "b")}
        backend.updated = time.monotonic()
        self.backend = backend
        self.config["port"] = serve(Server(("127.0.0.1", 0), backend))
        self.config_file = root / "admin.json"
        self.config_file.write_text(json.dumps(self.config))
        now = observer_module.now()
        self.observer.store.meta("collector_status", "ok")
        self.observer.store.meta("collector_last_success", now)
        self.observer.store.meta("quota_status", "ok")
        self.observer.store.meta("quota_snapshot", json.dumps({"checked_at": now, "accounts": [
            {"id": a, "enabled": True, "stale": False, "as_of": now,
             "windows": {"five_hour": {"remaining_percent": 25 + i * 50, "resets_at": now}}} for i, a in enumerate(("a", "b"))]}))
        # No collector thread or inference request: populate a test-only observation.
        self.observer.store.ingest([{"timestamp": now, "api_key": self.keys[0], "execution_id": "fixture-1",
            "model": "claude-fixture", "provider": "claude", "auth_index": "fixture-account", "failed": False,
            "tokens": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}}])

    def run_checks(self, change=None):
        def request(endpoint, path, key=None):
            status, data = verify.get(endpoint, path, key)
            return change(path, key, status, data) if change else (status, data)
        runner = verify.Verification(request)
        try:
            runner.run(self.config, self.observer_config)
        except verify.CheckFailed:
            pass
        return runner.report()

    def test_real_http_readonly_and_no_secret_output(self):
        def dump(store):
            with store.db() as con:
                return list(con.iterdump())
        before = dump(self.backend.store), dump(self.observer.store)
        report = self.run_checks()
        self.assertTrue(report["ok"], report)
        self.assertEqual(before, (dump(self.backend.store), dump(self.observer.store)))
        self.assertEqual(self.cpa_calls, ["/v8/management/config/access/api-keys"])
        for secret in self.keys + ["synthetic-hmac", "synthetic-management"]:
            self.assertNotIn(secret, json.dumps(report))
        self.assertIn("new_usage_event_ingestion", report["not_verified"])

    def test_bad_scope_acl_totals_and_overview_each_fail(self):
        for failure in ("scope", "acl", "totals", "overview", "client"):
            with self.subTest(failure=failure):
                self.observer.rate.clear()
                def change(path, key, status, data):
                    if path == "/v1/quota" and failure == "scope": data["scope"] = "authorized_pool"
                    if path == "/v1/quota" and failure == "acl": data["accounts"] = []
                    if path == "/v1/usage?period=overview" and failure == "totals": data["windows"][0]["totals"]["requests"] += 1
                    if path == "/v1/usage?period=overview" and failure == "overview": data["windows"].reverse()
                    if path == "/v1/usage?period=all" and failure == "client": data["client"] = "another-client"
                    return status, data
                self.assertFalse(self.run_checks(change)["ok"])

    def test_stale_collector_fails_without_reinterpreting_zero_history(self):
        self.observer.store.meta("collector_last_success", "2020-01-01T00:00:00+00:00")
        self.assertFalse(self.run_checks()["ok"])

    def test_existing_http_failure_causes_nonzero_exit(self):
        with patch.object(verify.Verification, "run", side_effect=RuntimeError(self.keys[0])):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = verify.main(["--config", str(self.config_file)])
            self.assertEqual(code, 1)
            self.assertNotIn(self.keys[0], output.getvalue())
            self.assertFalse(json.loads(output.getvalue())["ok"])
        self.assertFalse(self.run_checks(lambda p, k, s, d: (503, {}) if p == "/healthz" else (s, d))["ok"])

    def test_no_public_target_or_redirect_is_followed(self):
        for url in ("https://example.invalid", "http://example.invalid", "http://127.0.0.1/path",
                    "http://user:secret@127.0.0.1", "http://127.0.0.1?query=1"):
            with self.subTest(url=url), self.assertRaises(ValueError): verify.loopback(url)
        self.assertEqual(verify.loopback("http://[::1]:9876"), ("::1", 9876))
        connection = unittest.mock.MagicMock()
        connection.getresponse.return_value.status = 302
        connection.getresponse.return_value.read.return_value = b''
        with patch.object(verify.http.client, "HTTPConnection", return_value=connection):
            self.assertEqual(verify.get("http://127.0.0.1:9876", "/healthz"), (302, None))
        connection.request.assert_called_once_with("GET", "/healthz", headers={})

    def test_unknown_accounting_and_empty_history_keep_their_meaning(self):
        data = self.observer.store.summary(self.observer.store.client(self.keys[0]), "all")
        self.assertTrue(verify.model_totals_match(data))
        self.assertIsNone(data["totals"]["cache_read_tokens"])
        bad = copy.deepcopy(data)
        bad["totals"]["cache_read_tokens"] = 0
        self.assertFalse(verify.model_totals_match(bad))
        empty = self.observer.store.summary(self.observer.store.client(self.keys[1]), "all")
        self.assertTrue(verify.model_totals_match(empty))
        self.assertEqual(empty["totals"]["requests"], 0)
        self.assertEqual(empty["history_before_collection"], "not_available")


if __name__ == "__main__":
    unittest.main()
