"""Isolated routing probe: fake keys and loopback upstreams, no paid requests."""
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time


def run(binary):
    calls = []
    failure = {"a": False}

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            account = self.server.account
            calls.append(account)
            status = 429 if failure.get(account) else 200
            if status == 429:
                body = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "fixture"}}).encode()
            elif self.path.startswith("/v1/messages/count_tokens"):
                body = b'{"input_tokens":1}'
            elif data.get("stream"):
                body = ("event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {
                    "id": "fixture", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                    "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}}) +
                    "\n\nevent: message_stop\ndata: {\"type\":\"message_stop\"}\n\n").encode()
            else:
                body = json.dumps({"id": "fixture", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                    "content": [{"type": "text", "text": account}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if status == 200 and data.get("stream") else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstreams = []
    try:
        for account in ("a", "b"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            server.account = account
            threading.Thread(target=server.serve_forever, daemon=True).start()
            upstreams.append(server)
        with tempfile.TemporaryDirectory(prefix="cpa-route-probe-") as directory:
            root = Path(directory)
            (root / "auth").mkdir()
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            config = {"host": "127.0.0.1", "port": port, "auth-dir": str(root / "auth"),
                      "api-keys": ["fixture-client"], "request-retry": 1, "max-retry-interval": 1,
                      "usage-statistics-enabled": True,
                      "remote-management": {"disable-control-panel": True, "secret-key": "fixture-management-only"}, "claude-api-key": [
                          {"api-key": "fixture-" + account, "prefix": "pool-" + account,
                           "base-url": "http://127.0.0.1:" + str(server.server_port)}
                          for account, server in zip(("a", "b"), upstreams)]}
            config_path = root / "config.yaml"
            config_path.write_text(json.dumps(config))
            with (root / "process.log").open("wb") as log:
                process = subprocess.Popen([binary, "-config", str(config_path), "--local-model"],
                                           cwd=root, stdout=log, stderr=log)
                try:
                    def request(model=None, stream=False, path="/v1/messages"):
                        con = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
                        body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "fixture"}], "stream": stream}
                        con.request("POST" if model else "GET", path if model else "/v1/models",
                                    json.dumps(body) if model else None,
                                    {"Authorization": "Bearer fixture-client", "Content-Type": "application/json"})
                        response = con.getresponse()
                        code, raw = response.status, response.read()
                        con.close()
                        return code, raw
                    for _ in range(100):
                        if process.poll() is not None:
                            raise RuntimeError("isolated_cpa_did_not_start")
                        try:
                            if request()[0] == 200:
                                break
                        except OSError:
                            pass
                        time.sleep(.1)
                    report = []
                    for model, stream, path, want in [
                        ("pool-a/claude-sonnet-5-5", False, "/v1/messages", "a"),
                        ("pool-b/claude-sonnet-5-5", True, "/v1/messages", "b"),
                        ("pool-a/claude-sonnet-5-5", False, "/v1/messages/count_tokens", "a")]:
                        calls.clear()
                        code, _ = request(model, stream, path)
                        assert code == 200 and (set(calls) == {want} or (not calls and path.endswith("count_tokens"))), (path, code, calls)
                        report.append({"case": path + (" stream" if stream else ""), "status": code, "selected": sorted(set(calls))})
                    failure["a"] = True
                    calls.clear()
                    code, _ = request("pool-a/claude-sonnet-5-5")
                    assert code >= 400 and "b" not in calls, (code, calls)
                    report.append({"case": "rate_limit_no_escape", "status": code, "selected": sorted(set(calls))})
                    calls.clear()
                    code, _ = request("pool-missing/claude-sonnet-5-5")
                    assert code >= 400 and not calls, (code, calls)
                    report.append({"case": "unknown_prefix_denied", "status": code, "upstream_calls": len(calls)})
                    # Only this isolated instance is drained, never the production queue.
                    events = []
                    for _ in range(20):
                        con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                        con.request("GET", "/v8/management/observability/usage/queue?count=100",
                                    headers={"Authorization": "Bearer fixture-management-only"})
                        response = con.getresponse()
                        assert response.status == 200, "isolated_usage_queue_unavailable"
                        events.extend(json.loads(response.read()))
                        con.close()
                        if len(events) >= 3:
                            break
                        time.sleep(.05)
                    successful = [e for e in events if not e.get("failed")]
                    assert successful, "missing_isolated_usage_events"
                    assert all(e.get("api_key") == "fixture-client" for e in successful), "client_metering_changed"
                    assert all(e.get("model") == "claude-sonnet-5-5" for e in successful), "metering_model_changed"
                    assert all(e.get("auth_index") for e in successful), "missing_account_metering"
                    report.append({"case": "metering_identity", "events": len(events), "successful": len(successful),
                                   "original_client_key": True, "unprefixed_model": True, "account_attribution": True})
                    return report
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
    finally:
        for server in upstreams:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    import sys
    print(json.dumps(run(sys.argv[1]), ensure_ascii=False))
