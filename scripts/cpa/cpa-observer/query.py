#!/usr/bin/env python3
"""Query the observer without putting an API key in command arguments."""
import argparse
import getpass
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("endpoint", choices=["quota", "usage"])
    parser.add_argument("--client", help="Read CPA_API_KEY_<CLIENT> from environment")
    parser.add_argument("--period", choices=["day", "month", "all"], default="day")
    parser.add_argument("--date", help="YYYY-MM-DD for day, YYYY-MM for month")
    args = parser.parse_args()
    base = (os.environ.get("CPA_BASEURL") or os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    parsed = urlsplit(base)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("Set CPA_BASEURL or ANTHROPIC_BASE_URL to the gateway root URL")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        parser.error("Public gateways must use HTTPS")
    if args.client:
        if not re.fullmatch(r"[A-Za-z0-9_]+", args.client):
            parser.error("Invalid client label")
        key = os.environ.get("CPA_API_KEY_" + args.client.upper())
        if not key:
            parser.error("The selected client key is not present in the environment")
    else:
        key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or getpass.getpass("CPA API Key: ")
    path = "/v1/" + args.endpoint
    if args.endpoint == "usage":
        params = {"period": args.period}
        if args.date:
            params["date"] = args.date
        path += "?" + urlencode(params)
    try:
        request = Request(base + path, headers={"Authorization": "Bearer " + key})
        with build_opener(NoRedirect()).open(request, timeout=45) as response:
            body = json.loads(response.read())
        print(json.dumps(body, ensure_ascii=False, indent=2))
    except HTTPError as error:
        print(json.dumps({"error": "http_error", "status": error.code}))
        raise SystemExit(1)
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
