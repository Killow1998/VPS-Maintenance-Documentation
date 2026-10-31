#!/usr/bin/env bash
# Standalone client: Bash + Python 3.10+, optional private connection profile.
set -euo pipefail
cpa_python=''
for candidate in "${CPA_PYTHON:-python3}" python3.14 python3.13 python3.12 python3.11 python3.10; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    cpa_python="$candidate"
    break
  fi
done
if [[ -z "$cpa_python" ]]; then
  printf '%s\n' '需要 Python 3.10+，无需安装 pip 包。' >&2
  exit 1
fi
"$cpa_python" - "$@" <<'PY'
import sys

if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required. No connection or configuration changes were made.")

import argparse
import contextlib
import datetime as dt
import getpass
import io
import json
import math
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
import unicodedata
import warnings
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

BEIJING = dt.timezone(dt.timedelta(hours=8))
WINDOW = re.compile(r"(?:five_hour|seven_day)(?:_[a-z0-9_]+)?")
COLOR = sys.stdout.isatty() and not bool(os.environ.get("NO_COLOR"))


class TerminalUI:
    def __enter__(self):
        try:
            import curses
        except ImportError:
            raise RuntimeError('This Python installation needs its standard curses module for TUI mode.') from None
        self.curses = curses
        self.screen = None
        self.saved_fds = []
        self.tty = None
        try:
            self.tty = open("/dev/tty", "r+b", buffering=0)
            sys.stdout.flush()
            self.saved_fds = [os.dup(0), os.dup(1)]
            # The embedded Python source occupies stdin; curses needs the real TTY.
            os.dup2(self.tty.fileno(), 0)
            os.dup2(self.tty.fileno(), 1)
            self.screen = curses.initscr()
            curses.noecho()
            curses.cbreak()
            self.screen.keypad(True)
            curses.set_escdelay(30)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self.colors = curses.has_colors() and not bool(os.environ.get("NO_COLOR"))
            self.pairs = {}
            if curses.has_colors():
                curses.start_color()
                for index, color in enumerate((curses.COLOR_BLACK, curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW,
                                               curses.COLOR_BLUE, curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_WHITE)):
                    for code in (30 + index, 90 + index):
                        pair = len(self.pairs) + 1
                        # Bold black is still black on terminals that do not brighten bold text.
                        foreground = (250 if curses.COLORS >= 256 else curses.COLOR_WHITE) if code == 90 else color
                        curses.init_pair(pair, foreground, curses.COLOR_BLACK)
                        self.pairs[code] = pair
                self.screen.bkgd(" ", curses.color_pair(self.pairs[37]))
            return self
        except (OSError, curses.error):
            self.__exit__(None, None, None)
            raise RuntimeError("TUI requires an interactive terminal with TERM configured.") from None

    def __exit__(self, *args):
        if self.screen is not None:
            try:
                self.screen.keypad(False)
                self.curses.nocbreak()
                self.curses.echo()
                self.curses.endwin()
            except self.curses.error:
                pass
        if self.saved_fds:
            sys.stdout.flush()
            for target, saved in enumerate(self.saved_fds):
                os.dup2(saved, target)
                os.close(saved)
        if self.tty:
            self.tty.close()

    def size(self):
        height, width = self.screen.getmaxyx()
        return max(1, width - 1), max(1, height - 4)

    def draw(self, title, lines, footer, selected=-1, notice=""):
        curses = self.curses
        height, width = self.screen.getmaxyx()
        self.screen.erase()
        for row in range(height):
            text, highlight = "", False
            if row == 0:
                text = "\033[96m " + title
            elif row == 1:
                text = "\033[90m" + "-" * max(0, width - 1)
            elif row == height - 2:
                text = "\033[93m" + notice
            elif row == height - 1:
                text = "\033[37m" + footer
            elif row - 2 < len(lines):
                text, highlight = lines[row - 2], row - 2 == selected
            column, color = 0, 97
            for part in re.split(r"(\033\[[0-9;]*m)", text):
                if re.fullmatch(r"\033\[[0-9;]*m", part):
                    for code in part[2:-1].split(";"):
                        color = 97 if code in ("", "0") else int(code) if code.isdigit() else color
                    continue
                remaining = max(0, width - 1 - column)
                if not remaining:
                    break
                part_width = sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in part)
                safe = cell(part, min(remaining, part_width))
                attr = curses.A_REVERSE if highlight else curses.A_NORMAL
                if self.colors and not highlight:
                    if color in self.pairs:
                        attr |= curses.color_pair(self.pairs[color])
                    if color >= 90 and color != 90:
                        attr |= curses.A_BOLD
                try:
                    self.screen.addstr(row, column, safe, attr)
                except curses.error:
                    pass
                column += sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in safe)
        self.screen.refresh()

    def key(self):
        curses = self.curses
        value = self.screen.get_wch()
        if value == "\x03":
            raise KeyboardInterrupt
        names = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left",
                 curses.KEY_RIGHT: "right", curses.KEY_HOME: "home", curses.KEY_END: "end",
                 curses.KEY_PPAGE: "pageup", curses.KEY_NPAGE: "pagedown",
                 curses.KEY_BACKSPACE: "backspace", curses.KEY_DC: "delete",
                 curses.KEY_BTAB: "backtab", curses.KEY_RESIZE: "resize",
                 curses.KEY_ENTER: "enter", "\n": "enter", "\r": "enter",
                 "\t": "tab", "\x1b": "escape", "\x7f": "backspace", "\x08": "backspace"}
        return names.get(value, value)

    def form(self, title, fields, actions=("Continue", "Cancel"), initial=0):
        values = [field.get("value", "") for field in fields]
        positions = [len(str(value)) for value in values]
        replace = [True] * len(fields)
        selected, notice = initial, ""
        while True:
            width, capacity = self.size()
            available = max(1, width - 24)
            lines = []
            for index, field in enumerate(fields):
                text = str(values[index])
                if field.get("secret"):
                    text = "*" * len(text)
                if field.get("toggle"):
                    text = "[x]" if values[index] else "[ ]"
                start = max(0, positions[index] - available + 1) if index == selected and not field.get("toggle") else 0
                lines.append("  " + cell(field["label"], 21) + " " + text[start:])
            lines.extend("  " + action for action in actions)
            self.draw(title, lines, "Tab / arrows: move   Enter: select   Space: toggle   Esc: exit", selected, notice)
            try:
                editable = selected < len(fields) and not fields[selected].get('toggle')
                self.curses.curs_set(1 if editable else 0)
                if editable and selected < capacity and width >= 51:
                    start = max(0, positions[selected] - available + 1)
                    self.screen.move(selected + 2, min(width - 1, 24 + positions[selected] - start))
                    self.screen.refresh()
            except self.curses.error:
                pass
            key = self.key()
            if key == "escape":
                raise EOFError
            if width < 51 or capacity < len(lines):
                notice = "Enlarge the terminal to continue. Esc exits."
                continue
            count = len(fields) + len(actions)
            if key in ("up", "down", "tab", "backtab"):
                selected = (selected + (-1 if key in ("up", "backtab") else 1)) % count
                continue
            if key == "enter":
                if selected >= len(fields):
                    action = selected - len(fields)
                    if action:
                        return action, values
                    valid = True
                    for index, field in enumerate(fields):
                        try:
                            if field.get("validate"):
                                values[index] = field["validate"](values[index])
                        except RuntimeError as error:
                            selected, notice, valid = index, str(error), False
                            break
                    if valid:
                        return action, values
                else:
                    try:
                        if fields[selected].get("validate"):
                            values[selected] = fields[selected]["validate"](values[selected])
                        positions[selected] = len(str(values[selected]))
                        selected, notice = (selected + 1) % count, ""
                    except RuntimeError as error:
                        notice = str(error)
                continue
            if selected >= len(fields):
                continue
            field = fields[selected]
            if field.get("toggle"):
                if key in (" ", "left", "right"):
                    values[selected] = not values[selected]
                continue
            value, position = str(values[selected]), positions[selected]
            if key == "\x01":
                replace[selected] = True
            elif key == "\x15":
                values[selected], positions[selected], replace[selected] = "", 0, False
            elif key in ("left", "right", "home", "end"):
                positions[selected] = (max(0, position - 1) if key == "left" else min(len(value), position + 1)
                                       if key == "right" else 0 if key == "home" else len(value))
                replace[selected] = False
            elif key == "backspace":
                if replace[selected]:
                    values[selected], positions[selected] = "", 0
                elif position:
                    values[selected] = value[:position - 1] + value[position:]
                    positions[selected] -= 1
                replace[selected] = False
            elif key == "delete":
                values[selected] = value[:position] + value[position + 1:]
                replace[selected] = False
            elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                if replace[selected]:
                    value, position = "", 0
                if len(value) < field.get("max_length", 2048):
                    values[selected] = value[:position] + key + value[position:]
                    positions[selected] = position + 1
                replace[selected], notice = False, ""


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


