import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend import Backend, scope_quota


class ObserverIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.snapshot = {"accounts": [
            {"id": "a", "enabled": True, "status": "ok", "stale": False, "as_of": "2026-10-26T12:00:00Z",
             "windows": {"five_hour": {"remaining_percent": 65, "resets_at": "2026-10-26T16:00:00Z"}}},
            {"id": "b", "enabled": True, "status": "ok", "stale": False, "as_of": "2026-10-26T12:00:00Z",
             "windows": {"five_hour": {"remaining_percent": 25, "resets_at": "2026-10-26T17:00:00Z"}}}],
            "checked_at": "2026-10-26T12:00:00Z", "query_status": "ok", "stale": False, "windows": {}}
        self.failures = set()
        fixture = self

        class Observer(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self):
                key = self.headers.get("Authorization", "")
                if key not in ("Bearer fixture-a", "Bearer fixture-b"):
                    code, data = 401, {"error": "invalid fixture key"}
                elif self.path in fixture.failures:
                    code, data = 503, {"error": "private diagnostic must not escape"}
                elif self.path == "/v1/quota":
                    code, data = 200, fixture.snapshot
                elif self.path == "/v1/usage?period=1h":
                    count = 2 if key.endswith("a") else 0
                    code, data = 200, {"totals": {"requests": count, "total_tokens": count * 123},
                                      "collector_status": "ok", "last_collected_at": "2026-10-26T12:00:00Z",
                                      "collection_started_at": "2026-10-25T12:00:00Z"}
                else:
                    code, data = 404, {}
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.observer = ThreadingHTTPServer(("127.0.0.1", 0), Observer)
        threading.Thread(target=self.observer.serve_forever, daemon=True).start()
        root = Path(self.temp.name)
        config = root / "observer.json"
        config.write_text(json.dumps({"cpa_base_url": "http://127.0.0.1:8317", "port": self.observer.server_port,
                                      "database": str(root / "unavailable-observer.sqlite"), "quota_poll_seconds": 300}))
        self.backend = Backend({"observer_config": str(config), "database": str(root / "control.sqlite"), "active": True})
        self.backend.client_keys = {"ca": "fixture-a", "cb": "fixture-b"}
        self.backend.updated = time.monotonic()
        self.backend.store.sync_inventory([{"id": "ca", "name": "A"}, {"id": "cb", "name": "B"}],
                                          [{"id": "a", "name": "A", "enabled": True}, {"id": "b", "name": "B", "enabled": True}])

    def tearDown(self):
        self.observer.shutdown()
        self.observer.server_close()
        self.temp.cleanup()

    def test_live_http_snapshot_without_observer_database_access(self):
        state = self.backend.state()
        self.assertEqual(state["accounts"][0]["windows"]["five_hour"]["remaining_percent"], 65)
        self.assertEqual(state["accounts"][0]["as_of"], self.snapshot["checked_at"])
        self.assertEqual(state["clients"][0]["usage"]["requests"], 2)
        self.assertEqual(state["clients"][1]["usage"]["requests"], 0)
        self.assertEqual(state["collection"]["collector_status"], "ok")
        self.assertFalse(Path(self.backend.c["database"]).exists())
        self.assertNotIn("fixture-a", json.dumps(state))

    def test_observer_failure_is_unknown_and_does_not_disable_accounts(self):
        self.failures.add("/v1/quota")
        state = self.backend.state()
        self.assertEqual(state["quota"]["query_status"], "unavailable")
        self.assertTrue(state["accounts"][0]["enabled"])
        self.assertEqual(state["accounts"][0]["quota_reason"], "observer_unavailable")
        self.assertIsNone(state["clients"][0]["usage"]["requests"])
        self.assertNotIn("private diagnostic", json.dumps(state))

    def test_usage_failure_is_not_zero_and_does_not_hide_quota(self):
        self.failures.add("/v1/usage?period=1h")
        state = self.backend.state()
        self.assertIsNone(state["clients"][0]["usage"]["requests"])
        self.assertEqual(state["collection"]["collector_status"], "unavailable")
        self.assertFalse(state["accounts"][0]["stale"])

    def test_public_quota_recalculates_aggregates_after_acl_filter(self):
        result = self.backend.quota({"a"})
        self.assertEqual(result["scope"], "shared_pool")
        self.assertEqual(result["visibility"], "authorized_accounts")
        self.assertEqual([a["id"] for a in result["accounts"]], ["a"])
        self.assertEqual(result["windows"]["five_hour"]["remaining_percent"], 65)
        self.assertEqual(result["windows"]["five_hour"]["min_remaining_percent"], 65)
        self.assertEqual(result["accounts_total"], 1)
        denied = self.backend.quota(set())
        self.assertEqual(denied["accounts"], [])
        self.assertEqual(denied["windows"], {})
        self.assertTrue(denied["stale"])

    def test_stale_and_unknown_values_do_not_become_fresh_aggregates(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["accounts"][0]["stale"] = True
        snapshot["accounts"][1]["windows"]["five_hour"]["remaining_percent"] = None
        result = scope_quota(snapshot, {"a", "b"})
        self.assertEqual(result["windows"]["five_hour"]["known_accounts"], 0)
        self.assertIsNone(result["windows"]["five_hour"]["min_remaining_percent"])
        self.assertTrue(result["stale"])
        self.assertEqual(self.snapshot["accounts"][1]["windows"]["five_hour"]["remaining_percent"], 25)


if __name__ == "__main__":
    unittest.main()
