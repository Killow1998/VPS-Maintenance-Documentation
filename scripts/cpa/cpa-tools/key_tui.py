"""Single-screen CPA Key management, backed by the existing CLI operations."""
import argparse
import curses
import datetime as dt
import locale
import queue
import sys
import threading
import time
from zoneinfo import ZoneInfo

from common import remote, ssh_session
from key_view import cell, clip, detail_lines, table_header, table_line, width
from quota_view import QuotaView
from tui_theme import apply_theme, style


class Screen:
    def __init__(self, window, cfg, execute, validate):
        self.w, self.cfg, self.execute, self.validate = window, cfg, execute, validate
        self.data, self.usage, self.selected = [], {}, 0
        self.period, self.filter, self.history = "day", "", False
        self.status, self.error, self.busy, self.writing = "", False, False, False
        self.updated = "尚未刷新"
        self.events = queue.Queue()
        self.cache, self.dirty, self.pending_refresh = {}, True, False
        self.quota_snapshot = None
        self.colors = apply_theme(window)
        self.w.keypad(True)
        self.w.timeout(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    def put(self, y, x, text, attr=0):
        height, size = self.w.getmaxyx()
        if 0 <= y < height and 0 <= x < size - 1:
            try:
                self.w.addstr(y, x, clip(text, size - x - 1), style(self.colors, attr))
            except curses.error:
                pass

    def rows(self):
        return [r for r in self.data if (self.history or r["active"]) and self.filter.casefold() in r["name"].casefold()]

    def draw(self):
        self.w.erase()
        height, size = self.w.getmaxyx()
        if height < 18 or size < 80:
            self.put(0, 0, "请将终端放大至 80×18；q 退出。", curses.A_BOLD)
            self.w.refresh()
            return
        periods = {"day": "今日", "month": "本月", "all": "全部"}
        self.put(0, 1, "CPA / API KEYS", curses.A_BOLD)
        self.put(0, 22, "1 今日   2 本月   3 全部  |  " + periods[self.period] + " · 北京时间")
        demo = "演示 · " if self.cfg.get("_demo") else ""
        self.put(1, 1, demo + str(sum(r["active"] for r in self.data)) + " 有效  /  " + str(len(self.data)) +
                 " 总数  ·  " + self.updated + ("  ·  处理中…" if self.busy else ""), curses.A_DIM)
        self.put(3, 1, table_header(size - 3), curses.A_BOLD)
        rows = self.rows()
        self.selected = max(0, min(self.selected, len(rows) - 1))
        visible = height - 11
        start = max(0, self.selected - visible + 1)
        for i, row in enumerate(rows[start:start + visible], start):
            totals = self.usage.get(row["id"], {}).get("totals", {})
            attr = curses.A_REVERSE if i == self.selected else 0
            self.put(4 + i - start, 1, cell(table_line(row, totals, size - 3), size - 3), attr)
        if not rows:
            self.put(5, 2, "正在加载…" if self.busy else "没有匹配的 Key", curses.A_DIM)
        if rows:
            row = rows[self.selected]
            self.put(height - 6, 1, "选中 " + row["name"] + "  ·  ID " + row["id"][:12] + "  ·  Enter 查看模型明细", curses.A_DIM)
        self.put(height - 5, 1, "筛选 " + (self.filter or "全部") + "  ·  " + ("含历史" if self.history else "仅有效") +
                 "  ·  USD 为 API 标价估算；未知不等于零", curses.A_DIM)
        self.put(height - 3, 1, self.status, curses.A_BOLD if self.error else curses.A_DIM)
        self.put(height - 2, 1, "↑↓ 选择  n 新增  e 改名  k 换Key  x 删除  s 导出  Enter 明细")
        self.put(height - 1, 1, "g 剩余额度  / 筛选  h 历史  r 刷新  1/2/3 时间范围  q 退出")
        self.w.refresh()

    def background(self, kind, work):
        self.busy, self.writing, self.error = True, kind == "action", False
        self.dirty = True
        def worker():
            try:
                self.events.put((kind, work(), None))
            except Exception as error:
                self.events.put((kind, None, str(error) if isinstance(error, RuntimeError) else type(error).__name__))
        threading.Thread(target=worker, daemon=True).start()

    def apply_data(self, result):
        self.data = sorted(result["keys"], key=lambda r: (not r["active"], r["name"]))
        self.usage = {u["id"]: u for u in result.get("usage", [])}
        self.updated = result["_updated"] + " 更新"
        self.status = "用量暂不可用，Key 列表可用" if result.get("usage_error") else "就绪"
        if result.get("quota") is not None:
            self.quota_snapshot = result["quota"]
        self.dirty = True

    def refresh(self, force=False):
        cached = self.cache.get(self.period)
        if not force and cached and time.monotonic() - cached[0] < 15:
            self.apply_data(cached[1])
            return
        if self.busy:
            self.pending_refresh = True
            return
        self.status = "读取 VPS 状态与用量…"
        period = self.period
        def load():
            result = ({**demo_data(), **demo_quota()} if self.cfg.get("_demo") else
                      remote(self.cfg, "keys_list", include_usage=True, include_quota=True, period=period))
            result.update(_period=period, _updated=dt.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S"))
            return result
        self.background("load", load)

    def notice(self, title, lines):
        offset = 0
        while True:
            self.w.erase()
            height, size = self.w.getmaxyx()
            wrapped = []
            for line in lines:
                line = str(line)
                while width(line) > max(4, size - 4):
                    part = clip(line, max(4, size - 4))
                    wrapped.append(part)
                    line = line[len(part):]
                wrapped.append(line)
            visible = max(1, height - 5)
            offset = max(0, min(offset, len(wrapped) - visible))
            self.put(1, 2, title, curses.A_BOLD)
            for i, line in enumerate(wrapped[offset:offset + visible]):
                self.put(3 + i, 2, line)
            self.put(height - 1, 2, "Enter / Esc 返回   ↑↓ 滚动", curses.A_DIM)
            self.w.refresh()
            key = self.w.getch()
            if key == -1:
                # A modal has no background work to paint; wait for input rather
                # than redrawing ten times per second.
                while key == -1:
                    key = self.w.getch()
            if key in (10, 13, 27, ord("q")):
                return
            if key == curses.KEY_DOWN:
                offset += 1
            if key == curses.KEY_UP:
                offset -= 1

    def prompt(self, title, label, default=""):
        value = default
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                self.draw()
                height, size = self.w.getmaxyx()
                for y in range(max(0, height - 5), height):
                    self.w.move(y, 0)
                    self.w.clrtoeol()
                self.put(height - 5, 2, title, curses.A_BOLD)
                self.put(height - 4, 2, label)
                self.put(height - 3, 2, "> " + value[-max(1, size - 7):])
                self.put(height - 2, 2, "Enter 确认   Esc 取消", curses.A_DIM)
                self.w.refresh()
                try:
                    key = self.w.get_wch()
                except curses.error:
                    while True:
                        try:
                            key = self.w.get_wch()
                            break
                        except curses.error:
                            pass
                if key in ("\n", "\r"):
                    return value.strip()
                if key == "\x1b":
                    return None
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    value = value[:-1]
                elif isinstance(key, str) and key.isprintable() and len(value) < 160:
                    value += key
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def action(self, key):
        if self.cfg.get("_demo"):
            self.notice("演示模式", ["只展示界面；不连接 VPS，不创建或更改 Key。"])
            return
        rows = self.rows()
        row = rows[self.selected] if rows else None
        args = argparse.Namespace(command=None, yes=True, selector=row["id"] if row else None,
                                  name=None, out=None, period=self.period, date=None)
        if key == "n":
            value = self.prompt("新增 Key", "调用者名称；自动保存到原 CPA env")
            if value is None:
                return
            args.command, args.name = "add", self.validate(value)
        else:
            if not row or not row["active"]:
                self.status, self.error = "请选择有效的 Key。历史用量可按 Enter 查看。", True
                return
            if key == "e":
                value = self.prompt("修改名称", "新名称", row["name"])
                if value is None:
                    return
                args.command, args.name = "rename", self.validate(value)
            elif key in ("k", "x"):
                warning = "旧 Key 将失效，需重新加载 env" if key == "k" else "撤销访问，保留历史用量"
                value = self.prompt(("换 Key · " if key == "k" else "删除 · ") + row["name"],
                                    warning + "；输入调用者名称确认")
                if value != row["name"]:
                    self.status = "操作已取消"
                    return
                args.command = "rotate" if key == "k" else "delete"
            elif key == "s":
                args.command, args.name = "export", row["name"]
        self.status = "正在保存，请等待结果…"
        self.background("action", lambda: self.execute(self.cfg, args))

    def loop(self):
        self.refresh()
        while True:
            try:
                kind, result, error = self.events.get_nowait()
            except queue.Empty:
                pass
            else:
                self.busy, self.writing = False, False
                if error:
                    self.status, self.error = "操作失败 · r 刷新状态后可重试", True
                    self.notice("操作未完成", error.splitlines())
                elif kind == "load":
                    self.cache[result["_period"]] = (time.monotonic(), result)
                    if result["_period"] == self.period:
                        self.apply_data(result)
                    else:
                        self.pending_refresh = True
                elif kind == "quota":
                    self.quota_snapshot = result["quota"]
                    self.show_quota()
                    self.status = "就绪"
                else:
                    lines = [result.get("message", "完成")]
                    if result.get("env_file"):
                        lines += ["", "CPA env（绝对路径）", result["env_file"]]
                    if result.get("reload_required"):
                        lines += ["", "需要在当前终端使用时，source 上述文件。"]
                    self.notice("操作成功", lines)
                    self.cache.clear()
                    self.pending_refresh = False
                    self.refresh()
                self.dirty = True
                if self.pending_refresh and not self.busy:
                    self.pending_refresh = False
                    self.refresh(force=True)
            if self.dirty:
                self.draw()
                self.dirty = False
            key = self.w.getch()
            if key == -1:
                continue
            self.dirty = True
            if key in (ord("q"), 27):
                if self.writing:
                    self.status = "正在提交变更，完成后可退出。"
                else:
                    return
            rows = self.rows()
            height, size = self.w.getmaxyx()
            if height < 18 or size < 80:
                continue
            if key == curses.KEY_UP:
                self.selected = max(0, self.selected - 1)
            elif key == curses.KEY_DOWN:
                self.selected = min(max(0, len(rows) - 1), self.selected + 1)
            elif key in (10, 13) and rows:
                row = rows[self.selected]
                self.notice("用量明细", detail_lines(row, self.usage.get(row["id"], {})))
            try:
                char = chr(key) if 0 <= key < 256 else ""
                if char in ("1", "2", "3"):
                    self.period = {"1": "day", "2": "month", "3": "all"}[char]
                    self.usage = {}
                    self.refresh()
                elif char == "r":
                    self.refresh(force=True)
                elif char == "h":
                    self.history = not self.history
                    self.selected = 0
                elif char == "/":
                    value = self.prompt("筛选调用者", "名称包含；留空显示全部", self.filter)
                    if value is not None:
                        self.filter, self.selected = value, 0
                elif char == "g" and self.quota_snapshot is not None:
                    self.show_quota()
                elif not self.busy and char == "g":
                    self.status = "读取剩余额度…"
                    self.background("quota", lambda: demo_quota() if self.cfg.get("_demo") else remote(self.cfg, "keys_quota"))
                elif not self.busy and char in ("n", "e", "k", "x", "s"):
                    self.action(char)
            except (RuntimeError, argparse.ArgumentTypeError) as error:
                self.notice("操作未完成", str(error).splitlines())

    def show_quota(self):
        def refresh():
            result = demo_quota() if self.cfg.get("_demo") else remote(self.cfg, "keys_quota")
            return result["quota"]
        panel = QuotaView(self.w, self.quota_snapshot, refresh=refresh)
        panel.show()
        self.quota_snapshot = panel.data
        self.dirty = True


def demo_data():
    rows, usage = [], []
    for i, name in enumerate(("client_a", "client_b", "client_c", "client_old")):
        identity = str(i + 1) * 64
        totals = {"requests": 157 + i * 51, "failed_requests": i, "total_tokens": 1351200 + i * 8345,
                  "estimated_cost_usd_low": 2.12 + i, "estimated_cost_usd_high": 2.14 + i}
        rows.append({"id": identity, "name": name, "active": i < 3})
        usage.append({"id": identity, "client": name, "totals": totals, "models": [], "collector_status": "ok"})
    return {"keys": rows, "usage": usage}


def demo_quota():
    now = dt.datetime.now(dt.timezone.utc)
    accounts = []
    for i, (short, week) in enumerate(((80, 20), (35, 7.5), (60, None)), 1):
        accounts.append({"id": "account-demo-" + str(i), "email": "client_" + str(i) + "@example.com",
                         "enabled": True, "stale": i == 3, "windows": {
                             "five_hour": {"remaining_percent": short, "used_percent": 100 - short,
                                           "resets_at": (now + dt.timedelta(hours=2)).isoformat()},
                             "seven_day": {"remaining_percent": week, "used_percent": None if week is None else 100 - week,
                                           "resets_at": (now + dt.timedelta(days=2)).isoformat()}}})
    return {"quota": {"accounts_total": 3, "accounts_enabled": 3, "accounts_with_fresh_quota": 2,
                      "checked_at": now.isoformat(), "stale": True, "accounts": accounts}}


def start(cfg, execute, validate):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("TUI 需要交互终端；脚本调用请使用 list / usage 子命令，可加 --json。")
    locale.setlocale(locale.LC_ALL, "")
    try:
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        with ssh_session(cfg) as session:
            curses.wrapper(lambda window: Screen(window, session, execute, validate).loop())
    except curses.error:
        raise RuntimeError("终端无法启动 TUI，请使用支持 curses 的终端，或运行 list / usage。") from None
