"""Read-only production acceptance. Never sends a valid model generation request."""
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import sqlite3
import subprocess
from urllib.parse import urlsplit


def main():
    config = json.loads(Path("/etc/cpa-admin/config.json").read_text())
    observer = json.loads(Path(config["observer_config"]).read_text())
    origin = config["origin"]
    host = urlsplit(origin).hostname

    def request(path, key=None, data=None, port=None, headers=None):
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=35) if port else http.client.HTTPSConnection(host, timeout=35)
        h = {"Content-Type": "application/json", **(headers or {})}
        if key: h["Authorization"] = "Bearer " + key
        try:
            con.request("POST" if data is not None else "GET", path, json.dumps(data) if data is not None else None, h)
            response = con.getresponse()
            body, hs = response.read(16 * 1024 * 1024), dict(response.getheaders())
            try: parsed = json.loads(body)
            except ValueError: parsed = None
            return response.status, parsed, hs
        finally: con.close()

    management = observer["management_key"]
    keys = request("/v8/management/config/access/api-keys", management, port=8317)[1]
    state = request("/internal/state", management, port=8319)[1]
    result = {"revision": state["revision"], "collection": state.get("collection"), "accounts": [
        {"id": a["id"], "route_ready": a["route_ready"], "quota_windows": len(a["windows"])} for a in state["accounts"]], "keys": {}}
    for key in keys:
        identity = hmac.new(observer["hmac_secret"].encode(), ("client:" + key).encode(), hashlib.sha256).hexdigest()
        name = observer["labels"].get(identity, identity[:10])
        models = request("/v1/models", key)
        quota = request("/v1/quota", key)
        usage = {period: request("/v1/usage?period=" + period, key)[0] for period in ("1h", "7d", "30d", "all", "day", "month")}
        result["keys"][name] = {"models_http": models[0], "sonnet_5_5": bool(models[1]) and any(m["id"] == "claude-sonnet-5-5" for m in models[1].get("data", [])),
                               "quota_http": quota[0], "account_ids": [a["id"] for a in (quota[1] or {}).get("accounts", [])], "usage_http": usage}
    result["boundary_http"] = {p: request(p)[0] for p in ("/admin/", "/admin/api/state", "/v1/models", "/v1/quota", "/v1/usage", "/healthz", "/internal/state")}
    result["management_with_real_management_key"] = request("/v8/management/config", management)[0]
    result["admin_with_client_key"] = request("/admin/api/state", keys[0])[0]
    # Slash is rejected by the gateway before forwarding; this cannot consume model tokens.
    result["client_prefix_injection"] = request("/v1/messages", keys[0], {"model": "forbidden/claude-sonnet-5-5", "messages": []})[0]
    ticket = request("/internal/login-ticket", management, {}, port=8319)[1]["url"].split("#ticket=", 1)[1]
    login = request("/admin/api/login", data={"ticket": ticket}, headers={"Origin": origin})
    cookie = login[2].get("Set-Cookie", "").split(';', 1)[0]
    result["login"] = {"ticket": login[0], "reuse": request("/admin/api/login", data={"ticket": ticket}, headers={"Origin": origin})[0],
                       "state": request("/admin/api/state", headers={"Cookie": cookie})[0],
                       "csrf": request("/admin/api/policies", data={}, headers={"Cookie": cookie, "Origin": "https://invalid.example"})[0]}
    request("/admin/api/logout", data={}, headers={"Cookie": cookie, "Origin": origin})
    result["login"]["logged_out"] = request("/admin/api/state", headers={"Cookie": cookie})[0]
    with sqlite3.connect("file:" + observer["database"] + "?mode=ro", uri=True) as con:
        result["records"] = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        result["collector"] = dict(con.execute("SELECT key,value FROM metadata WHERE key IN ('collector_status','collector_last_success','collection_started_at')"))
    result["services"] = {u: subprocess.check_output(["systemctl", "show", u, "-p", "MainPID", "--value"], text=True).strip()
                          for u in ("cliproxyapi", "cpa-observer", "xboard-node", "cpa-admin")}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()
