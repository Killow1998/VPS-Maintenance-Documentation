import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from observer import Observer, QuotaError, Server, Store, now, period_bounds, quota_windows


def event(key="test-key-a", execution="execution-a"):
    return {"timestamp": now(), "api_key": key, "execution_id": execution,
            "model": "claude-sonnet-5-5", "provider": "claude", "auth_index": "private-account",
            "source": "private@example.invalid", "failed": False, "latency_ms": 20,
            "tokens": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130},
            "token_breakdown": {"schema_version": 2, "quality": "complete", "total_tokens": 130,
                                "input": {"total_tokens": 100, "uncached_tokens": 60,
                                          "cache_read_tokens": 30, "cache_write_tokens": 10},
                                "output": {"total_tokens": 30, "reasoning_tokens": 20},
                                "unclassified_tokens": 0}}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "usage.sqlite"
        self.prices = json.loads(Path(__file__).with_name("prices.json").read_text())
        self.store = Store(self.path, "test-secret", {}, self.prices)

    def tearDown(self):
        self.temp.cleanup()

    def test_dedup_cache_and_cost_bounds(self):
        self.assertEqual(self.store.ingest([event(), event()]), (1, 0))
        totals = self.store.summary(self.store.client("test-key-a"), "all")["totals"]
        self.assertEqual(totals["total_tokens"], 130)
        self.assertEqual(totals["uncached_tokens"], 60)
        self.assertEqual(totals["output_tokens"], 30)  # Thinking is already included.
        self.assertAlmostEqual(totals["estimated_cost_usd_low"], (60*2 + 30*.1 + 10*2.5 + 30*10)/1e6)
        self.assertGreater(totals["estimated_cost_usd_high"], totals["estimated_cost_usd_low"])

    def test_per_key_isolation_and_storage_contains_no_secrets(self):
        self.store.ingest([event(), event("test-key-b", "execution-b")])
        a = self.store.summary(self.store.client("test-key-a"), "all")
        self.assertEqual(a["totals"]["requests"], 1)
        with self.store.db() as con:
            rows = json.dumps([tuple(r) for r in con.execute("SELECT * FROM records")])
        for secret in ["test-key-a", "test-key-b", "private-account", "private@example.invalid"]:
            self.assertNotIn(secret, rows)

    def test_unknown_accounting_is_not_reported_as_zero(self):
        item = event()
        item.pop("token_breakdown")
        self.store.ingest([item])
        totals = self.store.summary(self.store.client("test-key-a"), "all")["totals"]
        self.assertIsNone(totals["cache_read_tokens"])
        self.assertIsNone(totals["estimated_cost_usd_low"])
        self.assertEqual(totals["incomplete_accounting_requests"], 1)

    def test_bad_breakdown_cannot_be_priced(self):
        item = event()
        item["token_breakdown"]["input"]["uncached_tokens"] = 999
        self.store.ingest([item])
        self.assertIsNone(self.store.summary(self.store.client("test-key-a"), "all")["totals"]["estimated_cost_usd_low"])

    def test_beijing_day_boundary(self):
        start, end = period_bounds("day", "2026-10-25")
        self.assertTrue(start.startswith("2026-10-24T16:00:00"))
        self.assertTrue(end.startswith("2026-10-25T16:00:00"))
        with self.assertRaises(ValueError):
            period_bounds("month", "2026-13")

    def test_account_and_recent_filters_preserve_unknowns(self):
        before, after = event(execution="before"), event(execution="after")
        before.update(timestamp="2026-10-25T15:59:59Z", auth_index=None)
        before.pop("token_breakdown")
        after.update(timestamp="2026-10-25T16:00:00Z")
        self.store.ingest([before, after])
        report = self.store.summary(self.store.client("test-key-a"), "day", "2026-10-25")
        self.assertEqual(report["totals"]["requests"], 1)
        self.assertEqual(len(report["recent"]), 1)
        self.assertIsNone(report["accounts"][0]["id"])
        self.assertIsNone(report["recent"][0]["account_id"])
        self.assertIsNone(report["accounts"][0]["cache_read_tokens"])
        self.assertIsNone(report["recent"][0]["estimated_cost_usd_low"])


