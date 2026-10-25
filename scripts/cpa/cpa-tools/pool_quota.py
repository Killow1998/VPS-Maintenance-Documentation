"""Public pool quota query: only Base URL and a downstream CPA API Key."""
import getpass
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def query(base_url=None, key_env=None):
    base = (base_url or os.environ.get("CPA_BASEURL") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    parsed = urlsplit(base)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")))):
        raise RuntimeError("请提供 HTTPS Base URL：--base-url 或 CPA_BASEURL。")
    if key_env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
            raise RuntimeError("Key 环境变量名称无效。")
        key = os.environ.get(key_env)
        if not key:
            raise RuntimeError("指定的 Key 环境变量未加载，请先 source 原 env。")
    else:
        key = os.environ.get("CPA_API_KEY") or getpass.getpass("CPA API Key（隐藏输入）：")
    if not key or any(c in key for c in "\r\n"):
        raise RuntimeError("API Key 无效。")
    try:
        request = Request(base + "/v1/quota", headers={"Authorization": "Bearer " + key})
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("额度响应过大。")
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("scope") != "shared_pool" or not isinstance(body.get("accounts"), list):
            raise RuntimeError("此入口未返回 CPA observer 的号池额度，请检查 /v1/quota 路由。")
        return {"ok": True, "quota": body}
    except HTTPError as error:
        messages = {401: "Key 无效或已撤销", 404: "入口尚未配置 /v1/quota", 429: "查询过于频繁，请稍后再试"}
        raise RuntimeError("额度查询 HTTP " + str(error.code) + "：" + messages.get(error.code, "服务暂不可用")) from None
    except (OSError, ValueError):
        raise RuntimeError("额度查询失败，请检查网络、TLS 证书和接口响应。") from None
