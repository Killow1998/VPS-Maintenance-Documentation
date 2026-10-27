"""Deploy on an already configured VPS. Input is supplied privately over SSH."""
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def command(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=45).stdout


def restricted_client_id(clients, selector):
    if not isinstance(selector, str) or not selector.strip():
        raise RuntimeError("Restricted Key selector is required; no policy applied")
    matched = [c["id"] for c in clients if selector in (c["id"], c["name"])]
    if len(matched) != 1:
        raise RuntimeError("Restricted Key not found or ambiguous; use its full ID")
    return matched[0]


def initial_policy(state, selector, email):
    client_id = restricted_client_id(state["clients"], selector)
    accounts = [a["id"] for a in state["accounts"] if a["enabled"]]
    if not accounts or any(a["enabled"] and not a["route_ready"] for a in state["accounts"]):
        raise RuntimeError("Incomplete account isolation routes; Nginx unchanged")
    if not isinstance(email, str) or not email.strip():
        raise RuntimeError("Restricted account identity is required; no policy applied")
    matched = [a for a in state["accounts"] if a["name"].casefold() == email.casefold()]
    if len(matched) != 1 or not matched[0]["enabled"]:
        raise RuntimeError("Restricted account identity not unique or disabled; Nginx unchanged")
    return {"expected_revision": state["revision"], "clients": [
        {"id": c["id"], "enabled": True,
         "account_ids": [matched[0]["id"]] if c["id"] == client_id else list(accounts)}
        for c in state["clients"]]}


