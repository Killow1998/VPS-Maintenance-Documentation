import copy
import tempfile
import unittest
from pathlib import Path

from server import PolicyError, Store


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "control.sqlite")

    def payload(self):
        state = self.store.snapshot()
        return {"expected_revision": state["revision"], "clients": [
            {k: row[k] for k in ("id", "enabled", "account_ids")} for row in state["clients"]]}

    def test_default_deny_and_authorized_selection(self):
        self.assertTrue(self.store.check("client-a", "account-a")["allowed"])
        self.assertFalse(self.store.check("client-a", "account-b")["allowed"])
        self.assertFalse(self.store.check("unknown-key", "account-a")["allowed"])

    def test_empty_whitelist_blocks_all_and_preserves_other_keys(self):
        payload = self.payload()
        payload["clients"][0]["account_ids"] = []
        state = self.store.save(payload)
        self.assertEqual(state["revision"], 2)
        for account in state["accounts"]:
            self.assertFalse(self.store.check("client-a", account["id"])["allowed"])
        self.assertTrue(self.store.check("client-b", "account-a")["allowed"])
        self.assertEqual(len(state["audit"]), 1)

    def test_multiple_allowed_accounts_and_global_disable(self):
        payload = self.payload()
        payload["clients"][0]["account_ids"] = ["account-a", "account-b"]
        self.store.save(payload)
        self.assertTrue(self.store.check("client-a", "account-b")["allowed"])
        payload = self.payload()
        payload["clients"][0]["enabled"] = False
        self.store.save(payload)
        self.assertFalse(self.store.check("client-a", "account-a")["allowed"])

    def test_unavailable_account_does_not_authorize_fallback(self):
        payload = self.payload()
        payload["clients"][0]["account_ids"] = ["account-c"]
        self.store.save(payload)
        for account in ("account-a", "account-b", "account-c"):
            self.assertFalse(self.store.check("client-a", account)["allowed"])

    def test_stale_editor_cannot_overwrite_newer_policy(self):
        first = self.payload()
        stale = copy.deepcopy(first)
        first["clients"][0]["account_ids"] = []
        self.store.save(first)
        with self.assertRaises(PolicyError) as error:
            self.store.save(stale)
        self.assertEqual(error.exception.status, 409)
        self.assertFalse(self.store.check("client-a", "account-a")["allowed"])

    def test_invalid_changes_are_atomic(self):
        payload = self.payload()
        payload["clients"][0]["account_ids"] = []
        payload["clients"][1]["account_ids"] = ["unknown-account"]
        with self.assertRaises(PolicyError):
            self.store.save(payload)
        self.assertTrue(self.store.check("client-a", "account-a")["allowed"])
        self.assertEqual(self.store.snapshot()["revision"], 1)

    def test_duplicate_and_omitted_keys_are_rejected(self):
        for mutate in (lambda p: p["clients"].append(p["clients"][0]), lambda p: p["clients"].pop()):
            payload = self.payload()
            mutate(payload)
            with self.assertRaises(PolicyError):
                self.store.save(payload)

    def test_no_op_does_not_create_audit_or_revision(self):
        state = self.store.save(self.payload())
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["audit"], [])

    def test_policy_survives_store_restart(self):
        payload = self.payload()
        payload["clients"][0]["account_ids"] = []
        self.store.save(payload)
        again = Store(self.store.path)
        self.assertFalse(again.check("client-a", "account-a")["allowed"])
        self.assertEqual(again.snapshot()["revision"], 2)


if __name__ == "__main__":
    unittest.main()
