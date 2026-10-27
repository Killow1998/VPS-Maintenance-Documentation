"""Executed on the VPS via SSH stdin; never run as a public HTTP service."""
import sys

if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required. No connection or configuration changes were made.")

import copy
import datetime as dt
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from zoneinfo import ZoneInfo


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def atomic_json(path, value):
    path = Path(path)
    stat = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=".cpa-admin-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if stat:
            os.chown(temporary, stat.st_uid, stat.st_gid)
            os.chmod(temporary, stat.st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sqlite_snapshot(source, destination):
    """Use the live database owner's identity so WAL sidecars stay writable."""
    source, destination = Path(source), Path(destination)
    owner = source.stat()
    if os.geteuid() not in (0, owner.st_uid):
        raise RuntimeError("database_owner_required")
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=source.parent) as temporary:
        if os.geteuid() == 0:
            os.chown(temporary, owner.st_uid, owner.st_gid)
        identity = {"user": owner.st_uid, "group": owner.st_gid, "extra_groups": []} if os.geteuid() != owner.st_uid else {}
        target = Path(temporary) / "usage.sqlite"
        script = 'import sqlite3,sys\ns=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True)\nd=sqlite3.connect(sys.argv[2])\ns.backup(d)\nd.close()\ns.close()'
        subprocess.run([sys.executable, "-c", script, str(source), str(target)],
                       check=True, capture_output=True, timeout=60, **identity)
        shutil.move(str(target), destination)
        os.chown(destination, os.geteuid(), os.getegid())
        os.chmod(destination, 0o600)


