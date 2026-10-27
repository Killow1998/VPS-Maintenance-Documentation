"""Optional profile persistence: synthetic secrets, isolated home, no network."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_quota_clients import CLIENT, ROOT, overview


@unittest.skipUnless(os.name == 'posix', 'Bash client targets Linux/macOS')
class PrivateConfigTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.path = Path(self.home.name) / '.cpa' / 'query.json'
        self.mapping = patch.dict(CLIENT, {'config_path': lambda: self.path})
        self.mapping.start(); self.addCleanup(self.mapping.stop)
        env = patch.dict(os.environ, {'CPA_BASEURL': '', 'CPA_PROXY': ''})
        env.start(); self.addCleanup(env.stop)
        os.environ.pop('CPA_QUERY_CHOICE', None)

    def seed(self):
        CLIENT['save_config']('https://cpa.example.invalid', 'http://127.0.0.1:7897', 'synthetic-key')

    def run_client(self, prompts, fail=False, args=None):
        events, answers, output = [], iter(prompts), io.StringIO()
        output.isatty = lambda: True
        def prompt(label, validate):
            events.append('save' if '[y/N]' in label else 'input')
            return validate(next(answers))
        def fetch(base, key, path, scope, opener):
            events.append('request')
            if fail: raise RuntimeError('synthetic unavailable')
            self.assertEqual(key, 'synthetic-key')
            return overview() if scope == 'own_key' else {'scope': scope, 'accounts': []}
        with patch('sys.argv', ['quota', '--once'] + (args or [])), contextlib.redirect_stdout(output), \
             contextlib.redirect_stderr(io.StringIO()), patch.dict(CLIENT, {'prompt_setting': prompt,
             'read_key': lambda _: 'synthetic-key', 'fetch': fetch}):
            if fail:
                with self.assertRaises(SystemExit): CLIENT['main']()
            else: CLIENT['main']()
        self.assertNotIn('synthetic-key', output.getvalue())
        return events

    def test_first_success_asks_then_saves_and_loads(self):
        events = self.run_client(['7897', 'https://cpa.example.invalid', 'y'])
        self.assertEqual(events, ['input', 'input', 'request', 'request', 'save'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(CLIENT['load_config']()['api_key'], 'synthetic-key')
        self.assertEqual(self.run_client(['1']), ['input', 'request', 'request'])
        self.assertEqual(self.run_client(['2', '7897', 'https://new.example.invalid', 'y']),
                         ['input', 'input', 'input', 'request', 'request', 'save'])
        self.assertEqual(CLIENT['load_config']()['base_url'], 'https://new.example.invalid')

    def test_decline_failure_and_noninteractive_do_not_save(self):
        self.run_client(['7897', 'https://cpa.example.invalid', 'n'])
        self.assertFalse(self.path.exists())
        events = self.run_client(['7897', 'https://cpa.example.invalid'], fail=True)
        self.assertNotIn('save', events)
        self.assertFalse(self.path.exists())
        with patch('sys.stdout.isatty', return_value=False):
            self.assertIsNone(CLIENT['select_config'](None))

    def test_bootstrap_choice_does_not_ask_twice_and_arguments_win(self):
        self.seed()
        with patch.dict(os.environ, {'CPA_QUERY_CHOICE': '1'}):
            self.assertEqual(self.run_client([]), ['request', 'request'])
        events = self.run_client(['n'], args=['https://explicit.example.invalid', '--proxy', 'http://127.0.0.1:7897'])
        self.assertEqual(events, ['request', 'request', 'save'])

    def test_bootstrap_selects_saved_proxy_without_printing_key(self):
        source = (ROOT.parents[1] / 'cpa.sh').read_text().split("<<'PY'\n", 1)[1].split('\nPY', 1)[0]
        self.seed()
        output = io.StringIO()
        with patch('pathlib.Path.home', return_value=Path(self.home.name)), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            exec(compile(source, 'bootstrap', 'exec'), {'open': lambda *a, **k: io.StringIO('1\n')})
        self.assertEqual(output.getvalue(), '1\nhttp://127.0.0.1:7897\n')
        self.assertNotIn('synthetic-key', output.getvalue())

    def test_insecure_corrupt_symlink_and_failed_atomic_replace(self):
        self.seed(); original = self.path.read_bytes()
        self.path.chmod(0o644)
        with self.assertRaises(RuntimeError): CLIENT['load_config']()
        self.path.chmod(0o600)
        self.path.write_text('{"api_key":"never-display-this"')
        with self.assertRaises(RuntimeError) as failed: CLIENT['load_config']()
        self.assertNotIn('never-display-this', str(failed.exception))
        self.path.write_bytes(original)
        with patch('os.replace', side_effect=OSError), self.assertRaises(RuntimeError): self.seed()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('.query-*')), [])
        self.path.unlink(); target = Path(self.home.name) / 'unrelated'; target.write_bytes(original)
        self.path.symlink_to(target)
        with self.assertRaises(RuntimeError): self.seed()
        self.assertEqual(target.read_bytes(), original)


class PowerShellConfigTests(unittest.TestCase):
    def test_private_profile_round_trip_and_interactive_choices(self):
        shells = [s for n in ('pwsh', 'powershell') if (s := shutil.which(n))]
        if not shells: self.skipTest('PowerShell is not installed')
        for shell in shells:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as home:
                code = r'''
$ErrorActionPreference='Stop'
$s=[IO.File]::ReadAllText($env:CPA_TEST_SOURCE).TrimStart([char]0xFEFF)
# Synthetic-only diagnostics preserve the failing operation; production errors stay redacted.
$s=$s.Replace("throw '配置未保存；请检查 ~/.cpa 的权限和可用空间。'", 'throw ("fixture save: " + $_.Exception.Message)')
. ([scriptblock]::Create($s.Substring(0,$s.LastIndexOf('Invoke-CpaReport'))))
function Get-CpaConfigPath { return (Join-Path (Join-Path $env:CPA_TEST_HOME '.cpa') 'query.json') }
function Test-CpaTerminal { return $true }
function Clear-Host {}
$script:calls=0; $script:prompts=[Collections.Generic.Queue[string]]::new()
function Read-Host {
 param($Prompt,[switch]$AsSecureString)
 if ($AsSecureString) { return (ConvertTo-SecureString 'synthetic-key' -AsPlainText -Force) }
 if ($Prompt -like '*[[]y/N[]]*' -and $script:calls -lt 2) { throw 'saved before success' }
 if (-not $script:prompts.Count) { throw 'unexpected prompt' }
 return $script:prompts.Dequeue()
}
function Invoke-RestMethod {
 param($Uri,$Headers,$Method,$Proxy,[switch]$UseBasicParsing,$MaximumRedirection,$TimeoutSec,$ErrorAction)
 if ($Headers.Authorization -ne 'Bearer synthetic-key') { throw 'wrong key' }
 $script:calls++
 if ($Uri -like '*/v1/quota') { return ('{"scope":"shared_pool","accounts":[]}' | ConvertFrom-Json) }
 return ('DATA' | ConvertFrom-Json)
}
$Once=$true
foreach($answer in @('7897','https://cpa.example.invalid','y')) { $script:prompts.Enqueue($answer) }
Invoke-CpaReport
$profile=Read-CpaConfig
if($profile.api_key -ne 'synthetic-key' -or $profile.proxy_url -ne 'http://127.0.0.1:7897/') { throw 'wrong profile' }
$script:prompts.Enqueue('1'); Invoke-CpaReport
if($script:prompts.Count -or $script:calls -ne 4) { throw 'load did not reuse settings' }
foreach($answer in @('2','7897','https://new.example.invalid','y')) { $script:prompts.Enqueue($answer) }
Invoke-CpaReport
if((Read-CpaConfig).base_url -ne 'https://new.example.invalid') { throw 'overwrite failed' }
[IO.File]::WriteAllText((Get-CpaConfigPath), '{"api_key":"never-display-this"')
$failed=$false;try { Read-CpaConfig } catch { $failed=$true; if($_.Exception.Message -match 'never-display-this'){throw 'secret leak'} }
if(-not $failed) { throw 'corrupt profile accepted' }
'''.replace('DATA', json.dumps(overview()))
                env = dict(os.environ, CPA_TEST_HOME=home, CPA_TEST_SOURCE=str(ROOT/'quota.ps1'),
                           CPA_BASEURL='', CPA_PROXY='', CPA_QUERY_CHOICE='')
                result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-OutputFormat', 'Text', '-EncodedCommand',
                    base64.b64encode(code.encode('utf-16-le')).decode()], env=env, capture_output=True, timeout=40)
                self.assertNotIn(b'synthetic-key', result.stdout+result.stderr)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))


if __name__ == '__main__': unittest.main()
