"""Bootstrap contract tests: synthetic downloads only, no real credentials."""
import base64
import hashlib
import http.server
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import unittest


ROOT = Path(__file__).resolve().parents[2]
BASH_SOURCE = (ROOT / "cpa.sh").read_text(encoding="utf-8")
PS_SOURCE = (ROOT / "cpa.ps1").read_text(encoding="utf-8")


class PinTests(unittest.TestCase):
    def test_windows_bootstrap_is_ascii_without_bom(self):
        # The HTTP -> iex path does not strip a BOM like ReadAllText does.
        (ROOT / 'cpa.ps1').read_bytes().decode('ascii')

    def test_clients_match_pinned_hashes_and_revision(self):
        for launcher, name in ((BASH_SOURCE, "quota.sh"), (PS_SOURCE, "quota.ps1")):
            with self.subTest(name=name):
                payload = (ROOT / "scripts/cpa" / name).read_bytes()
                if name.endswith(".sh"):
                    payload = payload.rstrip(b"\n")
                digest = hashlib.sha256(payload).hexdigest()
                self.assertIn(digest, launcher)
                self.assertRegex(launcher, r"https://raw\.githubusercontent\.com/Killow1998/"
                                           r"VPS-Maintenance-Documentation/[0-9a-f]{40}/scripts/cpa/" + re.escape(name))


class BashLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell = (str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe")
                     if os.name == "nt" else shutil.which("bash"))
        if not cls.shell or not Path(cls.shell).exists():
            raise unittest.SkipTest("Bash is not installed")

    def run_bash(self, suffix, interactive=False):
        python = sys.executable
        if os.name == "nt":
            python = "/" + python[0].lower() + python[2:].replace("\\", "/")
        env = {**os.environ, "CPA_TEST_PYTHON": python, "CPA_PROXY": "stale", "CPA_BASEURL": "stale",
               "NO_PROXY": "*", "no_proxy": "*"}
        source = BASH_SOURCE.rsplit("\nmain\n", 1)[0]
        if interactive:
            source = source.replace('if [[ ! -t 1 || ! -r /dev/tty ]]; then', 'if false; then')
        code = source + '\npython3() { "$CPA_TEST_PYTHON" "$@"; }\n' + suffix
        return subprocess.run([self.shell, "--noprofile", "--norc"], input=code.encode("utf-8"), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    def test_verified_download_runs_with_proxy_and_blank_base(self):
        payload = (ROOT / "scripts/cpa/quota.sh").read_bytes()
        code = r'''
saved_proxy_choice() { printf '2\nhttp://127.0.0.1:7897\n'; }
curl() {
  [[ "$*" == *'--proxy http://127.0.0.1:7897 --noproxy  --proto =https'* ]] || return 98
  [[ "$*" == *'--location --max-redirs 0'* ]] || return 98
  printf '%s' 'PAYLOAD' | python3 -c 'import base64,sys;sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))'
}
bash() {
  [[ "$CPA_PROXY" == 'http://127.0.0.1:7897' && "$CPA_BASEURL" == '' && "$1" == '-c' ]] || return 98
  [[ "$2" == *'def main():'* ]] || return 98
  printf '%s\n' CLIENT_STARTED
}
main
'''.replace("PAYLOAD", base64.b64encode(payload).decode())
        result = self.run_bash(code, interactive=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout.strip(), b"CLIENT_STARTED")

    def test_failed_or_corrupt_download_never_runs(self):
        for status in (0, 7):
            with self.subTest(status=status):
                result = self.run_bash(r'''
saved_proxy_choice() { printf '2\nhttp://127.0.0.1:7897\n'; }
curl() { printf '%s' 'echo UNVERIFIED_EXECUTION'; return STATUS; }
bash() { printf '%s' UNVERIFIED_EXECUTION; }
main
'''.replace("STATUS", str(status)), interactive=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b"UNVERIFIED_EXECUTION", result.stdout + result.stderr)
                self.assertIn(b"mismatch" if status == 0 else b"download failed", result.stderr)

    def test_nonterminal_and_old_python_stop_before_download(self):
        for setup in ("", "find_python() { return 1; }"):
            with self.subTest(setup=setup):
                result = self.run_bash(setup + '\ncurl() { echo UNEXPECTED_DOWNLOAD; }\nmain\n')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b"UNEXPECTED_DOWNLOAD", result.stdout)


class PowerShellLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shells = [path for name in ("powershell", "pwsh") if (path := shutil.which(name))]
        if not cls.shells:
            raise unittest.SkipTest("PowerShell is not installed")

    def run_ps(self, shell, code):
        prefix = "$ErrorActionPreference='Stop'; $launcher=[Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($env:CPA_TEST_LAUNCHER)); "
        env = {**os.environ, "CPA_TEST_LAUNCHER": str(ROOT / "cpa.ps1"), "CPA_BASEURL": "stale",
               "CPA_PROXY": "stale", "NO_PROXY": "*", "no_proxy": "*"}
        env = {key: value for key, value in env.items() if key.casefold() != "psmodulepath"}
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                                 base64.b64encode((prefix + code).encode("utf-16-le")).decode()],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors="replace"))

    def test_prompt_retry_verified_download_and_forwarding(self):
        code = r'''
$payload = 'param([string]$ProxyUrl,[string]$BaseUrl); if($ProxyUrl -ne "http://127.0.0.1:7897" -or $BaseUrl){throw "wrong settings"}; $script:started=$true'
$bytes = [Text.Encoding]::UTF8.GetBytes($payload)
$sha = [Security.Cryptography.SHA256]::Create()
try { $digest = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
finally { $sha.Dispose() }
$pinnedDigest = [regex]::Match($launcher, "'([0-9a-f]{64})'").Groups[1].Value
if (-not $pinnedDigest) { throw 'missing pinned hash' }
$launcher = $launcher.Replace($pinnedDigest, $digest)
# Input is synthetic here; a real console exercise covers raw keys separately.
$launcher = $launcher.Replace("`$Host.Name -ne 'ConsoleHost' -or [Console]::IsInputRedirected -or [Console]::IsOutputRedirected", '$false')
$script:reads=0; $script:downloads=0; $script:started=$false
function Test-Form {
    param($Title,$Fields,$Actions)
    $script:reads++
    foreach($bad in @('0','65536')) {
        $rejected=$false
        try { & $Fields[0].Validate $bad } catch { $rejected=$true }
        if(-not $rejected){throw 'invalid port accepted'}
    }
    return [pscustomobject]@{Action=0;Values=@((& $Fields[0].Validate '7897'))}
}
function Invoke-WebRequest {
    param($Uri,$Proxy,[switch]$UseBasicParsing,$MaximumRedirection,$TimeoutSec)
    if($Proxy -ne 'http://127.0.0.1:7897' -or $MaximumRedirection -ne 0 -or $TimeoutSec -ne 60){throw 'unsafe download'}
    if($Uri -notmatch '/[0-9a-f]{40}/scripts/cpa/quota.ps1$'){throw 'unpinned URL'}
    $script:downloads++
    return [pscustomobject]@{StatusCode=200;RawContentStream=[IO.MemoryStream]::new($bytes)}
}
$launcher=$launcher.Insert($launcher.LastIndexOf('Start-CpaQuota'), "function Get-CpaBootstrapConfigPath { return ([IO.Path]::Combine([IO.Path]::GetTempPath(), 'cpa-absent-'+[guid]::NewGuid())) }`nfunction Read-CpaConsoleForm { param(`$Title,`$Fields,`$Actions) Test-Form `$Title `$Fields `$Actions }`n")
& ([scriptblock]::Create($launcher))
if(-not $script:started -or $script:reads -ne 1 -or $script:downloads -ne 1){throw 'wrong launch sequence'}
'''
        for shell in self.shells:
            with self.subTest(shell=shell):
                self.run_ps(shell, code)

    def test_bad_download_and_nonterminal_never_run(self):
        for shell in self.shells:
            for mode in ("corrupt", "failed", "redirect", "nonterminal"):
                code = r'''
$mode='MODE'; $script:downloads=0; $script:rejected=$false
$launcher=$launcher.Insert($launcher.LastIndexOf('Start-CpaQuota'), "function Get-CpaBootstrapConfigPath { return ([IO.Path]::Combine([IO.Path]::GetTempPath(), 'cpa-absent-'+[guid]::NewGuid())) }`n")
if($mode -ne 'nonterminal'){
    $launcher=$launcher.Insert($launcher.LastIndexOf('Start-CpaQuota'), "function Read-CpaProxyPort { return 7897 }`n")
}
function Read-Host { throw 'UNEXPECTED_PROMPT' }
function Invoke-WebRequest {
    $script:downloads++
    if($mode -eq 'failed'){throw 'synthetic download failed'}
    $bytes=[Text.Encoding]::UTF8.GetBytes('throw "UNVERIFIED_EXECUTION"')
    $status=if($mode -eq 'redirect'){302}else{200}
    return [pscustomobject]@{StatusCode=$status;RawContentStream=[IO.MemoryStream]::new($bytes)}
}
try { & ([scriptblock]::Create($launcher)) }
catch {
    if($_.Exception.Message -match 'UNVERIFIED_EXECUTION|UNEXPECTED_PROMPT'){throw}
    $script:rejected=$true
}
if(-not $script:rejected){throw 'unsafe launch accepted'}
if($mode -eq 'nonterminal' -and $script:downloads){throw 'downloaded without terminal'}
'''.replace("MODE", mode)
                with self.subTest(shell=shell, mode=mode):
                    self.run_ps(shell, code)

    def test_http_text_to_iex_preserves_header_and_starts(self):
        payload = PS_SOURCE.rsplit('Start-CpaQuota', 1)[0] + "function Start-CpaQuota { $script:started=$true }; Start-CpaQuota\n"
        raw = payload.encode('utf-8')

        class Download(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Download) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for shell in self.shells:
                    with self.subTest(shell=shell):
                        self.run_ps(shell, "$script:started=$false; irm http://127.0.0.1:PORT/ | iex; if(-not $script:started){throw 'not started'}".replace('PORT', str(server.server_port)))
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
