import concurrent.futures
from email.message import Message
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server import Store
from session_routing import SessionRouter, session_id


class SessionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "control.sqlite")
        self.router = SessionRouter(self.store, "synthetic-hmac")
        self.candidates = [{"id": "a"}, {"id": "b"}]

    def select(self, session="test-session", client="client", candidates=None, router=None):
        headers = Message()
        if session is not None:
            headers['X-Session-ID'] = session
        return (router or self.router).select(client, 'test-model', headers, {}, self.candidates if candidates is None else candidates)

    def test_known_header_and_claude_metadata_formats(self):
        for header in ('x-session-id', 'X-Claude-Code-Session-Id', 'Session-Id', 'X-Http-Session-Id'):
            headers = Message(); headers[header] = 'abc'
            self.assertEqual(session_id(headers, {}), 'abc')
        for user in ('user_hash_account_uuid_session_abcd-1234', json.dumps({'session_id': 'abcd-1234'})):
            self.assertEqual(session_id(Message(), {'metadata': {'user_id': user}}), 'abcd-1234')
        self.assertEqual(session_id(Message(), {'metadata': {'session_id': 'opaque'}}), 'opaque')
        for metadata in (None, [], {'user_id': 'plain-user'}, {'session_id': '\x85bad'}, {'session_id': 'x' * 257}):
            self.assertEqual(session_id(Message(), {'metadata': metadata}), '')

    def test_existing_session_and_cursors_survive_restart(self):
        self.assertEqual(self.select('one').primary, 'a')
        for _ in range(4): self.assertEqual(self.select('one').primary, 'a')
        restarted = SessionRouter(self.store, 'synthetic-hmac')
        self.assertEqual(self.select('one', router=restarted).primary, 'a')
        self.assertEqual(self.select('two', router=restarted).primary, 'b')
        self.assertEqual(self.select('three', router=restarted).primary, 'a')

    def test_key_isolation_missing_identity_and_current_permissions(self):
        self.assertEqual(self.select('same', client='first').primary, 'a')
        self.assertEqual(self.select('same', client='second').primary, 'b')
        fallback = self.select(None).primary
        self.assertEqual(self.select(None).primary, fallback)
        self.assertEqual(self.select('same', client='first', candidates=[{'id': 'b'}]).primary, 'b')
        self.assertEqual([a['id'] for a in self.select('restricted', candidates=[{'id': 'a'}]).candidates], ['a'])

    def test_concurrent_first_requests_share_one_reservation(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            accounts = list(pool.map(lambda _: self.select('concurrent').primary, range(24)))
        self.assertEqual(set(accounts), {'a'})
        self.assertEqual(self.select('next').primary, 'b')

    def test_missing_primary_preserves_binding_until_backup_succeeds(self):
        self.assertEqual(self.select().primary, 'a')
        temporary = self.select(candidates=[{'id': 'b'}])
        self.assertEqual(temporary.primary, 'b')
        self.assertEqual(self.select().primary, 'a')
        self.router.succeeded(temporary, 'b', migrate=False)
        self.assertEqual(self.select().primary, 'b')

    def test_missing_primary_uses_stable_backup_and_cas(self):
        self.assertEqual(self.select().primary, 'a')
        backups = [{'id': 'b'}, {'id': 'c'}]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            selections = list(pool.map(lambda _: self.select(candidates=backups), range(24)))
        self.assertEqual(len({selection.primary for selection in selections}), 1)
        first, late = selections[:2]
        self.router.succeeded(first, first.primary, migrate=False)
        other = 'c' if first.primary == 'b' else 'b'
        self.router.succeeded(late, other, migrate=True)
        self.assertEqual(self.select(candidates=self.candidates + [{'id': 'c'}]).primary, first.primary)

    def test_missing_primary_transient_backup_failure_does_not_migrate(self):
        self.assertEqual(self.select().primary, 'a')
        selection = self.select(candidates=[{'id': 'b'}, {'id': 'c'}])
        other = next(account['id'] for account in selection.candidates if account['id'] != selection.primary)
        self.router.succeeded(selection, other, migrate=False)
        self.assertEqual(self.select().primary, 'a')

    def test_failure_migration_does_not_bounce_or_lose_concurrent_update(self):
        first = self.select()
        self.router.succeeded(first, 'b', migrate=False)
        self.assertEqual(self.select().primary, 'a')
        self.router.succeeded(first, 'b', migrate=True)
        self.assertEqual(self.select().primary, 'b')
        second = self.select()
        self.router.succeeded(second, 'a', migrate=True)
        # Even after returning to A, an old A attempt cannot rebind the current generation.
        self.router.succeeded(first, 'b', migrate=True)
        self.assertEqual(self.select().primary, 'a')

    def test_idle_expiry_lru_bound_and_no_raw_session_or_prompt(self):
        router = SessionRouter(self.store, 'synthetic-hmac', ttl=10, limit=2)
        with patch('session_routing.time.time', return_value=100):
            self.assertEqual(self.select('private-session-one', router=router).primary, 'a')
            self.select('private-session-two', router=router)
            self.select('private-session-three', router=router)
        with self.store.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM session_routes').fetchone()[0], 2)
            rows = repr(con.execute('SELECT * FROM session_routes').fetchall())
            self.assertNotIn('private-session', rows)
        with patch('session_routing.time.time', return_value=111):
            self.select('new', router=router)
        with self.store.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM session_routes').fetchone()[0], 1)


if __name__ == '__main__': unittest.main()
