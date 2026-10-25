#!/usr/bin/env python3
"""Interactive CPA downstream API Key manager. Keys are never printed."""
import argparse
import fcntl
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import tempfile

from common import display, load_config, private_json, remote, run


def name_value(value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
        raise argparse.ArgumentTypeError("名称需以英文字母开头，只含字母、数字、下划线、连字符，最长 64 字符。")
    return value


def env_path(cfg, output=None):
    value = output or cfg.get("env_file")
    if not value:
        raise RuntimeError("请在私密部署配置中设置 env_file，指向原有 .cpa-env；或指定 --out。")
    filename = Path(value).expanduser()
    if not filename.is_absolute():
        filename = Path(cfg["_config_path"]).parent / filename
    if filename.is_symlink():
        raise RuntimeError("env 文件不能是符号链接。")
    return filename.absolute()


def update_env(cfg, updates=None, remove=(), output=None):
    filename = env_path(cfg, output)
    filename.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    updates = dict(updates or {})
    remove = set(remove)
    lock_fd = os.open(str(filename) + ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = ""
        if filename.exists():
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise RuntimeError("env 路径必须是普通文件。")
                original = source.read()
        assignment = re.compile(r"^\s*(?:export\s+)?(CPA_API_KEY_[A-Za-z0-9_]+)\s*=(.*)")
        lines, replaced = [], set()
        for line in original.splitlines(keepends=True):
            match = assignment.match(line)
            if not match:
                lines.append(line)
                continue
            variable, value = match.groups()
            if variable in remove or (variable in updates and variable in replaced):
                continue
            # Normalize syntax without parsing or executing the existing value.
            value = shlex.quote(updates[variable]) if variable in updates else value
            lines.append("export " + variable + "=" + value + "\n")
            replaced.add(variable)
        text = "".join(lines)
        if text and not text.endswith("\n"):
            text += "\n"
        for variable, value in updates.items():
            if variable not in replaced and variable not in remove:
                text += "export " + variable + "=" + shlex.quote(value) + "\n"
        if cfg.get("base_url") and not re.search(r"(?m)^\s*(?:export\s+)?CPA_BASEURL\s*=", text):
            text += "export CPA_BASEURL=" + shlex.quote(cfg["base_url"]) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".cpa-env-", dir=filename.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            # Detect edits made by an external editor that does not use our lock.
            if filename.is_symlink() or (filename.read_text() if filename.exists() else "") != original:
                raise RuntimeError("env 文件被其他程序修改，请核实后重新导出。")
            os.replace(temporary, filename)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return str(filename)


def env_name(name):
    return "CPA_API_KEY_" + re.sub(r"[^A-Za-z0-9_]", "_", name).upper()


def export_key(cfg, name, key, output=None):
    return update_env(cfg, {env_name(name): key}, output=output)


def install_key(cfg, action, name, output, selector=None):
    destination = env_path(cfg, output)
    key = "sk-cpa-" + secrets.token_urlsafe(36)
    pending = Path(cfg["_config_path"]).parent / "state" / "key-operations" / (secrets.token_hex(12) + ".private.json")
    job = {"action": action, "name": name, "selector": selector,
           "key": key, "env_file": str(destination), "status": "pending"}
    private_json(pending, job)
    try:
        arguments = {"name": name} if action == "keys_add" else {"selector": selector}
        result = remote(cfg, action, new_key=key, **arguments)
        job["status"] = "remote_applied"
        private_json(pending, job)
        path = export_key(cfg, name, key, destination)
    except Exception as error:
        reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        raise RuntimeError(reason + "\n恢复记录：" + str(pending) +
                           "\n先核实 VPS 状态；已生效时用导出同步原 env。") from None
    pending.unlink()
    return {"ok": True, "message": name + (" 已新增" if action == "keys_add" else " 已换 Key"),
            "env_file": path, "backup": result.get("backup"), "reload_required": True}


def confirmed(message, yes=False):
    return yes or input(message + " 输入 yes 确认：").strip() == "yes"


def execute(cfg, args):
    action = args.command
    if action in ("add", "delete", "rename", "rotate", "export", "normalize-env"):
        env_path(cfg, getattr(args, "out", None))
    if action == "list":
        return remote(cfg, "keys_list")
    elif action == "quota":
        return remote(cfg, "keys_quota")
    elif action == "usage":
        return remote(cfg, "keys_usage", selector=args.selector, period=args.period, date=args.date)
    elif action == "add":
        rows = remote(cfg, "keys_list")["keys"]
        if any(row["name"] == args.name for row in rows):
            raise RuntimeError("该名称已存在（含历史），请选择新名称。")
        normalized = lambda name: re.sub(r"[^A-Za-z0-9_]", "_", name).upper()
        if any(normalized(row["name"]) == normalized(args.name) for row in rows):
            raise RuntimeError("该名称对应的环境变量与已有调用者冲突，请选择不同名称。")
        return install_key(cfg, "keys_add", args.name, args.out)
    elif action == "delete":
        if confirmed("将撤销 " + args.selector + " 的调用权限，保留历史用量。", args.yes):
            result = remote(cfg, "keys_delete", selector=args.selector)
            return {"ok": True, "message": "已撤销；历史用量保留", "env_file": str(env_path(cfg)),
                    "backup": result.get("backup")}
    elif action == "rename":
        result = remote(cfg, "keys_rename", selector=args.selector, name=args.name)
        return {"ok": True, "message": "已改名为 " + args.name, "env_file": str(env_path(cfg)),
                "backup": result.get("backup")}
    elif action == "rotate":
        if not confirmed("将立即撤销旧 Key；调用端需要加载新 Key。历史用量单列为 retired。", args.yes):
            return
        rows = remote(cfg, "keys_list")["keys"]
        matched = [r for r in rows if r["active"] and args.selector in (r["id"], r["name"])]
        if len(matched) != 1:
            raise RuntimeError("未找到唯一的活动 Key，请用 list 中的完整 id。")
        return install_key(cfg, "keys_rotate", matched[0]["name"], args.out, args.selector)
    elif action == "export":
        body = remote(cfg, "keys_export", selector=args.selector)
        path = export_key(cfg, args.name, body.pop("private_key"), args.out)
        return {"ok": True, "message": "已同步 " + args.name, "env_file": path, "reload_required": True}
    elif action == "normalize-env":
        return {"ok": True, "message": "已统一 export 写法；Key 值保留", "env_file": update_env(cfg)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="私密部署 JSON；默认与 auth 工具共用")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON，供脚本使用")
    parser.add_argument("--demo", action="store_true", help="离线预览 TUI，不连接 VPS 或修改文件")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="查看名称、完整 id 和活动状态；不显示 Key")
    commands.add_parser("normalize-env", help="统一原 env 的 export 写法，不更换 Key")
    add = commands.add_parser("add", help="生成并启用一个新 Key")
    add.add_argument("name", type=name_value)
    add.add_argument("--out", help="可选；默认更新配置 env_file 指向的原有 env")
    delete = commands.add_parser("delete", help="撤销 Key，保留历史")
    delete.add_argument("selector")
    delete.add_argument("--yes", action="store_true")
    rename = commands.add_parser("rename", help="修改显示名称，保留 Key 和历史")
    rename.add_argument("selector")
    rename.add_argument("name", type=name_value)
    rotate = commands.add_parser("rotate", help="换 Key，撤销旧 Key；历史单列")
    rotate.add_argument("selector")
    rotate.add_argument("--out", help="可选；默认更新原有 env")
    rotate.add_argument("--yes", action="store_true")
    usage = commands.add_parser("usage", help="管理员查询指定 Key 或全部历史用量")
    usage.add_argument("selector", nargs="?")
    usage.add_argument("--period", choices=["day", "month", "all"], default="day")
    usage.add_argument("--date", help="北京时间 YYYY-MM-DD 或 YYYY-MM")
    quota = commands.add_parser("quota", help="用 Base URL + Key 查询号池及匿名上游账号额度")
    quota.add_argument("--base-url", help="HTTPS 根地址；默认 CPA_BASEURL")
    quota.add_argument("--key-env", help="读取指定环境变量；不把 Key 放入命令参数")
    quota.add_argument("--admin", action="store_true", help="使用管理员 SSH 查询，与 TUI 相同")
    export = commands.add_parser("export", help="导出现有 Key 到私密文件；不在终端显示")
    export.add_argument("selector")
    export.add_argument("--name", required=True, type=name_value)
    export.add_argument("--out", help="可选；默认更新原有 env")
    for command in commands.choices.values():
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出 JSON")
    args = parser.parse_args()
    if args.command == "quota" and args.admin and (args.base_url or args.key_env):
        parser.error("--admin 使用 SSH 配置；不能同时指定 --base-url 或 --key-env。")
    if args.demo:
        from key_tui import start
        start({"_demo": True}, execute, name_value)
        return
    if args.command == "quota" and not args.admin:
        from pool_quota import query
        result = query(args.base_url, args.key_env)
        if args.json:
            display(result)
        else:
            from key_view import render_cli
            render_cli(result)
        return
    cfg = load_config(args.config)
    if args.command:
        result = execute(cfg, args)
        if result is not None:
            if args.json:
                display(result)
            else:
                from key_view import render_cli
                render_cli(result)
    else:
        from key_tui import start
        start(cfg, execute, name_value)


if __name__ == "__main__":
    run(main)
