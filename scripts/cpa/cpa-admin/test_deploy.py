import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import deploy_remote
from server import Store


class DeploymentPolicyTest(unittest.TestCase):
    def setUp(self):
        self.state = {"revision": 1, "clients": [
            {"id": "client-a", "name": "limited", "enabled": True, "account_ids": []},
            {"id": "client-b", "name": "shared", "enabled": True, "account_ids": []}],
            "accounts": [
                {"id": "account-a", "name": "a@example.invalid", "enabled": True, "route_ready": True},
                {"id": "account-b", "name": "b@example.invalid", "enabled": True, "route_ready": True}]}

    def test_unique_selector_preserves_restricted_and_shared_access(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "policy.sqlite", seed=copy.deepcopy(self.state))
            payload = deploy_remote.initial_policy(store.snapshot(), "limited", "A@example.invalid")
            store.save(payload)
            self.assertTrue(store.check("client-a", "account-a")["allowed"])
            self.assertFalse(store.check("client-a", "account-b")["allowed"])
            self.assertTrue(store.check("client-b", "account-a")["allowed"])
            self.assertTrue(store.check("client-b", "account-b")["allowed"])

    def test_unknown_empty_and_ambiguous_selectors_are_rejected(self):
        for selector in ("limtied", "LIMITED", "", " ", None):
            with self.subTest(selector=selector), self.assertRaises(RuntimeError):
                deploy_remote.initial_policy(self.state, selector, "a@example.invalid")
        self.state["clients"][1]["name"] = "limited"
        with self.assertRaises(RuntimeError):
            deploy_remote.initial_policy(self.state, "limited", "a@example.invalid")
        payload = deploy_remote.initial_policy(self.state, "client-a", "a@example.invalid")
        self.assertEqual(payload["clients"][0]["account_ids"], ["account-a"])
        self.assertEqual(payload["clients"][1]["account_ids"], ["account-a", "account-b"])

    def test_missing_ambiguous_disabled_or_unrouted_accounts_are_rejected(self):
        changes = (
            lambda s: s["accounts"].clear(),
            lambda s: s["accounts"][0].update(name="other@example.invalid"),
            lambda s: s["accounts"][1].update(name="a@example.invalid"),
            lambda s: s["accounts"][0].update(enabled=False),
            lambda s: s["accounts"][1].update(route_ready=False),
        )
        for change in changes:
            state = copy.deepcopy(self.state)
            change(state)
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                deploy_remote.initial_policy(state, "limited", "a@example.invalid")
        for email in ("", " ", None):
            with self.subTest(email=email), self.assertRaises(RuntimeError):
                deploy_remote.initial_policy(self.state, "limited", email)

    def test_invalid_selector_stops_before_files_services_or_routes_change(self):
        job = {"deployment": {"observer_config": "observer.fixture.json", "base_url": "https://control.example"},
               "remote_admin": "", "restricted_key": "limtied"}
        admin = Mock()
        admin.records.return_value = self.state["clients"]

        def load_admin(source, scope):
            scope["Admin"] = Mock(return_value=admin)

        def read_text(path):
            if path.name == "observer.fixture.json":
                return "{}"
            if path.name == "control.example":
                return "    location / {\n        proxy_pass http://127.0.0.1:8317;\n    }\n"
            self.fail("Unexpected file read: " + str(path))

        with patch.dict(sys.modules, {"pwd": Mock()}), \
             patch.object(deploy_remote.os, "umask"), \
             patch.object(deploy_remote.sys, "stdin", io.StringIO(json.dumps(job))), \
             patch.object(deploy_remote, "exec", side_effect=load_admin, create=True), \
             patch.object(Path, "read_text", autospec=True, side_effect=read_text), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(Path, "mkdir") as mkdir, \
             patch.object(Path, "write_text") as write, \
             patch.object(deploy_remote, "command") as command:
            with self.assertRaisesRegex(RuntimeError, "Restricted Key not found"):
                deploy_remote.main()
            mkdir.assert_not_called()
            write.assert_not_called()
            command.assert_not_called()
            admin.control_operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
