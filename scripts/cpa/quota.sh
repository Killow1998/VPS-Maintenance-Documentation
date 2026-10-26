#!/usr/bin/env bash
# Standalone client: Bash + Python 3.9+, no package installation or local files.
set -euo pipefail
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '需要 Python 3.9+，无需安装 pip 包。' >&2
  exit 1
fi
python3 - "$@" <<'PY'
import argparse
import datetime as dt
import getpass
import json
import math
import os
import re
import shutil
import sys
import unicodedata
import warnings
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

BEIJING = dt.timezone(dt.timedelta(hours=8))
WINDOW = re.compile(r"(?:five_hour|seven_day)(?:_[a-z0-9_]+)?")
COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def timestamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def reset_label(value):
    target = timestamp(value)
    if target is None:
        return "未知"
    seconds = max(0, int((target - dt.datetime.now(dt.timezone.utc)).total_seconds()))
    if seconds >= 86400:
        left = f"{seconds // 86400}d{seconds % 86400 // 3600}h"
    elif seconds >= 3600:
        left = f"{seconds // 3600}h{seconds % 3600 // 60}m"
    elif seconds >= 60:
        left = f"{seconds // 60}m{seconds % 60}s"
    else:
        left = f"{seconds}s"
    return target.astimezone(BEIJING).strftime("%m-%d %H:%M") + f" ({left} left)"


def label(name):
    for prefix, text in (("five_hour", "5 小时"), ("seven_day", "7 天")):
        if name == prefix:
            return text
        if name.startswith(prefix + "_"):
            return text + " " + name[len(prefix) + 1:].replace("_", " ").title()
    return name


def cell(text, size=16):
    result, used = "", 0
    for char in str(text):
        if not char.isprintable():
            char = " "
        width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + width > size:
            break
        result, used = result + char, used + width
    return result + " " * (size - used)


def tone(text, value, stale):
    code = 90 if stale or value is None else 32 if value > 50 else 33 if value > 20 else 31
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def read_key(env_name):
    if env_name:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
            raise RuntimeError("Key 环境变量名称无效。")
        key = os.environ.get(env_name, "")
        if not key:
            raise RuntimeError("指定的 Key 环境变量未加载。")
    else:
        # stdin contains this script when invoked with curl | bash.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("CPA API Key（隐藏输入）：")
        except (OSError, getpass.GetPassWarning):
            raise RuntimeError("隐藏输入需要交互终端；自动化请用 --key-env 指定已有环境变量。") from None
    if not re.fullmatch(r"[\x21-\x7e]{1,1024}", key):
        raise RuntimeError("API Key 格式无效。")
    return key


def normalize_base(base):
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        parsed = urlsplit(base)
        valid = (parsed.scheme == "https" and parsed.hostname and parsed.port != 0
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                 and not any(c.isspace() or ord(c) < 32 for c in base))
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("请提供 HTTPS Base URL，不包含用户名、密码、查询参数或片段。")
    return base


def fetch(base, key, path, scope):
    try:
        request = Request(base + path, headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("接口响应过大。")
        data = json.loads(raw)
    except HTTPError as error:
        message = {401: "Key 无效或已撤销", 404: "入口未配置用量/额度接口", 429: "查询过于频繁，请稍后再试"}.get(
            error.code, "请求失败，请检查入口服务；脚本不跟随重定向")
        raise RuntimeError(f"HTTP {error.code}：{message}。") from None
    except (OSError, ValueError):
        raise RuntimeError("查询失败，请检查网络、TLS 证书和接口响应。") from None
    if not isinstance(data, dict) or data.get("scope") != scope:
        raise RuntimeError("接口返回的数据范围错误，请检查 observer 路由。")
    if scope == "shared_pool" and not isinstance(data.get("accounts"), list):
        raise RuntimeError("此入口未返回匿名号池，请检查 /v1/quota 路由。")
    if scope == "own_key" and not isinstance(data.get("totals"), dict):
        raise RuntimeError("此入口未返回本 Key 用量，请检查 /v1/usage 路由。")
    return data


def number(value):
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "未知"


def cost(row):
    low, high = row.get("estimated_cost_usd_low"), row.get("estimated_cost_usd_high")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (low, high)):
        return "未知"
    return f"${low:.4f}" if low == high else f"${low:.4f}–${high:.4f}"


def account_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"account-[0-9a-f]{12}", value) else "未归属"


def beijing(value, seconds=False):
    target = timestamp(value)
    return target.astimezone(BEIJING).strftime("%m-%d %H:%M:%S" if seconds else "%m-%d %H:%M") if target else "未知"


