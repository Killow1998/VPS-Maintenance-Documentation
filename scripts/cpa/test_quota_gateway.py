"""Published client contract against the permission gateway's actual response."""
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "cpa-admin"))
from backend import scope_quota
from test_quota_clients import CLIENT


class GatewayClientContractTests(unittest.TestCase):
    def test_published_client_accepts_filtered_pool_without_widening_access(self):
        full = {"scope": "shared_pool", "accounts": [
            {"id": "account-111111111111", "enabled": True, "stale": False,
             "windows": {"five_hour": {"remaining_percent": 31}}},
            {"id": "account-222222222222", "enabled": True, "stale": False,
             "windows": {"five_hour": {"remaining_percent": 89}}}], "query_status": "ok"}
        result = scope_quota(full, {"account-111111111111"})

        class Opener:
            def open(self, request, timeout):
                return io.BytesIO(json.dumps(result).encode())

        actual = CLIENT["fetch"]("https://cpa.example.invalid", "synthetic-key", "/v1/quota", "shared_pool", Opener())
        self.assertEqual(actual["accounts_total"], 1)
        self.assertEqual(actual["windows"]["five_hour"]["max_remaining_percent"], 31)
        self.assertNotIn("account-222222222222", json.dumps(actual))
        self.assertEqual(actual["visibility"], "authorized_accounts")
        self.assertEqual(full["scope"], "shared_pool")

        # A misrouted usage body must still be rejected, not treated as pool data.
        result = {"scope": "own_key", "totals": {}}
        with self.assertRaises(RuntimeError):
            CLIENT["fetch"]("https://cpa.example.invalid", "synthetic-key", "/v1/quota", "shared_pool", Opener())


if __name__ == "__main__":
    unittest.main()
