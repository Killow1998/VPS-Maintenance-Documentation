"""Offline business-contract and safety tests. Never connects to a VPS."""

import ast
import contextlib
import copy
import http.server
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import shlex
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import vpsctl as ctl


def account(**changes):
    return {"id": 8, "email": "friend@example.com", "plan_id": 2, "group_id": 1,
            "transfer_enable": 100 * 2**30, "u": 100, "d": 200, "reset_count": 1,
            "banned": False, "is_admin": False, "is_staff": False, "next_reset_at": 2000000000,
            "token": "PRIVATE_TOKEN", "uuid": "PRIVATE_UUID", "password": "PRIVATE_PASSWORD",
            "last_login_ip": "PRIVATE_SOURCE", "subscribe_url": "https://example.com/?token=PRIVATE_TOKEN",
            **changes}


class FakeAPI:
    def __init__(self, rows=None):
        self.auth = "already logged in"
        self.rows = rows if rows is not None else [account()]
        self.calls = []
        self.users_updated_at = time.time()

    def ensure_auth(self):
        pass

    def users(self, force=False):
        return copy.deepcopy(self.rows)

    def admin(self, path, payload=None):
        self.calls.append((path, copy.deepcopy(payload)))
        if path == "/plan/fetch":
            return {"data": [{"id": 2, "name": "Monthly", "reset_traffic_method": 0}]}
        if path == "/user/update":
            row = next(r for r in self.rows if r["id"] == payload["id"])
            row.update({k: v for k, v in payload.items() if k != "invite_user_email"})
        if path == "/traffic-reset/reset-user":
            row = next(r for r in self.rows if r["id"] == payload["user_id"])
            row.update(u=0, d=0, reset_count=row["reset_count"] + 1)
        if path == "/user/destroy":
            self.rows = [r for r in self.rows if r["id"] != payload["id"]]
        if path == "/user/generate":
            self.rows.append(account(id=9, email=payload["email_prefix"] + "@" + payload["email_suffix"]))
        return {"data": True}


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.args = ctl.parser().parse_args(["--host", "example", "--dry-run"])
        self.ssh = Mock()
        self.console = ctl.Console(self.args, self.ssh, "http://127.0.0.1:1",
                                   {"admin_path": "test", "db": "sqlite", "reset_method": 0})
        self.console.api = FakeAPI()
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()

    def tearDown(self):
        self.redirect.__exit__(None, None, None)

    def test_explicit_units_and_exact_decimal_conversion(self):
        self.assertEqual(ctl.quota_bytes("50GB"), 50_000_000_000)
        self.assertEqual(ctl.quota_bytes("50GiB"), 50 * 2**30)
        self.assertEqual(ctl.quota_bytes("0.001GB"), 1_000_000)
        for text in ("50g", "0GB", "-1GB", "nanGB", "999999999999TB"):
            with self.assertRaises(ctl.Error):
                ctl.quota_bytes(text)

    def test_safe_projection_does_not_expose_secrets(self):
        self.console.action("users", {})
        for secret in ("PRIVATE_TOKEN", "PRIVATE_UUID", "PRIVATE_PASSWORD", "PRIVATE_SOURCE"):
            self.assertNotIn(secret, self.output.getvalue())
        self.assertNotIn("subscribe_url", json.dumps(ctl.safe_user(account())))

    def test_ambiguous_short_name_rejected(self):
        with self.assertRaises(ctl.Error):
            ctl.select_one([account(), account(id=9, email="friend@other.example")], "friend")

    def test_dry_run_write_actions_never_backup_or_post(self):
        for cmd, data in [("quota", {"value": "50GiB"}), ("rename", {"value": "new@example.com"}),
                          ("reset", {}), ("disable", {}), ("enable", {}), ("delete", {})]:
            self.console.action(cmd, {"user": "8", **data})
        self.assertEqual(self.console.api.calls, [])
        self.ssh.backup.assert_not_called()

    def test_admin_and_staff_protected_before_confirmation(self):
        for field in ("is_admin", "is_staff"):
            self.console.api = FakeAPI([account(**{field: True})])
            for cmd in ("reset", "delete", "disable", "quota"):
                with self.assertRaises(ctl.Error):
                    self.console.action(cmd, {"user": "8", "value": "50GiB"})
        self.ssh.backup.assert_not_called()

    def test_write_confirmation_rejects_pipe_and_mistyped_phrase(self):
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(ctl.Error):
                ctl.confirm("RESET 8")
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="yes"):
            self.assertFalse(ctl.confirm("RESET 8"))

    def test_quota_preserves_inviter_and_existing_usage(self):
        self.console.api = FakeAPI([account(invite_user_id=20, invite_user={"email": "inviter@example.com"})])
        with patch.object(self.console, "approve", return_value=True):
            self.console.action("quota", {"user": "8", "value": "50GiB"})
        payload = self.console.api.calls[0][1]
        self.assertEqual(payload, {"id": 8, "transfer_enable": 50 * 2**30,
                                   "invite_user_email": "inviter@example.com"})
        self.assertEqual(self.console.api.rows[0]["u"], 100)

    def test_missing_inviter_refuses_update(self):
        with self.assertRaises(ctl.Error):
            self.console.update(account(invite_user_id=20), {"banned": True})
        self.assertEqual(self.console.api.calls, [])

    def test_backup_failure_prevents_delete(self):
        self.args.dry_run = False
        self.ssh.backup.side_effect = ctl.Error("Backup failed")
        with patch("vpsctl.confirm", return_value=True), self.assertRaises(ctl.Error):
            self.console.action("delete", {"user": "8"})
        self.assertEqual(self.console.api.calls, [])

    def test_delete_only_selected_account(self):
        self.console.api = FakeAPI([account(), account(id=9)])
        with patch.object(self.console, "approve", return_value=True):
            self.console.action("delete", {"user": "8"})
        self.assertEqual([r["id"] for r in self.console.api.rows], [9])

    def test_pool_quota_uses_guarded_narrow_application_update(self):
        row = {"id": 2, "name": "Demo", "code": "demo", "transfer_enable": 1000*2**30}
        self.console.api.admin = Mock(return_value={"data": [row]})
        self.ssh.php.return_value = {"id": 2, "transfer_enable": 900*10**9}
        with patch.object(self.console, "approve", return_value=True):
            self.console.action("pool-set", {"node": "2", "value": "900GB"})
        source = self.ssh.php.call_args[0][0]
        self.assertIn('lockForUpdate()', source)
        self.assertIn('$s->transfer_enable=', source)
        self.assertIn('Unrelated field changed', source)
        self.assertNotIn('$s->u=', source)
        self.assertNotIn('$s->d=', source)

    def test_reset_uses_native_single_user_endpoint(self):
        other = account(id=9)
        self.console.api = FakeAPI([account(), copy.deepcopy(other)])
        with patch.object(self.console, "approve", return_value=True):
            self.console.action("reset", {"user": "8"})
        self.assertEqual(self.console.api.calls[0][0], "/traffic-reset/reset-user")
        self.assertEqual(self.console.api.calls[0][1]["user_id"], 8)
        self.assertEqual(self.console.api.rows[1], other)
        self.ssh.php.assert_not_called()

    def test_create_checks_plan_and_dry_run_is_read_only(self):
        self.console.action("create", {"email": "new@example.com", "plan": "2", "quota": "50GiB", "days": 365})
        self.assertEqual(self.console.api.calls, [("/plan/fetch", None)])
        self.ssh.backup.assert_not_called()

    def test_single_plan_is_automatically_selected(self):
        with patch.object(self.console, "approve", return_value=True):
            self.console.action("create", {"email":"new@example.com","plan":None,"quota":"50GiB","days":365})
        generated = [p for path,p in self.console.api.calls if path=='/user/generate']
        self.assertEqual(generated[0]['plan_id'],2)

    def test_restart_revokes_own_access_before_command(self):
        with patch('vpsctl.confirm',return_value=True):
            self.console.restart('panel')
        self.assertEqual([c[0] for c in self.ssh.mock_calls],['revoke_access','run'])
        self.assertIsNone(self.console.api.auth)

    def test_create_uses_new_account_only(self):
        before = copy.deepcopy(self.console.api.rows[0])
        with patch.object(self.console, "approve", return_value=True):
            self.console.action("create", {"email": "new@example.com", "plan": "2", "quota": "50GiB", "days": 365})
        self.assertEqual(self.console.api.rows[0], before)
        self.assertEqual(self.console.api.rows[1]["transfer_enable"], 50 * 2**30)

    def test_create_failure_is_reported_as_partial_without_retry(self):
        original = self.console.api.admin
        def call(path, payload=None):
            if path == "/user/update":
                raise ctl.Error("simulated timeout")
            return original(path, payload)
        self.console.api.admin = call
        with patch.object(self.console, "approve", return_value=True), self.assertRaisesRegex(ctl.Error, "可能已存在"):
            self.console.action("create", {"email": "new@example.com", "plan": "2", "quota": "50GiB", "days": 365})
        self.assertEqual(len(self.console.api.rows), 2)

    def test_restart_dry_run_does_not_login_or_run_commands(self):
        self.console.api.auth = None
        for target in ("panel", "node", "vps"):
            self.console.action("restart", {"target": target})
        self.ssh.run.assert_not_called()
        self.assertEqual(self.console.api.calls, [])

    def test_main_status_does_not_require_admin_metadata(self):
        self.ssh.status.return_value = {"panel": "running"}
        with patch("vpsctl.SSH", return_value=self.ssh):
            ctl.main(["--host", "example", "status"])
        self.ssh.metadata.assert_not_called()
        self.ssh.status.assert_called_once()

    def test_ssh_bootstrap_reuses_short_session_and_revokes_only_its_name(self):
        with patch('vpsctl.ssh_binary', return_value='ssh'):
            ssh = ctl.SSH(self.args)
        ssh.bridge = Mock()
        access = {'authorization':'Bearer TEST_SECRET','session_id':99,'expires_at':time.time()+900}
        ssh.php = Mock(side_effect=[access, {'removed':1}])
        self.assertEqual(ssh.access(), access)
        self.assertEqual(ssh.access(), access)
        self.assertEqual(ssh.php.call_count,1)
        self.assertIn('addMinutes(15)',ssh.php.call_args[0][0])
        ssh.revoke_access()
        source = ssh.php.call_args[0][0]
        self.assertIn('whereKey(99)',source)
        self.assertIn('where("name"',source)
        self.assertNotIn('removeAllSessions',source)
        self.assertIsNone(ssh._access)

    def test_reject_ssh_command_injection(self):
        self.args.host = "-oProxyCommand=bad"
        with self.assertRaises(ctl.Error):
            ctl.SSH(self.args)

    def test_backup_online_sqlite_and_permissions(self):
        self.args.no_backup = False
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            root = Path(tmp)
            database = root / "source.sqlite"
            with contextlib.closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TABLE test(n INTEGER)")
                db.execute("INSERT INTO test VALUES(42)")
            self.args.database = str(database)
            with patch("vpsctl.ssh_binary", return_value="ssh"):
                ssh = ctl.SSH(self.args)
            def execute(source):
                ast.parse(source)
                output = io.StringIO()
                with patch("pathlib.Path.home", return_value=root), contextlib.redirect_stdout(output):
                    exec(source, {})
                return json.loads(output.getvalue())
            with patch.object(ssh, "python", side_effect=execute):
                ssh.backup()
            copies = list((root / "xboard-cli-backups").glob("*.sqlite"))
            self.assertEqual(len(copies), 1)
            with contextlib.closing(sqlite3.connect(copies[0])) as db:
                self.assertEqual(db.execute("SELECT n FROM test").fetchone()[0], 42)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_request(None)

            def do_POST(self):
                self.handle_request(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

            def handle_request(self, payload):
                cls.requests.append((self.path, payload))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/should-not-follow")
                    self.end_headers()
                    return
                data = True
                result = {"status": "success", "data": data}
                if "user/fetch" in self.path:
                    page = 2 if "current=2" in self.path else 1
                    result = {"data": [account(id=page)], "last_page": 2}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_pagination_cached_until_refresh_and_no_password_login(self):
        self.requests.clear()
        with patch.dict("os.environ", {"http_proxy": "http://127.0.0.1:1"}):
            provider = Mock(return_value={'authorization':'Bearer TEST_SECRET','expires_at':time.time()+900})
            api = ctl.API(self.base, "private-admin", provider)
            self.assertEqual([r["id"] for r in api.users()], [1, 2])
            self.assertEqual([r["id"] for r in api.users()], [1,2])
            self.assertEqual(len(self.requests),2)
            api.users(force=True)
            self.assertEqual(len(self.requests),4)
            provider.assert_called_once_with()
        self.assertTrue(all(payload is None for _,payload in self.requests))

    def test_no_api_access_without_ssh_authorization(self):
        self.requests.clear()
        with self.assertRaises(ctl.Error):
            ctl.API(self.base, "admin").users()
        self.assertEqual(self.requests, [])

    def test_writes_invalidate_cached_users(self):
        self.requests.clear()
        api = ctl.API(self.base,'admin',lambda:{'authorization':'Bearer TEST_SECRET','expires_at':time.time()+900})
        api.users()
        api.admin('/user/update',{'id':1,'banned':True})
        api.users()
        self.assertEqual(len(self.requests),5)

    def test_redirect_not_followed(self):
        self.requests.clear()
        with self.assertRaises(ctl.Error):
            ctl.API(self.base, "admin").call("/redirect", {"password": "test"})
        self.assertEqual(len(self.requests), 1)


class BridgeTests(unittest.TestCase):
    def test_one_process_runs_multiple_requests_and_does_not_retry_failures(self):
        process = subprocess.Popen([sys.executable,'-u','-c',ctl.BRIDGE],stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,text=True,encoding='utf-8')
        bridge = ctl.Bridge(process)
        try:
            self.assertTrue(bridge.receive(5)['ready'])
            command = shlex.join([sys.executable,'-c','import sys;print(sys.stdin.read())'])
            self.assertEqual(bridge.run(command,'one').strip(),'one')
            self.assertEqual(bridge.run(command,'two').strip(),'two')
            with self.assertRaises(ctl.Error):
                bridge.run(shlex.join([sys.executable,'-c','raise SystemExit(7)']))
            self.assertEqual(bridge.sequence,3)
            self.assertEqual(bridge.run(command,'healthy').strip(),'healthy')
            with self.assertRaises(ctl.Error):
                bridge.run(command,'should not run',cleanup={'container':'demo','name':'OTHER_SESSION'})
            self.assertEqual(bridge.sequence,5)
        finally:
            process.stdin.close()
            process.wait(timeout=5)
            bridge.reader.join(timeout=1)
            process.stdout.close()


if __name__ == "__main__":
    unittest.main()