def render_usage(data):
    period = {"day": "今日", "month": "本月", "all": "全部"}.get(data.get("period"), "选定期间")
    if data.get("date"):
        period = cell(data["date"], 10).strip()
    total = data["totals"]
    print(f"本 Key · {period}用量（北京）")
    print(f"请求 {number(total.get('requests'))} · 失败 {number(total.get('failed_requests'))} · "
          f"Tokens {number(total.get('total_tokens'))} · API 标价估算 {cost(total)}")
    print(f"输入 {number(total.get('input_tokens'))} · 输出 {number(total.get('output_tokens'))} · "
          f"缓存读 {number(total.get('cache_read_tokens'))} · 缓存写 {number(total.get('cache_write_tokens'))}")
    print(f"计价覆盖 {number(total.get('priced_requests'))}/{number(total.get('requests'))} · "
          f"采集至 {beijing(data.get('last_collected_at'), True)} · 开始 {beijing(data.get('collection_started_at'))}")
    if "accounts" not in data or "recent" not in data:
        print("服务尚未提供调用账号与明细，请更新 VPS observer。")
        return
    print("\n按调用账号汇总 · 仅本 Key")
    print(f"{cell('匿名账号', 21)} {cell('请求', 7)} {cell('失败', 5)} {cell('Tokens', 13)} API 标价估算")
    for row in data["accounts"]:
        print(f"{cell(account_id(row.get('id')), 21)} {cell(number(row.get('requests')), 7)} "
              f"{cell(number(row.get('failed_requests')), 5)} {cell(number(row.get('total_tokens')), 13)} {cost(row)}")
    if not data["accounts"]:
        print("选定期间暂无已采集调用。")
    print(f"\n近期调用 · 选定期间最近 {data.get('recent_limit', 10)} 条 · 北京时间")
    print(f"{cell('时间', 14)} {cell('匿名账号', 21)} {cell('模型', 24)} {cell('Tokens', 13)} 状态")
    for row in data["recent"]:
        state = "失败" if row.get("failed") else "成功"
        print(f"{cell(beijing(row.get('timestamp'), True), 14)} {cell(account_id(row.get('account_id')), 21)} "
              f"{cell(row.get('model', '未知'), 24)} {cell(number(row.get('total_tokens')), 13)} {state}")
    if not data["recent"]:
        print("暂无记录。")
    print("输入已含缓存，输出已含 reasoning；API 标价估算不代表订阅扣款；重试按多次上游执行记录。")


def render_quota(data):
    accounts = [a for a in data["accounts"] if isinstance(a, dict)]
    enabled = sum(a.get("enabled") is True for a in accounts)
    fresh = sum(a.get("enabled") is True and a.get("stale") is False for a in accounts)
    checked = timestamp(data.get("checked_at"))
    checked_text = checked.astimezone(BEIJING).strftime("%m-%d %H:%M") if checked else "未知"
    print(f"匿名号池 · 剩余额度\n{len(accounts)} 个账号 · {enabled} 启用 · {fresh} 新鲜 · 更新 {checked_text}（北京）")
    bar_size = max(8, min(24, shutil.get_terminal_size((100, 24)).columns - 66))
    if not accounts:
        print("暂无账号额度，请检查 observer 和上游授权。")
    for index, account in enumerate(accounts, 1):
        identity = account.get("id", "")
        if not isinstance(identity, str) or not re.fullmatch(r"account-[0-9a-f]{12}", identity):
            identity = f"account-{index:02d}"
        active = account.get("enabled") is True
        stale = not active or account.get("stale") is not False
        state = "已禁用" if not active else "旧值 / 未知" if stale else "正常"
        print(f"\n{identity} · {state}")
        windows = account.get("windows")
        windows = windows if isinstance(windows, dict) else {}
        rows = [(n, w) for n, w in windows.items() if WINDOW.fullmatch(n) and isinstance(w, dict)]
        if not rows:
            print("  暂无额度窗口")
        for name, window in rows:
            value = window.get("remaining_percent")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
                value = None
            filled = int(bar_size * value / 100 + .5) if value is not None else 0
            bar = "[" + "█" * filled + "░" * (bar_size - filled) + "]"
            percent = f"{value:5.1f}%" if value is not None else "  未知"
            meter = tone(bar + " " + percent, value, stale)
            print(f"  {cell(label(name))} {meter}  重置 {reset_label(window.get('resets_at'))}")
    print("\n█ 剩余  ░ 已用 · 时间为北京时间 · 灰色=旧值/未知 · 各账号百分比不可相加")


def main():
    parser = argparse.ArgumentParser(description="本 Key 用量、近期调用与匿名号池余量；只需 Base URL + 自己的 CPA API Key。")
    parser.add_argument("base_url", nargs="?", default=os.environ.get("CPA_BASEURL", ""), help="HTTPS 根地址；默认 CPA_BASEURL")
    parser.add_argument("--key-env", help="读取已有 Key 环境变量；默认隐藏输入")
    parser.add_argument("--view", choices=["all", "usage", "quota"], default="all", help="默认同时显示个人用量与号池余量")
    parser.add_argument("--period", choices=["day", "month", "all"], default="day", help="用量期间，默认北京时间今日")
    parser.add_argument("--date", help="指定 YYYY-MM-DD 或 YYYY-MM；与 --period 配合")
    parser.add_argument("--recent", type=int, default=10, help="近期明细条数，0–50，默认 10")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("请提供 HTTPS Base URL。")
    if not 0 <= args.recent <= 50:
        parser.error("--recent 必须为 0–50。")
    base = normalize_base(args.base_url)
    key = read_key(args.key_env)
    params = {"period": args.period, "recent": args.recent}
    if args.date:
        params["date"] = args.date
    views = ["usage", "quota"] if args.view == "all" else [args.view]
    failed = False
    for view in views:
        try:
            if view == "usage":
                render_usage(fetch(base, key, "/v1/usage?" + urlencode(params), "own_key"))
            else:
                render_quota(fetch(base, key, "/v1/quota", "shared_pool"))
            print()
        except RuntimeError as error:
            print(f"CPA {view}：{error}", file=sys.stderr)
            failed = True
    if failed:
        sys.exit(1)


try:
    main()
except (KeyboardInterrupt, EOFError):
    print("\n已取消。", file=sys.stderr)
    sys.exit(130)
except RuntimeError as error:
    print(f"CPA：{error}", file=sys.stderr)
    sys.exit(1)
PY
