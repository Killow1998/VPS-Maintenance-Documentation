"""Scrollable quota bars; private emails and public anonymous IDs share a layout."""
import curses
import queue
import threading
import time

from key_view import beijing, cell, clip, quota_label, remaining_value, reset_label
from tui_theme import apply_theme, style


class QuotaView:
    def __init__(self, window, data, refresh=None):
        self.w, self.data, self.offset = window, data, 0
        self.colors = apply_theme(window)
        self.refresh, self.loading, self.error = refresh, False, None
        self.events = queue.Queue()

    def put(self, y, x, text, attr=0):
        height, width = self.w.getmaxyx()
        if 0 <= y < height and 0 <= x < width - 1:
            try:
                self.w.addstr(y, x, clip(text, width - x - 1), style(self.colors, attr))
            except curses.error:
                pass

    def tone(self, value, stale=False):
        if stale or value is None:
            return curses.A_DIM
        return self.colors.get("good" if value > 50 else "warning" if value > 20 else "low", curses.A_BOLD)

    def rows(self):
        rows = []
        for account in self.data.get("accounts", []):
            rows.append(("account", account))
            windows = account.get("windows", {})
            if not windows:
                rows.append(("empty", account))
            rows.extend(("window", (account, name, window)) for name, window in windows.items())
            rows.append(("space", None))
        return rows or [("empty", {})]

    def draw(self):
        self.w.erase()
        height, width = self.w.getmaxyx()
        if height < 14 or width < 70:
            self.put(0, 0, "请将终端放大至 70×14；Enter / Esc 返回。")
            self.w.refresh()
            return
        accounts = self.data.get("accounts", [])
        private = any(account.get("email") for account in accounts)
        self.put(0, 2, "剩余额度", curses.A_BOLD)
        self.put(0, 17, "管理员 · 账号号池" if private else "匿名号池", curses.A_DIM)
        self.put(1, 2, f"{self.data.get('accounts_total', 0)} 个账号  ·  {self.data.get('accounts_enabled', 0)} 启用  ·  "
                 f"{self.data.get('accounts_with_fresh_quota', 0)} 新鲜  ·  更新 {beijing(self.data.get('checked_at'))}", curses.A_DIM)
        bar_x = 22
        bar_size = max(8, min(36, width - 65))
        percent_x = bar_x + bar_size + 3
        reset_x = percent_x + 9
        self.put(3, 2, "账号 / 窗口", curses.A_DIM)
        self.put(3, bar_x, "剩余额度", curses.A_DIM)
        self.put(3, reset_x, "重置（北京） / 倒计时", curses.A_DIM)
        rows = self.rows()
        visible = height - 9
        self.offset = max(0, min(self.offset, len(rows) - visible))
        for y, (kind, item) in enumerate(rows[self.offset:self.offset + visible], 5):
            if kind == "account":
                identity = item.get("email") or item.get("id") or "身份未知"
                state = "已禁用" if not item.get("enabled") else "旧值 / 过时" if item.get("stale", True) else "正常"
                self.put(y, 2, cell(identity, width - 22), curses.A_BOLD)
                self.put(y, width - 18, state, curses.A_DIM if state != "正常" else self.colors.get("good", 0))
            elif kind == "window":
                account, name, window = item
                value = remaining_value(window.get("remaining_percent"))
                stale = account.get("stale", True) or not account.get("enabled")
                self.put(y, 3, cell(quota_label(name), 17))
                self.put(y, bar_x - 1, "[", curses.A_DIM)
                self.put(y, bar_x + bar_size, "]", curses.A_DIM)
                self.put(y, bar_x, "░" * bar_size, curses.A_DIM)
                if value is None:
                    self.put(y, bar_x + max(0, (bar_size - 4) // 2), "未知", curses.A_DIM)
                else:
                    filled = int(bar_size * value / 100 + .5)
                    self.put(y, bar_x, "█" * filled, self.tone(value, stale))
                self.put(y, percent_x, "未知" if value is None else f"{value:5.1f}%", self.tone(value, stale))
                self.put(y, reset_x, reset_label(window.get("resets_at")), curses.A_DIM)
            elif kind == "empty":
                text = "已禁用，暂无窗口" if item and not item.get("enabled") else "暂无额度数据"
                if item.get("reason"):
                    text += " · " + str(item["reason"])
                self.put(y, 3, text, curses.A_DIM)
        self.put(height - 3, 2, self.error or ("更新快照…" if self.loading else
                 "█ 剩余  ░ 已用   绿 >50%   黄 >20%   红 ≤20%   灰=旧值/未知"), curses.A_DIM)
        self.put(height - 2, 2, "↑↓ / PgUp / PgDn 滚动   r 更新   Enter / Esc 返回", curses.A_DIM)
        if len(rows) > visible:
            self.put(0, width - 15, f"{self.offset + 1}–{min(self.offset + visible, len(rows))}/{len(rows)}", curses.A_DIM)
        self.w.refresh()

    def show(self):
        self.draw()
        last_clock = 0
        last_labels = None
        while True:
            if time.monotonic() - last_clock >= 1:
                labels = tuple(reset_label(window.get("resets_at")) for account in self.data.get("accounts", [])
                               for window in account.get("windows", {}).values())
                if labels != last_labels:
                    self.draw()
                    last_labels = labels
                last_clock = time.monotonic()
            try:
                data, error = self.events.get_nowait()
            except queue.Empty:
                pass
            else:
                self.loading, self.error = False, error
                if data:
                    self.data = data
                self.draw()
            key = self.w.getch()
            if key == -1:
                continue
            if key in (10, 13, 27, ord("q")):
                return
            height, _ = self.w.getmaxyx()
            if key == curses.KEY_DOWN:
                self.offset += 1
            elif key == curses.KEY_UP:
                self.offset -= 1
            elif key == curses.KEY_NPAGE:
                self.offset += max(1, height - 9)
            elif key == curses.KEY_PPAGE:
                self.offset -= max(1, height - 9)
            elif key == curses.KEY_HOME:
                self.offset = 0
            elif key == curses.KEY_END:
                self.offset = len(self.rows())
            elif key == ord("r") and self.refresh and not self.loading:
                self.loading, self.error = True, None
                def worker():
                    try:
                        self.events.put((self.refresh(), None))
                    except Exception as error:
                        self.events.put((None, str(error) if isinstance(error, RuntimeError) else type(error).__name__))
                threading.Thread(target=worker, daemon=True).start()
            self.draw()