def proxy_from_port(value):
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
        raise RuntimeError("代理端口必须是 1–65535 的整数。")
    return "http://127.0.0.1:" + str(int(value))


def prompt_setting(prompt, validate):
    # Python stdin is the embedded source, including when Bash reads a local file.
    if not sys.stdout.isatty():
        raise RuntimeError("交互设置需要终端；自动化请指定 Base URL、--proxy 和 --key-env。")
    try:
        terminal = open("/dev/tty", "r", encoding="utf-8")
    except OSError:
        raise RuntimeError("交互设置需要终端；自动化请指定 Base URL、--proxy 和 --key-env。") from None
    with terminal:
        while True:
            print(prompt, end="", flush=True)
            value = terminal.readline()
            if not value:
                raise EOFError
            try:
                return validate(value.strip())
            except RuntimeError as error:
                print(str(error), file=sys.stderr)


def config_path():
    return Path.home() / ".cpa" / "query.json"


def private_path(path, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("配置权限无效；目录需要 700，文件需要 600，且归当前用户所有。")


def load_config():
    path = config_path()
    try:
        private_path(path.parent, directory=True)
        private_path(path)
        with path.open(encoding="utf-8") as source:
            saved = json.loads(source.read(16385))
        if saved.get("version") != 1:
            raise ValueError
        saved["base_url"] = normalize_base(saved["base_url"])
        RequiredProxy(saved["proxy_url"])
        if not re.fullmatch(r"[\x21-\x7e]{1,1024}", saved["api_key"]):
            raise ValueError
        return saved
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        raise RuntimeError("已存配置无法读取、权限不安全或内容无效；请重新输入。") from None


def save_config(base, proxy, key):
    path = config_path()
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        private_path(path.parent, directory=True)
        if path.exists() or path.is_symlink():
            private_path(path)
        fd, temporary = tempfile.mkstemp(prefix=".query-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"version": 1, "base_url": base, "proxy_url": proxy, "api_key": key}, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except (OSError, RuntimeError):
        raise RuntimeError("配置未保存；请检查 ~/.cpa 的权限和可用空间。") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def config_choice(value):
    if value not in ("1", "2"):
        raise RuntimeError("请输入 1 或 2。")
    return value


def select_config(args):
    if not sys.stdout.isatty():
        return None
    choice = os.environ.get("CPA_QUERY_CHOICE")
    if choice is None and not (args.base_url or args.proxy or args.key_env) and config_path().exists():
        if args.tui is not False and args.view == 'all' and args.period == 'overview':
            with TerminalUI() as terminal:
                selected, _ = terminal.form('CPA / 已保存的连接', [], ('载入已有配置', '重新配置', '退出'))
            if selected == 2:
                raise EOFError
            choice = str(selected + 1)
        else:
            choice = prompt_setting("1 载入已有配置  2 重新输入：", config_choice)
    if choice == "1":
        try:
            return load_config()
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
    return None


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
    if low < 0 or high < low:
        return "未知"
    precision = 8 if any(0 < x < .0001 for x in (low, high)) else 4
    left, right = f"${low:.{precision}f}", f"${high:.{precision}f}"
    value = left if left == right else left + "–" + right
    priced, requests = row.get("priced_requests"), row.get("requests")
    partial = isinstance(priced, int) and isinstance(requests, int) and 0 <= priced < requests
    return ("*" if partial else "") + value


def cache_percent(row):
    read, total = row.get("cache_read_tokens"), row.get("input_tokens")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (read, total)):
        return None
    return read / total * 100 if total > 0 and 0 <= read <= total else None


def cache_label(row):
    value = cache_percent(row)
    return "未知" if value is None else f"{value:.1f}%"


def compact(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "未知"
    for size, suffix in ((10**12, "T"), (10**9, "B"), (10**6, "M"), (10**3, "K")):
        if value >= size * 10:
            return f"{value / size:.2f}{suffix}"
    return number(value)


def coverage(row):
    return compact(row.get("priced_requests")) + "/" + compact(row.get("requests"))


def render_models(models, page=0, paged=False):
    if not isinstance(models, list):
        print("服务尚未提供模型分项，请更新 VPS observer；不能据此判断 Key 没有调用。")
        return
    if not models:
        print("选定期间暂无已采集调用。")
        return
    rows = sorted(models, key=lambda r: (-(r.get("estimated_cost_usd_high") or 0),
                                        -(r.get("total_tokens") or 0), r.get("model", "")))
    pages = max(1, (len(rows) + 4) // 5)
    if paged:
        page %= pages
        print(f"模型 {len(rows)} 个 · 第 {page + 1}/{pages} 页")
        rows = rows[page * 5:(page + 1) * 5]
    print(f"{cell('模型', 18)} {cell('请求', 7)} {cell('Tokens', 9)} {cell('缓存', 6)} {cell('估算 USD', 23)} 计价覆盖")
    for row in rows:
        name = str(row.get("model", "未知"))
        if len(name) > 18:
            print(cell(name, 160).strip())
            name = "  >"
        print(f"{cell(name, 18)} {cell(compact(row.get('requests')), 7)} {cell(compact(row.get('total_tokens')), 9)} "
              f"{cell(cache_label(row), 6)} {cell(cost(row), 23)} {coverage(row)}")


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
    print(f"Token 缓存命中率 {cache_label(total)} · * 部分计价，不是全部费用")
    render_models(data.get("models"))
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


def render_overview(data, selected="1h", page=0, paged=False):
    windows = data.get("windows", [])
    if data.get("period") != "overview" or [w.get("period") for w in windows] != ["1h", "7d", "30d", "all"]:
        raise RuntimeError("服务未提供多时间段总览，请更新 VPS observer。")
    print(f"本 Key · 用量总览 · 截止 {beijing(data.get('as_of'), True)}（北京）")
    print(f"{cell('时间段', 10)} {cell('请求/失败', 11)} {cell('Tokens', 9)} {cell('缓存', 7)} {cell('估算 USD', 23)} 计价覆盖")
    labels = {"1h": "近 1 小时", "7d": "近 7 天", "30d": "近 30 天", "all": "总历史"}
    for window in windows:
        row = window["totals"]
        requests = compact(row.get("requests")) + '/' + compact(row.get("failed_requests"))
        print(f"{cell(labels[window['period']], 10)} {cell(requests, 11)} {cell(compact(row.get('total_tokens')), 9)} "
              f"{cell(cache_label(row), 7)} {cell(cost(row), 23)} {coverage(row)}")
    window = next(w for w in windows if w["period"] == selected)
    total = window["totals"]
    print(f"\n模型明细 · {labels[selected]} · 按估算 USD 降序")
    print(f"输入 {number(total.get('input_tokens'))} · 输出 {number(total.get('output_tokens'))}")
    print(f"缓存读 {number(total.get('cache_read_tokens'))} · 缓存写 {number(total.get('cache_write_tokens'))}")
    render_models(window.get("models"), page, paged)
    print("缓存 = 缓存读 / 全部输入 token；* 部分计价，缺失不计为零；USD 不是订阅扣款。")
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


def tint(text, color=97):
    return f"\033[{color}m{text}\033[0m"


def dashboard_lines(results, errors, selected="1h", page=0, panel="overview", width=100):
    lines = ["   ".join(tint("[" + title + "]", 96) if key == panel else tint(" " + title + " ", 90)
                        for key, title in (("overview", "总览"), ("models", "模型明细"), ("quota", "账号额度"))), ""]
    usage, quota = results.get("usage", {}), results.get("quota", {})
    windows = usage.get("windows", [])
    window = None
    if usage and "usage" not in errors:
        if usage.get("period") != "overview" or [row.get("period") for row in windows] != ["1h", "7d", "30d", "all"]:
            errors["usage"] = "服务未提供多时间段总览，请更新 observer。"
        else:
            window = next(row for row in windows if row["period"] == selected)
    if panel != "quota":
        lines.append(tint("── 本 Key 用量", 96) + tint(f"  截止 {beijing(usage.get('as_of'), True)} 北京", 90))
        if "usage" in errors:
            lines.append(tint(errors["usage"], 91))
        elif window:
            lines.append(tint(f"{cell('期间', 10)} {cell('请求/失败', 12)} {cell('Tokens', 12)} {cell('缓存', 8)} 估算 USD", 90))
            labels = {"1h": "1 近1小时", "7d": "2 近7天", "30d": "3 近30天", "all": "4 总历史"}
            for row in windows:
                total = row["totals"]
                requests = compact(total.get("requests")) + "/" + compact(total.get("failed_requests"))
                lines.append(tint(cell(labels[row["period"]], 10), 96 if row["period"] == selected else 37) + " " +
                             cell(requests, 12) + " " + tint(cell(compact(total.get("total_tokens")), 12), 96) + " " +
                             tint(cell(cache_label(total), 8), 92) + " " + tint(cost(total), 93))
        lines.append("")
    if panel != "models":
        accounts = [row for row in quota.get("accounts", []) if isinstance(row, dict)]
        active = sum(row.get("enabled") is True for row in accounts)
        lines.append(tint("── 账号额度", 96) + tint(f"  {active}/{len(accounts)} 启用 · 剩余 · 更新 {beijing(quota.get('checked_at'))} 北京", 90))
        if "quota" in errors:
            lines.append(tint(errors["quota"], 91))
        elif not accounts:
            lines.append(tint("暂无账号额度", 90))
        else:
            quota_lines = []
            for index, account in enumerate(accounts, 1):
                stale = account.get("enabled") is not True or account.get("stale") is not False
                state = "禁用" if account.get("enabled") is not True else "旧值" if stale else ""
                if panel == "quota":
                    identity = account.get("id", "")
                    if not isinstance(identity, str) or not re.fullmatch(r"account-[0-9a-f]{12}", identity):
                        identity = f"account-{index:02d}"
                    quota_lines.append(tint(f"A{index:02d}  {identity}  {state}", 37))
                account_windows = account.get("windows")
                rows = [(name, row) for name, row in (account_windows if isinstance(account_windows, dict) else {}).items()
                        if WINDOW.fullmatch(name) and isinstance(row, dict)]
                if not rows:
                    quota_lines.append(tint(f"A{index:02d}  暂无额度窗口", 90))
                for name, row in rows:
                    value = row.get("remaining_percent")
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
                        value = None
                    color = 90 if stale or value is None else 92 if value > 50 else 93 if value > 20 else 91
                    size = 24 if width >= 100 else 16
                    filled = int(size * value / 100 + .5) if value is not None else 0
                    bar = tint("█" * filled, color) + tint("░" * (size - filled), 90)
                    percent = f"{value:5.1f}%" if value is not None else "  未知"
                    quota_lines.append(cell(f"A{index:02d} {label(name)}", 15) + " " + bar + " " +
                                       tint(percent, color) + " " + tint(state + " " + reset_label(row.get("resets_at")), 37))
                if panel == "quota":
                    quota_lines.append("")
            limit = 4 if panel == "overview" else len(quota_lines)
            lines.extend(quota_lines[:limit])
            if len(quota_lines) > limit:
                lines.append(tint(f"另 {len(quota_lines) - limit} 条，Tab 查看账号额度", 90))
        lines.append("")
    if panel != "quota" and window:
        models = window.get("models")
        rows = sorted(models or [], key=lambda row: (-(row.get("estimated_cost_usd_high") or 0), -(row.get("total_tokens") or 0), row.get("model", "")))
        size = 3 if panel == "overview" else 5
        pages = max(1, math.ceil(len(rows) / size))
        page %= pages
        lines.append(tint(f"── 模型 / {selected}", 96) + tint(f"  {len(rows)} 个 · 第 {page + 1}/{pages} 页 · USD 降序", 90))
        if models is None:
            lines.append(tint("接口缺少模型明细，请更新 observer", 93))
        elif not rows:
            lines.append(tint("选定期间暂无已采集调用", 90))
        else:
            name_width = 30 if width >= 100 else 24
            lines.append(tint(f"{cell('模型', name_width)} {'请求':>5} {'Tokens':>11} {'缓存':>5} 估算 USD", 90))
            for index, row in enumerate(rows[page * size:(page + 1) * size], page * size):
                lines.append(tint(cell(row.get("model", ""), name_width), (96, 92, 95, 93)[index % 4]) + " " +
                             cell(compact(row.get("requests")), 7).strip().rjust(7) + " " + cell(compact(row.get("total_tokens")), 11).strip().rjust(11) + " " +
                             tint(cell(cache_label(row), 7).strip().rjust(7), 92) + " " + tint(cost(row), 93))
        if panel == "models":
            total = window["totals"]
            lines.extend(["", tint(f"输入 {compact(total.get('input_tokens'))}  输出 {compact(total.get('output_tokens'))}  "
                                   f"缓存读 {compact(total.get('cache_read_tokens'))}  写 {compact(total.get('cache_write_tokens'))}", 37),
                          tint(f"计价覆盖 {coverage(total)} · * 仅含已计价费用", 90),
                          tint(f"采集至 {beijing(usage.get('last_collected_at'), True)} 北京 · {cell(usage.get('collector_status', '未知'), 20).strip()} · "
                               f"拒收 {number(usage.get('rejected_events'))}", 90)])
    return lines


def connection_form(proxy, base, key_env):
    port, host = '7890', '127.0.0.1'
    if proxy:
        RequiredProxy(proxy)
        parsed = urlsplit(proxy)
        port, host = str(parsed.port), '[::1]' if parsed.hostname == '::1' else '127.0.0.1'
    key = read_key(key_env) if key_env else ''
    def validate_key(value):
        if not re.fullmatch(r'[\x21-\x7e]{1,1024}', value):
            raise RuntimeError('请输入有效的 API Key。')
        return value
    def validate_port(value):
        return str(urlsplit(proxy_from_port(value)).port)
    with TerminalUI() as terminal:
        action, values = terminal.form('CPA / 连接设置', [
            {'label': 'VPN 代理端口', 'value': port, 'max_length': 5, 'validate': validate_port},
            {'label': 'HTTPS Base URL', 'value': base or '', 'validate': normalize_base},
            {'label': 'API Key', 'value': key, 'secret': True, 'max_length': 1024, 'validate': validate_key},
            {'label': '记住连接（含 Key）', 'value': False, 'toggle': True},
        ], ('连接', '退出'), initial=1 if proxy else 0)
    if action:
        raise EOFError
    return 'http://' + host + ':' + values[0], values[1], values[2], values[3]


def main():
    parser = argparse.ArgumentParser(description="本 Key 用量、近期调用与匿名号池余量；只需 Base URL + 自己的 CPA API Key。")
    parser.add_argument("base_url", nargs="?", default=os.environ.get("CPA_BASEURL", ""), help="HTTPS 根地址；默认 CPA_BASEURL；未提供则交互输入")
    parser.add_argument("--proxy", default=os.environ.get("CPA_PROXY", ""), help="本机 HTTP/mixed 代理；默认 CPA_PROXY；未提供则询问端口；禁止直连")
    parser.add_argument("--key-env", help="读取已有 Key 环境变量；默认隐藏输入")
    parser.add_argument("--view", choices=["all", "usage", "quota"], default="all", help="默认同时显示个人用量与号池余量")
    parser.add_argument("--period", choices=["overview", "1h", "7d", "30d", "day", "month", "all"], default="overview",
                        help="默认同屏显示滚动 1h/7d/30d 与总历史；day/month 为北京日历期间")
    parser.add_argument("--date", help="指定 YYYY-MM-DD 或 YYYY-MM；与 --period 配合")
    parser.add_argument("--recent", type=int, default=10, help="近期明细条数，0–50，默认 10")
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--tui", dest="tui", action="store_true", help="同屏总览，R/Enter 刷新、Q 退出；交互设置后默认启用")
    display.add_argument("--once", dest="tui", action="store_false", help="仅查询一次后退出，不进入 TUI")
    parser.set_defaults(tui=None)
    args = parser.parse_args()
    if not 0 <= args.recent <= 50:
        parser.error("--recent 必须为 0–50。")
    if args.date and args.period not in ("day", "month"):
        parser.error("--date 仅支持 day/month。")
    if args.tui and (args.view != "all" or args.period != "overview" or not sys.stdout.isatty()):
        parser.error("TUI 需要交互终端且使用默认 all/overview；单次查询请用 --once。")
    saved = select_config(args)
    if saved:
        args.base_url, args.proxy = saved["base_url"], saved["proxy_url"]
    prompted = bool(saved) or not args.proxy or not args.base_url
    connection_key, remember = None, False
    if (args.tui is not False and sys.stdout.isatty() and not saved and
            (prompted or not sys.argv[1:] or args.tui) and args.view == 'all' and args.period == 'overview'):
        args.proxy, args.base_url, connection_key, remember = connection_form(args.proxy, args.base_url, args.key_env)
    if not args.proxy:
        args.proxy = prompt_setting("本机 HTTP/mixed 代理端口（如 7890）：", proxy_from_port)
    try:
        opener = build_opener(RequiredProxy(args.proxy), NoRedirect())
    except ValueError:
        raise RuntimeError("必须用 --proxy 或 CPA_PROXY 指定本机 HTTP/mixed 代理，例如 http://127.0.0.1:7890；禁止直连。") from None
    base = normalize_base(args.base_url) if args.base_url else prompt_setting("HTTPS Base URL：", normalize_base)
    if args.tui is None:
        args.tui = (prompted or not sys.argv[1:]) and args.view == "all" and args.period == "overview" and sys.stdout.isatty()
    params = {"period": args.period}
    if args.period != "overview":
        params["recent"] = args.recent
    if args.date:
        params["date"] = args.date
    views = ["usage", "quota"] if args.view == "all" else [args.view]
    terminal = None
    key = None
    try:
        if args.tui:
            terminal = TerminalUI().__enter__()
        key = saved["api_key"] if saved else connection_key if connection_key is not None else read_key(args.key_env)
        connection_key = None
        offer_save = sys.stdout.isatty() and not saved and not args.tui
        refresh, selected, page, offset, notice, panel = True, "1h", 0, 0, '', 'overview'
        while True:
            if refresh:
                if terminal:
                    terminal.draw('CPA / 用量与额度', ['正在通过 VPN 查询…'], '查询中')
                results, errors = {}, {}
                for view in views:
                    path = "/v1/usage?" + urlencode(params) if view == "usage" else "/v1/quota"
                    scope = "own_key" if view == "usage" else "shared_pool"
                    try:
                        results[view] = fetch(base, key, path, scope, opener)
                    except RuntimeError as error:
                        errors[view] = str(error)
            failed = bool(errors)
            if args.tui:
                lines = dashboard_lines(results, errors, selected, page, panel, terminal.size()[0])
                failed = bool(errors)
            output = io.StringIO() if args.tui else sys.stdout
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output if args.tui else sys.stderr):
                if not args.tui:
                    print("CPA 只读看板 | VPN 代理")
                for view in ([] if args.tui else views):
                    try:
                        if view in errors:
                            raise RuntimeError(errors[view])
                        if view == "quota":
                            render_quota(results[view])
                        elif args.period == "overview":
                            render_overview(results[view], selected, page, args.tui)
                        else:
                            render_usage(results[view])
                        print()
                    except RuntimeError as error:
                        print(f"CPA {view}：{error}", file=sys.stderr)
                        failed = True
            if not failed and remember:
                save_config(base, args.proxy, key)
                remember, notice = False, '已保存连接配置。'
            if not failed and offer_save:
                offer_save = False
                answer = prompt_setting(f"保存代理、Base URL 和 API Key 到 {config_path()}？[y/N]：", str.lower)
                if answer in ("y", "yes"):
                    save_config(base, args.proxy, key)
                    print(f"已保存：{config_path()}")
            if not args.tui:
                break
            capacity = terminal.size()[1]
            limit = max(0, len(lines) - capacity)
            offset = min(offset, limit)
            color_state = '彩色' if terminal.colors else '单色(C开启)'
            status = '部分查询失败' if failed else '只读 · ' + color_state
            terminal.draw('CPA  用量看板 · ' + status, lines[offset:offset + capacity],
                          'Tab 视图  1-4 时段  N/P 翻页  ↑↓ 滚动  C 配色  R 刷新  Q 退出', notice=notice)
            action = terminal.key()
            if action in ('q', 'Q', 'escape'):
                break
            action = action.lower() if isinstance(action, str) else ''
            refresh = action in ('enter', 'r')
            if action in ("1", "2", "3", "4"):
                selected, page = ("1h", "7d", "30d", "all")[int(action) - 1], 0
            elif action in ("n", "p"):
                page += 1 if action == "n" else -1
            elif action in ('left', 'right', 'tab', 'backtab'):
                panels = ('overview', 'models', 'quota')
                panel = panels[(panels.index(panel) + (1 if action in ('right', 'tab') else -1)) % 3]
                offset, page = 0, 0
            elif action == 'c':
                terminal.colors = not terminal.colors and terminal.curses.has_colors()
            elif action == 'up':
                offset = max(0, offset - 1)
            elif action == 'down':
                offset = min(limit, offset + 1)
            elif action == 'pageup':
                offset = max(0, offset - capacity)
            elif action == 'pagedown':
                offset = min(limit, offset + capacity)
            elif action == 'home':
                offset = 0
            elif action == 'end':
                offset = limit
    finally:
        key = None
        if terminal:
            terminal.__exit__(None, None, None)
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