def main():
    import pwd

    os.umask(0o077)
    job = json.load(sys.stdin)
    cfg = job["deployment"]
    scope = {"__name__": "cpa_tools_deploy"}
    exec(job["remote_admin"], scope)
    observer_path = Path(cfg["observer_config"])
    observer = json.loads(observer_path.read_text())
    hostname = urlsplit(cfg["base_url"]).hostname
    if not hostname or cfg["base_url"] != "https://" + hostname:
        raise RuntimeError("Expected HTTPS origin without path")
    site = Path("/etc/nginx/sites-enabled") / hostname
    includes = Path("/etc/nginx/cpa-observer-locations.conf")
    if not site.is_file() or not includes.is_file():
        raise RuntimeError("Expected existing Nginx files missing")
    resume = job.get("resume_backup")
    if Path("/etc/cpa-admin/config.json").exists() and not resume:
        raise RuntimeError("Existing control deployment found; use an explicit update workflow")
    nginx = site.read_text()
    if nginx.count("proxy_pass http://127.0.0.1:8317;") != 1:
        raise RuntimeError("Unexpected Nginx upstream; no changes made")
    admin = scope["Admin"]({**cfg, "action": "control_sync_routes"})
    clients = admin.records(admin.keys())
    restricted_id = restricted_client_id(clients, job["restricted_key"])
    stamp = dt.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    backup = Path(resume) if resume else Path("/var/backups/cpa-admin") / (stamp + "-initial")
    if resume:
        if backup.parent != Path("/var/backups/cpa-admin") or not backup.is_dir():
            raise RuntimeError("Invalid recovery backup")
        if nginx != (backup / "nginx-site.conf").read_text() or includes.read_text() != (backup / "observer-locations.conf").read_text():
            raise RuntimeError("Routing changed since backup; inspect before retry")
        saved = json.loads((backup / "baseline.json").read_text())
        baseline, records = saved["pids"], saved["records"]
    else:
        backup.mkdir(parents=True, mode=0o700)
        for name, path in (("nginx-site.conf", site), ("observer-locations.conf", includes),
                           ("observer-config.private.json", observer_path), ("cpa-config.private.yaml", Path("/opt/CLIProxyAPI/config.yaml"))):
            shutil.copyfile(path, backup / name)
        scope["sqlite_snapshot"](observer["database"], backup / "usage.sqlite")
        with sqlite3.connect(backup / "usage.sqlite") as snapshot:
            records = snapshot.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        baseline = {unit: command("systemctl", "show", unit, "-p", "MainPID", "--value").strip()
                    for unit in ("cliproxyapi", "cpa-observer", "xboard-node")}
        (backup / "baseline.json").write_text(json.dumps({"pids": baseline, "records": records}))
    source_root = Path("/opt/cpa-admin")
    source_root.mkdir(mode=0o755, exist_ok=True)
    source_root.chmod(0o755)
    for name, text in job["files"].items():
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError("Invalid source path")
        target = source_root / rel
        target.parent.mkdir(mode=0o755, exist_ok=True)
        target.parent.chmod(0o755)
        target.write_text(text)
        target.chmod(0o644)
    account = pwd.getpwnam("cpa-observer")
    data_root = Path("/var/lib/cpa-admin")
    data_root.mkdir(mode=0o700, exist_ok=True)
    os.chown(data_root, account.pw_uid, account.pw_gid)
    config_root = Path("/etc/cpa-admin")
    config_root.mkdir(mode=0o750, exist_ok=bool(resume))
    config_root.chmod(0o750)
    os.chown(config_root, 0, account.pw_gid)
    control = {"origin": cfg["base_url"], "observer_config": str(observer_path),
               "observer_script": cfg["observer_script"], "database": str(data_root / "control.sqlite"),
               "port": 8319, "active": True}
    control_path = config_root / "config.json"
    control_path.write_text(json.dumps(control))
    control_path.chmod(0o640)
    os.chown(control_path, 0, account.pw_gid)
    shutil.copyfile(source_root / "cpa-admin.service", "/etc/systemd/system/cpa-admin.service")
    os.chmod("/etc/systemd/system/cpa-admin.service", 0o644)
    command("systemctl", "daemon-reload")
    command("systemctl", "enable", "cpa-admin")
    command("systemctl", "restart", "cpa-admin")

    def rpc(path, data=None, key=None, method=None, port=8319):
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            con.request(method or ("POST" if data is not None else "GET"), path,
                        json.dumps(data) if data is not None else None,
                        {"Authorization": "Bearer " + (key or observer["management_key"]), "Content-Type": "application/json"})
            response = con.getresponse()
            raw = response.read()
            if response.status != 200:
                raise RuntimeError("Control preflight HTTP " + str(response.status))
            return json.loads(raw)
        finally:
            con.close()

    for _ in range(80):
        try:
            rpc("/healthz")
            break
        except (OSError, RuntimeError):
            time.sleep(.25)
    else:
        raise RuntimeError("Control service did not start; Nginx unchanged")
    # Reuse the same backed-up route provisioning used by the TUI.
    routes = admin.control_operation()
    initial = rpc("/internal/state")
    if {c["id"] for c in initial["clients"]} != {c["id"] for c in clients}:
        raise RuntimeError("Key inventory changed during deployment; no policy applied")
    if restricted_client_id(initial["clients"], job["restricted_key"]) != restricted_id:
        raise RuntimeError("Restricted Key changed during deployment; no policy applied")
    payload = initial_policy(initial, restricted_id, job["restricted_email"])
    applied = rpc("/internal/policies", payload)
    keys = admin.keys()
    for key in keys:
        result = rpc("/v1/models", key=key)
        if not any(m["id"] == "claude-sonnet-5-5" for m in result["data"]):
            raise RuntimeError("Model catalog preflight failed; Nginx unchanged")
    (backup / "initial-policy.json").write_text(json.dumps(payload))
    rollback = backup / "rollback-routing.py"
    rollback.write_text('''#!/usr/bin/python3
import pathlib, shutil, subprocess
root = pathlib.Path(__file__).resolve().parent
site = pathlib.Path(''' + repr(str(site.resolve())) + ''')
include = pathlib.Path(''' + repr(str(includes)) + ''')
current_site, current_include = site.read_bytes(), include.read_bytes()
shutil.copyfile(root / "nginx-site.conf", site)
shutil.copyfile(root / "observer-locations.conf", include)
test = subprocess.run(["nginx", "-t"], capture_output=True)
if test.returncode:
    site.write_bytes(current_site); include.write_bytes(current_include)
    raise SystemExit("Nginx validation failed; current routing retained")
subprocess.run(["systemctl", "reload", "nginx"], check=True)
print("Previous routing restored. Current databases, Key and OAuth credentials retained. Previous shared-pool access restored.")
''')
    rollback.chmod(0o700)
    admin_locations = '''
    location = /admin { return 308 /admin/; }
    location ^~ /admin/ {
        proxy_pass http://127.0.0.1:8319;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Connection "";
        proxy_http_version 1.1;
        proxy_read_timeout 40s;
        client_max_body_size 128k;
        proxy_buffering off;
    }
    location ^~ /internal/ { return 404; }
    location = /healthz { return 404; }
    location ^~ /v0/management { return 404; }
    location ^~ /v8/management { return 404; }
    location = /management.html { return 404; }
'''
    marker = "    location / {\n        proxy_pass http://127.0.0.1:8317;"
    if nginx.count(marker) != 1:
        raise RuntimeError("Nginx insertion point not unique; Nginx unchanged")
    updated = nginx.replace(marker, admin_locations + "\n" + marker).replace("proxy_pass http://127.0.0.1:8317;", "proxy_pass http://127.0.0.1:8319;")
    try:
        site.write_text(updated)
        includes.write_text(includes.read_text().replace("127.0.0.1:8318", "127.0.0.1:8319"))
        command("nginx", "-t")
        command("systemctl", "reload", "nginx")
    except Exception:
        command("python3", str(rollback))
        raise RuntimeError("Routing change failed; prior Nginx routing restored") from None
    result = {"backup": str(backup), "route_backup": routes.get("backup"), "rollback": str(rollback),
              "policy_revision": applied["revision"], "keys": len(keys), "accounts": sum(a["enabled"] for a in initial["accounts"]),
              "restricted_key": job["restricted_key"], "prior_records": records, "original_pids": baseline,
              "current_pids": {u: command("systemctl", "show", u, "-p", "MainPID", "--value").strip() for u in baseline},
              "source_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in job["files"].items()}}
    (backup / "deployment-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": str(error) if type(error) is RuntimeError else type(error).__name__}))
        sys.exit(1)
