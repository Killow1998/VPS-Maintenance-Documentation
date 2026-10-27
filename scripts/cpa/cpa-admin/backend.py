"""Read-only CPA inventory and observer snapshots shared by Web, TUI and gateway."""
import copy
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import threading
import time
from urllib.parse import quote, urlsplit

from server import PolicyError, Store


def scope_quota(snapshot, allowed_ids):
    result = copy.deepcopy(snapshot)
    accounts = [a for a in result.get("accounts", []) if a["id"] in allowed_ids]
    windows = {}
    for name in sorted({w for account in accounts for w in account.get("windows", {})}):
        values = [a["windows"][name]["remaining_percent"] for a in accounts
                  if name in a["windows"] and not a.get("stale", True)
                  and isinstance(a["windows"][name].get("remaining_percent"), (int, float))]
        windows[name] = {"known_accounts": len(values),
                         "remaining_percent": values[0] if len(accounts) == 1 and len(values) == 1 else None,
                         "min_remaining_percent": min(values) if values else None,
                         "max_remaining_percent": max(values) if values else None,
                         "resets_at": accounts[0]["windows"][name].get("resets_at") if len(accounts) == 1 else None}
    # Keep the published client contract. The pool is shared subscription quota;
    # visibility is restricted by the caller's ACL, not a new response category.
    result.update(scope="shared_pool", visibility="authorized_accounts", accounts=accounts, windows=windows,
                  accounts_total=len(accounts), accounts_enabled=sum(bool(a.get("enabled")) for a in accounts),
                  accounts_with_fresh_quota=sum(not a.get("stale", True) for a in accounts),
                  stale=not accounts or any(a.get("enabled") and a.get("stale", True) for a in accounts))
    return result


