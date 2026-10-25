#!/usr/bin/env python3
"""Claude 双授权入口：setup-token 模型凭据 + profile/quota 辅助凭据。"""
import argparse
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from common import DEFAULT_CONFIG, ROOT, display, load_config, private_json, remote, run

AUTH = ROOT.parent / "cpa-auth"


def init_config(filename):
    path = Path(filename or DEFAULT_CONFIG).expanduser().resolve()
    if path.exists():
        raise RuntimeError("配置已存在；请编辑该私密文件或选择新路径。")
    host = input("VPS IP/主机名：").strip()
    user = input("SSH 用户 [root]：").strip() or "root"
    port = input("SSH 端口 [22]：").strip() or "22"
    key = str(Path(input("本机 SSH 私钥路径：").strip()).expanduser().resolve())
    base = input("CPA HTTPS Base URL（不含 /v1）：").strip().rstrip("/")
    from urllib.parse import urlsplit
    parsed = urlsplit(base)
    if (not host or host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.-]+", user)
            or not port.isdigit() or not 1 <= int(port) <= 65535 or not Path(key).is_file()
            or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise RuntimeError("SSH 或 HTTPS 配置信息无效。")
    cfg = {"ssh": ["ssh", "-i", key, "-p", port, "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
                   "-o", "StrictHostKeyChecking=yes", user + "@" + host],
           "base_url": base,
           "observer_config": input("VPS observer 配置 [/etc/cpa-observer/config.json]：").strip() or "/etc/cpa-observer/config.json",
           "observer_script": "/opt/cpa-observer/observer.py", "observer_unit": "cpa-observer.service",
           "model": input("验证模型 [claude-sonnet-5-5]：").strip() or "claude-sonnet-5-5"}
    existing_env = input("首次部署生成的本机 env 路径 [配置目录/.cpa-env]：").strip()
    cfg["env_file"] = str(Path(existing_env).expanduser().resolve()) if existing_env else str(path.parent / ".cpa-env")
    cfg["remote_python"] = ["python3"] if user == "root" else ["sudo", "-n", "python3"]
    private_json(path, cfg)
    print("配置已保存：" + str(path))
    print("请先通过普通 ssh 确认主机指纹。工具不会自动接受陌生主机。")


