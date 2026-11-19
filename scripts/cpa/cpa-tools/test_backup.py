import os
import datetime as dt
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from remote_admin import Admin, sqlite_snapshot


class QuotaSnapshotTests(unittest.TestCase):
    def test_nonempty_snapshot_includes_active_source_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite'
            now = dt.datetime.now(dt.timezone.utc)
            deadline = (now + dt.timedelta(hours=1)).isoformat()
            snapshot = {'accounts': [{'id': 'account-one', 'enabled': True, 'status': 'ok',
                'stale': False, 'as_of': now.isoformat(), 'windows': {'five_hour': {
                    'remaining_percent': 80, 'resets_at': None}}}]}
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT)')
                con.executemany('INSERT INTO metadata VALUES (?,?)', [
                    ('quota_snapshot', json.dumps(snapshot)), ('quota_status', 'ok'),
                    ('quota_account_source:account-one', 'source-one'), ('quota_backoff:source-one', deadline)])
            con.close()
            observer = Path(__file__).resolve().parent.parent / 'cpa-observer' / 'observer.py'
            admin = Admin.__new__(Admin)
            admin.c = {'database': str(db), 'hmac_secret': 'fixture'}
            admin.j = {'observer_script': str(observer)}
            with patch.object(admin, 'files', return_value=[]), patch.object(sys, 'path', [str(observer.parent), *sys.path]):
                result = admin.pool_snapshot()
            self.assertEqual(result['quota']['accounts'][0]['retry_at'], deadline)
            self.assertTrue(result['quota']['accounts'][0]['stale'])

    def test_verified_snapshot_is_visible_with_current_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite'
            secret = 'fixture'
            binding = {'credential': 'helper', 'expected_email': 'a@example.invalid'}
            def digest(value):
                return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
            value = json.dumps(binding, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
            account = {'id': 'account-one', 'enabled': True, 'status': 'ok', 'stale': False,
                'as_of': dt.datetime.now(dt.timezone.utc).isoformat(), 'source_identity': 'source-one',
                'source_verified': True, 'binding_name': digest('quota-binding-name:main'),
                'binding_revision': digest('quota-binding-value:' + value),
                'windows': {'five_hour': {'remaining_percent': 80, 'resets_at': None}}}
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT)')
                con.executemany('INSERT INTO metadata VALUES (?,?)', [
                    ('quota_snapshot', json.dumps({'accounts': [account]})), ('quota_status', 'ok')])
            con.close()
            observer = Path(__file__).resolve().parent.parent / 'cpa-observer' / 'observer.py'
            admin = Admin.__new__(Admin)
            admin.c = {'database': str(db), 'hmac_secret': secret, 'quota_bindings': {'main': binding}}
            admin.j = {'observer_script': str(observer)}
            with patch.object(admin, 'files', return_value=[]), patch.object(sys, 'path', [str(observer.parent), *sys.path]):
                result = admin.pool_snapshot()['quota']['accounts'][0]
            self.assertEqual(result['windows']['five_hour']['remaining_percent'], 80)
            self.assertNotIn('binding_name', result)
            self.assertNotIn('binding_revision', result)


@unittest.skipUnless(os.name == 'posix', 'Administrator runtime is Linux/WSL/macOS')
class SnapshotTests(unittest.TestCase):
    def test_committed_wal_is_in_snapshot_and_source_keeps_accepting_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            source, backup = Path(directory)/'live.sqlite', Path(directory)/'backup.sqlite'
            con = sqlite3.connect(source)
            try:
                con.execute('PRAGMA journal_mode=WAL')
                con.execute('CREATE TABLE records (id PRIMARY KEY, value TEXT)')
                con.execute("INSERT INTO records VALUES (1, 'retained')");con.commit()
                self.assertTrue(Path(str(source)+'-wal').exists())
                sqlite_snapshot(source, backup)
                with sqlite3.connect(backup) as snapshot:
                    self.assertEqual(snapshot.execute('SELECT * FROM records').fetchall(), [(1, 'retained')])
                    self.assertEqual(snapshot.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                con.execute("INSERT INTO records VALUES (2, 'new')");con.commit()
                self.assertEqual(con.execute('SELECT count(*) FROM records').fetchone()[0], 2)
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                self.assertEqual(list(Path(directory).glob('.snapshot-*')), [])
            finally:con.close()

if __name__ == '__main__': unittest.main()
