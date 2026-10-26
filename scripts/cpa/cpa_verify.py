#!/usr/bin/env python3
"""Verify CPA auth and one real generation without printing credentials/bodies."""
import sys

if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required. No connection or configuration changes were made.")

import argparse
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-models", action="store_true")
    args = parser.parse_args()
    base = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not key):
        print("Set an HTTPS ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN.")
        return 2
    if base.endswith("/v1"):
        base = base[:-3]
    opener = build_opener(NoRedirect())

    def request(path, authenticated=True, payload=None):
        headers = {"Authorization": "Bearer " + key} if authenticated else {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers.update({"Content-Type": "application/json", "anthropic-version": "2023-06-01"})
        try:
            with opener.open(Request(base + path, headers=headers, data=body), timeout=60) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    return response.status, {}, "response_too_large"
                return response.status, json.loads(raw), None
        except HTTPError as error:
            status = error.code
            error.close()
            return status, {}, "http_error"
        except (OSError, ValueError):
            return None, {}, "network_tls_or_json_error"

    without, _, _ = request("/v1/models", authenticated=False)
    status, catalog, failure = request("/v1/models")
    items = catalog.get("data", []) if isinstance(catalog, dict) else []
    items = items if isinstance(items, list) else []
    models = [item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)]
    auth_ok = without == 401 and status == 200
    print(json.dumps({"without_key": without, "with_key": status, "auth_pass": auth_ok, "error": failure}))
    for model in models:
        print("Model:", "".join(c for c in model if c.isprintable()))
    if not auth_ok:
        return 1
    if args.list_models:
        print("Model listing only; generation not verified.")
        return 0
    model = os.environ.get("CPA_TEST_MODEL")
    if not model or model not in models:
        print("Set CPA_TEST_MODEL to an exact ID listed above.")
        return 2
    status, result, failure = request("/v1/messages", payload={"model": model, "max_tokens": 32,
                                    "messages": [{"role": "user", "content": "Reply only OK."}]})
    content = result.get("content", []) if isinstance(result, dict) else []
    content = content if isinstance(content, list) else []
    has_text = any(isinstance(item, dict) and item.get("type") == "text"
                   and isinstance(item.get("text"), str) and bool(item["text"].strip()) for item in content)
    passed = status == 200 and has_text
    print(json.dumps({"generation_status": status, "has_text": has_text, "generation_pass": passed, "error": failure}))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
