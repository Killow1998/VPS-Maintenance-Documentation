#!/usr/bin/env python3
"""XBoard terminal TUI over SSH. Python 3.10+, standard library only.

Run without a subcommand for the menu; use --help for automation-friendly reads.
No agent, daemon, package, or script is installed on the VPS. Writes require
interactive confirmation. No client credentials are written to local files.
"""

import sys

if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required. No connection or configuration changes were made.")

import argparse
import base64
import contextlib
import datetime as dt
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import re
import queue
import secrets
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import webbrowser


class Error(Exception):
    pass


BRIDGE = r'''import base64,json,re,shlex,signal,subprocess,sys,threading
cleanups={}
lock=threading.Lock()
def cleanup(container,name):
 code='require "/www/vendor/autoload.php";$app=require "/www/bootstrap/app.php";'
 code+='$app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap();'
 code+='Laravel\\Sanctum\\PersonalAccessToken::where("name",base64_decode("'
 code+=base64.b64encode(name.encode()).decode()+'"))->delete();'
 try:
  result=subprocess.run(['docker','exec','-w','/www',container,'php','-r',code],
   capture_output=True,timeout=20)
  if result.returncode==0:
   with lock:cleanups.pop((container,name),None)
 except Exception:pass
def register(item):
 container,name=item['container'],item['name']
 if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',container):raise ValueError('Container')
 if not re.fullmatch(r'vpsctl:[0-9a-f]{32}',name):raise ValueError('Session name')
 timer=threading.Timer(max(1,min(900,item.get('delay',900))),cleanup,args=(container,name))
 timer.daemon=True
 with lock:
  if len(cleanups)>=64:raise ValueError('Too many sessions')
  cleanups[(container,name)]=timer
 timer.start()
def stopped(*args):raise SystemExit(0)
for key in ['SIGTERM','SIGHUP']:
 if hasattr(signal,key):signal.signal(getattr(signal,key),stopped)
print(json.dumps({'ready':True}),flush=True)
try:
 for line in sys.stdin:
  request={}
  try:
   if len(line)>2*1024*1024:raise ValueError('Request too large')
   request=json.loads(line)
   if request.get('cleanup'):register(request['cleanup'])
   result=subprocess.run(shlex.split(request['command']),input=request.get('data'),
    capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=min(55,request.get('timeout',55)))
   code=result.returncode
   output=result.stdout
   if len(output)>2*1024*1024:code,output=74,''
  except Exception:
   code,output=70,''
  print(json.dumps({'id':request.get('id'),'code':code,'stdout':output}),flush=True)
finally:
 with lock:pending=list(cleanups.items())
 for (container,name),timer in pending:
  timer.cancel()
  cleanup(container,name)
'''


def quota_bytes(text):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(GB|GiB|TB|TiB)", text, re.I)
    if not match:
        raise Error("额度必须带单位，例如 50GB 或 50GiB；两者不同。")
    number, unit = match.groups()
    value = Decimal(number) * {"gb": 10**9, "gib": 2**30,
                               "tb": 10**12, "tib": 2**40}[unit.lower()]
    if not 0 < value <= 2**63 - 1 or value != int(value):
        raise Error("额度必须为正的整数字节，且不超过有符号 64 位整数。")
    return int(value)


def size(value):
    value = int(value or 0)
    return f"{value / 2**30:.2f} GiB ({value / 10**9:.2f} GB)"


def stamp(value):
    return dt.datetime.fromtimestamp(int(value), dt.timezone.utc).isoformat() if value else None


def safe_user(row):
    keys = ("id", "email", "plan_id", "group_id", "banned", "is_admin", "is_staff",
            "transfer_enable", "u", "d", "expired_at", "next_reset_at", "reset_count")
    result = {key: row.get(key) for key in keys}
    result["used_bytes"] = int(row.get("u") or 0) + int(row.get("d") or 0)
    result["remaining_bytes"] = max(0, int(row.get("transfer_enable") or 0) - result["used_bytes"])
    return result


def select_one(rows, selector, kind="用户"):
    matches = [r for r in rows if str(r["id"]) == str(selector)
               or r.get("email") == selector or r.get("code") == selector
               or r.get("name") == selector
               or r.get("email", "").split("@")[0] == selector]
    if len(matches) != 1:
        raise Error(f"{kind}需唯一匹配；找到 {len(matches)} 项。请使用列表中的 ID。")
    return matches[0]


def protect_user(row):
    if row.get("is_admin") or row.get("is_staff"):
        raise Error("此工具禁止修改、重置或删除管理员和员工账号。")


def confirm(phrase, dry_run=False, reader=None):
    if dry_run:
        print("[预演] 未执行业务修改，也未创建备份。")
        return False
    if not sys.stdin.isatty():
        raise Error("修改操作要求交互终端；不能通过管道或定时任务绕过确认。")
    required = phrase if phrase.startswith("DELETE ") else "y"
    if (reader or input)(f"{phrase}\n输入 {required} 确认，其他输入取消：").strip() != required:
        print("已取消。")
        return False
    return True


def ssh_binary():
    if os.name == "nt":
        native = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/OpenSSH/ssh.exe"
        if native.is_file():
            return str(native)
    path = shutil.which("ssh")
    if not path:
        raise Error("找不到 OpenSSH 客户端。先配置系统 ssh 和免密登录。")
    return path


