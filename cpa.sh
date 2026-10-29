#!/usr/bin/env bash
# Public bootstrap only. The client download and all API calls require a proxy.
set -euo pipefail

find_python() {
  local candidate
  for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  printf '%s\n' 'Python 3.10+ is required.' >&2
  return 1
}

saved_proxy_choice() {
  "$python" - <<'PY'
import json, os, re, stat, sys, unicodedata
from pathlib import Path
from urllib.parse import urlsplit
def cell(text, size):
    result, used = "", 0
    for char in str(text):
        if not char.isprintable():
            char = " "
        width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + width > size:
            break
        result, used = result + char, used + width
    return result + " " * (size - used)


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
                        curses.init_pair(pair, color, curses.COLOR_BLACK)
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
                    if color >= 90:
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

path = Path.home() / '.cpa' / 'query.json'
try:
    with TerminalUI() as terminal:
        choice = 1
        if path.exists() or path.is_symlink():
            choice, _ = terminal.form('CPA / Saved connection', [], ('Load saved connection', 'Set up a connection', 'Exit'))
            if choice == 2: raise EOFError
        proxy = None
        if choice == 0:
            try:
                for item, kind in ((path.parent, stat.S_ISDIR), (path, stat.S_ISREG)):
                    info = item.lstat()
                    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077: raise ValueError
                with path.open(encoding='utf-8') as source: data = json.loads(source.read(16385))
                proxy = data['proxy_url']; parsed = urlsplit(proxy)
                if (data['version'] != 1 or parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1','::1') or
                        not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment or
                        parsed.path not in ('','/') or any(c.isspace() or ord(c) < 32 for c in proxy)):
                    raise ValueError
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                proxy = None
        loaded = proxy is not None
        if not loaded:
            def port(value):
                if not re.fullmatch(r'[1-9][0-9]{0,4}', value) or int(value) > 65535:
                    raise RuntimeError('Enter a port from 1 to 65535.')
                return str(int(value))
            action, values = terminal.form('CPA / VPN connection', [{'label': 'VPN proxy port', 'value': '7890', 'max_length': 5, 'validate': port}])
            if action: raise EOFError
            proxy = 'http://127.0.0.1:' + values[0]
    print(('1' if loaded else '2') + '\n' + proxy)
except (EOFError, KeyboardInterrupt):
    raise SystemExit('已取消。')
except RuntimeError as error:
    raise SystemExit(str(error))
PY
}

main() {
  if [[ ! -t 1 || ! -r /dev/tty ]]; then
    printf '%s\n' 'Run this launcher in an interactive terminal.' >&2
    return 1
  fi
  command -v curl >/dev/null || { printf '%s\n' 'curl is required.' >&2; return 1; }
  local python
  python=$(find_python) || return 1
  local proxy source digest selection choice
  selection=$(saved_proxy_choice) || return 1
  choice="${selection%%$'\n'*}"
  proxy="${selection#*$'\n'}"
  source=$(curl -q -fsS --proxy "$proxy" --noproxy '' --proto '=https' \
    --location --max-redirs 0 --connect-timeout 10 --max-time 60 \
    'https://raw.githubusercontent.com/Killow1998/VPS-Maintenance-Documentation/e0b7d07386c0681a4c42e50dc7b23e623e3e2a35/scripts/cpa/quota.sh') || {
    printf '%s\n' 'Client download failed; no direct fallback.' >&2
    return 1
  }
  # Command substitution strips trailing LF bytes; hash that exact representation.
  digest=$(printf '%s' "$source" | "$python" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
  if [[ "$digest" != 'd7dab37c36f9ab450f4e0e340313aa68ce3cc40a046b509a1ea6ac8aa76bd257' ]]; then
    printf '%s\n' 'Client SHA256 mismatch; nothing was executed.' >&2
    return 1
  fi
  CPA_PYTHON="$python" CPA_QUERY_CHOICE="$choice" CPA_PROXY="$proxy" CPA_BASEURL='' bash -c "$source"
}

main