class FakeBackend:
    def __init__(self):
        self.keys = {"test-key-a", "test-key-b"}
        self.fail_quota = False

    def valid_key(self, key):
        return key in self.keys

    def accounts(self):
        return [{"auth_index": "private-index", "provider": "claude", "disabled": False}]

    def quota(self, _):
        if self.fail_quota:
            raise ValueError("permission denied")
        return {"five_hour": {"utilization": 25, "resets_at": "2026-10-26T00:00:00Z"},
                "seven_day": {"utilization": 90, "resets_at": None}}


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = {"hmac_secret": "test-secret", "management_key": "test-manager",
                  "database": str(Path(self.temp.name) / "usage.sqlite"), "cpa_base_url": "http://127.0.0.1:8317",
                  "prices_file": str(Path(__file__).with_name("prices.json"))}
        self.observer = Observer(config)
        self.observer.backend = FakeBackend()
        self.observer.store.ingest([event(), event("test-key-b", "execution-b")])
        self.observer.refresh_quota()
        self.server = Server(("127.0.0.1", 0), self.observer)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def get(self, path, key=None):
        headers = {"Authorization": "Bearer " + key} if key else {}
        try:
            with urlopen(Request(self.base + path, headers=headers), timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_auth_and_no_other_client_selector(self):
        self.assertEqual(self.get("/v1/quota")[0], 401)
        self.assertEqual(self.get("/v1/quota", "invalid")[0], 401)
        self.assertEqual(self.get("/v1/usage?client=other", "test-key-a")[0], 400)
        self.assertEqual(self.get("/v1/quota?account=other", "test-key-a")[0], 400)
        status, body = self.get("/v1/usage?period=all", "test-key-a")
        self.assertEqual(status, 200)
        self.assertEqual(body["totals"]["requests"], 1)

    def test_quota_is_shared_and_contains_no_credentials(self):
        status, body = self.get("/v1/quota", "test-key-a")
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], "shared_pool")
        self.assertEqual(body["windows"]["five_hour"]["remaining_percent"], 75)
        self.assertNotIn("private-index", json.dumps(body))

    def test_own_usage_account_ids_match_quota_without_leaking_other_keys(self):
        extra = event(execution="newer-a")
        extra.update(auth_index="private-index", timestamp="2099-01-01T00:00:00Z")
        self.observer.store.ingest([extra])
        status, body = self.get("/v1/usage?period=all&recent=1", "test-key-a")
        quota_id = self.observer.quota_response()["accounts"][0]["id"]
        self.assertEqual(status, 200)
        self.assertEqual(body["totals"]["requests"], 2)
        self.assertEqual(sum(a["requests"] for a in body["accounts"]), 2)
        self.assertEqual(len(body["recent"]), 1)
        self.assertEqual(body["recent"][0]["account_id"], quota_id)
        self.assertEqual(body["recent"][0]["total_tokens"], 130)
        self.assertIn(quota_id, {a["id"] for a in body["accounts"]})
        other = self.get("/v1/usage?period=all&recent=50", "test-key-b")[1]
        self.assertEqual(other["totals"]["requests"], 1)
        self.assertEqual(len(other["recent"]), 1)
        self.assertNotEqual(other["recent"][0]["account_id"], quota_id)
        for private in ("test-key-a", "test-key-b", "private-index", "private@example.invalid", "execution-a"):
            self.assertNotIn(private, json.dumps(body))

    def test_recent_limits_cannot_expand_client_scope(self):
        body = self.get("/v1/usage?period=all&recent=0", "test-key-a")[1]
        self.assertEqual(body["recent"], [])
        self.assertEqual(body["totals"]["requests"], 1)
        for query in ("recent=51", "recent=-1", "recent=", "recent=1&recent=2", "account=other", "client=other"):
            self.assertEqual(self.get("/v1/usage?" + query, "test-key-a")[0], 400)

    def test_revoke_takes_effect_after_short_cache(self):
        self.assertEqual(self.get("/v1/quota", "test-key-a")[0], 200)
        self.observer.backend.keys.remove("test-key-a")
        with self.observer.lock:
            self.observer.auth_cache.clear()  # Simulate expiry of the five-second cache.
        self.assertEqual(self.get("/v1/quota", "test-key-a")[0], 401)

    def test_failed_quota_stays_stale_not_full(self):
        self.observer.backend.fail_quota = True
        self.observer.refresh_quota()
        body = self.observer.quota_response()
        self.assertTrue(body["stale"])
        self.assertEqual(body["accounts_with_fresh_quota"], 0)
        self.assertIsNone(body["windows"]["five_hour"]["remaining_percent"])

    def test_multiple_accounts_do_not_add_percentages(self):
        data = json.loads(self.observer.store.meta("quota_snapshot"))
        data["accounts"].append(copy.deepcopy(data["accounts"][0]))
        data["accounts"][1]["id"] = "account-2"
        self.observer.store.meta("quota_snapshot", json.dumps(data))
        window = self.observer.quota_response()["windows"]["five_hour"]
        self.assertIsNone(window["remaining_percent"])
        self.assertEqual(window["min_remaining_percent"], 75)
        self.assertEqual(window["max_remaining_percent"], 75)

    def paired_backend(self):
        backend = FakeBackend()
        backend.calls = []
        backend.refreshes = []
        backend.accounts = lambda: [
            {"name": "main.json", "auth_index": "model-index", "provider": "claude", "disabled": False},
            {"name": "quota.json", "auth_index": "quota-index", "provider": "claude", "disabled": True,
             "email": "owner@example.invalid"}]
        original = backend.quota
        def quota(index):
            backend.calls.append(index)
            return original(index)
        backend.quota = quota
        backend.refresh_credential = backend.refreshes.append
        self.observer.config["quota_bindings"] = {
            "main.json": {"credential": "quota.json", "expected_email": "owner@example.invalid"}}
        self.observer.backend = backend
        return backend

    def test_paired_quota_credential_is_not_a_second_pool_account(self):
        backend = self.paired_backend()
        self.observer.refresh_quota()
        body = self.observer.quota_response()
        self.assertEqual(backend.calls, ["quota-index"])
        self.assertEqual(body["accounts_total"], 1)
        self.assertEqual(body["accounts_enabled"], 1)
        self.assertEqual(body["accounts_with_fresh_quota"], 1)
        for private in ("main.json", "quota.json", "owner@example.invalid", "quota-index", "source_revision"):
            self.assertNotIn(private, json.dumps(body))

    def test_wrong_quota_identity_never_queries_or_reuses_other_account_data(self):
        backend = self.paired_backend()
        self.observer.refresh_quota()
        self.observer.config["quota_bindings"]["main.json"]["expected_email"] = "other@example.invalid"
        backend.calls.clear()
        self.observer.refresh_quota()
        body = self.observer.quota_response()
        self.assertEqual(backend.calls, [])
        self.assertEqual(body["accounts"][0]["reason"], "quota_identity_mismatch")
        self.assertEqual(body["accounts"][0]["windows"], {})
        self.assertEqual(body["accounts_with_fresh_quota"], 0)

    def test_expired_quota_source_refreshes_once_without_enabling_it(self):
        backend = self.paired_backend()
        original = FakeBackend().quota
        def quota(index):
            backend.calls.append(index)
            if len(backend.calls) == 1:
                raise QuotaError("upstream_http_401", 401)
            return original(index)
        backend.quota = quota
        self.observer.refresh_quota()
        self.assertEqual(backend.refreshes, ["quota.json"])
        self.assertEqual(backend.calls, ["quota-index", "quota-index"])
        self.assertEqual(self.observer.quota_response()["accounts_with_fresh_quota"], 1)
        self.assertTrue(backend.accounts()[1]["disabled"])

    def test_scope_failure_does_not_trigger_refresh_and_rate_limit_backs_off(self):
        backend = self.paired_backend()
        def quota(index):
            backend.calls.append(index)
            raise QuotaError("missing_scope", 403)
        backend.quota = quota
        self.observer.refresh_quota()
        self.assertEqual(backend.refreshes, [])
        self.assertEqual(self.observer.quota_response()["accounts"][0]["reason"], "missing_scope")
        def limited(index):
            backend.calls.append(index)
            raise QuotaError("upstream_http_429", 429, 3600)
        backend.quota = limited
        self.observer.refresh_quota()
        before = len(backend.calls)
        self.observer.refresh_quota()
        self.assertEqual(len(backend.calls), before)
        self.assertIn("retry_at", self.observer.quota_response()["accounts"][0])


if __name__ == "__main__":
    unittest.main()