class Backend:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.updated = 0
        self.client_keys, self.accounts, self.models = {}, [], {}
        self.c = json.loads(Path(config["observer_config"]).read_text())
        base = urlsplit(self.c["cpa_base_url"])
        if base.scheme != "http" or base.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError("CPA backend must be loopback HTTP")
        self.host, self.port = base.hostname, base.port or 80
        self.store = Store(Path(config["database"]), seed={"clients": [], "accounts": []})

    def rpc(self, path, data=None, method="GET"):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=12)
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None,
                {"Authorization": "Bearer " + self.c["management_key"], "Content-Type": "application/json"})
            response = connection.getresponse()
            body = response.read(16 * 1024 * 1024)
            if response.status != 200:
                raise PolicyError(503, "CPA 管理接口暂不可用。")
            return json.loads(body)
        finally:
            connection.close()

    def identity(self, kind, value):
        return hmac.new(self.c["hmac_secret"].encode(), (kind + ":" + value).encode(), hashlib.sha256).hexdigest()

    def refresh(self):
        with self.refresh_lock:
            return self._refresh()

    def _refresh(self):
        config = json.loads(Path(self.config["observer_config"]).read_text())
        if config["hmac_secret"] != self.c["hmac_secret"]:
            raise RuntimeError("Observer identity secret changed")
        self.c = config
        keys = self.rpc("/v8/management/config/access/api-keys")
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise RuntimeError("Invalid CPA client key inventory")
        files = self.rpc("/v8/management/credentials").get("files", [])
        helpers = {v["credential"] for v in self.c.get("quota_bindings", {}).values()}
        accounts, catalog, owners = [], {}, {}
        for entry in files:
            models = self.rpc("/v8/management/credentials/models?name=" + quote(entry["name"]))["models"]
            if not entry.get("disabled", True):
                for model in models:
                    if model["id"].startswith("acl-"):
                        owners.setdefault(model["id"], set()).add(entry["name"])
            if entry.get("provider") != "claude" or entry.get("name") in helpers or not entry.get("auth_index"):
                continue
            # Only logical inference credentials appear in the ACL.
            identity = "account-" + self.identity("account", entry["auth_index"])[:12]
            prefix = "acl-" + identity.removeprefix("account-")
            scoped = {m["id"][len(prefix) + 1:]: m for m in models if m["id"].startswith(prefix + "/")}
            catalog[identity] = scoped
            binding = self.c.get("quota_bindings", {}).get(entry["name"], {})
            accounts.append({"id": identity, "name": entry.get("email") or binding.get("expected_email") or identity,
                             "plan": "Claude", "enabled": not entry.get("disabled", True),
                             "route_ready": bool(scoped), "prefix": prefix})
        if any(len(names) != 1 for names in owners.values()):
            raise RuntimeError("Account route prefixes must be unique")
        clients = [{"id": self.identity("client", key), "name": self.c.get("labels", {}).get(self.identity("client", key),
                    "client-" + self.identity("client", key)[:10])} for key in keys]
        self.store.sync_inventory(sorted(clients, key=lambda c: c["name"]), sorted(accounts, key=lambda a: a["id"]))
        with self.lock:
            self.client_keys = {self.identity("client", k): k for k in keys}
            self.accounts, self.models, self.updated = accounts, catalog, time.monotonic()

    def require_fresh(self):
        if time.monotonic() - self.updated > 20:
            raise PolicyError(503, "账号状态同步中，请稍后重试。")

    def authenticate(self, key):
        self.require_fresh()
        if not key:
            raise PolicyError(401, "需要有效的 API Key。")
        identity = self.identity("client", key)
        with self.lock:
            expected = self.client_keys.get(identity)
        if expected is None or not hmac.compare_digest(expected, key):
            raise PolicyError(401, "API Key 无效。")
        return identity

    def allowed(self, identity):
        self.require_fresh()
        state = self.store.snapshot()
        client = next((c for c in state["clients"] if c["id"] == identity), None)
        return [a for a in state["accounts"] if client and client["enabled"] and a["id"] in client["account_ids"]]

    def quota(self, allowed_ids=None, *, key=None):
        if key is None:
            with self.lock:
                key = next(iter(self.client_keys.values()), None)
        if not key:
            raise PolicyError(503, "尚无可用于查询额度的 API Key。")
        snapshot = self.observer_get("/v1/quota", key)
        return snapshot if allowed_ids is None else scope_quota(snapshot, allowed_ids)

    def observer_get(self, path, key):
        # The observer owns SQLite/WAL. Reuse its authenticated read API instead
        # of depending on transient WAL sidecars in this service's read-only mount.
        connection = http.client.HTTPConnection("127.0.0.1", self.c.get("port", 8318), timeout=5)
        try:
            connection.request("GET", path, headers={"Authorization": "Bearer " + key})
            response = connection.getresponse()
            body = response.read(16 * 1024 * 1024 + 1)
            if response.status != 200 or len(body) > 16 * 1024 * 1024:
                raise PolicyError(503, "额度与用量查询服务暂不可用。")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise PolicyError(503, "查询服务响应格式不正确。")
            return data
        finally:
            connection.close()

    def account_quota(self, accounts=None):
        self.require_fresh()
        accounts = copy.deepcopy(accounts if accounts is not None else self.store.snapshot()["accounts"])
        try:
            snapshot = self.quota()
        except Exception:
            snapshot = {"accounts": [], "checked_at": None, "query_status": "unavailable", "stale": True}
        by_id = {a["id"]: a for a in snapshot.get("accounts", [])}
        for account in accounts:
            item = by_id.get(account["id"], {})
            account.update(windows=item.get("windows", {}), stale=item.get("stale", True),
                           as_of=item.get("as_of"), quota_status=item.get("status", "unknown"),
                           quota_reason=item.get("reason") or ("observer_unavailable" if snapshot.get("query_status") == "unavailable" else None),
                           retry_at=item.get("retry_at"))
        return {"accounts": accounts, "checked_at": snapshot.get("checked_at"),
                "query_status": snapshot.get("query_status", "unknown"), "stale": snapshot.get("stale", True),
                "poll_seconds": self.c.get("quota_poll_seconds", 300)}

    def state(self):
        self.require_fresh()
        state = self.store.snapshot()
        quota = self.account_quota(state["accounts"])
        state["accounts"] = quota.pop("accounts")
        state["quota"] = quota
        state.update(mode="live", enforcement="enforced" if self.config.get("active") else "prepared")
        state["collection"] = {"collector_status": "unavailable"}
        with self.lock:
            keys = dict(self.client_keys)
        for client in state["clients"]:
            counts = {"requests": None, "tokens": None, "period": "1h"}
            try:
                if quota["query_status"] == "unavailable":
                    raise PolicyError(503, "查询服务暂不可用。")
                data = self.observer_get("/v1/usage?period=1h", keys[client["id"]])
                totals = data["totals"]
                counts.update(requests=totals.get("requests"), tokens=totals.get("total_tokens"))
                state["collection"] = {"collector_status": data.get("collector_status", "unknown"),
                                       "collector_last_success": data.get("last_collected_at"),
                                       "collection_started_at": data.get("collection_started_at")}
            except Exception:
                pass  # Unknown is kept as None, never reported as zero usage.
            client["usage"] = counts
        return state

    def save(self, body, actor):
        self.require_fresh()
        state = self.store.snapshot()
        ready = {a["id"] for a in state["accounts"] if a["route_ready"]}
        for c in body.get("clients", []) if isinstance(body, dict) else []:
            if isinstance(c, dict) and any(a not in ready for a in c.get("account_ids", []) if isinstance(a, str)):
                raise PolicyError(409, "所选账号尚未配置隔离路由；先运行管理员路由同步。")
        self.store.save(body, actor)
        return self.state()