class Admin:
    def __init__(self, job):
        self.j = job
        self.path = Path(job["observer_config"])
        self.c = json.loads(self.path.read_text())
        base = self.c["cpa_base_url"].rstrip("/")
        require(urlsplit(base).hostname in ("127.0.0.1", "localhost", "::1"), "backend_must_be_loopback")
        self.base = base
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, path, data=None, method=None, key=None, observer=False):
        headers = {"Authorization": "Bearer " + (self.c["management_key"] if key is None else key)}
        if data is not None:
            headers["Content-Type"] = "application/json"
        root = "http://127.0.0.1:" + str(self.c.get("port", 8318)) if observer else self.base
        request = Request(root + path, headers=headers, method=method,
                          data=json.dumps(data).encode() if data is not None else None)
        with self.opener.open(request, timeout=45) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
            require(len(raw) <= 16 * 1024 * 1024, "response_too_large")
            return json.loads(raw)

    def files(self):
        return self.call("/v8/management/credentials").get("files", [])

    def control_call(self, path, data=None):
        request = Request("http://127.0.0.1:" + str(self.j.get("control_port", 8319)) + path,
                          headers={"Authorization": "Bearer " + self.c["management_key"], "Content-Type": "application/json"},
                          data=json.dumps(data).encode() if data is not None else None)
        try:
            with self.opener.open(request, timeout=35) as response:
                return json.loads(response.read(4 * 1024 * 1024))
        except Exception as error:
            if getattr(error, "code", None) == 409:
                raise RuntimeError("权限已在另一端修改，或账号路由未就绪；返回后重新打开权限页。") from None
            raise RuntimeError("权限服务不可用；检查 cpa-admin 服务状态。") from None

    def control_operation(self):
        action = self.j["action"]
        if action == "control_state":
            return {"state": self.control_call("/internal/state")}
        if action == "control_save":
            return {"state": self.control_call("/internal/policies", self.j["payload"])}
        if action == "control_ticket":
            return self.control_call("/internal/login-ticket", {})
        if action == "control_password":
            return self.control_call("/internal/password", {"username": self.j["username"], "password": self.j["password"]})
        if action == "control_sync_routes":
            auth_dir = Path(os.path.expanduser(self.call("/v8/management/config/oauth")["auth-dir"]))
            helpers = {v["credential"] for v in self.c.get("quota_bindings", {}).values()}
            changes = []
            for item in self.files():
                if item.get("provider") != "claude" or item.get("name") in helpers or not item.get("auth_index"):
                    continue
                name = item["name"]
                require(Path(name).name == name and name.endswith(".json"), "unsupported_credential_path")
                filename = auth_dir / name
                data = json.loads(filename.read_text())
                if data.get("cpa_auth_role") == "quota_only":
                    continue
                prefix = "acl-" + hmac.new(self.c["hmac_secret"].encode(), ("account:" + item["auth_index"]).encode(), hashlib.sha256).hexdigest()[:12]
                if data.get("prefix", "") != prefix:
                    changes.append({"name": name, "prefix": prefix, "previous_prefix": data.get("prefix", ""), "path": str(filename)})
            if not changes:
                self.control_call("/internal/refresh", {})
                return {"message": "账号路由已就绪；现有权限保留。"}
            destination = self.backup("account-routes", tuple(item["path"] for item in changes))
            atomic_json(destination / "route-changes.json", changes)
            applied = []
            try:
                for item in changes:
                    self.call("/v8/management/credentials/fields", {"name": item["name"], "prefix": item["prefix"]}, "PATCH")
                    applied.append(item)
                self.control_call("/internal/refresh", {})
            except Exception:
                for item in applied:
                    self.call("/v8/management/credentials/fields", {"name": item["name"], "prefix": item["previous_prefix"]}, "PATCH")
                raise RuntimeError("路由同步失败；已还原本次前缀。备份：" + str(destination)) from None
            return {"message": "已配置 " + str(len(changes)) + " 个账号的隔离路由；请在权限页授予 Key 访问。", "backup": str(destination)}
        raise RuntimeError("unknown_control_action")

    def keys(self):
        keys = self.call("/v8/management/config/access/api-keys")
        require(isinstance(keys, list) and all(isinstance(k, str) for k in keys), "unexpected_key_list")
        return keys

    def digest(self, key):
        return hmac.new(self.c["hmac_secret"].encode(), ("client:" + key).encode(), hashlib.sha256).hexdigest()

    def records(self, keys):
        labels = self.c.get("labels", {})
        return [{"id": self.digest(k), "name": labels.get(self.digest(k), "client-" + self.digest(k)[:10]),
                 "active": True} for k in keys]

    def choose(self, keys):
        selection = self.j["selector"]
        matched = [key for key, row in zip(keys, self.records(keys))
                   if selection in (row["id"], row["name"])]
        require(len(matched) == 1, "key_not_found_or_name_ambiguous_use_full_id")
        return matched[0]

    def backup(self, purpose, credentials=()):
        root = Path(self.j.get("backup_dir", "/var/backups/cpa-tools"))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = Path(tempfile.mkdtemp(prefix=dt.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S-") + purpose + "-", dir=root))
        for i, path in enumerate([self.path, *credentials]):
            shutil.copy2(path, destination / (str(i) + "-" + Path(path).name))
            os.chmod(destination / (str(i) + "-" + Path(path).name), 0o600)
        if credentials:
            sqlite_snapshot(self.c["database"], destination / "usage.sqlite")
        return destination

    def restart_observer(self):
        subprocess.run(["systemctl", "restart", self.j.get("observer_unit", "cpa-observer.service")],
                       check=True, capture_output=True, timeout=50)
        for _ in range(40):
            try:
                if self.call("/healthz", observer=True).get("status") == "ok":
                    return
            except Exception:
                pass
            time.sleep(.25)
        raise RuntimeError("observer_not_ready")

    def key_operation(self):
        action = self.j["action"]
        if action == "keys_quota":
            return self.pool_snapshot()
        keys = self.keys()
        if action == "keys_list":
            rows = self.records(keys)
            active = {r["id"] for r in rows}
            rows.extend({"id": k, "name": v, "active": False}
                        for k, v in self.c.get("labels", {}).items() if k not in active)
            result = {"keys": rows}
            if self.j.get("include_usage"):
                try:
                    result.update(self.usage(keys))
                except Exception as error:
                    result["usage_error"] = type(error).__name__
            if self.j.get("include_quota"):
                try:
                    result.update(self.pool_snapshot())
                except Exception as error:
                    result["quota_error"] = type(error).__name__
            return result
        if action == "keys_usage":
            return self.usage(keys)
        if action == "keys_export":
            return {"private_key": self.choose(keys)}
        old = copy.deepcopy(self.c)
        new_keys = list(keys)
        labels = self.c.setdefault("labels", {})
        name = self.j.get("name")
        if name is not None:
            require(isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name), "invalid_key_name")
        new_key = self.j.get("new_key")
        if action in ("keys_add", "keys_rotate"):
            require(isinstance(new_key, str) and 32 <= len(new_key) <= 1024 and new_key not in keys, "invalid_or_duplicate_key")
        if action == "keys_add":
            require(name not in labels.values(), "name_exists_including_history")
            new_keys.append(new_key)
            labels[self.digest(new_key)] = name
        else:
            selected = self.choose(keys)
            identity = self.digest(selected)
            previous_name = labels.get(identity, "client-" + identity[:10])
            if action == "keys_delete":
                require(len(keys) > 1, "refusing_to_remove_last_key")
                new_keys.remove(selected)
                labels.setdefault(identity, previous_name)
            elif action == "keys_rename":
                require(name == previous_name or name not in labels.values(), "name_exists_including_history")
                labels[identity] = name
            elif action == "keys_rotate":
                new_keys[new_keys.index(selected)] = new_key
                # Historical records keep their immutable key ID. A visible suffix
                # distinguishes retired usage rather than silently merging bills.
                labels[identity] = previous_name + "-retired-" + identity[:8]
                labels[self.digest(new_key)] = previous_name
            else:
                raise RuntimeError("unknown_key_action")
        destination = self.backup(action)
        atomic_json(destination / "client-keys.private.json", keys)
        try:
            require(self.keys() == keys and json.loads(self.path.read_text()) == old, "concurrent_change_detected")
            atomic_json(self.path, self.c)
            if new_keys != keys:
                self.call("/v8/management/config/access/api-keys", new_keys, "PUT")
                require(self.keys() == new_keys, "key_write_not_applied")
            self.restart_observer()
        except Exception:
            # Do not overwrite a third party's concurrent update during rollback.
            rollback_ok = True
            try:
                current = self.keys()
                if current == new_keys and new_keys != keys:
                    self.call("/v8/management/config/access/api-keys", keys, "PUT")
                elif current != keys:
                    rollback_ok = False
                if json.loads(self.path.read_text()) == self.c:
                    atomic_json(self.path, old)
                self.restart_observer()
            except Exception:
                rollback_ok = False
            raise RuntimeError("key_change_failed_" + ("rolled_back" if rollback_ok else "manual_recovery_required") + ":" + str(destination)) from None
        return {"changed": True, "backup": str(destination), "keys": self.records(new_keys),
                "history_preserved": True, "revocation_cache_seconds": 5,
                "needs_account_permissions": action in ("keys_add", "keys_rotate") and Path("/etc/cpa-admin/config.json").exists()}

    def usage(self, keys):
        spec = importlib.util.spec_from_file_location("cpa_usage_observer", self.j.get("observer_script", "/opt/cpa-observer/observer.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        store = module.Store(self.c["database"], self.c["hmac_secret"], self.c.get("labels", {}),
                             json.loads(Path(self.c["prices_file"]).read_text()))
        clients = set(self.c.get("labels", {})) | {self.digest(k) for k in keys}
        with store.db() as connection:
            clients.update(r[0] for r in connection.execute("SELECT DISTINCT client_id FROM records"))
        selector = self.j.get("selector")
        if selector:
            matched = [c for c in clients if selector in (c, store.label(c))]
            require(len(matched) == 1, "usage_selector_not_found_or_ambiguous")
            clients = matched
        return {"usage": [{"id": c, **store.summary(c, self.j.get("period", "day"), self.j.get("date"))}
                          for c in sorted(clients)]}

    def pool_snapshot(self):
        # Use the collector's saved snapshot, not the public route's additional
        # client authentication. No provider query or token refresh runs here.
        spec = importlib.util.spec_from_file_location("cpa_quota_observer", self.j.get("observer_script", "/opt/cpa-observer/observer.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with sqlite3.connect("file:" + self.c["database"] + "?mode=ro", uri=True, timeout=2) as connection:
            metadata = dict(connection.execute("SELECT key,value FROM metadata WHERE key IN ('quota_snapshot','quota_status')"))
        reader = module.Observer.__new__(module.Observer)
        reader.config = self.c
        reader.store = SimpleNamespace(meta=lambda key: metadata.get(key))
        quota = reader.quota_response()
        identities = {}
        for credential in self.files():
            index = credential.get("auth_index")
            if credential.get("provider") != "claude" or not index:
                continue
            identity = "account-" + hmac.new(self.c["hmac_secret"].encode(),
                                            ("account:" + index).encode(), hashlib.sha256).hexdigest()[:12]
            binding = self.c.get("quota_bindings", {}).get(credential.get("name"), {})
            identities[identity] = credential.get("email") or binding.get("expected_email")
        for account in quota.get("accounts", []):
            account["email"] = identities.get(account.get("id"))
        return {"quota": quota, "view": "administrator"}

    def auth_status(self):
        return {"accounts": [{"name": f.get("name"), "disabled": f.get("disabled"),
                               "status": f.get("status"), "provider": f.get("provider")}
                              for f in self.files() if f.get("provider") == "claude"]}

    def auth_preflight(self):
        files = self.files()
        for field in ("credential_name", "quota_credential_name"):
            name = self.j.get(field, "")
            require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.json", name), "invalid_credential_name")
            require(not any(f.get("name") == name for f in files), "credential_name_exists")
        email = self.j["expected_email"].casefold()
        require(not any(str(f.get("email", "")).casefold() == email and not f.get("disabled")
                        for f in files), "account_already_active")
        self.keys()
        return {"ready": True}

    def bind(self):
        main_name, quota_name = self.j["credential_name"], self.j["quota_credential_name"]
        for name in (main_name, quota_name):
            require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.json", name), "invalid_credential_name")
        auth_dir = Path(os.path.expanduser(self.call("/v8/management/config/oauth")["auth-dir"]))
        main_path, helper_path = auth_dir / main_name, auth_dir / quota_name
        main, helper = json.loads(main_path.read_text()), json.loads(helper_path.read_text())
        require(main.get("is_setup_token") is True and not main.get("refresh_token"), "main_not_clean_setup_token")
        require(helper.get("disabled") is True and helper.get("cpa_auth_role") == "quota_only"
                and helper.get("quota_for") == main_name, "invalid_quota_helper_role")
        expected = self.j["expected_email"].casefold()
        require(str(helper.get("email", "")).casefold() == expected, "quota_email_mismatch")
        source = next(f for f in self.files() if f.get("name") == quota_name)
        refreshed = False
        def upstream(path):
            nonlocal refreshed
            payload = {"auth_index": source["auth_index"], "method": "GET",
                       "url": "https://api.anthropic.com/api/oauth/" + path,
                       "header": {"Authorization": "Bearer $TOKEN$", "anthropic-beta": "oauth-2025-04-20"}}
            data = self.call("/v8/management/requests/api-call", payload)
            if data.get("status_code") == 401 and not refreshed:
                request = Request(self.base + "/v8/management/credentials/refresh", method="POST",
                                  headers={"Authorization": "Bearer " + self.c["management_key"],
                                           "Content-Type": "application/json"},
                                  data=json.dumps({"name": quota_name}).encode())
                # A refresh response contains tokens. Closing it without reading
                # leaves the refreshed credential exclusively on the VPS.
                with self.opener.open(request, timeout=45):
                    pass
                refreshed = True
                data = self.call("/v8/management/requests/api-call", payload)
            require(data.get("status_code") == 200, path + "_not_200")
            return json.loads(data["body"]) if isinstance(data["body"], str) else data["body"]
        account = upstream("profile").get("account", {})
        require(str(account.get("email", "")).casefold() == expected and account.get("uuid"), "profile_identity_mismatch")
        require(account["uuid"] == helper.get("account_uuid"), "profile_account_uuid_mismatch")
        quota = upstream("usage")
        require(any(isinstance(quota.get(w), dict) for w in ("five_hour", "seven_day")), "quota_windows_missing")
        require(self.j.get("same_account_confirmed") is True and self.j.get("generation_verified") is True,
                "generation_or_same_account_confirmation_missing")
        old_main, old_config = copy.deepcopy(main), copy.deepcopy(self.c)
        destination = self.backup("auth-bind", (main_path, helper_path))
        main.update(email=helper["email"], account_uuid=helper["account_uuid"],
                    authorization_method="claude_setup_token",
                    cpa_identity_verified_by="quota_oauth_profile_and_user_confirmed_same_account",
                    cpa_identity_verified_at=dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), disabled=True)
        if helper.get("organization_uuid"):
            main["organization_uuid"] = helper["organization_uuid"]
        self.c.setdefault("quota_bindings", {})[main_name] = {"credential": quota_name,
                                                            "expected_email": self.j["expected_email"]}
        try:
            require(json.loads(main_path.read_text()) == old_main and json.loads(self.path.read_text()) == old_config,
                    "concurrent_change_detected")
            self.call("/v8/management/credentials?name=" + quote(main_name), main, "POST")
            os.chmod(main_path, 0o600)
            atomic_json(self.path, self.c)
            self.call("/v8/management/credentials/status", {"name": main_name, "disabled": False}, "PATCH")
            self.restart_observer()
            key = self.keys()[0]
            self.call("/v1/models", key=key)
            entries = self.files()
            target = next(f for f in entries if f.get("name") == main_name)
            target_id = "account-" + hmac.new(self.c["hmac_secret"].encode(),
                                              ("account:" + target["auth_index"]).encode(), hashlib.sha256).hexdigest()[:12]
            sources = {v["credential"] for v in self.c["quota_bindings"].values()}
            expected_count = sum(f.get("provider") == "claude" and bool(f.get("auth_index"))
                                 and f.get("name") not in sources for f in entries)
            result = None
            accepted = False
            for _ in range(60):
                result = self.call("/v1/quota", key=key, observer=True)
                matched = next((a for a in result.get("accounts", []) if a.get("id") == target_id), {})
                accepted = (result.get("accounts_total") == expected_count and matched.get("enabled") is True
                            and matched.get("status") == "ok" and not matched.get("stale", True)
                            and not result.get("stale", True))
                if accepted:
                    break
                time.sleep(.5)
            require(accepted, "quota_coverage_incomplete")
            require(json.loads(main_path.read_text())["access_token"] == old_main["access_token"], "inference_token_changed")
            require(json.loads(helper_path.read_text()).get("disabled") is True, "helper_was_enabled")
        except Exception:
            try:
                self.call("/v8/management/credentials?name=" + quote(main_name), dict(old_main, disabled=True), "POST")
                os.chmod(main_path, 0o600)
                atomic_json(self.path, old_config)
                self.restart_observer()
            except Exception:
                raise RuntimeError("binding_failed_manual_recovery_required:" + str(destination)) from None
            raise RuntimeError("binding_failed_new_account_disabled:" + str(destination)) from None
        return {"enabled": True, "identity_verified": True, "generation_verified": True,
                "quota_verified": True, "backup": str(destination),
                "accounts_total": result["accounts_total"],
                "accounts_with_fresh_quota": result["accounts_with_fresh_quota"]}


def main():
    os.umask(0o077)
    job = json.load(sys.stdin)
    path = Path(job["observer_config"])
    with open(str(path) + ".admin.lock", "a") as lock:
        reads = {"keys_list", "keys_usage", "keys_quota", "keys_export", "auth_status", "auth_preflight", "control_state"}
        fcntl.flock(lock, fcntl.LOCK_SH if job["action"] in reads else fcntl.LOCK_EX)
        admin = Admin(job)
        action = job["action"]
        if action.startswith("control_"):
            result = admin.control_operation()
        elif action.startswith("keys_"):
            result = admin.key_operation()
        elif action == "auth_status":
            result = admin.auth_status()
        elif action == "auth_preflight":
            result = admin.auth_preflight()
        elif action == "auth_bind":
            result = admin.bind()
        else:
            raise RuntimeError("unknown_action")
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error) if type(error) is RuntimeError else type(error).__name__}))
        sys.exit(1)
