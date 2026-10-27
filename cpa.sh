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

read_proxy_port() {
  local port
  if [[ ! -t 1 || ! -r /dev/tty ]]; then
    printf '%s\n' 'Run this launcher in an interactive terminal.' >&2
    return 1
  fi
  while true; do
    read -r -p 'Local HTTP/mixed proxy port (e.g. 7890): ' port </dev/tty || return 1
    if [[ "$port" =~ ^[1-9][0-9]{0,4}$ && "$port" -le 65535 ]]; then
      REPLY=$port
      return
    fi
    printf '%s\n' 'Enter an integer from 1 to 65535.' >&2
  done
}

saved_proxy_choice() {
  "$python" - <<'PY'
import json, os, stat, sys
from pathlib import Path
from urllib.parse import urlsplit
path = Path.home() / '.cpa' / 'query.json'
if not path.exists() and not path.is_symlink():
    print('2')
    raise SystemExit
try:
    with open('/dev/tty', 'r') as terminal:
        while True:
            print('1 载入已有配置  2 重新输入：', end='', file=sys.stderr, flush=True)
            choice = terminal.readline().strip()
            if choice in ('1', '2'): break
            if not choice: raise EOFError
    if choice == '2':
        print('2'); raise SystemExit
    for item, kind in ((path.parent, stat.S_ISDIR), (path, stat.S_ISREG)):
        info = item.lstat()
        if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError
    with path.open(encoding='utf-8') as source: data = json.loads(source.read(16385))
    proxy = data['proxy_url']; parsed = urlsplit(proxy)
    if (data['version'] != 1 or parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1','::1') or
            not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment or
            parsed.path not in ('','/') or any(c.isspace() or ord(c) < 32 for c in proxy)):
        raise ValueError
    print('1\n' + proxy)
except (OSError, ValueError, TypeError, KeyError, AttributeError):
    print('已存配置无法读取、权限不安全或内容无效；请重新输入。', file=sys.stderr)
    print('2')
except (EOFError, KeyboardInterrupt):
    raise SystemExit('已取消。')
PY
}

main() {
  command -v curl >/dev/null || { printf '%s\n' 'curl is required.' >&2; return 1; }
  local python
  python=$(find_python) || return 1
  local REPLY proxy source digest selection choice
  selection=$(saved_proxy_choice) || return 1
  choice="${selection%%$'\n'*}"
  if [[ "$choice" == '1' ]]; then
    proxy="${selection#*$'\n'}"
  else
    read_proxy_port || return 1
    proxy="http://127.0.0.1:$REPLY"
  fi
  source=$(curl -q -fsS --proxy "$proxy" --noproxy '' --proto '=https' \
    --location --max-redirs 0 --connect-timeout 10 --max-time 60 \
    'https://raw.githubusercontent.com/Killow1998/VPS-Maintenance-Documentation/8335db5d27211d6af37704db85ec8efb0172b5c0/scripts/cpa/quota.sh') || {
    printf '%s\n' 'Client download failed; no direct fallback.' >&2
    return 1
  }
  # Command substitution strips trailing LF bytes; hash that exact representation.
  digest=$(printf '%s' "$source" | "$python" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
  if [[ "$digest" != '67c731b821773712c821030364fa9fec8105ae4d272294e5615c9090121276e0' ]]; then
    printf '%s\n' 'Client SHA256 mismatch; nothing was executed.' >&2
    return 1
  fi
  CPA_PYTHON="$python" CPA_QUERY_CHOICE="$choice" CPA_PROXY="$proxy" CPA_BASEURL='' bash -c "$source"
}

main
