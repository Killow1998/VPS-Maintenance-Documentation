import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from remote_admin import sqlite_snapshot


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
