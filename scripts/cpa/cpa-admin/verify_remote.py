"""Read-only loopback acceptance for an existing CPA permission gateway.

No login, database access, queue consumption, external HTTP or model generation.
Public HTTPS/proxy checks and before/after database comparison are separate steps.
"""
import sys

if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required. No changes were made.")

import argparse
import datetime as dt
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

LIMIT = 16 * 1024 * 1024
PERIODS = ("1h", "7d", "30d", "all", "day", "month")
TOKENS = ("input_tokens", "uncached_tokens", "cache_read_tokens", "cache_write_tokens",
          "output_tokens", "reasoning_tokens", "total_tokens")
COSTS = ("estimated_cost_usd_low", "estimated_cost_usd_high")
COUNTS = ("requests", "failed_requests", "priced_requests", "incomplete_accounting_requests")


class CheckFailed(Exception):
    pass


def loopback(url):
    parsed = urlsplit(url)
    host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
    if (parsed.scheme != "http" or not host or not ipaddress.ip_address(host).is_loopback
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("Only loopback HTTP origins are accepted")
    return host, parsed.port or 80


def get(endpoint, path, key=None):
    """Never follow redirects or consult environment proxy settings."""
    host, port = loopback(endpoint)
    connection = http.client.HTTPConnection(host, port, timeout=35)
    try:
        headers = {"Authorization": "Bearer " + key} if key is not None else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            return None, None
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            body = None
        return response.status, body
    except (OSError, http.client.HTTPException, ValueError):
        return None, None
    finally:
        connection.close()


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def model_totals_match(data):
    models, totals = data.get("models"), data.get("totals")
    if not isinstance(models, list) or not isinstance(totals, dict):
        return False
    if any(not isinstance(m, dict) or not isinstance(m.get("model"), str) for m in models):
        return False
    if len({m["model"] for m in models}) != len(models):
        return False
    for field in COUNTS + TOKENS + COSTS:
        if field not in totals or any(field not in m for m in models):
            return False
        values = [m[field] for m in models]
        if any(v is not None and not numeric(v) for v in values):
            return False
        if field in COUNTS and (any(type(v) is not int for v in values) or type(totals[field]) is not int):
            return False
        known = [v for v in values if v is not None]
        expected = (None if field in TOKENS and len(known) != len(values) else
                    sum(known) if known else None if field in COSTS else 0)
        actual = totals[field]
        if expected is None:
            if actual is not None:
                return False
        elif not numeric(actual) or not math.isclose(actual, expected, rel_tol=0,
                                                     abs_tol=max(1, len(models)) * 1e-8 if field in COSTS else 0):
            return False
    return True


def quota_matches(data, expected_ids):
    if data.get("scope") != "shared_pool" or data.get("visibility") != "authorized_accounts":
        return False
    accounts, windows = data.get("accounts"), data.get("windows")
    if not isinstance(accounts, list) or not isinstance(windows, dict):
        return False
    if any(not isinstance(a, dict) or not isinstance(a.get("id"), str)
           or not isinstance(a.get("windows"), dict) for a in accounts):
        return False
    if {a["id"] for a in accounts} != expected_ids or len(accounts) != len(expected_ids):
        return False
    counts = {"accounts_total": len(accounts), "accounts_enabled": sum(bool(a.get("enabled")) for a in accounts),
              "accounts_with_fresh_quota": sum(not a.get("stale", True) for a in accounts)}
    if any(data.get(k) != v for k, v in counts.items()):
        return False
    names = {name for a in accounts for name in a["windows"]}
    if set(windows) != names:
        return False
    for name in names:
        values = [a["windows"][name].get("remaining_percent") for a in accounts
                  if name in a["windows"] and not a.get("stale", True)
                  and numeric(a["windows"][name].get("remaining_percent"))]
        expected = {"known_accounts": len(values), "min_remaining_percent": min(values) if values else None,
                    "max_remaining_percent": max(values) if values else None,
                    "remaining_percent": values[0] if len(accounts) == len(values) == 1 else None,
                    "resets_at": accounts[0]["windows"][name].get("resets_at") if len(accounts) == 1 else None}
        if any(windows[name].get(k) != v for k, v in expected.items()):
            return False
    return True


def policy_signature(state):
    return sorted((c["id"], c["enabled"], sorted(c["account_ids"])) for c in state["clients"])


class Verification:
    def __init__(self, request=get):
        self.request = request
        self.checks = []

    def check(self, name, passed):
        self.checks.append({"check": name, "ok": bool(passed)})
        return bool(passed)

    def fetch(self, endpoint, path, name, key=None, expected=200, required=True):
        status, body = self.request(endpoint, path, key)
        ok = self.check(name, status == expected)
        if expected == 200 and path != "/admin/":
            ok = self.check(name + ".json", isinstance(body, (dict, list))) and ok
        if not ok and required:
            raise CheckFailed
        return body

    def run(self, config, observer):
        admin = "http://127.0.0.1:" + str(config.get("port", 8319))
        obs = "http://127.0.0.1:" + str(observer.get("port", 8318))
        cpa = observer["cpa_base_url"].rstrip("/")
        for endpoint in (admin, obs, cpa):
            loopback(endpoint)
        management = observer["management_key"]
        self.fetch(admin, "/healthz", "admin.health")
        self.fetch(obs, "/healthz", "observer.health")
        keys = self.fetch(cpa, "/v8/management/config/access/api-keys", "keys.inventory", management)
        if not self.check("keys.valid", isinstance(keys, list) and bool(keys)
                          and all(isinstance(k, str) and k for k in keys) and len(set(keys)) == len(keys)):
            raise CheckFailed
        state = self.fetch(admin, "/internal/state", "policy.before", management)
        identities = {hmac.new(observer["hmac_secret"].encode(), ("client:" + k).encode(), hashlib.sha256).hexdigest(): k for k in keys}
        self.check("policy.key_inventory", {c["id"] for c in state["clients"]} == set(identities))
        self.check("gateway.enforced", state.get("enforcement") == "enforced")
        by_id = {c["id"]: c for c in state["clients"]}
        account_ids = {a["id"] for a in state["accounts"]}
        for slot, (identity, key) in enumerate(identities.items(), 1):
            name = "key_" + str(slot)
            client = by_id[identity]
            expected_ids = set(client["account_ids"]) & account_ids if client["enabled"] else set()
            catalog = self.fetch(admin, "/v1/models", name + ".models", key)
            self.check(name + ".models.unprefixed", isinstance(catalog.get("data"), list)
                       and all(isinstance(m.get("id"), str) and "/" not in m["id"] for m in catalog["data"]))
            quota = self.fetch(admin, "/v1/quota", name + ".quota", key)
            self.check(name + ".quota.acl_and_aggregates", quota_matches(quota, expected_ids))
            overview = self.fetch(admin, "/v1/usage?period=overview", name + ".overview", key)
            windows = overview.get("windows", [])
            self.check(name + ".overview.contract", overview.get("scope") == "own_key"
                       and overview.get("period") == "overview"
                       and [w.get("period") for w in windows] == list(PERIODS[:4])
                       and bool(overview.get("as_of"))
                       and all(w.get("end_utc") == overview["as_of"] and model_totals_match(w) for w in windows))
            try:
                last = dt.datetime.fromisoformat(overview["last_collected_at"].replace("Z", "+00:00"))
                age = (dt.datetime.now(dt.timezone.utc) - last).total_seconds()
            except (KeyError, ValueError, TypeError, AttributeError):
                age = float("inf")
            self.check(name + ".collector.healthy", overview.get("collector_status") == "ok"
                       and -5 <= age <= max(60, 3 * observer.get("usage_poll_seconds", 3)))
            for period in PERIODS:
                data = self.fetch(admin, "/v1/usage?period=" + period, name + ".usage." + period, key)
                self.check(name + ".usage." + period + ".contract", data.get("scope") == "own_key"
                           and data.get("period") == period and data.get("client") == client["name"]
                           and model_totals_match(data))
        for path in ("/v1/models", "/v1/quota", "/v1/usage"):
            self.fetch(admin, path, "unauthenticated" + path, expected=401, required=False)
            self.fetch(admin, path, "invalid_key" + path, "cpa-verification-invalid", expected=401, required=False)
        self.fetch(admin, "/admin/", "admin.page")
        self.fetch(admin, "/admin/api/state", "admin.rejects_client_key", keys[0], expected=401, required=False)
        self.fetch(admin, "/internal/state", "internal.rejects_client_key", keys[0], expected=401, required=False)
        self.fetch(admin, "/v1/usage?client=other", "usage.rejects_selector", keys[0], expected=400, required=False)
        self.fetch(admin, "/v1/quota?account=other", "quota.rejects_selector", keys[0], expected=400, required=False)
        after = self.fetch(admin, "/internal/state", "policy.after", management)
        self.check("policy.unchanged_during_check", state["revision"] == after["revision"]
                   and policy_signature(state) == policy_signature(after))

    def report(self):
        return {"ok": bool(self.checks) and all(c["ok"] for c in self.checks), "checks": self.checks,
                "scope": "loopback_read_interfaces", "not_verified": ["public_https_and_proxy",
                "persisted_history_against_backup", "new_usage_event_ingestion", "paid_model_generation",
                "administrator_login_and_csrf"],
                "history_note": "Zero retained records do not prove no calls occurred."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/cpa-admin/config.json")
    args = parser.parse_args(argv)
    verification = Verification()
    try:
        config = json.loads(Path(args.config).read_text())
        observer = json.loads(Path(config["observer_config"]).read_text())
        verification.run(config, observer)
    except CheckFailed:
        pass
    except Exception:
        verification.check("configuration_or_response_invalid", False)
    result = verification.report()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
