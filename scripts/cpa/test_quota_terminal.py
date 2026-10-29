"""Real terminal integration. Synthetic API responses; no external requests."""
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from test_quota_clients import overview, quota


ROOT = Path(__file__).resolve().parent
SECRET = 'synthetic-ui-key'


class Terminal:
    def __init__(self, command, env, rows, columns):
        self.output = ''
        self.status = None
        if os.name == 'nt':
            from winpty import PtyProcess
            self.process = PtyProcess.spawn(command, env=env, dimensions=(rows, columns))
            self.chunks = queue.Queue()

            def read():
                try:
                    while True:
                        value = self.process.read(8192)
                        if not value:
                            break
                        self.chunks.put(value)
                except (EOFError, OSError, ValueError):
                    pass

            self.reader = threading.Thread(target=read, daemon=True)
            self.reader.start()
        else:
            import pty
            self.pid, self.fd = pty.fork()
            if self.pid == 0:
                os.execvpe(command[0], command, env)
            self.resize(rows, columns)

    def resize(self, rows, columns):
        if os.name == 'nt':
            self.process.setwinsize(rows, columns)
        else:
            import fcntl
            import struct
            import termios
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))

    def pump(self):
        if os.name == 'nt':
            try:
                while True:
                    self.output += self.chunks.get_nowait()
            except queue.Empty:
                pass
        else:
            import select
            try:
                while select.select([self.fd], [], [], 0)[0]:
                    data = os.read(self.fd, 65536)
                    if not data:
                        break
                    self.output += data.decode('utf-8', errors='replace')
            except OSError:
                pass

    def send(self, text):
        if os.name == 'nt':
            self.process.write(text)
        else:
            os.write(self.fd, text.encode('utf-8'))

    def alive(self):
        self.pump()
        if os.name == 'nt':
            return self.process.isalive()
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
        return self.status is None

    def close(self):
        if os.name == 'nt':
            self.process.close(force=True)
            self.process.fileobj.close()
            self.process._server.close()
            self.reader.join(timeout=2)
        else:
            if self.alive():
                import signal
                os.kill(self.pid, signal.SIGTERM)
                os.waitpid(self.pid, 0)
            os.close(self.fd)


class TerminalIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name == 'nt':
            try:
                import winpty  # noqa: F401
            except ImportError:
                raise unittest.SkipTest('Optional Windows test driver pywinpty is not installed')
            cls.shells = [value for name in ('powershell', 'pwsh') if (value := shutil.which(name))]
        else:
            cls.shells = ['python']

    def start(self, directory, shell, rows=24, columns=80, preview=None, colors=True):
        home = Path(directory)
        env = {key: value for key, value in os.environ.items() if key.casefold() != 'psmodulepath'}
        env.update(CPA_PROXY='', CPA_BASEURL='', CPA_QUERY_CHOICE='2', TERM='xterm-256color',
                   CPA_TEST_HOME=str(home), CPA_TEST_SOURCE=str(ROOT / 'quota.ps1'),
                   CPA_TEST_EVENTS=str(home / 'events'), CPA_TEST_DATA=str(home / 'data.json'))
        env.pop('NO_COLOR', None)
        if not colors:
            env['NO_COLOR'] = '1'
        (home / 'data.json').write_text(json.dumps(overview()), encoding='utf-8')
        (home / 'quota.json').write_text(json.dumps(quota()), encoding='utf-8')
        if os.name == 'nt':
            code = r'''
$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
$source=[IO.File]::ReadAllText($env:CPA_TEST_SOURCE)
. ([scriptblock]::Create($source.Substring(0,$source.LastIndexOf('Invoke-CpaReport'))))
function Get-CpaConfigPath { return (Join-Path (Join-Path $env:CPA_TEST_HOME '.cpa') 'query.json') }
function Invoke-RestMethod {
    param($Uri,$Headers,$Method,$Proxy,[switch]$UseBasicParsing,$MaximumRedirection,$TimeoutSec,$ErrorAction)
    if($Headers.Authorization -ne 'Bearer synthetic-ui-key' -or [string]$Proxy -ne 'http://127.0.0.1:7897/' -or $MaximumRedirection -ne 0){throw 'unsafe request'}
    [IO.File]::AppendAllText($env:CPA_TEST_EVENTS,"request`n")
    if($Uri -like '*/v1/usage?period=overview'){return ([IO.File]::ReadAllText($env:CPA_TEST_DATA)|ConvertFrom-Json)}
    if($Uri -like '*/v1/quota'){return ([IO.File]::ReadAllText((Join-Path $env:CPA_TEST_HOME 'quota.json'))|ConvertFrom-Json)}
    throw 'unexpected request'
}
$script:originalLines=${function:Get-CpaDashboardLines}
function Get-CpaDashboardLines($Results,$Failures,$Views,$Selected,$Page,$Panel,$Width) {
    [IO.File]::AppendAllText($env:CPA_TEST_EVENTS,"view:${Selected}:${Page}`n")
    & $script:originalLines $Results $Failures $Views $Selected $Page $Panel $Width
}
Invoke-CpaReport
'''
            if preview:
                hook = str(preview).replace("'", "''")
                code = code.rsplit('\nInvoke-CpaReport', 1)[0] + "\n. ([scriptblock]::Create([IO.File]::ReadAllText('" + hook + "')))\nInvoke-CpaReport\n"
            command = [shell, '-NoProfile', '-EncodedCommand', base64.b64encode(code.encode('utf-16-le')).decode()]
        else:
            source = (ROOT / 'quota.sh').read_text(encoding='utf-8').split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
            source = source.rsplit('\ntry:\n    main()', 1)[0]
            code = source + '''
sys.argv = ['quota']
config_path = lambda: Path(os.environ['CPA_TEST_HOME']) / '.cpa' / 'query.json'
def event(value):
    with open(os.environ['CPA_TEST_EVENTS'], 'a', encoding='utf-8') as output:
        output.write(value + '\\n')
def fetch(base, key, path, scope, opener):
    assert key == 'synthetic-ui-key'
    assert base == 'https://cpa.example.invalid'
    event('request')
    path = Path(os.environ['CPA_TEST_DATA']) if scope == 'own_key' else Path(os.environ['CPA_TEST_HOME']) / 'quota.json'
    return json.loads(path.read_text())
original_render = dashboard_lines
def dashboard_lines(results, errors, selected, page, panel, width):
    event(f'view:{selected}:{page}')
    return original_render(results, errors, selected, page, panel, width)
main()
'''
            path = home / 'terminal_test.py'
            path.write_text(code, encoding='utf-8')
            command = [sys.executable, str(path)]
        return Terminal(command, env, rows, columns)

    def wait_for(self, terminal, condition, timeout=20):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            terminal.pump()
            if condition():
                return
            if not terminal.alive():
                if os.name == 'nt':
                    terminal.reader.join(timeout=.5)
                    terminal.pump()
                    if condition():
                        return
                self.fail('Terminal exited early: ' + terminal.output[-2500:])
            time.sleep(.03)
        self.fail('Terminal timed out: ' + terminal.output[-2500:])

    def start_bootstrap(self, directory, shell, loaded):
        home = Path(directory)
        env = {key: value for key, value in os.environ.items() if key.casefold() != 'psmodulepath'}
        env.update(HOME=str(home), TERM='xterm-256color', CPA_TEST_HOME=str(home), CPA_TEST_LOAD=str(int(loaded)),
                   CPA_TEST_SOURCE=str(ROOT.parents[1] / 'cpa.ps1'), CPA_TEST_CLIENT=str(ROOT / 'quota.ps1'))
        if os.name == 'nt':
            code = r'''
$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
if($env:CPA_TEST_LOAD -eq '1') {
    $client=[IO.File]::ReadAllText($env:CPA_TEST_CLIENT)
    . ([scriptblock]::Create($client.Substring(0,$client.LastIndexOf('Invoke-CpaReport'))))
    function Get-CpaConfigPath { return (Join-Path (Join-Path $env:CPA_TEST_HOME '.cpa') 'query.json') }
    Save-CpaConfig 'https://cpa.example.invalid' 'http://127.0.0.1:7897' 'synthetic-ui-key'
}
$launcher=[IO.File]::ReadAllText($env:CPA_TEST_SOURCE)
$payload='param($ProxyUrl,$BaseUrl); if($ProxyUrl.TrimEnd("/") -ne "http://127.0.0.1:7897" -or $BaseUrl){throw "wrong settings"}; Write-Host BOOTSTRAP_STARTED'
$bytes=[Text.Encoding]::UTF8.GetBytes($payload)
$sha=[Security.Cryptography.SHA256]::Create()
try {$digest=([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()} finally {$sha.Dispose()}
$launcher=[regex]::Replace($launcher,"'[0-9a-f]{64}'",("'"+$digest+"'"))
$hooks=@'
function Get-CpaBootstrapConfigPath { return (Join-Path (Join-Path $env:CPA_TEST_HOME '.cpa') 'query.json') }
function Invoke-WebRequest {
    param($Uri,$Proxy,[switch]$UseBasicParsing,$MaximumRedirection,$TimeoutSec)
    if($Proxy.TrimEnd('/') -ne 'http://127.0.0.1:7897' -or $MaximumRedirection -ne 0){throw 'unsafe download'}
    return [pscustomobject]@{StatusCode=200;RawContentStream=[IO.MemoryStream]::new($bytes)}
}
'@
$launcher=$launcher.Insert($launcher.LastIndexOf('Start-CpaQuota'),$hooks+"`n")
& ([scriptblock]::Create($launcher))
'''
            command = [shell, '-NoProfile', '-EncodedCommand', base64.b64encode(code.encode('utf-16-le')).decode()]
        else:
            if loaded:
                folder = home / '.cpa'
                folder.mkdir(mode=0o700)
                path = folder / 'query.json'
                path.write_text(json.dumps({'version': 1, 'base_url': 'https://cpa.example.invalid',
                                           'proxy_url': 'http://127.0.0.1:7897', 'api_key': SECRET}))
                path.chmod(0o600)
            payload = '[[ "${CPA_PROXY%/}" == http://127.0.0.1:7897 && -z "$CPA_BASEURL" ]] || exit 98\nprintf "BOOTSTRAP_STARTED\\n"'
            source = (ROOT.parents[1] / 'cpa.sh').read_text(encoding='utf-8').rsplit('\nmain\n', 1)[0]
            source = re.sub("'[0-9a-f]{64}'", "'" + hashlib.sha256(payload.encode()).hexdigest() + "'", source)
            source += '\ncurl() { printf "%s" "$CPA_TEST_PAYLOAD"; }\nmain\n'
            env['CPA_TEST_PAYLOAD'] = payload
            command = ['bash', '-c', source]
        return Terminal(command, env, 24, 80)

    def test_bootstrap_port_and_saved_menu_use_real_keys(self):
        for shell in self.shells:
            for loaded in (False, True):
                with self.subTest(shell=shell, loaded=loaded), tempfile.TemporaryDirectory() as directory:
                    terminal = self.start_bootstrap(directory, shell, loaded)
                    try:
                        self.wait_for(terminal, lambda: ('Load saved connection' if loaded else 'VPN proxy port') in terminal.output)
                        terminal.send('\r' if loaded else '7897\t\r')
                        self.wait_for(terminal, lambda: 'BOOTSTRAP_STARTED' in terminal.output)
                        self.wait_for(terminal, lambda: not terminal.alive())
                        self.assertNotIn(SECRET, terminal.output)
                    finally:
                        terminal.close()

    def test_form_keyboard_refresh_and_private_profile(self):
        for shell in self.shells:
            for rows, columns in ((24, 80), (32, 100)):
                with self.subTest(shell=shell, size=(rows, columns)), tempfile.TemporaryDirectory() as directory:
                    terminal = self.start(directory, shell, rows, columns)
                    events = Path(directory) / 'events'
                    history = lambda: events.read_text(encoding='utf-8') if events.exists() else ''
                    try:
                        self.wait_for(terminal, lambda: 'API Key' in terminal.output)
                        self.assertIn('连接设置', terminal.output)
                        terminal.send('7897\thttps://cpa.example.invalid/v1/\t' + SECRET + '\t \t\r')
                        self.wait_for(terminal, lambda: history().count('view:') >= 1)
                        self.assertEqual(history().count('request'), 2)
                        for key in ('\t', '\t', 'c', 'c', '\t'):
                            before = history().count('view:')
                            terminal.send(key)
                            self.wait_for(terminal, lambda: history().count('view:') > before)
                        self.assertEqual(history().count('request'), 2)
                        for key in ('2', 'n', 'p', '4'):
                            before = history().count('view:')
                            terminal.send(key)
                            self.wait_for(terminal, lambda: history().count('view:') > before)
                        self.assertEqual(history().count('request'), 2)
                        terminal.resize(28, 90)
                        terminal.send('r')
                        self.wait_for(terminal, lambda: history().count('request') == 4)
                        terminal.send('q')
                        self.wait_for(terminal, lambda: not terminal.alive())
                        self.assertNotIn(SECRET, terminal.output)
                        for codes in ('32|92|38;5;(?:2|10)', '33|93|38;5;(?:3|11|14)', '31|91|38;5;(?:1|9|12)'):
                            self.assertRegex(terminal.output, r'\x1b\[(?:' + codes + r')m')
                        for view in ('view:1h:0', 'view:7d:0', 'view:7d:1', 'view:all:0'):
                            self.assertIn(view, history())
                        saved = json.loads((Path(directory) / '.cpa/query.json').read_text(encoding='utf-8'))
                        self.assertEqual(saved['api_key'], SECRET)
                        self.assertEqual(saved['base_url'], 'https://cpa.example.invalid')
                        self.assertEqual(saved['proxy_url'].rstrip('/'), 'http://127.0.0.1:7897')
                    finally:
                        terminal.close()

    def test_no_color_can_be_overridden_without_requests(self):
        pattern = r'\x1b\[(?:31|91|38;5;(?:1|9|12))m'
        for shell in self.shells:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as directory:
                terminal = self.start(directory, shell, colors=False)
                events = Path(directory) / 'events'
                history = lambda: events.read_text(encoding='utf-8') if events.exists() else ''
                try:
                    self.wait_for(terminal, lambda: 'API Key' in terminal.output)
                    terminal.send('7897\thttps://cpa.example.invalid/v1/\t' + SECRET + '\t\t\r')
                    self.wait_for(terminal, lambda: history().count('view:') >= 1)
                    terminal.pump()
                    self.assertNotRegex(terminal.output, pattern)
                    terminal.send('c')
                    self.wait_for(terminal, lambda: re.search(pattern, terminal.output))
                    self.assertEqual(history().count('request'), 2)
                    terminal.send('q')
                    self.wait_for(terminal, lambda: not terminal.alive())
                    self.assertNotIn(SECRET, terminal.output)
                    self.assertFalse((Path(directory) / '.cpa/query.json').exists())
                finally:
                    terminal.close()

    def test_escape_leaves_without_requests_or_profile(self):
        for shell in self.shells:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as directory:
                terminal = self.start(directory, shell)
                try:
                    self.wait_for(terminal, lambda: 'API Key' in terminal.output)
                    terminal.send('\x1b')
                    self.wait_for(terminal, lambda: not terminal.alive())
                    self.assertFalse((Path(directory) / 'events').exists())
                    self.assertFalse((Path(directory) / '.cpa/query.json').exists())
                finally:
                    terminal.close()


if __name__ == '__main__':
    unittest.main()