class SSH:
    def __init__(self, args):
        self.args = args
        self.bridge = None
        self._access = None
        self.session_name = "vpsctl:" + secrets.token_hex(16)
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:\-]*", args.host):
            raise Error("SSH 地址只能是主机名、SSH 别名或 user@host，不能包含命令或选项。")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.container):
            raise Error("容器名称无效。")
        self.base = [ssh_binary(), "-p", str(args.port), "-o", "BatchMode=yes", "-o",
                     "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o",
                     "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", "-o", "LogLevel=ERROR"]
        if args.identity:
            self.base += ["-i", str(Path(args.identity).expanduser())]

    def run(self, command, data=None, timeout=60):
        if self.bridge:
            return self.bridge.run(command, data, timeout)
        try:
            result = subprocess.run(self.base + [self.args.host, command], input=data,
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise Error("SSH 超时。若已提交修改，结果可能不确定；先查询，勿重复提交。") from exc
        if result.returncode:
            raise Error(f"SSH 命令失败（退出码 {result.returncode}）。核对免密、主机指纹和权限；未自动重试。")
        return result.stdout

    def python(self, source):
        return json.loads(self.run("python3 -B -", source))

    def php(self, source, cleanup=None):
        command = shlex.join(["docker", "exec", "-i", "-w", "/www", self.args.container, "php"])
        bootstrap = '<?php require "/www/vendor/autoload.php"; $app = require "/www/bootstrap/app.php"; '
        bootstrap += '$app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap(); '
        result = self.bridge.run(command, bootstrap + source, cleanup=cleanup) if cleanup else self.run(command, bootstrap + source)
        # Startup warnings are not JSON and must not accidentally expose app data.
        try:
            return json.loads(result)
        except json.JSONDecodeError as exc:
            raise Error("应用返回了非 JSON 内容；已隐藏原文。请检查 PHP 启动日志和版本兼容性。") from exc

    def metadata(self):
        return self.php('echo json_encode(["admin_path"=>admin_setting("secure_path", '
                        'admin_setting("frontend_admin_path", hash("crc32b",config("app.key")))), '
                        '"timezone"=>config("app.timezone"), "db"=>config("database.default"), '
                        '"reset_method"=>(int)admin_setting("reset_traffic_method",1)]);')

    def access(self):
        if self._access and self._access["expires_at"] > time.time() + 60:
            return self._access
        self.revoke_access()
        if not self.bridge:
            raise Error("短期管理授权必须使用受控 SSH 连接。")
        self.session_name = "vpsctl:" + secrets.token_hex(16)
        # SSH Docker access already grants control of this application. Create a
        # named, expiring native session, without reading or changing passwords.
        name = base64.b64encode(self.session_name.encode()).decode()
        self._access = self.php('$u=App\\Models\\User::where("is_admin",1)->where("banned",0)->orderBy("id")->firstOrFail();'
            '$expiry=now()->addMinutes(15);$t=$u->createToken(base64_decode("' + name + '"),["*"],$expiry);'
            'echo json_encode(["admin_path"=>admin_setting("secure_path",admin_setting("frontend_admin_path",'
            'hash("crc32b",config("app.key")))),"timezone"=>config("app.timezone"),'
            '"db"=>config("database.default"),"reset_method"=>(int)admin_setting("reset_traffic_method",1),'
            '"authorization"=>"Bearer ".$t->plainTextToken,"session_id"=>(int)$t->accessToken->id,'
            '"expires_at"=>$expiry->timestamp]);', cleanup={"container":self.args.container,"name":self.session_name,"delay":900})
        return self._access

    def revoke_access(self):
        if self._access:
            identifier = int(self._access["session_id"])
            name = base64.b64encode(self.session_name.encode()).decode()
            self.php('$n=Laravel\\Sanctum\\PersonalAccessToken::whereKey(' + str(identifier) + ')'
                     '->where("name",base64_decode("' + name + '"))->delete();'
                     'echo json_encode(["removed"=>$n]);')
            self._access = None

    def backup(self):
        if self.args.no_backup:
            print("警告：按 --no-backup 跳过本次备份。")
            return
        path = self.args.database or self.args.app_dir.rstrip("/") + "/.docker/.data/database.sqlite"
        result = self.python('''import contextlib,datetime,json,os,pathlib,secrets,shutil,sqlite3
from urllib.parse import quote
os.umask(0o077)
source=pathlib.Path(''' + repr(path) + ''').resolve(strict=True)
with source.open('rb') as f:
 if f.read(16)!=b'SQLite format 3\\0': raise RuntimeError('Expected SQLite')
folder=pathlib.Path.home()/'xboard-cli-backups'
folder.mkdir(mode=0o700,exist_ok=True)
if folder.is_symlink(): raise RuntimeError('Backup directory must not be a symlink')
os.chmod(folder,0o700)
if shutil.disk_usage(folder).free < max(source.stat().st_size*3,100*1024*1024):
 raise RuntimeError('Not enough disk space for a safe backup')
name=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(4)+'.sqlite'
target=folder/name
try:
 with contextlib.closing(sqlite3.connect('file:'+quote(str(source))+'?mode=ro',uri=True,timeout=10)) as src:
  with contextlib.closing(sqlite3.connect(target)) as dst:
   src.backup(dst)
   if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise RuntimeError('Backup check failed')
except BaseException:
 target.unlink(missing_ok=True)
 raise
print(json.dumps({'backup':str(target)}))
''')
        print("数据库备份：" + result["backup"] + "（私有；不会自动删除旧备份）")

    def status(self):
        container = repr(self.args.container)
        unit = repr(self.args.node_service)
        return self.python('''import json,subprocess,os,datetime
def call(args):
 r=subprocess.run(args,capture_output=True,text=True,timeout=15)
 return r.stdout.strip() if r.returncode==0 else 'unavailable'
raw=call(['docker','inspect','--format','{{json .State}}',''' + container + '''])
state=json.loads(raw) if raw!='unavailable' else {}
memory={}
for l in open('/proc/meminfo'):
 k,v=l.split(':',1)
 if k in ['MemAvailable','MemTotal','SwapTotal','SwapFree']:memory[k]=int(v.split()[0])*1024
print(json.dumps({'at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'load':os.getloadavg(),'memory_bytes':memory,
 'panel':{k:state.get(k) for k in ['Status','OOMKilled','StartedAt']},
 'node':call(['systemctl','is-active',''' + unit + ''']),
 'nginx':call(['systemctl','is-active','nginx']),
 'pool_timer':call(['systemctl','show','xboard-reset-node-pool.timer','-p','ActiveState','-p','NextElapseUSecRealtime']),
 'note':'Local component status only; not an end-to-end forwarding test.'}))
''')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base, admin_path, access_provider=None, access=None):
        self.base = base
        self.prefix = "/api/v2/" + urllib.parse.quote(admin_path.strip("/"), safe="")
        self.auth = access.get("authorization") if access else None
        self.expires_at = access.get("expires_at", 0) if access else 0
        self.access_provider = access_provider
        self._users = None
        self.users_updated_at = 0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, path, payload=None):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.auth:
            headers["Authorization"] = self.auth
        request = urllib.request.Request(self.base + path,
                                         data=None if payload is None else json.dumps(payload).encode(),
                                         headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise Error("API 响应超过安全读取上限。")
            result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise Error(f"API HTTP {exc.code}；已隐藏响应正文。请核对输入、权限和版本。") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise Error("面板连接失败；若是修改请求，先查询结果，不自动重试。") from exc
        except json.JSONDecodeError as exc:
            raise Error("面板返回非 JSON，检查面板路径和版本。") from exc
        if not isinstance(result, dict) or result.get("status") == "fail":
            raise Error("面板拒绝请求；响应正文未输出。")
        return result

    def admin(self, path, payload=None):
        self.ensure_auth()
        if payload is not None:
            self._users = None
        return self.call(self.prefix + path, payload)

    def ensure_auth(self):
        if self.auth and self.expires_at > time.time() + 60:
            return
        if not self.access_provider:
            raise Error("没有可用的 SSH 授权；不会退回密码登录。")
        access = self.access_provider()
        self.auth, self.expires_at = access["authorization"], access["expires_at"]

    def users(self, force=False):
        if self._users is not None and not force:
            return self._users
        rows = []
        for page in range(1, 1001):
            result = self.admin(f"/user/fetch?current={page}&pageSize=100")
            rows.extend(result["data"])
            if page >= result.get("last_page", 1):
                self._users = rows
                self.users_updated_at = time.time()
                return rows
        raise Error("用户页数超过上限，已停止，未返回不完整列表。")


class Bridge:
    def __init__(self, process):
        self.process = process
        self.responses = queue.Queue(maxsize=2)
        self.sequence = 0
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        try:
            while line := self.process.stdout.readline(3 * 1024 * 1024):
                self.responses.put(json.loads(line), timeout=1)
        except (ValueError, OSError, queue.Full):
            pass
        finally:
            with contextlib.suppress(queue.Full):
                self.responses.put_nowait({"disconnected": True})

    def receive(self, timeout):
        try:
            response = self.responses.get(timeout=timeout)
        except queue.Empty as exc:
            raise Error("SSH 请求超时；修改结果可能不确定，先查询，不自动重试。") from exc
        if response.get("disconnected"):
            raise Error("SSH 连接已断开；先核对结果，不自动重复修改。")
        return response

    def run(self, command, data=None, timeout=60, cleanup=None):
        with self.lock:
            self.sequence += 1
            request = {"id": self.sequence, "command": command, "data": data, "timeout": min(timeout,55)}
            if cleanup:
                request["cleanup"] = cleanup
            try:
                self.process.stdin.write(json.dumps(request) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise Error("SSH 连接不可写；修改请求不自动重试。") from exc
            response = self.receive(timeout)
            if response.get("id") != self.sequence or response.get("code") != 0:
                raise Error("远端请求未通过；已隐藏错误正文。请检查状态，不自动重试。")
            return response["stdout"]


@contextlib.contextmanager
def tunnel(ssh):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    code = base64.b64encode(BRIDGE.encode()).decode()
    command = shlex.join(["python3", "-u", "-c", "exec(__import__('base64').b64decode('" + code + "'))"])
    process = subprocess.Popen(ssh.base + ["-o", "ExitOnForwardFailure=yes", "-L",
                              f"127.0.0.1:{port}:127.0.0.1:{ssh.args.panel_port}", ssh.args.host, command],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", errors="strict")
    bridge = Bridge(process)
    try:
        if not bridge.receive(20).get("ready"):
            raise Error("SSH 连接初始化失败。")
        ssh.bridge = bridge
        yield f"http://127.0.0.1:{port}"
    finally:
        try:
            ssh.revoke_access()
        except Error:
            print("警告：本次会话未能撤销；旧版面板可能不校验到期时间，请检查 vpsctl 命名会话。不会删除其他设备会话。", file=sys.stderr)
        ssh.bridge = None
        if process.stdin:
            with contextlib.suppress(OSError):
                process.stdin.close()
        try:
            process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        bridge.reader.join(timeout=1)
        if process.stdout:
            process.stdout.close()


class Console:
    def __init__(self, args, ssh, base, meta):
        self.args, self.ssh, self.base, self.meta = args, ssh, base, meta
        self.api = API(base, args.admin_path or meta.get("admin_path", ""), ssh.access, meta)
        self.ui = None

    def read(self, prompt):
        if self.ui:
            return self.ui.read(prompt)
        return input(prompt)

    def login(self):
        self.api.ensure_auth()

    def user(self, selector, fresh=False):
        return select_one(self.api.users(force=fresh), selector)

    def show(self, rows):
        if self.args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        if self.ui:
            if isinstance(rows, dict) and "panel" in rows:
                memory = rows["memory_bytes"]
                print(f"面板   {rows['panel']['Status']}    节点   {rows['node']}    nginx   {rows['nginx']}")
                print(f"负载   {' / '.join(f'{x:.2f}' for x in rows['load'])}")
                print(f"可用内存   {memory.get('MemAvailable', 0) / 2**20:.0f} MiB")
                print(f"已用 Swap  {(memory.get('SwapTotal',0)-memory.get('SwapFree',0)) / 2**20:.0f} MiB")
                print("\n总池月度任务\n" + rows["pool_timer"])
                print("\n本地组件状态不代表端到端转发已通过。")
                return
            users = rows if isinstance(rows, list) else [rows]
            if users and all("email" in row for row in users):
                widths = [4, max(10, self.ui.width - 53), 9, 9, 9, 8]
                def line(values):
                    return " ".join(pad(str(value), width) for value, width in zip(values, widths))
                print(line(["ID", "用户", "已用GiB", "配额GiB", "剩余GiB", "状态"]))
                print("-" * (self.ui.width - 5))
                for row in users:
                    state = "管理员" if row.get("is_admin") else "停用" if row.get("banned") else "到期" if (
                        row.get("expired_at") and int(row["expired_at"]) <= time.time()
                    ) else "用尽" if not row["remaining_bytes"] else "正常"
                    print(line([row["id"], row["email"], f"{row['used_bytes']/2**30:.2f}",
                                f"{int(row['transfer_enable'] or 0)/2**30:.2f}",
                                f"{row['remaining_bytes']/2**30:.2f}", state]))
                if not isinstance(rows, list):
                    print("\n下次重置(UTC)：" + str(stamp(rows.get("next_reset_at"))))
                return
        if not isinstance(rows, list):
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        for row in rows:
            if "email" in row:
                print(f"#{row['id']} {row['email']}  已用 {size(row['used_bytes'])} / {size(row['transfer_enable'])}")
                print(f"  剩余 {size(row['remaining_bytes'])}  停用={bool(row['banned'])}  "
                      f"下次重置(UTC)={stamp(row['next_reset_at'])}")
            else:
                print(json.dumps(row, ensure_ascii=False))

    def approve(self, phrase):
        if not confirm(phrase, self.args.dry_run, self.read):
            return False
        if self.meta.get("db") != "sqlite" and not self.args.no_backup:
            raise Error("自动备份仅支持 SQLite；此数据库类型需另行安排备份。")
        if self.ui and self.ui.pending:
            self.ui.pending.seek(0)
            self.ui.pending.truncate()
            print("操作：" + phrase)
        self.ssh.backup()
        return True

    def update(self, row, fields):
        # Current controller otherwise clears invite_user_id even for quota-only updates.
        invited = row.get("invite_user") or {}
        payload = {"id": row["id"], **fields}
        if row.get("invite_user_id"):
            if not invited.get("email"):
                raise Error("无法保留邀请关系；请在 XBoard 后台修改该用户。")
            payload["invite_user_email"] = invited["email"]
        self.api.admin("/user/update", payload)

    def action(self, command, params):
        if command == "status":
            self.show(self.ssh.status())
            return
        if command == "web":
            url = self.base + "/" + urllib.parse.quote(self.args.admin_path or self.meta["admin_path"], safe="")
            print("本机管理入口：" + url + "\n保持终端开启；关闭后本次隧道自动结束。")
            webbrowser.open(url)
            self.read("使用完毕后按 Enter 返回：")
            return
        if command == "restart":
            self.restart(params["target"])
            return
        self.login()
        if command == "users":
            self.show([safe_user(r) for r in self.api.users()])
        elif command == "plans":
            rows = self.api.admin("/plan/fetch")["data"]
            self.show([{k: p.get(k) for k in ("id", "name", "group_id", "transfer_enable", "reset_traffic_method")}
                       for p in rows])
            if not self.args.json:
                print("套餐额度单位为 GiB；reset_traffic_method=0 表示每月一日，null 跟随系统。")
        elif command == "link":
            row = self.user(params["user"])
            url = row.get("subscribe_url")
            if not url or not url.startswith("https://"):
                raise Error("面板未返回 HTTPS 订阅地址；先核对 subscribe_url 设置。")
            print(url)  # Explicit link request only; never included in status/list output.
        elif command == "create":
            email = params["email"].strip().lower()
            if not re.fullmatch(r"[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", email):
                raise Error("请提供完整邮箱，例如 friend@example.com。")
            if any(r.get("email") == email for r in self.api.users()):
                raise Error("该邮箱已存在，不会覆盖或重复创建。")
            plans = self.api.admin("/plan/fetch")["data"]
            if not params.get("plan"):
                if len(plans) != 1:
                    raise Error("存在多个套餐，请在 TUI 中选择；不会猜测套餐。")
                plan = plans[0]
            else:
                plan = select_one(plans, params["plan"], "套餐")
            method = plan.get("reset_traffic_method")
            if (self.meta["reset_method"] if method is None else int(method)) != 0:
                raise Error("新增用户需选择每月一日重置的现有套餐；本工具不修改共享套餐。")
            days = int(params["days"])
            if not 1 <= days <= 36500:
                raise Error("有效期须为 1 至 36500 天；永久有效可能导致月度重置失效。")
            limit = quota_bytes(params["quota"])
            expiry = int(time.time()) + days * 86400
            print(f"新增 {email}，套餐 #{plan['id']}，额度 {size(limit)}，有效期 {days} 天。")
            if not self.approve("CREATE " + email):
                return
            password = secrets.token_urlsafe(20)
            prefix, suffix = email.split("@", 1)
            self.api.admin("/user/generate", {"email_prefix": prefix, "email_suffix": suffix,
                           "plan_id": plan["id"], "expired_at": expiry, "password": password})
            try:
                row = self.user(email)
                if int(row["transfer_enable"]) != limit:
                    self.update(row, {"transfer_enable": limit})
                row = self.user(email)
                if int(row["transfer_enable"]) != limit or not row.get("next_reset_at"):
                    raise Error("创建后额度或重置时间未通过检查。")
            except Exception as exc:
                raise Error("创建请求已提交，但后续步骤未通过。用户可能已存在；请查询后处理，不要重复创建。") from exc
            self.show(safe_user(row))
            print("新用户密码（仅此处显示）：" + password)
            print("使用 link 命令按需查询订阅地址。")
        elif command in ("quota", "rename", "disable", "enable", "reset", "delete"):
            row = self.user(params["user"])
            protect_user(row)
            self.show(safe_user(row))
            fields = {}
            if command == "quota":
                fields["transfer_enable"] = quota_bytes(params["value"])
                print("新额度：" + size(fields["transfer_enable"]) + "；不清零已有用量。")
            elif command == "rename":
                fields["email"] = params["value"].strip().lower()
                print("新邮箱：" + fields["email"] + "；订阅 token 和 UUID 不变。")
            elif command in ("disable", "enable"):
                fields["banned"] = command == "disable"
            elif command == "delete":
                print("永久删除账号及其关联订单、统计、工单等；建议优先停用。")
            else:
                print("仅重置此用户上传/下载计数；不重置共享总池，也不返还服务商流量。")
            if not self.approve(command.upper() + " " + row["email"]):
                return
            current = self.user(str(row["id"]), fresh=True)
            protect_user(current)
            if any(current.get(k) != row.get(k) for k in ("email", "plan_id", "transfer_enable", "banned")):
                raise Error("确认期间用户配置已变化，停止提交，请重新查询。")
            if command == "reset":
                self.api.admin("/traffic-reset/reset-user", {"user_id": row["id"], "reason": "vpsctl manual reset"})
            elif command == "delete":
                self.api.admin("/user/destroy", {"id": row["id"]})
            else:
                self.update(current, fields)
            if command == "delete":
                if any(r["id"] == row["id"] for r in self.api.users(force=True)):
                    raise Error("删除后检查未通过。")
                print("已删除。")
            else:
                after = self.user(str(row["id"]), fresh=True)
                if any(after.get(k) != current.get(k) for k in ("token", "uuid")):
                    raise Error("账号密钥意外变化，请检查；不自动重试。")
                for key, value in fields.items():
                    if after.get(key) != value:
                        raise Error("提交后字段未匹配；请检查，不自动重试。")
                if command == "reset" and after.get("reset_count", 0) <= row.get("reset_count", 0):
                    raise Error("重置记录未推进；请核对，不自动重试。")
                self.show(safe_user(after))
        elif command in ("pool", "pool-set"):
            nodes = self.api.admin("/server/manage/getNodes")["data"]
            if command == "pool":
                self.show([{k: n.get(k) for k in ("id", "name", "code", "transfer_enable", "u", "d", "enabled")}
                           for n in nodes])
                if not self.args.json:
                    print("单位为原始 bytes。这是面板计量，不是 Vultr 账单或网络层硬限额。")
                return
            if not params.get("node"):
                if len(nodes) != 1:
                    raise Error("存在多个节点，请在 TUI 中选择；不会猜测目标。")
                row = nodes[0]
            else:
                row = select_one(nodes, params["node"], "节点")
            limit = quota_bytes(params["value"])
            print(f"节点 #{row['id']} {row['name']}，新总池额度 {size(limit)}；保留 u/d 用量。")
            if not self.approve("POOL " + str(row["id"])):
                return
            # This XBoard API requires a full node save. Patch only the ORM quota
            # through trusted SSH, without transmitting private protocol settings.
            data = base64.b64encode(json.dumps({"id": row["id"], "old": row["transfer_enable"], "new": limit}).encode()).decode()
            result = self.ssh.php('$p=json_decode(base64_decode("' + data + '"),true); '
                '$r=Illuminate\\Support\\Facades\\DB::transaction(function()use($p){'
                '$s=App\\Models\\Server::whereKey($p["id"])->lockForUpdate()->firstOrFail();'
                'if((int)$s->transfer_enable!==(int)$p["old"])throw new RuntimeException("Quota changed");'
                '$before=$s->getAttributes(); $s->transfer_enable=$p["new"]; $s->save(); $s->refresh();'
                'foreach($before as$k=>$v)if(!in_array($k,["transfer_enable","updated_at"])&&'
                '$s->getRawOriginal($k)!==$v)throw new RuntimeException("Unrelated field changed");'
                'Illuminate\\Support\\Facades\\Log::info("vpsctl.pool.quota.changed",'
                '["node_id"=>$s->id,"old_bytes"=>$p["old"],"new_bytes"=>$p["new"]]);'
                'return ["id"=>$s->id,"transfer_enable"=>$s->transfer_enable];}); echo json_encode($r);')
            if int(result["transfer_enable"]) != limit:
                raise Error("总池修改后检查未通过。")
            self.show(result)
        else:
            raise Error("未知操作。")

    def restart(self, target):
        if target not in ("panel", "node", "vps"):
            raise Error("重启对象必须是 panel、node 或 vps。")
        print(f"将重启 {target}。node/vps 会中断代理连接；此操作不更改配置。")
        if not confirm("RESTART " + target, self.args.dry_run, self.read):
            return
        self.ssh.revoke_access()
        self.api.auth, self.api.expires_at = None, 0
        if target == "panel":
            command_line = shlex.join(["docker", "restart", "--time", "30", self.args.container])
        elif target == "node":
            command_line = shlex.join(["systemctl", "restart", self.args.node_service])
        else:
            command_line = "systemctl reboot"
        self.ssh.run(command_line)
        print("重启命令已完成或已接受；未承诺真实转发恢复，请重新查询状态。")


def cell_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def clean_line(text):
    return "".join(c if not unicodedata.category(c).startswith("C") else " " for c in str(text))


def pad(text, width):
    text = clean_line(text)
    clipped = ""
    used = 0
    for character in text:
        step = cell_width(character)
        if used + step > width:
            break
        clipped += character
        used += step
    return clipped + " " * max(0, width - used)


class TerminalUI:
    """Bounded ANSI screens; no curses/rich dependency or scrollback logging."""

    def __init__(self, console):
        self.console = console
        self.output = sys.stdout
        self.pending = None
        self.width = 90
        self.height = 24
        self.old_mode = None
        self.last_frame = []
        self.last_width = 0

    def __enter__(self):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise Error("菜单需要交互终端；脚本查询可使用 status、users 等子命令。")
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.windll.kernel32
            self.kernel.GetStdHandle.argtypes = [wintypes.DWORD]
            self.kernel.GetStdHandle.restype = wintypes.HANDLE
            self.kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            self.kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.handle = self.kernel.GetStdHandle(wintypes.DWORD(-11))
            value = wintypes.DWORD()
            if self.kernel.GetConsoleMode(self.handle, ctypes.byref(value)):
                self.old_mode = value.value
                if not self.kernel.SetConsoleMode(self.handle, value.value | 4):
                    raise Error("请使用 Windows Terminal 或支持 ANSI 的终端。")
        print("\x1b[?1049h", end="", file=self.output, flush=True)
        self.console.ui = self
        return self

    def __exit__(self, *exc):
        self.console.ui = None
        print("\x1b[0m\x1b[?1049l", end="", file=self.output, flush=True)
        if self.old_mode is not None:
            self.kernel.SetConsoleMode(self.handle, self.old_mode)

    def dimensions(self):
        columns, lines = shutil.get_terminal_size((100, 28))
        self.width = max(20, min(112, columns - 1))
        self.height = max(10, lines)
        if columns < 64 or lines < 20:
            raise Error("终端至少需要 64 列、20 行；放大终端后重试，或使用子命令。")

    def draw(self, title, lines, footer):
        self.dimensions()
        inside = self.width - 4
        body_height = self.height - 8
        edge = "+" + "-" * (self.width - 2) + "+"
        rows = [edge, "| " + pad("XBoard  /  " + title, inside) + " |", edge]
        rows += ["| " + pad(line, inside) + " |" for line in lines[:body_height]]
        rows += ["| " + " " * inside + " |"] * max(0, body_height - len(lines))
        rows += [edge, "| " + pad(footer, inside) + " |", edge]
        full = self.width != self.last_width or len(rows) != len(self.last_frame)
        chunks = ["\x1b[2J\x1b[H"] if full else []
        for index, row in enumerate(rows):
            color = "\x1b[1;36m" if index == 1 else "\x1b[2m" if index == len(rows)-2 else ""
            if full or row != self.last_frame[index]:
                chunks.append(f"\x1b[{index+1};1H" + color + row + "\x1b[0m\x1b[K")
        chunks.append(f"\x1b[{len(rows)+1};1H\x1b[J")
        self.output.write("".join(chunks))
        self.output.flush()
        self.last_frame, self.last_width = rows, self.width

    def read(self, prompt):
        self.dimensions()
        context = self.pending.getvalue().splitlines() if self.pending else []
        self.draw("输入", context[-max(1, self.height-12):] + [""] + prompt.splitlines(), "输入后按 Enter；修改需确认")
        print("> ", end="", file=self.output, flush=True)
        return input()

    def key(self):
        if os.name == "nt":
            import msvcrt
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                return {"H":"up", "P":"down", "I":"prev", "Q":"next", "G":"home", "O":"end"}.get(msvcrt.getwch(), "")
        else:
            import select
            import termios
            import tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                key = sys.stdin.read(1)
                if key == "\x1b" and select.select([sys.stdin], [], [], 0.05)[0]:
                    key += os.read(fd, 8).decode(errors="ignore")
                    return {"\x1b[A":"up", "\x1b[B":"down", "\x1b[5~":"prev", "\x1b[6~":"next"}.get(key, "back")
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if key == "\x03":
            raise KeyboardInterrupt
        return "enter" if key in ("\r", "\n") else "back" if key == "\x1b" else key.lower()

    def pick(self, title, options, automatic=False):
        if not options:
            raise Error("没有可选项，请先在后台检查配置。")
        if automatic and len(options) == 1:
            return options[0][1]
        selected = 0
        while True:
            self.draw(title, [(" > " if i == selected else "   ") + label for i,(label,_) in enumerate(options)],
                      "上下选择  Enter 确认  Esc 返回")
            key = self.key()
            if key in ("up", "k"):
                selected = max(0, selected-1)
            elif key in ("down", "j"):
                selected = min(len(options)-1, selected+1)
            elif key == "enter":
                return options[selected][1]
            elif key in ("back", "q"):
                return None

    def wrap(self, text):
        width = self.width - 4
        result = []
        for original in text.splitlines():
            line = ""
            for character in clean_line(original):
                if cell_width(line + character) > width:
                    result.append(line)
                    line = ""
                line += character
            result.append(line)
        return result or ["无数据。"]

    def view(self, title, text, copy_text=None):
        page = 0
        while True:
            self.dimensions()
            lines = self.wrap(text)
            heading = []
            if len(lines) > 1 and lines[1] and set(lines[1]) == {"-"}:
                heading, lines = lines[:2], lines[2:]
            capacity = self.height - 8 - len(heading)
            pages = max(1, (len(lines) + capacity - 1) // capacity)
            page = min(page, pages-1)
            footer = f"{page+1}/{pages}  n 下一页  p 上一页  Enter 返回"
            if copy_text and os.name == "nt":
                footer += "  c 复制链接"
            self.draw(title, heading + lines[page*capacity:(page+1)*capacity], footer)
            key = self.key()
            if key in ("n", "next", "down"):
                page = min(page+1, pages-1)
            elif key in ("p", "prev", "up"):
                page = max(0, page-1)
            elif key == "c" and copy_text and os.name == "nt":
                try:
                    subprocess.run(["clip.exe"], input=copy_text.encode("utf-16"), check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                except (OSError, subprocess.SubprocessError):
                    self.draw(title, ["复制失败，请使用终端选择并复制。"], "Enter 返回")
                    input("> ")
            else:
                return

    def run(self, action, params):
        output = io.StringIO()
        self.pending = output
        success = True
        try:
            self.draw("处理中", ["正在通过 SSH 隧道请求服务器。"], "不自动重试修改请求")
            with contextlib.redirect_stdout(output):
                try:
                    self.console.action(action, params)
                except (Error, ValueError, KeyError) as exc:
                    success = False
                    print("未完成：" + (str(exc) if isinstance(exc, Error) else "输入或接口结构不匹配，请核对版本。"))
        finally:
            self.pending = None
        text = output.getvalue()
        link = text.strip() if action == "link" and text.strip().startswith("https://") else None
        self.view("结果", text or "操作已返回。", copy_text=link)
        return success


def menu(console):
    with TerminalUI(console) as ui:
        ui.draw("连接", ["正在读取全部用户流量。"], "SSH 密钥授权；不需要面板密码")
        rows = []
        selected = 0
        refresh = True
        while True:
            if refresh:
                try:
                    rows = [safe_user(r) for r in console.api.users(force=True)
                            if not r.get("is_admin") and not r.get("is_staff")]
                    selected = min(selected, max(0,len(rows)-1))
                except Error as exc:
                    ui.view("读取失败", str(exc))
                refresh = False
            ui.dimensions()
            capacity = ui.height-11
            start = (selected//capacity)*capacity
            widths = [2, ui.width-55, 8, 8, 8, 8, 8]
            def table(values):
                return " ".join(pad(str(v),w) for v,w in zip(values,widths))
            loaded = stamp(console.api.users_updated_at) if console.api.users_updated_at else None
            preview = "预演" if console.args.dry_run else "确认后执行"
            lines = [f"{len(rows)} 位用户  |  读取 {loaded[11:19]+' UTC' if loaded else '未完成'}  |  {preview}",
                     table(["", "用户", "上传GiB", "下载GiB", "已用GiB", "配额GiB", "剩余GiB"]),
                     "-"*(ui.width-5)]
            for i,row in enumerate(rows[start:start+capacity],start):
                lines.append(table([">" if i==selected else "", row["email"],
                    f"{int(row['u'] or 0)/2**30:.2f}",f"{int(row['d'] or 0)/2**30:.2f}",
                    f"{row['used_bytes']/2**30:.2f}",f"{int(row['transfer_enable'] or 0)/2**30:.2f}",
                    f"{row['remaining_bytes']/2**30:.2f}"]))
            ui.draw("全部用户流量", lines, "上下选择 Enter操作 r刷新 a新增 s状态 t总池 m维护 d预演 q退出")
            choice = ui.key()
            if choice in ("0", "q"):
                return
            if choice == "d":
                console.args.dry_run = not console.args.dry_run
                continue
            if choice in ("up", "k", "down", "j", "next", "n", "prev", "p", "home", "end"):
                delta = -1 if choice in ("up","k") else 1 if choice in ("down","j") else capacity if choice in ("next","n") else -capacity
                selected = 0 if choice == "home" else len(rows)-1 if choice == "end" else max(0,min(len(rows)-1,selected+delta))
                selected = max(0,selected)
                continue
            if choice == "r":
                refresh = True
                continue
            action, params = None, {}
            if choice == "enter" and rows:
                row = rows[selected]
                action = ui.pick(row["email"], [("复制 / 查看订阅链接", "link"),("调整流量额度", "quota"),
                    ("重置已用流量", "reset"),("启用用户" if row["banned"] else "停用用户", "enable" if row["banned"] else "disable"),
                    ("修改邮箱", "rename"),("永久删除用户", "delete")])
                params["user"] = str(row["id"])
            elif choice in ("s", "t", "a"):
                action = {"s":"status", "t":"pool", "a":"create"}[choice]
            elif choice == "m":
                action = ui.pick("维护", [("调整总池上限","pool-set"),("重启服务","restart"),
                    ("打开已有后台网页","web"),("查看套餐","plans")])
            if not action:
                continue
            if action in ("quota", "rename", "pool-set"):
                params["value"] = ui.read("新邮箱：" if action == "rename" else "新额度（如 50GiB 或 50GB）：").strip()
            if action == "pool-set":
                nodes = console.api.admin("/server/manage/getNodes")["data"]
                params["node"] = ui.pick("选择节点",[(n["name"], str(n["id"])) for n in nodes],automatic=True)
                if params["node"] is None:
                    continue
            if action == "restart":
                params["target"] = ui.pick("重启对象", [("仅重启面板","panel"),("重启节点（中断代理）","node"),
                    ("重启整台 VPS（中断所有服务）","vps")])
                if params["target"] is None:
                    continue
            if action == "create":
                plans = console.api.admin("/plan/fetch")["data"]
                plan = ui.pick("选择套餐",[(p["name"], str(p["id"])) for p in plans],automatic=True)
                if plan is None:
                    continue
                params = {"email": ui.read("新用户完整邮箱：").strip(), "plan": plan,
                          "quota": ui.read("额度（如 50GiB）：").strip(), "days": ui.read("有效天数（不能留空）：").strip()}
            success = ui.run(action, params)
            refresh = success and action in ("create","quota","reset","enable","disable","rename","delete") and not console.args.dry_run


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=os.environ.get("VPSCTL_HOST"), help="SSH 别名或 user@host；不传则询问")
    p.add_argument("--port", type=int, default=17724, help="SSH 端口，默认 17724")
    p.add_argument("--identity", help="SSH 私钥路径；默认使用现有 SSH 配置")
    p.add_argument("--panel-port", type=int, default=7001)
    p.add_argument("--container", default="xboard-xboard-1")
    p.add_argument("--node-service", default="xboard-node.service")
    p.add_argument("--app-dir", default="/opt/Xboard", help="宿主机 XBoard 目录，仅用于备份路径")
    p.add_argument("--database", help="宿主机 SQLite 数据库路径，可覆盖默认路径")
    p.add_argument("--admin-path", help="覆盖自动识别的管理路径，不含斜杠")
    p.add_argument("--dry-run", action="store_true", help="只展示修改计划；短期会话和正常审计仍会发生")
    p.add_argument("--no-backup", action="store_true", help="明确跳过本次业务修改备份，不影响确认")
    p.add_argument("--json", action="store_true", help="查询结果以 JSON 输出；默认列表不输出订阅凭据")
    sub = p.add_subparsers(dest="command")
    for name in ("status", "users", "plans", "pool", "web"):
        sub.add_parser(name)
    for name in ("link", "reset", "disable", "enable", "delete"):
        sub.add_parser(name).add_argument("user")
    for name in ("quota", "rename"):
        s = sub.add_parser(name)
        s.add_argument("user")
        s.add_argument("value")
    s = sub.add_parser("create")
    s.add_argument("email")
    s.add_argument("--plan", help="套餐名称或 ID；仅有一个套餐时自动选择")
    s.add_argument("--quota", required=True)
    s.add_argument("--days", required=True, type=int)
    s = sub.add_parser("pool-set")
    s.add_argument("node", nargs="?", help="节点名称、code 或 ID；仅有一个节点时可省略")
    s.add_argument("value")
    sub.add_parser("restart").add_argument("target", choices=("panel", "node", "vps"))
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.host:
        args.host = input("SSH 地址（如 root@服务器IP 或已有 SSH 别名）：").strip()
    if not (1 <= args.port <= 65535 and 1 <= args.panel_port <= 65535):
        raise Error("端口必须在 1 至 65535。")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", args.node_service):
        raise Error("节点服务名称无效。")
    ssh = SSH(args)
    if args.command == "status":
        Console(args, ssh, "", {}).show(ssh.status())
        return
    if not args.command:
        print("正在通过 SSH 密钥连接…", file=sys.stderr)
    with tunnel(ssh) as base:
        meta = ssh.access()
        console = Console(args, ssh, base, meta)
        if args.command:
            console.action(args.command, vars(args))
        else:
            menu(console)


if __name__ == "__main__":
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已退出；本工具启动的 SSH 隧道已关闭。")
    except (Error, ValueError, KeyError, json.JSONDecodeError) as exc:
        print("错误：" + (str(exc) if isinstance(exc, Error) else "输入或响应结构不匹配，请检查兼容性。"), file=sys.stderr)
        sys.exit(1)
