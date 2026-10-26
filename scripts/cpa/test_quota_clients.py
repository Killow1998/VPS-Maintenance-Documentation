"""Offline client checks. Only synthetic keys and loopback sockets are used."""
import ast
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / "quota.sh").read_text(encoding="utf-8").split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
CLIENT = {}
exec(compile(SOURCE.rsplit("\ntry:\n    main()", 1)[0], str(ROOT / "quota.sh"), "exec"), CLIENT)


def overview():
    return {"scope": "own_key", "period": "overview", "as_of": "2026-10-25T12:00:00Z",
            "collector_status": "ok", "rejected_events": 0,
            "windows": [{"period": period, "totals": {"requests": index, "failed_requests": 0,
                         "input_tokens": index*100, "output_tokens": index*30, "total_tokens": index*130}}
                        for index, period in enumerate(("1h", "7d", "30d", "all"), 1)]}


class RejectTunnel(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 8192:
            part = self.request.recv(1024)
            if not part:
                break
            data += part
        self.server.requests.append(data)
        self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")


@contextlib.contextmanager
def proxy_server():
    with socketserver.TCPServer(("127.0.0.1", 0), RejectTunnel) as server:
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, "http://127.0.0.1:" + str(server.server_address[1])
        finally:
            server.shutdown()
            thread.join()


class PythonClientTests(unittest.TestCase):
    def test_required_proxy_validation(self):
        for bad in ("", "socks5://127.0.0.1:7890", "http://proxy.invalid:7890", "http://127.0.0.1",
                    "http://127.0.0.1:0", "http://u:p@127.0.0.1:7890", "http://127.0.0.1:7890/path"):
            with self.subTest(proxy=bad), self.assertRaises(ValueError):
                CLIENT["RequiredProxy"](bad)
        CLIENT["RequiredProxy"]("http://[::1]:7890")

    def test_no_proxy_cannot_bypass_and_connect_has_no_api_key(self):
        with proxy_server() as (server, proxy):
            opener = CLIENT["build_opener"](CLIENT["RequiredProxy"](proxy), CLIENT["NoRedirect"]())
            real_lookup = socket.getaddrinfo
            lookups = []

            def local_lookup(host, *args, **kwargs):
                lookups.append(host)
                self.assertEqual(host, "127.0.0.1")
                return real_lookup(host, *args, **kwargs)

            with patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}), patch("socket.getaddrinfo", local_lookup):
                with self.assertRaises(RuntimeError):
                    CLIENT["fetch"]("https://cpa.example.invalid", "synthetic-client-key", "/v1/quota", "shared_pool", opener)
            self.assertEqual(lookups, ["127.0.0.1"])
            self.assertEqual(len(server.requests), 1)
            self.assertTrue(server.requests[0].startswith(b"CONNECT cpa.example.invalid:443 HTTP/"))
            self.assertNotIn(b"synthetic-client-key", server.requests[0])

    def test_closed_proxy_never_tries_target(self):
        opener = CLIENT["build_opener"](CLIENT["RequiredProxy"]("http://127.0.0.1:7890"), CLIENT["NoRedirect"]())
        with patch("socket.create_connection", side_effect=ConnectionRefusedError) as connect:
            with self.assertRaises(RuntimeError):
                CLIENT["fetch"]("https://cpa.example.invalid", "test-key", "/v1/quota", "shared_pool", opener)
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(connect.call_args.args[0], ("127.0.0.1", 7890))

    def test_missing_proxy_stops_before_key_or_network(self):
        with patch.dict(os.environ, {"CPA_PROXY": ""}), patch("sys.argv", ["quota", "https://cpa.example.invalid"]), \
                patch.dict(CLIENT, {"read_key": lambda _: self.fail("must not read a key")}), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                CLIENT["main"]()

    def test_port_input_is_explicit_and_bounded(self):
        for bad in ("", "0", "65536", "-1", "1.5", "7890/path", "127.0.0.1:7890", "７８９０"):
            with self.subTest(port=bad), self.assertRaises(RuntimeError):
                CLIENT["proxy_from_port"](bad)
        self.assertEqual(CLIENT["proxy_from_port"]("7897"), "http://127.0.0.1:7897")
        self.assertEqual(CLIENT["proxy_from_port"]("65535"), "http://127.0.0.1:65535")

    def test_no_args_interactive_setup_refresh_and_quit(self):
        streams = iter((io.StringIO("0\n7897\n"),
                        io.StringIO("http://cpa.example.invalid\nhttps://cpa.example.invalid/v1/\n"),
                        io.StringIO("r\nq\n")))
        calls, events = [], []
        output = io.StringIO()
        output.isatty = lambda: True

        def terminal(path, mode, encoding):
            self.assertEqual((path, mode, encoding), ("/dev/tty", "r", "utf-8"))
            events.append("terminal")
            return next(streams)

        def hidden_key(prompt):
            events.append("key")
            return "synthetic-key"

        def fetch(base, key, path, scope, opener):
            events.append("request")
            self.assertEqual(base, "https://cpa.example.invalid")
            self.assertEqual(key, "synthetic-key")
            proxies = [handler for handler in opener.handlers if isinstance(handler, CLIENT["RequiredProxy"])]
            self.assertEqual(proxies[0].authority, "127.0.0.1:7897")
            calls.append(path)
            return overview() if scope == "own_key" else {"scope": "shared_pool", "accounts": []}

        with patch.dict(os.environ, {"CPA_BASEURL": "", "CPA_PROXY": ""}), patch("sys.argv", ["quota"]), \
                patch("getpass.getpass", hidden_key), patch.dict(CLIENT, {"open": terminal, "fetch": fetch}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            CLIENT["main"]()
        self.assertEqual(events, ["terminal", "terminal", "terminal", "key"] + ["request"] * 4)
        self.assertEqual(calls, ["/v1/usage?period=overview", "/v1/quota"] * 2)
        self.assertNotIn("synthetic-key", output.getvalue())

    def test_once_can_prompt_without_opening_refresh_terminal(self):
        calls = []
        output = io.StringIO()
        output.isatty = lambda: True

        def terminal(path, mode, encoding):
            calls.append(path)
            return io.StringIO("https://cpa.example.invalid\n")

        with patch.dict(os.environ, {"CPA_BASEURL": "", "CPA_PROXY": "http://127.0.0.1:7890"}), \
                patch("sys.argv", ["quota", "--once"]), contextlib.redirect_stdout(output), \
                patch.dict(CLIENT, {"open": terminal, "read_key": lambda _: "synthetic-key",
                                   "fetch": lambda base, key, path, scope, opener:
                                   overview() if scope == "own_key" else {"scope": scope, "accounts": []}}):
            CLIENT["main"]()
        self.assertEqual(calls, ["/dev/tty"])

    def test_closed_input_or_missing_terminal_stops_before_key(self):
        output = io.StringIO()
        output.isatty = lambda: True
        for fault, expected in ((lambda *a, **kw: io.StringIO(""), EOFError),
                                (unittest.mock.Mock(side_effect=OSError), RuntimeError)):
            with self.subTest(error=expected), patch.dict(os.environ, {"CPA_BASEURL": "", "CPA_PROXY": ""}), \
                    patch("sys.argv", ["quota"]), contextlib.redirect_stdout(output), \
                    patch.dict(CLIENT, {"open": fault, "read_key": lambda _: self.fail("must not read a key")}), \
                    self.assertRaises(expected):
                CLIENT["main"]()

    def test_two_requests_render_all_windows_and_pool(self):
        calls = []

        def fetch(base, key, path, scope, opener):
            calls.append(path)
            return overview() if scope == "own_key" else {"scope": "shared_pool", "accounts": []}

        output = io.StringIO()
        with patch("sys.argv", ["quota", "https://cpa.example.invalid", "--proxy", "http://127.0.0.1:7890"]), \
                patch.dict(CLIENT, {"read_key": lambda _: "synthetic-key", "fetch": fetch}), contextlib.redirect_stdout(output):
            CLIENT["main"]()
        self.assertEqual(calls, ["/v1/usage?period=overview", "/v1/quota"])
        self.assertNotIn("synthetic-key", output.getvalue())
        for label in ("近 1 小时", "近 7 天", "近 30 天", "总历史", "匿名号池"):
            self.assertIn(label, output.getvalue())

    def test_old_server_never_masquerades_as_overview(self):
        with self.assertRaises(RuntimeError):
            CLIENT["render_overview"]({"scope": "own_key", "period": "day", "totals": {}})

    def test_redirect_handler_never_follows(self):
        self.assertIsNone(CLIENT["NoRedirect"]().redirect_request(None, None, 302, "", {}, "https://other.invalid"))


class RuntimeVersionTests(unittest.TestCase):
    def test_python_310_syntax_for_operation_tools(self):
        for path in [ROOT.parent / "vpsctl.py", *ROOT.rglob("*.py")]:
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))
        ast.parse(SOURCE, feature_version=(3, 10))

    def test_entry_points_check_version_before_other_imports_or_work(self):
        entries = [ROOT.parent / "vpsctl.py", ROOT / "cpa_verify.py",
                   ROOT / "cpa-tools/api_keys.py", ROOT / "cpa-tools/claude_auth.py",
                   ROOT / "cpa-tools/remote_admin.py", ROOT / "cpa-observer/observer.py",
                   ROOT / "cpa-observer/query.py", ROOT / "cpa-auth/managed_setup.py",
                   ROOT / "cpa-auth/managed_quota.py"]
        sources = [(str(path), path.read_text(encoding="utf-8")) for path in entries] + [("quota.sh", SOURCE)]
        for name, source in sources:
            with self.subTest(entry=name):
                tree = ast.parse(source)
                body = tree.body[1:] if isinstance(tree.body[0], ast.Expr) else tree.body
                self.assertIsInstance(body[0], ast.Import)
                self.assertEqual([alias.name for alias in body[0].names], ["sys"])
                self.assertIsInstance(body[1], ast.If)
                guard = compile(ast.Module(body=body[:2], type_ignores=[]), name, "exec")
                with patch.object(sys, "version_info", (3, 9, 25)), self.assertRaisesRegex(SystemExit, "Python 3.10"):
                    exec(guard, {})
                with patch.object(sys, "version_info", (3, 10, 0)):
                    exec(guard, {})


class PowerShellClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shells = [path for name in ("powershell", "pwsh") if (path := shutil.which(name))]
        if not cls.shells:
            raise unittest.SkipTest("PowerShell is not installed")

    def run_ps(self, shell, code):
        env = {**os.environ, "CPA_TEST_SOURCE": str(ROOT / "quota.ps1"), "CPA_TEST_KEY": "synthetic-client-key",
               "CPA_BASEURL": "", "CPA_PROXY": "", "NO_PROXY": "*", "no_proxy": "*"}
        prefix = "$ErrorActionPreference='Stop'; $scriptSource=[IO.File]::ReadAllText($env:CPA_TEST_SOURCE).TrimStart([char]0xFEFF); "
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                                 base64.b64encode((prefix + code).encode("utf-16-le")).decode()],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertNotIn(b"synthetic-client-key", result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors="replace"))

    def test_mock_two_views_and_explicit_proxy_parameter(self):
        data = json.dumps(overview())
        code = r"""
$script:calls=0
function Invoke-RestMethod {
    param($Uri,$Headers,$Method,$Proxy,[switch]$UseBasicParsing,$MaximumRedirection,$TimeoutSec,$ErrorAction)
    if ([string]$Proxy -ne 'http://127.0.0.1:7890/' -or $MaximumRedirection -ne 0) { throw 'unsafe transport' }
    if ($Headers.Authorization -ne ('Bearer '+$env:CPA_TEST_KEY)) { throw 'missing auth' }
    $script:calls++
    if ($Uri -like '*/v1/usage?period=overview') { return ('DATA' | ConvertFrom-Json) }
    if ($Uri -like '*/v1/quota') { return ('{"scope":"shared_pool","accounts":[]}' | ConvertFrom-Json) }
    throw 'unexpected query'
}
& ([scriptblock]::Create($scriptSource)) -BaseUrl 'https://cpa.example.invalid' -ProxyUrl 'http://127.0.0.1:7890' -KeyEnv CPA_TEST_KEY
if ($script:calls -ne 2) { throw 'wrong request count' }
""".replace("DATA", data)
        for shell in self.shells:
            with self.subTest(shell=shell):
                self.run_ps(shell, code)

    def test_missing_proxy_rejected_before_network(self):
        code = r"""
$script:calls=0
function Invoke-RestMethod { $script:calls++; throw 'network must not run' }
$rejected=$false
try { & ([scriptblock]::Create($scriptSource)) -BaseUrl 'https://cpa.example.invalid' -KeyEnv CPA_TEST_KEY }
catch { $rejected=$true }
if (-not $rejected -or $script:calls) { throw 'missing proxy accepted' }
"""
        for shell in self.shells:
            with self.subTest(shell=shell):
                self.run_ps(shell, code)

    def test_prompted_setup_default_tui_and_once(self):
        code = r"""
$script:reads=0; $script:calls=0; $script:clears=0
function Read-Host {
    param($Prompt,[switch]$AsSecureString)
    $script:reads++
    if ($script:reads -eq 5) {
        if (-not $AsSecureString) { throw 'key must be hidden' }
        $secret = [Security.SecureString]::new()
        foreach ($char in $env:CPA_TEST_KEY.ToCharArray()) { $secret.AppendChar($char) }
        return $secret
    }
    if ($AsSecureString) { throw 'unexpected key prompt' }
    switch ($script:reads) {
        1 { return '0' }
        2 { return '7897' }
        3 { return 'http://cpa.example.invalid' }
        4 { return 'https://cpa.example.invalid/v1/' }
        6 { return 'r' }
        7 { return 'q' }
        default { throw 'unexpected input' }
    }
}
function Clear-Host { $script:clears++ }
function Invoke-RestMethod {
    param($Uri,$Headers,$Method,$Proxy,[switch]$UseBasicParsing,$MaximumRedirection,$TimeoutSec,$ErrorAction)
    if ([string]$Proxy -ne 'http://127.0.0.1:7897/' -or $MaximumRedirection -ne 0) { throw 'unsafe transport' }
    if ($Headers.Authorization -ne ('Bearer '+$env:CPA_TEST_KEY)) { throw 'missing auth' }
    if ($script:reads -lt 5) { throw 'network before setup' }
    $script:calls++
    if ($Uri -eq 'https://cpa.example.invalid/v1/usage?period=overview') { return ('DATA' | ConvertFrom-Json) }
    if ($Uri -eq 'https://cpa.example.invalid/v1/quota') { return ('{"scope":"shared_pool","accounts":[]}' | ConvertFrom-Json) }
    throw 'unexpected query'
}
# Simulate a terminal for the input-flow test; transport tests use real loopback sockets.
$scriptSource = $scriptSource.Insert($scriptSource.LastIndexOf('Invoke-CpaReport'), "function Test-CpaTerminal { return `$true }`n")
& ([scriptblock]::Create($scriptSource)) ARGUMENTS
if ($script:calls -ne CALLS -or $script:reads -ne READS -or $script:clears -ne CLEARS) { throw 'wrong interaction count' }
""".replace("DATA", json.dumps(overview()))
        for shell in self.shells:
            for args, calls, reads, clears in (("", 4, 7, 2), ("-Once", 2, 5, 0)):
                with self.subTest(shell=shell, args=args):
                    self.run_ps(shell, code.replace("ARGUMENTS", args).replace("CALLS", str(calls))
                                .replace("READS", str(reads)).replace("CLEARS", str(clears)))

    def test_no_terminal_stops_before_hidden_key(self):
        code = r"""
function Read-Host { throw 'key prompt must not run without a terminal' }
function Invoke-RestMethod { throw 'network must not run without a key' }
$rejected=$false
try { & ([scriptblock]::Create($scriptSource)) -BaseUrl 'https://cpa.example.invalid' -ProxyUrl 'http://127.0.0.1:7890' }
catch { $rejected = $_.Exception.Message -match 'KeyEnv' }
if (-not $rejected) { throw 'no-terminal preflight failed' }
"""
        for shell in self.shells:
            with self.subTest(shell=shell):
                self.run_ps(shell, code)

    def test_real_connect_uses_proxy_despite_no_proxy(self):
        for shell in self.shells:
            with self.subTest(shell=shell), proxy_server() as (server, proxy):
                code = r"""
$rejected=$false
try { & ([scriptblock]::Create($scriptSource)) -BaseUrl 'https://cpa.example.invalid' -ProxyUrl 'PROXY' -KeyEnv CPA_TEST_KEY -View quota }
catch { $rejected=$true }
if (-not $rejected) { throw 'failed proxy accepted' }
""".replace("PROXY", proxy)
                self.run_ps(shell, code)
                self.assertGreaterEqual(len(server.requests), 1)
                for request in server.requests:
                    self.assertTrue(request.startswith(b"CONNECT cpa.example.invalid:443 HTTP/"))
                    self.assertNotIn(b"synthetic-client-key", request)


if __name__ == "__main__":
    unittest.main()
