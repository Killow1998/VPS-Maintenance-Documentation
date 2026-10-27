"""Loopback-only permission editor prototype. Never connects to production CPA."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent


class PolicyError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def demo_state():
    now = dt.datetime.now(dt.timezone.utc)

    def window(percent, **delta):
        return {"remaining_percent": percent,
                "resets_at": (now + dt.timedelta(**delta)).isoformat()}

    return {
        "accounts": [
            {"id": "account-a", "name": "个人账号", "plan": "Claude Pro", "enabled": True,
             "windows": {"five_hour": window(35, hours=1), "seven_day": window(78, days=2, hours=3)}},
            {"id": "account-b", "name": "共享账号", "plan": "Claude Max", "enabled": True,
             "windows": {"five_hour": window(72, hours=3), "seven_day": window(46, days=5, hours=2)}},
            {"id": "account-c", "name": "备用账号", "plan": "Claude Pro", "enabled": False,
             "windows": {}},
        ],
        "clients": [
            {"id": "client-a", "name": "client_a", "enabled": True,
             "account_ids": ["account-a"], "usage": {"requests": 41, "tokens": 184020, "period": "1h"}},
            {"id": "client-b", "name": "client_b", "enabled": True,
             "account_ids": ["account-a", "account-b"], "usage": {"requests": 26, "tokens": 92400, "period": "1h"}},
            {"id": "client-c", "name": "service", "enabled": True,
             "account_ids": ["account-b"], "usage": {"requests": 7, "tokens": 21100, "period": "1h"}},
            {"id": "client-d", "name": "new_client", "enabled": False,
             "account_ids": [], "usage": {"requests": 0, "tokens": 0, "period": "1h"}},
        ],
    }


class Store:
    def __init__(self, path: Path, seed=None):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        with self.db() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS policy (
                    id INTEGER PRIMARY KEY CHECK (id=1), revision INTEGER NOT NULL,
                    data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL,
                    revision INTEGER NOT NULL, summary TEXT NOT NULL);
            """)
            con.execute("INSERT OR IGNORE INTO policy VALUES (1, 1, ?)",
                        (json.dumps(demo_state() if seed is None else seed, ensure_ascii=False),))
        path.chmod(0o600)

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.path, timeout=5)
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def snapshot(self, con=None):
        if con is None:
            with self.db() as own:
                own.execute("BEGIN")
                return self.snapshot(own)
        revision, raw = con.execute("SELECT revision, data FROM policy WHERE id=1").fetchone()
        data = json.loads(raw)
        data.update({"revision": revision, "mode": "demo", "enforcement": "not_connected"})
        data["audit"] = [dict(zip(("timestamp", "revision", "summary"), row)) for row in con.execute(
            "SELECT timestamp, revision, summary FROM audit ORDER BY id DESC LIMIT 8")]
        return data

    def save(self, payload, actor="local"):
        if not isinstance(payload, dict) or set(payload) != {"expected_revision", "clients"}:
            raise PolicyError(400, "策略格式不正确。")
        if type(payload["expected_revision"]) is not int or not isinstance(payload["clients"], list):
            raise PolicyError(400, "版本或 Key 列表格式不正确。")
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            current = self.snapshot(con)
            if payload["expected_revision"] != current["revision"]:
                raise PolicyError(409, "另一窗口已修改策略。请重新载入后编辑。")
            by_id = {row["id"]: row for row in current["clients"]}
            known_accounts = {row["id"] for row in current["accounts"]}
            seen, changes = set(), []
            account_names = {a["id"]: a["name"] for a in current["accounts"]}
            for row in payload["clients"]:
                if not isinstance(row, dict) or set(row) != {"id", "enabled", "account_ids"}:
                    raise PolicyError(400, "Key 策略格式不正确。")
                key_id, accounts = row["id"], row["account_ids"]
                if not isinstance(key_id, str) or key_id not in by_id or key_id in seen:
                    raise PolicyError(400, "Key 不存在或重复。")
                if type(row["enabled"]) is not bool or not isinstance(accounts, list):
                    raise PolicyError(400, "开关必须是布尔值，账号必须是列表。")
                if any(not isinstance(a, str) or a not in known_accounts for a in accounts):
                    raise PolicyError(400, "策略包含未知账号。")
                if len(set(accounts)) != len(accounts):
                    raise PolicyError(400, "策略包含重复账号。")
                seen.add(key_id)
                old = by_id[key_id]
                before, after = set(old["account_ids"]), set(accounts)
                if old["enabled"] != row["enabled"]:
                    changes.append(f'{old["name"]}：' + ("启用" if row["enabled"] else "停用"))
                for account in sorted(before ^ after):
                    changes.append(f'{old["name"]} → {account_names[account]}：' + ("允许" if account in after else "关闭"))
                old.update(enabled=row["enabled"], account_ids=sorted(accounts))
            if seen != set(by_id):
                raise PolicyError(400, "必须包含当前全部 Key；未保存任何修改。")
            if changes:
                revision = current["revision"] + 1
                data = {"clients": current["clients"], "accounts": current["accounts"]}
                con.execute("UPDATE policy SET revision=?, data=? WHERE id=1",
                            (revision, json.dumps(data, ensure_ascii=False)))
                timestamp = dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
                con.execute("INSERT INTO audit(timestamp, revision, summary) VALUES (?, ?, ?)",
                            (timestamp, revision, actor + " · " + "；".join(changes)))
                con.execute("DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 200)")
            return self.snapshot(con)

    def sync_inventory(self, clients, accounts):
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            current = self.snapshot(con)
            saved = {c["id"]: c for c in current["clients"]}
            active_ids = {a["id"] for a in accounts}
            merged = []
            for client in clients:
                old = saved.get(client["id"], {})
                merged.append({**client, "enabled": old.get("enabled", True),
                               "account_ids": [a for a in old.get("account_ids", []) if a in active_ids]})
            data = {"clients": merged, "accounts": accounts}
            if data != {k: current[k] for k in data}:
                con.execute("UPDATE policy SET revision=revision+1, data=? WHERE id=1",
                            (json.dumps(data, ensure_ascii=False),))
            return self.snapshot(con)

    def check(self, client_id, account_id):
        state = self.snapshot()
        client = next((c for c in state["clients"] if c["id"] == client_id), None)
        account = next((a for a in state["accounts"] if a["id"] == account_id), None)
        if client is None:
            reason = "Key 不存在。"
        elif not client["enabled"]:
            reason = "这个 Key 已停用。"
        elif account is None or account_id not in client["account_ids"]:
            reason = "管理员未授权这个 Key 调用所选账号。"
        elif not account["enabled"]:
            reason = "已授权；所选账号当前不可用。"
        elif account.get("route_ready") is False:
            reason = "已授权；所选账号尚未配置隔离路由。"
        else:
            return {"allowed": True, "reason": "已保存的策略允许调用这个账号。", "revision": state["revision"]}
        return {"allowed": False, "reason": reason, "revision": state["revision"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "CPAAdminPreview"

    def log_message(self, *_):
        pass

    def reply(self, code, body, mime="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def route_path(self):
        path = urlsplit(self.path).path
        return path[len("/admin"):] if path.startswith("/admin/") else path

    def do_GET(self):
        if not self.valid_host():
            return self.reply(403, {"error": "不接受这个主机名。"})
        if urlsplit(self.path).path == "/admin":
            self.send_response(308)
            self.send_header("Location", "/admin/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = self.route_path()
        if path == "/api/state":
            return self.reply(200, self.server.store.snapshot())
        if path == "/api/quota":
            return self.reply(200, {"accounts": self.server.store.snapshot()["accounts"], "query_status": "demo"})
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path not in static:
            return self.reply(404, {"error": "页面不存在。"})
        name, mime = static[path]
        return self.reply(200, (ROOT / "web" / name).read_bytes(), mime)

    def do_POST(self):
        origin = "http://" + self.headers.get("Host", "")
        if not self.valid_host() or self.headers.get("Origin") != origin:
            return self.reply(403, {"error": "只接受本页发起的操作。"})
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            return self.reply(415, {"error": "请发送 JSON。"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise PolicyError(413, "策略内容超出限制。")
            payload = json.loads(self.rfile.read(length))
            path = self.route_path()
            if path == "/api/policies":
                return self.reply(200, self.server.store.save(payload))
            if path == "/api/check" and isinstance(payload, dict) and set(payload) == {"client_id", "account_id"}:
                return self.reply(200, self.server.store.check(payload["client_id"], payload["account_id"]))
            raise PolicyError(404, "接口不存在。")
        except (json.JSONDecodeError, ValueError):
            return self.reply(400, {"error": "JSON 格式不正确。"})
        except PolicyError as error:
            return self.reply(error.status, {"error": error.message})


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("请使用 Python 3.10+，本机可运行 python3.11。")
    parser = argparse.ArgumentParser(description="CPA 管理后台本地原型；仅监听回环，不连接 VPS。")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--db", type=Path, default=ROOT / "var" / "control.sqlite")
    args = parser.parse_args()
    os.umask(0o077)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.store = Store(args.db)
    print(f"CPA 管理后台本地原型：http://127.0.0.1:{server.server_port}（演示数据，未连接 VPS）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
