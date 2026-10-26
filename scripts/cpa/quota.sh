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
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

BEIJING = dt.timezone(dt.timedelta(hours=8))
WINDOW = re.compile(r"(?:five_hour|seven_day)(?:_[a-z0-9_]+)?")
COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class RequiredProxy(ProxyHandler):
    def __init__(self, proxy):
        parsed = urlsplit(proxy)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1") or
                not parsed.port or parsed.username or parsed.password or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment or
                any(c.isspace() or ord(c) < 32 for c in proxy)):
            raise ValueError("invalid local proxy")
        self.authority = parsed.netloc
        super().__init__({"https": proxy})

    def proxy_open(self, request, proxy, kind):
        # ProxyHandler normally honors no_proxy, which would silently permit DIRECT.
        if request.type != "https":
            raise RuntimeError("仅允许 HTTPS 入口。")
        request.set_proxy(self.authority, "http")


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


def fetch(base, key, path, scope, opener):
    try:
        request = Request(base + path, headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
        with opener.open(request, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("接口响应过大。")
        data = json.loads(raw)
    except HTTPError as error:
        message = {400: "查询参数不受支持；总览/滚动时间段需要新版 observer",
                   401: "Key 无效或已撤销", 404: "入口未配置用量/额度接口", 429: "查询过于频繁，请稍后再试"}.get(
            error.code, "请求失败，请检查入口服务；脚本不跟随重定向")
        raise RuntimeError(f"HTTP {error.code}：{message}。") from None
    except (OSError, ValueError):
        raise RuntimeError("查询失败，请检查指定代理、TLS 和接口响应；不会回退直连。") from None
    if not isinstance(data, dict) or data.get("scope") != scope:
        raise RuntimeError("接口返回的数据范围错误，请检查 observer 路由。")
    if scope == "shared_pool" and not isinstance(data.get("accounts"), list):
        raise RuntimeError("此入口未返回匿名号池，请检查 /v1/quota 路由。")
    if scope == "own_key" and not isinstance(data.get("totals"), dict) and not isinstance(data.get("windows"), list):
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
    period = {"1h": "近 1 小时", "7d": "近 7 天", "30d": "近 30 天",
              "day": "今日", "month": "本月", "all": "全部"}.get(data.get("period"), "选定期间")
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


def render_overview(data):
    windows = data.get("windows", [])
    if data.get("period") != "overview" or [w.get("period") for w in windows] != ["1h", "7d", "30d", "all"]:
        raise RuntimeError("服务未提供多时间段总览，请更新 VPS observer。")
    print(f"本 Key · 用量总览 · 截止 {beijing(data.get('as_of'), True)}（北京）")
    print(f"{cell('时间段', 12)} {cell('请求/失败', 15)} {cell('输入 Tokens', 17)} {cell('输出 Tokens', 17)} 总 Tokens")
    labels = {"1h": "近 1 小时", "7d": "近 7 天", "30d": "近 30 天", "all": "总历史"}
    for window in windows:
        row = window["totals"]
        requests = number(row.get("requests")) + '/' + number(row.get("failed_requests"))
        print(f"{cell(labels[window['period']], 12)} {cell(requests, 15)} "
              f"{cell(number(row.get('input_tokens')), 17)} {cell(number(row.get('output_tokens')), 17)} "
              f"{number(row.get('total_tokens'))}")
    print(f"采集开始 {beijing(data.get('collection_started_at'))} · 采集至 {beijing(data.get('last_collected_at'), True)} · "
          f"状态 {cell(data.get('collector_status'), 24).strip()} · 拒收 {number(data.get('rejected_events'))}")
    print("滚动窗口相互重叠，不可相加；总历史仅含已采集并保留的数据，可能存在采集缺口。")


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
    parser.add_argument("--proxy", default=os.environ.get("CPA_PROXY", ""), help="必填本机 HTTP/mixed 代理；默认 CPA_PROXY；禁止直连")
    parser.add_argument("--key-env", help="读取已有 Key 环境变量；默认隐藏输入")
    parser.add_argument("--view", choices=["all", "usage", "quota"], default="all", help="默认同时显示个人用量与号池余量")
    parser.add_argument("--period", choices=["overview", "1h", "7d", "30d", "day", "month", "all"], default="overview",
                        help="默认同屏显示滚动 1h/7d/30d 与总历史；day/month 为北京日历期间")
    parser.add_argument("--date", help="指定 YYYY-MM-DD 或 YYYY-MM；与 --period 配合")
    parser.add_argument("--recent", type=int, default=10, help="近期明细条数，0–50，默认 10")
    parser.add_argument("--tui", action="store_true", help="同屏总览，R/Enter 刷新、Q 退出；不自动轮询")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("请提供 HTTPS Base URL。")
    if not 0 <= args.recent <= 50:
        parser.error("--recent 必须为 0–50。")
    base = normalize_base(args.base_url)
    try:
        opener = build_opener(RequiredProxy(args.proxy), NoRedirect())
    except ValueError:
        raise RuntimeError("必须用 --proxy 或 CPA_PROXY 指定本机 HTTP/mixed 代理，例如 http://127.0.0.1:7890；禁止直连。") from None
    if args.date and args.period not in ("day", "month"):
        parser.error("--date 仅支持 day/month。")
    if args.tui and (args.view != "all" or args.period != "overview" or not sys.stdout.isatty()):
        parser.error("TUI 需要交互终端且使用默认 all/overview；管道或日志采集请省略 --tui。")
    params = {"period": args.period}
    if args.period != "overview":
        params["recent"] = args.recent
    if args.date:
        params["date"] = args.date
    views = ["usage", "quota"] if args.view == "all" else [args.view]
    terminal = None
    try:
        if args.tui:
            try:
                terminal = open("/dev/tty", "r", encoding="utf-8")
            except OSError:
                raise RuntimeError("TUI 需要交互终端。") from None
        key = read_key(args.key_env)
        while True:
            results, errors = {}, {}
            for view in views:
                path = "/v1/usage?" + urlencode(params) if view == "usage" else "/v1/quota"
                scope = "own_key" if view == "usage" else "shared_pool"
                try:
                    results[view] = fetch(base, key, path, scope, opener)
                except RuntimeError as error:
                    errors[view] = str(error)
            if args.tui:
                print("\033[2J\033[H", end="", flush=True)
            print("CPA 只读看板 | 强制本机代理 | Clash 出站仍须选择真实节点")
            failed = bool(errors)
            for view in views:
                try:
                    if view in errors:
                        raise RuntimeError(errors[view])
                    if view == "quota":
                        render_quota(results[view])
                    elif args.period == "overview":
                        render_overview(results[view])
                    else:
                        render_usage(results[view])
                    print()
                except RuntimeError as error:
                    print(f"CPA {view}：{error}", file=sys.stderr)
                    failed = True
            if not args.tui:
                break
            while True:
                print("R/Enter 刷新 · Q 退出: ", end="", flush=True)
                action = terminal.readline()
                if not action or action.strip().lower() in ("", "r", "q"):
                    break
            if not action or action.strip().lower() == "q":
                break
    finally:
        key = None
        if terminal:
            terminal.close()
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
