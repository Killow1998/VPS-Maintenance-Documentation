"""Synthetic reset spending, durable replay protection and cooldown recovery."""
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from observer import Backend, Observer, QuotaError, Server, quota_windows, retry_at_after
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
            self.assertIn(data["auth_index"], ("profile", "profile-two"))
            url = data["url"]
            if "/usage" in url:
                body = grant_body()
            elif "/profile" in url:
                body = {"account": {"email": "a@example.invalid"},
                        "organization": {"uuid": "00000000-0000-0000-0000-000000000001"}}
            else:
                self.spends.append(json.loads(data["data"]))
                if self.answer == "timeout":
                    raise TimeoutError
                body = {"result": self.answer}
            return {"status_code": 200, "body": json.dumps(body)}
        self.obs.backend.request.side_effect = request
        self.obs.backend.quota.side_effect = lambda index: Backend.quota(self.obs.backend, index)

    def shared_account(self):
        self.obs.config['quota_bindings']['other'] = {'credential': 'helper', 'expected_email': 'a@example.invalid'}
        self.entries.append({'name': 'other', 'provider': 'claude', 'auth_index': 'other-inference'})
        self.obs.backend.accounts.return_value = copy.deepcopy(self.entries)
        return 'account-' + self.obs.store.digest('account:other-inference')[:12]

    def test_unknown_shared_source_blocks_other_account_after_restart(self):
        other = self.shared_account()
        self.answer = 'timeout'
        with patch('quota_actions.time.time', return_value=10000) as clock:
            self.assertEqual(self.obs.actions.redeem(self.account, 'full-test', 'first')['result'], 'unknown')
            clock.return_value = 15000
            restarted = Observer(self.config)
            restarted.backend = self.obs.backend
            with self.assertRaisesRegex(ValueError, 'reset_pending_or_recent'):
                restarted.actions.redeem(other, 'full-test', 'second')
        self.assertEqual(len(self.spends), 1)

    def test_shared_principal_blocks_parallel_redemption(self):
        other_account = self.shared_account()
        started, release = threading.Event(), threading.Event()
        original = self.obs.backend.request.side_effect
        def delayed(path, data):
            if 'reset_rate_limits' in data['url']:
                started.set()
                if not release.wait(5):
                    raise TimeoutError('test wait')
            return original(path, data)
        self.obs.backend.request.side_effect = delayed
        second = Observer(self.config)
        second.backend = self.obs.backend
        results, errors = [], []
        def first():
            try:
                results.append(self.obs.actions.redeem(self.account, 'full-test', 'first'))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            with self.assertRaisesRegex(ValueError, 'reset_pending_or_recent'):
                second.actions.redeem(other_account, 'full-test', 'second')
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]['result'], 'reset')
        self.assertEqual(len(self.spends), 1)

    def test_profile_identity_must_match_before_redemption(self):
        original = self.obs.backend.request.side_effect
        for index, account in enumerate(({}, {'email': 'other@example.invalid'})):
            def mismatched(path, data):
                if '/profile' in data['url']:
                    return {'status_code': 200, 'body': json.dumps({'account': account,
                        'organization': {'uuid': '00000000-0000-0000-0000-000000000001'}})}
                return original(path, data)
            self.obs.backend.request.side_effect = mismatched
            with patch('quota_actions.time.time', return_value=10000 + index * 60):
                with self.assertRaisesRegex(ValueError, 'reset_identity'):
                    self.obs.actions.redeem(self.account, 'full-test', 'profile-mismatch')
            self.assertFalse(self.spends)

    def test_same_email_in_separate_credentials_shares_unknown_lock(self):
        other = self.shared_account()
        self.obs.config['quota_bindings']['other']['credential'] = 'helper-two'
        self.entries.append({'name': 'helper-two', 'provider': 'claude', 'auth_index': 'profile-two',
                             'disabled': True, 'email': 'A@EXAMPLE.INVALID'})
        self.obs.backend.accounts.return_value = copy.deepcopy(self.entries)
        self.answer = 'timeout'
        self.assertEqual(self.obs.actions.redeem(self.account, 'full-test', 'first')['result'], 'unknown')
        with self.assertRaisesRegex(ValueError, 'reset_pending_or_recent'):
            self.obs.actions.redeem(other, 'full-test', 'second')
        self.assertEqual(len(self.spends), 1)

    def test_legacy_unknown_for_shared_account_blocks_reset(self):
        other = self.shared_account()
        self.obs.store.meta('reset_operation:' + self.account, json.dumps({'id': 'legacy',
            'grant_id': 'full-test', 'created': 1, 'request_id': 'previous', 'result': 'unknown'}))
        with self.assertRaisesRegex(ValueError, 'reset_unresolved_legacy'):
            self.obs.actions.redeem(other, 'full-test', 'second')
        self.assertFalse(self.spends)

    def test_unattributed_legacy_unknown_blocks_reset_until_reconciled(self):
        self.obs.store.meta('reset_operation:account-removed', json.dumps({'id': 'legacy',
            'grant_id': 'full-test', 'created': 1, 'request_id': 'previous', 'result': 'unknown'}))
        with self.assertRaisesRegex(ValueError, 'reset_unresolved_legacy'):
            self.obs.actions.redeem(self.account, 'full-test', 'next')
        self.assertFalse(self.spends)

    def test_rebound_account_legacy_unknown_blocks_other_principal(self):
        other = self.shared_account()
        self.obs.store.meta('reset_operation:' + self.account, json.dumps({'id': 'legacy',
            'grant_id': 'full-test', 'created': 1, 'request_id': 'previous', 'result': 'unknown'}))
        self.obs.config['quota_bindings']['main']['credential'] = 'helper-two'
        self.obs.config['quota_bindings']['main']['expected_email'] = 'changed@example.invalid'
        self.entries.append({'name': 'helper-two', 'provider': 'claude', 'auth_index': 'profile-two',
                             'disabled': True, 'email': 'changed@example.invalid'})
        self.obs.backend.accounts.return_value = copy.deepcopy(self.entries)
        with self.assertRaisesRegex(ValueError, 'reset_unresolved_legacy'):
            self.obs.actions.redeem(other, 'full-test', 'next')
        self.assertFalse(self.spends)

    def test_logical_account_email_must_match_bound_identity(self):
        self.entries[0]['email'] = 'other@example.invalid'
        self.obs.backend.accounts.return_value = copy.deepcopy(self.entries)
        with self.assertRaisesRegex(ValueError, 'identity_mismatch'):
            self.obs.actions.redeem(self.account, 'full-test', 'mismatched-account')
        self.assertFalse(self.spends)

    def test_completed_operation_replayed_via_alias_is_not_redeemed(self):
        other = self.shared_account()
        with patch('quota_actions.time.time', return_value=10000) as clock:
            first = self.obs.actions.redeem(self.account, 'full-test', 'same-operation')
            clock.return_value = 11000
            restarted = Observer(self.config)
            restarted.backend = self.obs.backend
            second = restarted.actions.redeem(other, 'full-test', 'same-operation')
            with self.assertRaisesRegex(ValueError, 'reset_selection_changed'):
                restarted.actions.redeem(other, 'different-grant', 'same-operation')
        self.assertEqual(first['request_id'], second['request_id'])
        self.assertEqual(len(self.spends), 1)

    def test_old_principal_record_is_migrated_before_alias_replay(self):
        other = self.shared_account()
        with patch('quota_actions.time.time', return_value=10000) as clock:
            first = self.obs.actions.redeem(self.account, 'full-test', 'old-operation')
            key = 'reset_principal_operation:' + first['reset_principal'] + ':old-operation'
            with self.obs.store.db() as con:
                con.execute('DELETE FROM metadata WHERE key=?', (key,))
            clock.return_value = 11000
            replay = self.obs.actions.redeem(other, 'full-test', 'old-operation')
        self.assertEqual(first['request_id'], replay['request_id'])
        self.assertEqual(len(self.spends), 1)

    def test_conflicting_old_principal_records_block_replay(self):
        self.shared_account()
        principal = self.obs.store.digest('reset-principal:a@example.invalid')
        for index in (1, 2):
            self.obs.store.meta('reset_operation_id:historical-' + str(index) + ':duplicate', json.dumps({
                'id': 'duplicate', 'grant_id': 'full-test', 'request_id': str(index),
                'reset_principal': principal, 'created': 1, 'result': 'reset'}))
        with self.assertRaisesRegex(ValueError, 'reset_ambiguous_history'):
            self.obs.actions.redeem(self.account, 'full-test', 'duplicate')
        self.assertFalse(self.spends)

    def test_completed_legacy_operation_without_principal_blocks_alias_replay(self):
        other = self.shared_account()
        self.obs.store.meta('reset_operation:' + self.account, json.dumps({'id': 'old-operation',
            'grant_id': 'full-test', 'created': 1, 'request_id': 'previous', 'result': 'reset'}))
        with self.assertRaisesRegex(ValueError, 'reset_ambiguous_history'):
            self.obs.actions.redeem(other, 'full-test', 'old-operation')
        self.assertFalse(self.spends)

    def test_two_new_preflights_do_not_block_each_other_as_legacy(self):
        other = self.shared_account()
        with patch('quota_actions.time.time', return_value=10000):
            first, _, _ = self.obs.actions._reserve(self.account, 'full-test', 'same-operation', 'admin')
            second, _, _ = self.obs.actions._reserve(other, 'full-test', 'same-operation', 'admin')
            aliases = [self.account, other]
            self.assertIsNone(self.obs.actions._claim_principal(self.account, 'profile', 'a@example.invalid', first, aliases))
            replay = self.obs.actions._claim_principal(other, 'profile', 'a@example.invalid', second, aliases)
            self.assertEqual(first['request_id'], replay['request_id'])

    def test_mismatched_logical_email_cannot_publish_other_accounts_quota(self):
        self.entries[0]['email'] = 'other@example.invalid'
        self.obs.backend.accounts.return_value = copy.deepcopy(self.entries)
        self.obs.store.meta('quota_snapshot', json.dumps({'accounts': [{
            'id': self.account, 'enabled': True, 'status': 'ok', 'stale': False,
            'windows': {'five_hour': {'remaining_percent': 63, 'resets_at': None}}}]}))
        self.obs.backend.quota.side_effect = None
        self.obs.backend.quota.return_value = {'five_hour': {'utilization': 37}}
        self.obs.refresh_quota()
        account = self.obs.quota_response()['accounts'][0]
        self.assertTrue(account['stale'])
        self.assertEqual(account['windows'], {})
        self.assertEqual(account['reason'], 'quota_identity_mismatch')
        self.obs.backend.quota.assert_not_called()

    def test_unverified_legacy_quota_is_not_published_when_inventory_fails(self):
        deadline = retry_at_after(3600)
        old = {
            'id': self.account, 'enabled': True, 'status': 'ok', 'stale': False,
            'as_of': dt.datetime.now(dt.timezone.utc).isoformat(), 'retry_at': deadline,
            'windows': {'five_hour': {'remaining_percent': 63, 'resets_at': None}}}
        self.obs.backend.accounts.side_effect = RuntimeError('synthetic failure')
        for source in (None, 'unverified-source'):
            with self.subTest(source=source):
                snapshot = dict(old)
                if source:
                    snapshot['source_identity'] = source
                self.obs.store.meta('quota_snapshot', json.dumps({'accounts': [snapshot]}))
                with self.assertRaises(RuntimeError):
                    self.obs.refresh_quota()
                for admin in (False, True):
                    account = self.obs.quota_response(admin=admin)['accounts'][0]
                    self.assertTrue(account['stale'])
                    self.assertEqual(account['windows'], {})
                    self.assertEqual(account['retry_at'], deadline)

    def test_hidden_legacy_quota_is_not_reported_as_ok(self):
        self.obs.store.meta('quota_status', 'ok')
        self.obs.store.meta('quota_snapshot', json.dumps({'accounts': [{
            'id': self.account, 'enabled': True, 'status': 'ok', 'stale': False,
            'as_of': dt.datetime.now(dt.timezone.utc).isoformat(),
            'windows': {'five_hour': {'remaining_percent': 63, 'resets_at': None}}}]}))
        response = self.obs.quota_response()
        self.assertEqual(response['query_status'], 'unavailable')
        self.assertEqual(response['accounts'][0]['windows'], {})

    def test_unknown_shared_principal_is_visible_for_all_aliases(self):
        other = self.shared_account()
        self.answer = 'timeout'
        self.obs.actions.redeem(self.account, 'full-test', 'first')
        self.obs.refresh_quota()
        accounts = {a['id']: a for a in self.obs.quota_response(admin=True)['accounts']}
        self.assertTrue(accounts[self.account]['reset_pending'])
        self.assertTrue(accounts[other]['reset_pending'])

    def test_alias_added_after_unknown_is_marked_pending_on_refresh(self):
        self.answer = 'timeout'
        self.obs.actions.redeem(self.account, 'full-test', 'first')
        other = self.shared_account()
        self.obs.refresh_quota()
        accounts = {a['id']: a for a in self.obs.quota_response(admin=True)['accounts']}
        self.assertTrue(accounts[other]['reset_pending'])

    def test_shared_source_legacy_backoff_blocks_reset_before_first_poll(self):
        other = self.shared_account()
        self.obs.store.meta('quota_snapshot', json.dumps({'accounts': [
            {'id': self.account, 'retry_at': retry_at_after(3600)}]}))
        with self.assertRaises(QuotaError):
            self.obs.actions.redeem(other, 'full-test', 'blocked-shared')
        self.obs.backend.request.assert_not_called()
        self.assertFalse(self.spends)

    def test_completed_replay_does_not_query_provider_again(self):
        self.obs.quota_control({'action': 'redeem', 'account_id': self.account,
                                'grant_id': 'full-test', 'operation_id': 'replay'})
        calls = self.obs.backend.request.call_count
        for _ in range(2):
            self.obs.quota_control({'action': 'redeem', 'account_id': self.account,
                                    'grant_id': 'full-test', 'operation_id': 'replay'})
        self.assertEqual(self.obs.backend.request.call_count, calls)
        self.assertEqual(len(self.spends), 1)

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

    def test_reset_preflight_respects_persisted_source_backoff(self):
        self.obs.store.quota_backoff('profile', retry_at_after(3600))
        with self.assertRaises(QuotaError):
            self.obs.actions.redeem(self.account, 'full-test', 'blocked')
        self.obs.backend.request.assert_not_called()
        self.assertFalse(self.spends)

    def test_reset_migrates_legacy_backoff_before_first_poll(self):
        self.obs.store.meta('quota_snapshot', json.dumps({'accounts': [
            {'id': self.account, 'retry_at': retry_at_after(3600)}]}))
        with self.assertRaises(QuotaError):
            self.obs.actions.redeem(self.account, 'full-test', 'legacy-blocked')
        self.obs.backend.request.assert_not_called()
        self.assertFalse(self.spends)

    def test_legacy_backoff_survives_temporary_missing_source(self):
        self.obs.store.meta('quota_snapshot', json.dumps({'accounts': [
            {'id': self.account, 'retry_at': retry_at_after(3600), 'windows': {}, 'stale': True}]}))
        self.obs.backend.accounts.return_value = [self.entries[0]]
        self.obs.refresh_quota()
        self.obs.backend.accounts.return_value = copy.deepcopy(self.entries)
        with self.assertRaises(QuotaError):
            self.obs.actions.redeem(self.account, 'full-test', 'after-missing')
        self.obs.backend.request.assert_not_called()
        self.assertFalse(self.spends)

    def test_legacy_backoff_survives_temporary_missing_account(self):
        self.obs.store.meta('quota_snapshot', json.dumps({'accounts': [
            {'id': self.account, 'retry_at': retry_at_after(3600), 'windows': {}, 'stale': True}]}))
        self.obs.backend.accounts.return_value = []
        self.obs.refresh_quota()
        self.obs.backend.accounts.return_value = copy.deepcopy(self.entries)
        with self.assertRaises(QuotaError):
            self.obs.actions.redeem(self.account, 'full-test', 'after-account-returned')
        self.obs.backend.request.assert_not_called()
        self.assertFalse(self.spends)

    def test_missing_source_reports_retained_legacy_deadline(self):
        deadline = retry_at_after(3600)
        self.obs.store.meta('quota_snapshot', json.dumps({'accounts': [
            {'id': self.account, 'retry_at': deadline, 'windows': {}, 'stale': True}]}))
        self.obs.backend.accounts.return_value = []
        self.obs.refresh_quota()
        self.obs.backend.accounts.return_value = [self.entries[0]]
        self.obs.refresh_quota()
        for admin in (False, True):
            account = self.obs.quota_response(admin=admin)['accounts'][0]
            self.assertEqual(account['retry_at'], deadline)

    def test_reset_preflight_429_persists_retry_after_without_spending(self):
        original = self.obs.backend.request.side_effect
        def limited(path, data):
            if '/usage' in data['url']:
                return {'status_code': 429, 'body': '{}', 'header': {'Retry-After': ['7200']}}
            return original(path, data)
        self.obs.backend.request.side_effect = limited
        self.obs.backend.quota.side_effect = lambda index: Backend.quota(self.obs.backend, index)
        with self.assertRaises(QuotaError):
            self.obs.actions.redeem(self.account, 'full-test', 'limited')
        deadline = self.obs.store.quota_backoff('profile')
        self.assertGreater((dt.datetime.fromisoformat(deadline) - dt.datetime.now(dt.timezone.utc)).total_seconds(), 7190)
        self.assertFalse(self.spends)

    def test_profile_429_backoff_prevents_post(self):
        original = self.obs.backend.request.side_effect
        def limited(path, data):
            if '/profile' in data['url']:
                return {'status_code': 429, 'body': '{}', 'header': {'Retry-After': ['3600']}}
            return original(path, data)
        self.obs.backend.request.side_effect = limited
        with self.assertRaises(ValueError):
            self.obs.actions.redeem(self.account, 'full-test', 'profile-limited')
        self.assertIsNotNone(self.obs.store.quota_backoff('profile'))
        self.assertFalse(self.spends)

    def test_profile_429_is_visible_in_reads_without_another_poll(self):
        self.obs.backend.quota.side_effect = lambda _: {**grant_body(),
            'five_hour': {'utilization': 20}, 'seven_day': {'utilization': 30}}
        self.obs.refresh_quota()
        original = self.obs.backend.request.side_effect
        def limited(path, data):
            if '/profile' in data['url']:
                return {'status_code': 429, 'body': '{}', 'header': {'Retry-After': ['7200']}}
            return original(path, data)
        self.obs.backend.request.side_effect = limited
        with self.assertRaises(ValueError):
            self.obs.actions.redeem(self.account, 'full-test', 'limited-profile')
        for admin in (False, True):
            result = self.obs.quota_response(admin=admin)
            self.assertTrue(result['accounts'][0]['stale'])
            self.assertEqual(result['accounts'][0]['reason'], 'quota_backoff')
            self.assertIsNotNone(result['accounts'][0]['retry_at'])
            self.assertEqual(result['accounts_with_fresh_quota'], 0)
            self.assertEqual(result['query_status'], 'unavailable')

    def test_profile_429_overlays_legacy_fresh_snapshot_before_first_poll(self):
        self.obs.store.meta('quota_snapshot', json.dumps({'accounts': [{
            'id': self.account, 'enabled': True, 'status': 'ok', 'stale': False,
            'as_of': dt.datetime.now(dt.timezone.utc).isoformat(),
            'windows': {'five_hour': {'used_percent': 20, 'remaining_percent': 80, 'resets_at': None}}}]}))
        self.obs.store.meta('quota_status', 'ok')
        original = self.obs.backend.request.side_effect
        def limited(path, data):
            if '/profile' in data['url']:
                return {'status_code': 429, 'body': '{}', 'header': {'Retry-After': ['3600']}}
            return original(path, data)
        self.obs.backend.request.side_effect = limited
        with self.assertRaises(ValueError):
            self.obs.actions.redeem(self.account, 'full-test', 'legacy-profile')
        result = self.obs.quota_response()
        self.assertTrue(result['accounts'][0]['stale'])
        self.assertIsNotNone(result['accounts'][0]['retry_at'])
        self.assertEqual(result['query_status'], 'unavailable')

    def test_post_429_backoff_prevents_immediate_quota_refresh(self):
        original = self.obs.backend.request.side_effect
        def limited(path, data):
            if 'reset_rate_limits' in data['url']:
                return {'status_code': 429, 'body': '{}', 'header': {'Retry-After': ['3600']}}
            return original(path, data)
        self.obs.backend.request.side_effect = limited
        result = self.obs.quota_control({'action': 'redeem', 'account_id': self.account,
                                         'grant_id': 'full-test', 'operation_id': 'post-limited'})
        self.assertEqual(result['result'], 'rate_limited')
        self.assertIsNotNone(self.obs.store.quota_backoff('profile'))
        usage = [call for call in self.obs.backend.request.call_args_list if '/usage' in call.args[1]['url']]
        self.assertEqual(len(usage), 1)

    def test_abandoned_preflight_expires_without_allowing_old_worker_to_post(self):
        with patch('quota_actions.time.time', return_value=10000) as clock:
            old, _, _ = self.obs.actions._reserve(self.account, 'full-test', 'abandoned', 'admin')
            self.obs.store.meta('quota_snapshot', json.dumps({'accounts': [{'id': self.account, 'enabled': True,
                'windows': {}, 'as_of': None, 'stale': True}]}))
            self.assertTrue(self.obs.quota_response(admin=True)['accounts'][0]['reset_pending'])
            clock.return_value = 10401
            self.assertFalse(self.obs.quota_response(admin=True)['accounts'][0]['reset_pending'])
            with self.assertRaises(ValueError):
                self.obs.actions._begin_post(self.account, old)
            self.assertEqual(self.obs.actions.redeem(self.account, 'full-test', 'new')['result'], 'reset')
            with self.assertRaises(ValueError):
                self.obs.actions._begin_post(self.account, old)
            self.assertEqual(self.obs.actions.redeem(self.account, 'full-test', 'abandoned')['result'], 'preflight_expired')
        self.assertEqual(len(self.spends), 1)

    def test_concurrent_processes_reserve_the_same_operation_once(self):
        gate = Path(self.temp.name) / 'start'
        code = '''
import json, pathlib, sys, time
from observer import Observer
config, account, gate = json.loads(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
observer = Observer(config)
deadline = time.monotonic() + 15
while not gate.exists() and time.monotonic() < deadline:
    time.sleep(.01)
operation, fresh, _ = observer.actions._reserve(account, "full-test", "same-process-id", "admin")
print(json.dumps({"fresh": fresh, "request_id": operation["request_id"]}))
'''
        processes = [subprocess.Popen([sys.executable, '-B', '-c', code, json.dumps(self.config), self.account, str(gate)],
                     cwd=Path(__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            gate.touch()
            results = []
            for process in processes:
                output, error = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, error)
                results.append(json.loads(output))
            self.assertEqual(sum(result['fresh'] for result in results), 1)
            self.assertEqual(results[0]['request_id'], results[1]['request_id'])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_active_backoff_marks_previously_fresh_snapshot_stale(self):
        self.obs.backend.quota.side_effect = lambda _: {**grant_body(),
            'five_hour': {'utilization': 20}, 'seven_day': {'utilization': 30}}
        self.obs.refresh_quota()
        self.assertFalse(self.obs.quota_response(admin=True)['accounts'][0]['stale'])
        self.obs.store.quota_backoff('profile', retry_at_after(3600))
        self.obs.refresh_quota()
        account = self.obs.quota_response(admin=True)['accounts'][0]
        self.assertTrue(account['stale'])
        self.assertEqual(account['reason'], 'quota_backoff')

    def test_ambiguous_redemption_blocks_new_operation_without_retrying(self):
        self.answer = "timeout"
        with patch('quota_actions.time.time', return_value=10000) as clock:
            self.assertEqual(self.obs.actions.redeem(self.account, "full-test", "one")["result"], "unknown")
            clock.return_value = 20000
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
        item['source_identity'] = self.obs.store.digest('quota-source:profile')
        item['source_verified'] = True
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
