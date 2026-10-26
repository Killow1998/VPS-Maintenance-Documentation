#!/usr/bin/env bash
# Public bootstrap only. The client download and all API calls require a proxy.
set -euo pipefail

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

main() {
  command -v curl >/dev/null || { printf '%s\n' 'curl is required.' >&2; return 1; }
  command -v python3 >/dev/null || { printf '%s\n' 'Python 3.10+ is required.' >&2; return 1; }
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10+ is required.")' || return 1
  local REPLY proxy source digest
  read_proxy_port || return 1
  proxy="http://127.0.0.1:$REPLY"
  source=$(curl -q -fsS --proxy "$proxy" --noproxy '' --proto '=https' \
    --location --max-redirs 0 --connect-timeout 10 --max-time 60 \
    'https://raw.githubusercontent.com/Killow1998/VPS-Maintenance-Documentation/8335db5d27211d6af37704db85ec8efb0172b5c0/scripts/cpa/quota.sh') || {
    printf '%s\n' 'Client download failed; no direct fallback.' >&2
    return 1
  }
  # Command substitution strips trailing LF bytes; hash that exact representation.
  digest=$(printf '%s' "$source" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
  if [[ "$digest" != 'f42d2e9afff73d1e55e42ef392486b8cb1ef128cdaa0c3ec8ad7ecb3662d8492' ]]; then
    printf '%s\n' 'Client SHA256 mismatch; nothing was executed.' >&2
    return 1
  fi
  CPA_PROXY="$proxy" CPA_BASEURL='' bash -c "$source"
}

main
