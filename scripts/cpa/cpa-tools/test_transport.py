import subprocess
import unittest
from unittest.mock import patch
import common


class TransportTest(unittest.TestCase):
    def cfg(self):
        return {"ssh": ["ssh", "fixture.invalid"], "_ssh_control_path": "/tmp/fixture-control"}

    def test_only_pre_session_descriptor_failure_can_retry(self):
        failed = subprocess.CompletedProcess([], 255, "", "mux_client_request_session: send fds failed")
        success = subprocess.CompletedProcess([], 0, '{"ok":true}', "")
        cfg = self.cfg()
        with patch('common.subprocess.run', side_effect=[failed, success, success]) as run:
            self.assertTrue(common.remote(cfg, 'keys_delete', selector='fixture')['ok'])
            self.assertIn('ControlPath=none', run.call_args_list[1].args[0])
            common.remote(cfg, 'control_state')
            self.assertIn('ControlPath=none', run.call_args_list[2].args[0])
            self.assertEqual(run.call_count, 3)

    def test_disconnect_and_remote_failures_never_retry(self):
        for code, output, error in ((255, '', 'Connection reset'), (1, '{"ok":false,"error":"fixture"}', ''),
                                     (255, '{}', 'mux_client_request_session: send fds failed')):
            with patch('common.subprocess.run', return_value=subprocess.CompletedProcess([], code, output, error)) as run:
                with self.assertRaises(RuntimeError): common.remote(self.cfg(), 'keys_delete', selector='fixture')
                self.assertEqual(run.call_count, 1)


if __name__ == '__main__': unittest.main()
