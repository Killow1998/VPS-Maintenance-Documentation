"""Loopback admin service and account-scoped CPA gateway behind Nginx."""
import argparse
import hashlib
import hmac
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

from authentication import Authentication
from backend import Backend
from server import PolicyError

ROOT = Path(__file__).resolve().parent
COOKIE = "__Secure-cpa_admin"
MODEL_PATHS = {"/v1/messages", "/v1/messages/count_tokens", "/v1/chat/completions", "/v1/responses", "/v1/completions"}


def unique_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate JSON member")
        data[key] = value
    return data


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, backend):
        self.backend = backend
        self.authentication = Authentication(backend.store)
        self.slots = threading.BoundedSemaphore(32)
        self.sequence, self.sequence_lock = 0, threading.Lock()
        self.body_bytes, self.body_lock = 0, threading.Lock()
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass  # Never print requests or exceptions that can contain credentials.


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CPAControl"

    def setup(self):
        super().setup()
        self.connection.settimeout(40)

    def log_message(self, *_):
        pass

    @property
    def backend(self):
        return self.server.backend

    def answer(self, status, data, mime="application/json; charset=utf-8", headers=None):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode() if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def body(self, maximum=65536):
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            raise PolicyError(400, "请求长度不正确。")
        if self.headers.get("Content-Encoding", "identity") != "identity":
            raise PolicyError(415, "请发送未压缩 JSON。")
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise PolicyError(415, "需要 JSON 请求。")
        length = int(self.headers["Content-Length"])
        if not 0 < length <= maximum:
            raise PolicyError(413, "请求体超过允许大小。")
        if maximum > 65536:
            with self.server.body_lock:
                if self.server.body_bytes and self.server.body_bytes + length > 32 * 1024 * 1024:
                    raise PolicyError(503, "大请求处理中，请稍后重试。")
                self.server.body_bytes += length
                self.reserved_bytes = length
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise PolicyError(400, "请求体不完整。")
        data = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(data, dict):
            raise PolicyError(400, "需要 JSON 对象。")
        return data

    def key(self):
        bearer = self.headers.get("Authorization", "")
        key = bearer[7:] if bearer.lower().startswith("bearer ") else ""
        other = self.headers.get("x-api-key", "")
        if key and other and key != other:
            raise PolicyError(401, "鉴权头冲突。")
        if any(len(self.headers.get_all(h, [])) > 1 for h in ("Authorization", "x-api-key")):
            raise PolicyError(401, "鉴权头重复。")
        key = key or other
        if len(key) > 1024:
            raise PolicyError(401, "API Key 无效。")
        return key

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            raise PolicyError(401, "请重新登录。") from None
        return cookie[COOKIE].value if COOKIE in cookie else ""

    def internal(self):
        if self.client_address[0] not in ("127.0.0.1", "::1") or not hmac.compare_digest(self.key(), self.backend.c["management_key"]):
            raise PolicyError(401, "需要 SSH 管理权限。")

    def origin(self):
        if self.headers.get("Origin") != self.backend.config["origin"]:
            raise PolicyError(403, "只接受管理页面发起的操作。")

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        try:
            target = urlsplit(self.path)
            host = urlsplit(self.backend.config["origin"]).netloc
            if self.headers.get("Host") not in (host, "127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)):
                raise PolicyError(403, "主机名不受支持。")
            path = target.path
            if path == "/healthz":
                self.backend.require_fresh()
                return self.answer(200, {"status": "ok"})
            if path.startswith("/internal/"):
                self.internal()
                if path == "/internal/state" and method == "GET":
                    return self.answer(200, self.backend.state())
                if method != "POST":
                    raise PolicyError(405, "方法不受支持。")
                data = self.body()
                if path == "/internal/policies":
                    return self.answer(200, self.backend.save(data, "ssh-admin"))
                if path == "/internal/login-ticket":
                    token = self.server.authentication.ticket()
                    return self.answer(200, {"url": self.backend.config["origin"] + "/admin/#ticket=" + token, "expires_seconds": 120})
                if path == "/internal/password":
                    return self.answer(200, self.server.authentication.set_password(data.get("username"), data.get("password")))
                if path == "/internal/refresh":
                    self.backend.refresh()
                    return self.answer(200, self.backend.state())
                raise PolicyError(404, "接口不存在。")
            if path.startswith("/admin/"):
                if target.query:
                    raise PolicyError(400, "管理接口不接受 URL 查询参数。")
                if path == "/admin/api/login" and method == "POST":
                    self.origin()
                    session = self.server.authentication.login(self.body(), self.headers.get("X-Real-IP", self.client_address[0]))
                    return self.answer(200, {"ok": True}, headers={"Set-Cookie": COOKIE + "=" + session + "; Path=/admin/; Max-Age=28800; HttpOnly; Secure; SameSite=Strict"})
                if path == "/admin/api/mode" and method == "GET":
                    return self.answer(200, {"mode": "live"})
                if path.startswith("/admin/api/"):
                    actor = self.server.authentication.actor(self.session())
                    if method == "POST":
                        self.origin()
                        data = self.body()
                        if path == "/admin/api/policies":
                            return self.answer(200, self.backend.save(data, actor))
                        if path == "/admin/api/logout":
                            self.server.authentication.logout(self.session())
                            return self.answer(200, {"ok": True}, headers={"Set-Cookie": COOKIE + "=; Path=/admin/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})
                        if path == "/admin/api/check":
                            return self.answer(200, self.backend.store.check(data.get("client_id"), data.get("account_id")))
                    elif path == "/admin/api/state":
                        return self.answer(200, self.backend.state())
                    elif path == "/admin/api/quota":
                        return self.answer(200, self.backend.account_quota())
                    raise PolicyError(404, "接口不存在。")
                static = {"/admin/": ("index.html", "text/html; charset=utf-8"),
                          "/admin/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/admin/style.css": ("style.css", "text/css; charset=utf-8")}
                if method == "GET" and path in static:
                    name, mime = static[path]
                    return self.answer(200, (ROOT / "web" / name).read_bytes(), mime)
                raise PolicyError(404, "页面不存在。")
            key = self.key()
            identity = self.backend.authenticate(key)
            allowed = self.backend.allowed(identity)
            if path == "/v1/quota" and method == "GET":
                if target.query:
                    raise PolicyError(400, "额度接口不接受账号选择参数。")
                return self.answer(200, self.backend.quota({a["id"] for a in allowed}))
            if path == "/v1/usage" and method == "GET":
                return self.forward_observer(key)
            usable = [a for a in allowed if a["enabled"] and a["route_ready"]]
            if path == "/v1/models" and method == "GET":
                with self.backend.lock:
                    models = sorted({m for a in usable for m in self.backend.models.get(a["id"], {})})
                if "anthropic-version" in self.headers:
                    return self.answer(200, {"data": [{"id": m, "type": "model", "display_name": m} for m in models],
                        "has_more": False, "first_id": models[0] if models else None, "last_id": models[-1] if models else None})
                return self.answer(200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "anthropic"} for m in models]})
            if path not in MODEL_PATHS or method != "POST":
                raise PolicyError(404, "端点不受支持。")
            if not usable:
                raise PolicyError(403, "这个 Key 没有可用的授权账号。")
            body = self.body(128 * 1024 * 1024)
            model = body.get("model")
            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:\[\]()\-]{1,180}", model):
                raise PolicyError(400, "使用不含账号前缀的模型名称。")
            base_model = re.sub(r"\([^()]*\)$", "", model)
            with self.backend.lock:
                candidates = [a for a in usable if base_model in self.backend.models.get(a["id"], {})]
            if not candidates:
                raise PolicyError(400, "获准账号不支持这个模型。")
            with self.server.sequence_lock:
                offset = self.server.sequence % len(candidates)
                self.server.sequence += 1
            candidates = candidates[offset:] + candidates[:offset]
            return self.forward_model(key, identity, body, model, candidates)
        except PolicyError as error:
            self.close_connection = True
            self.answer(error.status, {"type": "error", "error": {"type": "permission_error" if error.status in (401, 403) else "api_error", "message": error.message}})
        except (ValueError, TypeError, UnicodeError):
            self.close_connection = True
            self.answer(400, {"error": {"message": "请求格式不正确。"}})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            self.answer(503, {"error": {"message": "服务暂不可用，请稍后重试。"}})
        finally:
            if getattr(self, "reserved_bytes", 0):
                with self.server.body_lock:
                    self.server.body_bytes -= self.reserved_bytes
                    self.reserved_bytes = 0

    def forward_observer(self, key):
        con = http.client.HTTPConnection("127.0.0.1", self.backend.c.get("port", 8318), timeout=35)
        try:
            con.request("GET", self.path, headers={"Authorization": "Bearer " + key})
            response = con.getresponse()
            self.answer(response.status, response.read(16 * 1024 * 1024))
        finally:
            con.close()

    def forward_model(self, key, identity, body, model, candidates):
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept-Encoding": "identity"}
        for name, value in self.headers.items():
            lower = name.lower()
            if lower in ("accept", "user-agent", "x-app") or lower.startswith(("anthropic-", "x-stainless-", "x-claude-", "x-client-")):
                headers[name] = value
        for index, account in enumerate(candidates):
            # Every new attempt rechecks the committed policy. No fallback can widen its scope.
            if account["id"] not in {a["id"] for a in self.backend.allowed(identity) if a["enabled"] and a["route_ready"]}:
                continue
            connection = http.client.HTTPConnection(self.backend.host, self.backend.port, timeout=60)
            body["model"] = account["prefix"] + "/" + model
            try:
                connection.request("POST", self.path, json.dumps(body, ensure_ascii=False).encode(), headers)
                response = connection.getresponse()
                if response.status in (401, 403, 429, 503) and index < len(candidates) - 1:
                    response.read(1024 * 1024)
                    continue
                self.send_response(response.status)
                self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Transfer-Encoding", "chunked")
                for name, value in response.getheaders():
                    if name.lower() in ("retry-after", "request-id", "x-request-id") or name.lower().startswith("anthropic-ratelimit-"):
                        self.send_header(name, value)
                self.end_headers()
                # Once any response has been emitted, errors close the stream; never append a second HTTP response.
                try:
                    if connection.sock:
                        connection.sock.settimeout(3600)
                    while True:
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(("%x\r\n" % len(chunk)).encode() + chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except Exception:
                    self.close_connection = True
                return
            finally:
                connection.close()
        raise PolicyError(403, "账号权限已变更，请重新请求。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(Path(args.config).read_text())
    backend = Backend(config)
    backend.refresh()
    server = Server(("127.0.0.1", config.get("port", 8319)), backend)
    stop = threading.Event()
    def refresh():
        while not stop.wait(5):
            try:
                backend.refresh()
            except Exception:
                pass  # The freshness gate rejects routing after 20 seconds.
    threading.Thread(target=refresh, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
