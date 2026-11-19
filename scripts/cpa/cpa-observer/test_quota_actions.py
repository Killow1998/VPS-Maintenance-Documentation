"""Synthetic reset spending, durable replay protection and cooldown recovery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from observer import Observer, Server, quota_windows
from quota_actions import grants, recoverable


def grant_body():
    return {"cedar_ember": {"eligible": True, "at_limit": True, "grants": [{
        "id": "full-test", "resets_total": 2, "resets_left": 2, "clears": ["five_hour", "seven_day"],
        "usable_now": True, "use_requires_limit": False, "ends_at": "2099-01-01T00:00:00Z"}]}}


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"database": str(Path(self.temp.name) / "usage.sqlite"), "hmac_secret": "synthetic-hmac",
                       "cpa_base_url": "http://127.0.0.1:1", "management_key": "synthetic-management",
                       "prices_file": str(Path(__file__).with_name("prices.json")),
                       "quota_bindings": {"main": {"credential": "helper", "expected_email": "a@example.invalid"}}}
        self.obs = Observer(self.config)
        self.entries = [{"name": "main", "provider": "claude", "auth_index": "inference"},
                        {"name": "helper", "provider": "claude", "auth_index": "profile", "disabled": True,
                         "email": "a@example.invalid"}]
        self.obs.backend = Mock()
        self.obs.backend.accounts.return_value = copy.deepcopy(self.entries)
        self.account = "account-" + self.obs.store.digest("account:inference")[:12]
        self.spends = []
        self.answer = "reset"

        def request(path, data):
            self.assertEqual(data["auth_index"], "profile")
            url = data["url"]
            if "/usage" in url:
                body = grant_body()
            elif "/profile" in url:
                body = {"organization": {"uuid": "00000000-0000-0000-0000-000000000001"}}
            else:
                self.spends.append(json.loads(data["data"]))
                if self.answer == "timeout":
                    raise TimeoutError
                body = {"result": self.answer}
            return {"status_code": 200, "body": json.dumps(body)}
        self.obs.backend.request.side_effect = request

    def test_duplicate_confirmation_spends_once_even_after_process_restart(self):
        first = self.obs.actions.redeem(self.account, "full-test", "operation-one")
        restarted = Observer(self.config)
        restarted.backend = self.obs.backend
        second = restarted.actions.redeem(self.account, "full-test", "operation-one")
        self.assertEqual(first, second)
        self.assertEqual(len(self.spends), 1)
        self.assertEqual(self.spends[0]["program"], "cedar_ember")

    def test_old_operation_replay_after_another_operation_never_spends_again(self):
        with patch('quota_actions.time.time', return_value=10000) as clock:
            first = self.obs.actions.redeem(self.account, 'full-test', 'operation-a')
            clock.return_value = 10031
            self.obs.actions.redeem(self.account, 'full-test', 'operation-b')
            clock.return_value = 10062
            restarted = Observer(self.config)
            restarted.backend = self.obs.backend
            replay = restarted.actions.redeem(self.account, 'full-test', 'operation-a')
            with self.assertRaises(ValueError):
                restarted.actions.redeem(self.account, 'different-grant', 'operation-a')
        self.assertEqual(replay, first)
        self.assertEqual(len(self.spends), 2)

    def test_legacy_last_operation_remains_replayable_after_upgrade(self):
        legacy = {'id': 'legacy', 'grant_id': 'full-test', 'created': 10000,
                  'actor': 'admin', 'request_id': 'synthetic', 'result': 'reset'}
        self.obs.store.meta('reset_operation:' + self.account, json.dumps(legacy))
        with patch('quota_actions.time.time', return_value=10031) as clock:
            self.obs.actions.redeem(self.account, 'full-test', 'new')
            clock.return_value = 10062
            self.assertEqual(self.obs.actions.redeem(self.account, 'full-test', 'legacy'), legacy)
        self.assertEqual(len(self.spends), 1)

    def test_concurrent_observers_reserve_operation_before_spending(self):
        started, release = threading.Event(), threading.Event()
        original = self.obs.backend.request.side_effect
        def delayed(path, data):
            if 'reset_rate_limits' in data['url']:
                started.set()
                if not release.wait(5): raise TimeoutError('test wait')
            return original(path, data)
        self.obs.backend.request.side_effect = delayed
        other = Observer(self.config)
        other.backend = self.obs.backend
        results, errors = [], []
        def first():
            try:
                results.append(self.obs.actions.redeem(self.account, 'full-test', 'one'))
            except Exception as error:
                errors.append(error)
        with patch('quota_actions.time.time', return_value=10000):
            thread = threading.Thread(target=first)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                replay = other.actions.redeem(self.account, 'full-test', 'one')
                self.assertEqual(replay['result'], 'unknown')
                with self.assertRaises(ValueError):
                    other.actions.redeem(self.account, 'full-test', 'two')
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]['result'], 'reset')
        self.assertEqual(len(self.spends), 1)

    def test_failed_preflight_keeps_old_history_and_allows_later_retry(self):
        original = self.obs.backend.request.side_effect
        def fail_preflight(path, data):
            if '/usage' in data['url']:
                return {'status_code': 403, 'body': '{}'}
            return original(path, data)
        with patch('quota_actions.time.time', return_value=10000) as clock:
            previous = self.obs.actions.redeem(self.account, 'full-test', 'previous')
            clock.return_value = 10031
            self.obs.backend.request.side_effect = fail_preflight
            with self.assertRaises(ValueError):
                self.obs.actions.redeem(self.account, 'full-test', 'retry')
            self.assertEqual(json.loads(self.obs.store.meta('reset_operation:' + self.account)), previous)
            clock.return_value = 10062
            self.obs.backend.request.side_effect = original
            self.assertEqual(self.obs.actions.redeem(self.account, 'full-test', 'retry')['result'], 'reset')
            self.assertEqual(self.obs.actions.redeem(self.account, 'full-test', 'previous'), previous)
        self.assertEqual(len(self.spends), 2)

    def test_ambiguous_redemption_blocks_new_operation_without_retrying(self):
        self.answer = "timeout"
        self.assertEqual(self.obs.actions.redeem(self.account, "full-test", "one")["result"], "unknown")
        self.assertEqual(self.obs.actions.redeem(self.account, "full-test", "one")["result"], "unknown")
        with self.assertRaises(ValueError):
            self.obs.actions.redeem(self.account, "full-test", "two")
        self.assertEqual(len(self.spends), 1)

    def test_wrong_account_and_mismatched_profile_never_spend(self):
        with self.assertRaises(ValueError):
            self.obs.actions.redeem("unknown", "full-test", "one")
        self.obs.backend.accounts.return_value[1]["email"] = "other@example.invalid"
        with self.assertRaises(ValueError):
            self.obs.actions.redeem(self.account, "full-test", "one")
        self.assertFalse(self.spends)

    def test_http_200_with_unrecognized_business_result_is_unknown(self):
        self.answer = "some_new_value"
        self.assertEqual(self.obs.actions.redeem(self.account, "full-test", "one")["result"], "unknown")

    def test_fable_scope_not_mislabeled_full_and_missing_state_not_zero(self):
        body = grant_body()
        body["cedar_ember"]["grants"][0].update(gives_back_weekly=True, clears=["five_hour", "seven_day_overage_included"])
        self.assertEqual(grants(body)["grants"][0]["kind"], "fable")
        self.assertEqual(grants({})["status"], "unknown")

    def test_weekly_exhaustion_and_nonquota_errors_are_not_cleared(self):
        entry = {"cooldowns": [{"scope": "credential", "reason": "credential_quota"}]}
        windows = {"five_hour": {"remaining_percent": 100}, "seven_day": {"remaining_percent": 0}}
        self.assertFalse(recoverable(entry, windows))
        windows["seven_day"]["remaining_percent"] = 90
        self.assertTrue(recoverable(entry, windows))
        entry["cooldowns"].append({"scope": "model", "reason": "not_found"})
        self.assertFalse(recoverable(entry, windows))

    def test_mixed_model_cooldown_is_not_cleared_without_model_proof(self):
        entry = {"auth_index": "inference", "unavailable": True, "cooldowns": [
            {"scope": "credential", "reason": "credential_quota"},
            {"scope": "model", "reason": "quota", "model": "claude-opus"}]}
        windows = {"five_hour": {"remaining_percent": 80}, "seven_day": {"remaining_percent": 70}}
        self.assertFalse(recoverable(entry, windows))
        self.assertFalse(recoverable(entry, dict(windows, seven_day_opus={"remaining_percent": 90})))
        self.assertFalse(recoverable({"cooldowns": [{"reason": "quota"}]}, windows))
        item = {"windows": windows}
        self.obs.backend.request = Mock()
        self.obs.actions.recover(entry, item)
        self.obs.backend.request.assert_not_called()
        self.assertEqual(item["routing"]["recovery"], "unchanged")

    def test_official_fable_window_prevents_premature_recovery(self):
        windows = quota_windows({"five_hour": {"utilization": 0}, "seven_day": {"utilization": 10},
                                 "iguana_necktie": {"utilization": 100}})
        self.assertEqual(windows["seven_day_fable"]["remaining_percent"], 0)
        self.assertFalse(recoverable({"cooldowns": [{"scope": "credential", "reason": "quota"}]}, windows))

    def test_invalid_reported_window_is_unknown_and_cannot_be_recovered(self):
        windows = quota_windows({'five_hour': {'utilization': 20}, 'seven_day': {'utilization': 30},
                                 'seven_day_opus': {'utilization': None}, 'seven_day_sonnet': None})
        self.assertIsNone(windows['seven_day_opus']['remaining_percent'])
        self.assertIsNone(windows['seven_day_sonnet']['remaining_percent'])
        self.assertFalse(recoverable({'cooldowns': [{'scope': 'credential', 'reason': 'quota'}]}, windows))

    def test_recovery_never_clears_stale_snapshot_and_public_hides_grants(self):
        entry = {"auth_index": "inference", "cooldowns": [{"scope": "credential", "reason": "quota"}]}
        item = {"id": self.account, "enabled": True, "as_of": "2099-01-01T00:00:00+00:00", "stale": False,
                "windows": {"five_hour": {"remaining_percent": 100, "resets_at": None},
                            "seven_day": {"remaining_percent": 90, "resets_at": None}}, "resets": grants(grant_body())}
        self.obs.backend.request = Mock(return_value={"status": "ok"})
        self.obs.actions.recover(entry, item)
        self.obs.backend.request.assert_not_called()
        self.assertEqual(item['routing']['recovery'], 'manual')
        self.obs.store.meta("quota_snapshot", json.dumps({"accounts": [item]}))
        self.assertNotIn("resets", self.obs.quota_response()["accounts"][0])
        self.assertIn("resets", self.obs.quota_response(admin=True)["accounts"][0])

    def test_manual_refresh_is_coalesced_and_read_never_queries_upstream(self):
        self.obs._refresh_quota = Mock()
        self.obs.quota_control({"action": "read"})
        self.obs._refresh_quota.assert_not_called()
        import time
        self.obs.quota_last_attempt = time.monotonic()
        self.obs.quota_control({"action": "refresh"})
        self.obs._refresh_quota.assert_not_called()
        self.obs.quota_lock.acquire()
        self.assertEqual(self.obs.quota_control({"action": "refresh"})["status"], "busy")
        self.obs.quota_lock.release()

    def test_private_http_route_rejects_public_keys_and_accepts_management_read(self):
        server = Server(("127.0.0.1", 0), self.obs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = 'http://127.0.0.1:' + str(server.server_port) + '/internal/quota'
            for key in ('', 'ordinary-client-key', 'nonascii-é'):
                req = Request(url, data=b'{"action":"read"}', headers={'Content-Type':'application/json','Authorization':'Bearer '+key})
                with self.assertRaises(HTTPError) as error:
                    urlopen(req, timeout=3)
                self.assertEqual(error.exception.code, 401)
            req = Request(url, data=b'{"action":"read"}', headers={'Content-Type':'application/json','Authorization':'Bearer synthetic-management'})
            with urlopen(req, timeout=3) as response:
                self.assertEqual(json.load(response)['accounts'], [])
            self.obs.backend.request.assert_not_called()
        finally:
            server.shutdown();server.server_close()