def supervise(cfg_file, state, script):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (state / "status.json").exists():
        # A retry must not mistake a previous URL/status for a new live receiver.
        (state / "status.json").unlink()
    process = subprocess.Popen([sys.executable, "-u", str(AUTH / script),
                                "--deployment-config", str(cfg_file), "--state-dir", str(state)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    last_phase = None
    shown = None
    status = {}
    try:
        while True:
            try:
                status = json.loads((state / "status.json").read_text())
            except (FileNotFoundError, ValueError):
                pass
            phase = status.get("phase")
            if phase and phase != last_phase:
                print("状态：" + phase, flush=True)
                last_phase = phase
            link_file = state / "authorization-url.private"
            if phase == "waiting_for_user" and link_file.exists() and process.poll() is None:
                link = link_file.read_text().strip()
                if link != shown:
                    print("\n请在所选账号的隐私窗口打开此链接（终端保持运行）：\n" + link + "\n", flush=True)
                    shown = link
            if process.poll() is not None:
                try:
                    status = json.loads((state / "status.json").read_text())
                except (FileNotFoundError, ValueError):
                    pass
                if process.returncode:
                    raise RuntimeError("授权进程未完成。会话已保留；用 resume 继续，旧链接不可重用。")
                return status
            time.sleep(.25)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        link_file = state / "authorization-url.private"
        link_file.unlink(missing_ok=True)


def validate_model(cfg):
    spec = importlib.util.spec_from_file_location("managed_cpa_setup", AUTH / "managed_setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.remote(cfg, "validate")
    if result.get("generation_status") != 200 or not result.get("has_text"):
        raise RuntimeError("新 setup-token 的隔离模型调用未通过，账号保持禁用。可稍后 resume 重验。")
    return result


def continue_session(session):
    session = Path(session).expanduser().resolve()
    config_file = session / "deployment.private.json"
    cfg = load_config(config_file)
    journal_path = session / "onboarding.private.json"
    journal = json.loads(journal_path.read_text())
    if journal.get("completed"):
        print("此会话已完成；查看当前 VPS 状态请运行 status。")
        return
    with (session / "session.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("该会话仍在另一个终端运行。") from None
        # Reconcile a process interrupted after saving a credential but before
        # updating the local checkpoint. Never overwrite or request a duplicate.
        installed = {a["name"]: a for a in remote(cfg, "auth_status")["accounts"]}
        generation_checked = False
        if cfg["credential_name"] not in installed:
            print("第 1/2 步：setup-token 模型授权。请选择 " + cfg["expected_email"], flush=True)
            result = supervise(config_file, session / "setup", "managed_setup.py")
            if not result.get("imported"):
                raise RuntimeError("模型凭据未导入；请 resume 重新启动接收端。")
            generation_checked = result.get("generation_status") == 200 and bool(result.get("has_text"))
        if not generation_checked:
            print("验证新凭据的独立模型调用（会产生少量 token 用量）。", flush=True)
            validate_model(cfg)
        journal["generation_verified"] = True
        private_json(journal_path, journal)
        installed = {a["name"]: a for a in remote(cfg, "auth_status")["accounts"]}
        if cfg["quota_credential_name"] not in installed:
            print("第 2/2 步：同账号 profile/quota 授权。继续选择 " + cfg["expected_email"], flush=True)
            result = supervise(config_file, session / "quota", "managed_quota.py")
            if not result.get("ready_for_binding"):
                raise RuntimeError("资料/额度检查未通过，账号保持待验收；可 resume 重验。")
        if not journal.get("same_account_confirmed"):
            print("setup-token 不能独立披露邮箱。请确认两次授权都选择同一账号。")
            confirmed_email = input("输入两次使用的邮箱以确认：").strip()
            if confirmed_email.casefold() != cfg["expected_email"].casefold():
                raise RuntimeError("同账号确认未通过。凭据保持待验收，请核实后 resume。")
            journal["same_account_confirmed"] = True
            private_json(journal_path, journal)
        print("正在备份、绑定额度来源并验收；不要关闭终端。", flush=True)
        result = remote(cfg, "auth_bind", generation_verified=True, same_account_confirmed=True)
        journal.update(completed=True, accepted_at=dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), result=result)
        private_json(journal_path, journal)
        display(result)
        print("完成：setup-token 用于模型，profile 凭据仅查额度，两份凭据计为一个账号。")


def add_account(cfg, email=None, account=None):
    if not shutil.which("claude"):
        raise RuntimeError("本机未安装 Claude Code，请先安装官方 CLI。")
    email = (email or input("新增 Claude 账号邮箱：")).strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise RuntimeError("邮箱格式无效。")
    account = account or input("账号代号（字母/数字/连字符；留空自动生成）：").strip()
    account = account or ("account-" + secrets.token_hex(4))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", account):
        raise RuntimeError("账号代号无效。")
    job = {k: v for k, v in cfg.items() if not k.startswith("_")}
    job.update(expected_email=email, credential_name="claude-" + account + "-setup.json",
               quota_credential_name="claude-" + account + "-quota.json", defer_enable=True)
    remote(job, "auth_preflight")
    stamp = dt.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    session = Path(cfg["_config_path"]).parent / "state" / (stamp + "-" + account + "-" + secrets.token_hex(3))
    session.mkdir(parents=True, mode=0o700)
    private_json(session / "deployment.private.json", job)
    private_json(session / "onboarding.private.json", {"completed": False, "account": account,
                 "started_at": dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()})
    print("会话：" + str(session), flush=True)
    print("中断后继续：python3 " + str(ROOT / "claude_auth.py") + " resume --session " + str(session), flush=True)
    continue_session(session)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="私密部署 JSON；默认 cpa-tools/.deployment.private.json")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("init", help="首次交互配置 SSH、HTTPS Base URL 和 observer 路径")
    add = commands.add_parser("add", help="按顺序完成两种授权、验证、绑定和启用")
    add.add_argument("--email")
    add.add_argument("--account", help="唯一稳定代号；已存在的文件名不会覆盖")
    resume = commands.add_parser("resume", help="从会话目录继续；不重复已完成的授权")
    resume.add_argument("--session", required=True)
    commands.add_parser("status", help="查看 VPS Claude 凭据状态；不读取 token")
    args = parser.parse_args()
    if args.command == "init":
        init_config(args.config)
    elif args.command == "resume":
        continue_session(args.session)
    elif args.command == "status":
        display(remote(load_config(args.config), "auth_status"))
    elif args.command == "add":
        add_account(load_config(args.config), args.email, args.account)
    else:
        print("Claude 授权：1 新增账号  2 继续会话  3 查看状态  4 首次配置  0 退出")
        choice = input("> ").strip()
        if choice == "1":
            add_account(load_config(args.config))
        elif choice == "2":
            continue_session(input("会话目录：").strip())
        elif choice == "3":
            display(remote(load_config(args.config), "auth_status"))
        elif choice == "4":
            init_config(args.config)


if __name__ == "__main__":
    run(main)
