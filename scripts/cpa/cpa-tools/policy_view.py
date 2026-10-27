"""Account allowlist editor using the same control API as the web admin."""
import copy
import curses
import queue
import threading

from common import remote
from key_view import cell, clip
from tui_theme import apply_theme, style


class PolicyView:
    def __init__(self, window, cfg, state, client_id):
        self.w, self.cfg, self.state = window, cfg, state
        self.clients = copy.deepcopy(state["clients"])
        self.client = next(c for c in self.clients if c["id"] == client_id)
        self.colors = apply_theme(window)
        self.index, self.busy, self.message = 0, False, ""
        self.events = queue.Queue()

    def put(self, y, x, text, attr=0):
        height, width = self.w.getmaxyx()
        if 0 <= y < height:
            try:
                self.w.addstr(y, x, clip(text, max(0, width - x - 1)), style(self.colors, attr))
            except curses.error:
                pass

    def changed(self):
        return self.clients != self.state["clients"]

    def draw(self):
        self.w.erase()
        height, width = self.w.getmaxyx()
        self.put(0, 2, "账号权限 · " + self.client["name"], curses.A_BOLD)
        self.put(1, 2, "网页与 TUI 共用 · v" + str(self.state["revision"]) +
                 (" · 未保存" if self.changed() else " · 已保存"), curses.A_DIM)
        self.put(3, 2, "Key " + ("允许调用" if self.client["enabled"] else "暂停调用") + "   e 切换", curses.A_BOLD)
        accounts = self.state["accounts"]
        self.index = max(0, min(self.index, len(accounts) - 1))
        visible = max(1, height - 10)
        start = max(0, self.index - visible + 1)
        for i, account in enumerate(accounts[start:start + visible], start):
            selected = account["id"] in self.client["account_ids"]
            status = "路由未配置" if account.get("route_ready") is False else "停用" if not account["enabled"] else "可用"
            label = ("[开] " if selected else "[关] ") + cell(account["name"], max(15, width - 26)) + status
            self.put(5 + i - start, 2, label, curses.A_REVERSE if i == self.index else 0)
        self.put(height - 4, 2, self.message or "空白名单拒绝调用；保存后仅影响新请求。", curses.A_DIM)
        self.put(height - 3, 2, "正在保存…" if self.busy else "空格 切换账号   e 停用/启用 Key   s 保存   Esc/q 返回")
        self.put(height - 2, 2, "修改不会改变 Key 内容、OAuth 凭据或历史用量。", curses.A_DIM)
        self.w.refresh()

    def show(self):
        self.draw()
        while True:
            try:
                data, error = self.events.get_nowait()
            except queue.Empty:
                pass
            else:
                self.busy = False
                if error:
                    self.message = error
                else:
                    identity = self.client["id"]
                    self.state = data
                    self.clients = copy.deepcopy(data["clients"])
                    self.client = next(c for c in self.clients if c["id"] == identity)
                    self.message = "已保存 · " + ("新请求按此版本执行" if data["enforcement"] == "enforced" else "尚未切换线上流量")
                self.draw()
            key = self.w.getch()
            if key == -1:
                continue
            if key in (27, ord("q")):
                if self.busy:
                    self.message = "等待提交完成后再退出。"
                elif self.changed() and self.message != "有未保存修改；再按 Esc/q 放弃并返回。":
                    self.message = "有未保存修改；再按 Esc/q 放弃并返回。"
                else:
                    return
            elif not self.busy:
                self.message = ""
                if key == curses.KEY_UP:
                    self.index -= 1
                elif key == curses.KEY_DOWN:
                    self.index += 1
                elif key == ord("e"):
                    self.client["enabled"] = not self.client["enabled"]
                elif key == ord(" ") and self.state["accounts"]:
                    account = self.state["accounts"][self.index]
                    ids = self.client["account_ids"]
                    if account["id"] in ids:
                        ids.remove(account["id"])
                    elif account.get("route_ready") is False:
                        self.message = "先运行 api_keys.py sync-routes 配置新账号路由。"
                    else:
                        ids.append(account["id"])
                elif key == ord("s") and self.changed():
                    self.busy = True
                    payload = {"expected_revision": self.state["revision"], "clients": [
                        {k: c[k] for k in ("id", "enabled", "account_ids")} for c in self.clients]}
                    def worker():
                        try:
                            result = remote(self.cfg, "control_save", payload=payload)
                            self.events.put((result["state"], None))
                        except Exception as error:
                            self.events.put((None, str(error) if isinstance(error, RuntimeError) else "保存失败"))
                    threading.Thread(target=worker, daemon=True).start()
            self.draw()
