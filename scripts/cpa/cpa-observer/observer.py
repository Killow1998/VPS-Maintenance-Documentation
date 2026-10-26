#!/usr/bin/env python3
"""Local CPA usage collector and authenticated usage/quota read API."""
import argparse
import collections
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc


def now():
    return dt.datetime.now(UTC).isoformat()


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def stamp(value):
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(UTC).isoformat()


def period_bounds(period, date=None, timezone="Asia/Shanghai", at=None):
    zone = ZoneInfo(timezone)
    current = at or dt.datetime.now(zone)
    if period in ("1h", "7d", "30d"):
        if date:
            raise ValueError("date does not apply to rolling periods")
        duration = {"1h": dt.timedelta(hours=1), "7d": dt.timedelta(days=7), "30d": dt.timedelta(days=30)}[period]
        return (current - duration).astimezone(UTC).isoformat(), current.astimezone(UTC).isoformat()
    if period == "all":
        if date:
            raise ValueError("date does not apply to all")
        return None, None
    if period == "day":
        day = dt.date.fromisoformat(date) if date else current.date()
        start = dt.datetime.combine(day, dt.time(), zone)
        end = start + dt.timedelta(days=1)
    elif period == "month":
        if date and not re.fullmatch(r"\d{4}-\d{2}", date):
            raise ValueError("month date must be YYYY-MM")
        year, month = map(int, date.split("-")) if date else (current.year, current.month)
        start = dt.datetime(year, month, 1, tzinfo=zone)
        end = dt.datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=zone)
    else:
        raise ValueError("period must be 1h, 7d, 30d, day, month or all")
    return start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class QuotaError(ValueError):
    def __init__(self, reason, status=None, retry_after=None):
        super().__init__(reason)
        self.reason, self.status, self.retry_after = reason, status, retry_after


class Backend:
    def __init__(self, base, management_key):
        self.base = base.rstrip("/")
        if urlsplit(self.base).hostname not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("CPA backend must be local")
        self.management_key = management_key
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, payload=None, client_key=None):
        key = self.management_key if client_key is None else client_key
        headers = {"Authorization": "Bearer " + key}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, headers=headers, data=data)
        with self.opener.open(request, timeout=30) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("backend response exceeds safety limit")
            return json.loads(raw)

    def valid_key(self, key):
        try:
            data = self.request("/v1/models", client_key=key)
            return isinstance(data.get("data"), list)
        except HTTPError as error:
            if error.code in (401, 403):
                return False
            raise

    def pop_usage(self):
        data = self.request("/v8/management/observability/usage/queue?count=100")
        if not isinstance(data, list):
            raise ValueError("unexpected usage response")
        return data

    def accounts(self):
        return self.request("/v8/management/credentials").get("files", [])

    def quota(self, auth_index):
        data = self.request("/v8/management/requests/api-call", {
            "auth_index": auth_index, "method": "GET",
            "url": "https://api.anthropic.com/api/oauth/usage",
            "header": {"Authorization": "Bearer $TOKEN$",
                       "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json"}})
        status = data.get("status_code")
        if status != 200:
            raw = data.get("body")
            try:
                body = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                body = {}
            error = body.get("error", {}) if isinstance(body, dict) else {}
            message = str(error.get("message", "")) if isinstance(error, dict) else ""
            reason = "missing_scope" if status == 403 and "user:profile" in message else "upstream_http_" + str(status)
            retry = None
            headers = data.get("header") or data.get("headers") or {}
            if isinstance(headers, dict):
                for key, value in headers.items():
                    if key.lower() == "retry-after":
                        value = value[0] if isinstance(value, list) and value else value
                        try:
                            retry = max(1, min(86400, int(value)))
                        except (ValueError, TypeError):
                            pass
            raise QuotaError(reason, status, retry)
        body = data.get("body")
        return json.loads(body) if isinstance(body, str) else body

    def refresh_credential(self, name):
        # The refresh response includes private auth metadata. Do not read or
        # decode it: the updated token remains in CPA, never in this service.
        request = Request(self.base + "/v8/management/credentials/refresh",
                          headers={"Authorization": "Bearer " + self.management_key,
                                   "Content-Type": "application/json"},
                          data=json.dumps({"name": name}).encode())
        with self.opener.open(request, timeout=45) as response:
            if response.status != 200:
                raise QuotaError("credential_refresh_failed")


class Store:
    def __init__(self, path, secret, labels, prices):
        self.path, self.secret, self.labels, self.prices = str(path), secret.encode(), labels, prices
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    model TEXT NOT NULL, provider TEXT NOT NULL, account_id TEXT,
                    failed INTEGER NOT NULL, input_tokens INTEGER, uncached_tokens INTEGER,
                    cache_read_tokens INTEGER, cache_write_tokens INTEGER, output_tokens INTEGER,
                    reasoning_tokens INTEGER, total_tokens INTEGER, accounting_quality TEXT,
                    latency_ms REAL, cost_low REAL, cost_high REAL, price_version TEXT);
                CREATE INDEX IF NOT EXISTS usage_client_time ON records(client_id, timestamp);
            """)
            con.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", ("collection_started_at", now()))
        os.chmod(path, 0o600)

    @contextlib.contextmanager
    def db(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA synchronous=FULL")
        try:
            with con:
                yield con
        finally:
            con.close()

    def digest(self, text):
        return hmac.new(self.secret, text.encode(), hashlib.sha256).hexdigest()

    def client(self, key):
        return self.digest("client:" + key)

    def label(self, client):
        return self.labels.get(client, "client-" + client[:10])

    def meta(self, key, value=None):
        with self.db() as con:
            if value is not None:
                con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, value))
                return value
            row = con.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def normalize(self, event):
        key = event.get("api_key")
        if not isinstance(key, str) or not key:
            key = "unattributed"
        client = self.client(key)
        tokens = event.get("tokens") or {}
        breakdown = event.get("token_breakdown") or tokens.get("token_breakdown") or {}
        quality = breakdown.get("quality", "unclassified")
        inp, out = breakdown.get("input", {}), breakdown.get("output", {})
        complete = (breakdown.get("schema_version") == 2 and quality == "complete")
        if complete:
            raw = [inp.get("total_tokens"), inp.get("uncached_tokens"), inp.get("cache_read_tokens"),
                   inp.get("cache_write_tokens"), out.get("total_tokens"), out.get("reasoning_tokens"),
                   breakdown.get("total_tokens")]
            complete = (all(count(v) is not None for v in raw)
                        and raw[0] == sum(raw[1:4]) and raw[6] == raw[0] + raw[4]
                        and raw[5] <= raw[4] and breakdown.get("unclassified_tokens", 0) == 0)
        if complete:
            input_n, uncached, read, write, output, reasoning, total = raw
        else:
            quality = "unclassified"
            input_n, output, reasoning, total = (count(tokens.get(k)) for k in
                ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"))
            uncached = read = write = None
        model = str(event.get("model") or "unknown")[:160]
        price = self.prices.get("models", {}).get(model)
        if price is None:
            for name in sorted(self.prices.get("models", {}), key=len, reverse=True):
                if re.fullmatch(re.escape(name) + r"-\d{8}", model):
                    price = self.prices["models"][name]
                    break
        low = high = None
        if complete and price:
            basic = uncached * price["input"] + read * price["cache_read"] + output * price["output"]
            low = (basic + write * price["cache_write_5m"]) / 1_000_000
            high = (basic + write * price["cache_write_1h"]) / 1_000_000
        identity = str(event.get("execution_id") or event.get("request_id") or
                       json.dumps(event, sort_keys=True, separators=(",", ":")))
        account = str(event.get("auth_index") or "unknown")
        return (self.digest("event:" + identity), client, stamp(event["timestamp"]), model,
                str(event.get("provider") or "unknown")[:60], self.digest("account:" + account),
                int(bool(event.get("failed"))), input_n, uncached, read, write, output, reasoning,
                total, quality, event.get("latency_ms"), low, high,
                self.prices.get("version") if low is not None else None)

    def ingest(self, events):
        rows, rejected = [], 0
        for event in events:
            try:
                rows.append(self.normalize(event))
            except (ValueError, TypeError, KeyError, AttributeError):
                rejected += 1
        with self.db() as con:
            before = con.total_changes
            con.executemany("INSERT OR IGNORE INTO records VALUES (" + ",".join("?" * 19) + ")", rows)
            inserted = con.total_changes - before
            if rejected:
                old = con.execute("SELECT value FROM metadata WHERE key='rejected_events'").fetchone()
                con.execute("INSERT OR REPLACE INTO metadata VALUES ('rejected_events',?)",
                            (str(rejected + (int(old[0]) if old else 0)),))
        return inserted, rejected

    def public_account(self, identity):
        if (isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity)
                and identity != self.digest("account:unknown")):
            return "account-" + identity[:12]
        return None

    def aggregate(self, con, where, args, by):
        token_fields = ["input_tokens", "uncached_tokens", "cache_read_tokens", "cache_write_tokens",
                        "output_tokens", "reasoning_tokens", "total_tokens"]
        columns, group, order = ("model", "model", "model") if by == "model" else (
            "account_id, provider, max(timestamp) last_used_at", "account_id, provider", "max(timestamp) DESC, account_id")
        sql = "SELECT " + columns + """, count(*) requests, sum(failed) failed_requests,
                 sum(input_tokens) input_tokens, sum(uncached_tokens) uncached_tokens,
                 sum(cache_read_tokens) cache_read_tokens, sum(cache_write_tokens) cache_write_tokens,
                 sum(output_tokens) output_tokens, sum(reasoning_tokens) reasoning_tokens,
                 sum(total_tokens) total_tokens, sum(cost_low) estimated_cost_usd_low,
                 sum(cost_high) estimated_cost_usd_high,
                 sum(cost_low IS NOT NULL) priced_requests,
                 sum(accounting_quality!='complete') incomplete_accounting_requests
                 FROM records""" + where + " GROUP BY " + group + " ORDER BY " + order
        sql = sql.replace("FROM records", ", " + ", ".join(
            "count(" + name + ") known_" + name for name in token_fields) + " FROM records")
        rows = [dict(row) for row in con.execute(sql, args)]
        for item in rows:
            item["unknown_token_fields"] = []
            for name in token_fields:
                if item.pop("known_" + name) < item["requests"]:
                    item[name] = None
                    item["unknown_token_fields"].append(name)
            if by != "model":
                item["id"] = self.public_account(item.pop("account_id"))
        return rows

    def summary(self, client=None, period="day", date=None, recent=10):
        if not isinstance(recent, int) or isinstance(recent, bool) or not 0 <= recent <= 50:
            raise ValueError("recent must be between 0 and 50")
        start, end = period_bounds(period, date)
        clauses, args = [], []
        if client is not None:
            clauses.append("client_id=?")
            args.append(client)
        if start:
            clauses.append("timestamp>=? AND timestamp<?")
            args.extend([start, end])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db() as con:
            con.execute("BEGIN")
            models = self.aggregate(con, where, args, "model")
            accounts = self.aggregate(con, where, args, "account")
            records = [dict(row) for row in con.execute("""SELECT timestamp, model, provider, account_id,
                failed, input_tokens, uncached_tokens, cache_read_tokens, cache_write_tokens,
                output_tokens, reasoning_tokens, total_tokens, accounting_quality, latency_ms,
                cost_low estimated_cost_usd_low, cost_high estimated_cost_usd_high
                FROM records""" + where + " ORDER BY timestamp DESC, id DESC LIMIT ?", args + [recent])]
        for record in records:
            record["account_id"] = self.public_account(record["account_id"])
        totals = self.total_rows(models)
        for item in models + accounts + records:
            for name in ("estimated_cost_usd_low", "estimated_cost_usd_high"):
                if item.get(name) is not None:
                    item[name] = round(item[name], 8)
        return {"scope": "own_key" if client else "administrator", "client": self.label(client) if client else None,
                "period": period, "date": date, "timezone": "Asia/Shanghai", "start_utc": start, "end_utc": end,
                "collection_started_at": self.meta("collection_started_at"),
                "last_collected_at": self.meta("collector_last_success"),
                "collector_status": self.meta("collector_status") or "starting",
                "rejected_events": int(self.meta("rejected_events") or 0),
                "history_before_collection": "not_available", "totals": totals, "models": models,
                "accounts": accounts, "recent": records, "recent_limit": recent,
                "record_unit": "upstream_execution_attempt; retries may create multiple records",
                "cost_basis": "API list-price estimate; not subscription charges; cache writes span 5m/1h rates",
                "accounting_limitations": "in-memory queue can lose events on outage/restart or between pop and commit"}

    @staticmethod
    def total_rows(models):
        token_fields = ["input_tokens", "uncached_tokens", "cache_read_tokens", "cache_write_tokens",
                        "output_tokens", "reasoning_tokens", "total_tokens"]
        fields = ["requests", "failed_requests", "input_tokens", "uncached_tokens", "cache_read_tokens",
                  "cache_write_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
                  "estimated_cost_usd_low", "estimated_cost_usd_high", "priced_requests",
                  "incomplete_accounting_requests"]
        totals = {}
        for name in fields:
            values = [row[name] for row in models if row[name] is not None]
            if name in token_fields and any(row[name] is None for row in models):
                totals[name] = None
            else:
                totals[name] = sum(values) if values else (None if "cost" in name else 0)
        for name in ("estimated_cost_usd_low", "estimated_cost_usd_high"):
            if totals[name] is not None:
                totals[name] = round(totals[name], 8)
        return totals

    def overview(self, client):
        if not client:
            raise ValueError("overview requires an authenticated client")
        cutoff = dt.datetime.now(UTC)
        windows = []
        with self.db() as con:
            con.execute("BEGIN")
            metadata = dict(con.execute("SELECT key, value FROM metadata"))
            for period in ("1h", "7d", "30d", "all"):
                start, _ = period_bounds(period, at=cutoff)
                end = cutoff.isoformat()
                where, args = " WHERE client_id=? AND timestamp<?", [client, end]
                if start:
                    where += " AND timestamp>=?"
                    args.append(start)
                windows.append({"period": period, "start_utc": start, "end_utc": end,
                                "totals": self.total_rows(self.aggregate(con, where, args, "model"))})
        return {"scope": "own_key", "period": "overview", "timezone": "Asia/Shanghai",
                "as_of": cutoff.isoformat(), "windows": windows,
                "collection_started_at": metadata.get("collection_started_at"),
                "last_collected_at": metadata.get("collector_last_success"),
                "collector_status": metadata.get("collector_status", "starting"),
                "rejected_events": int(metadata.get("rejected_events", 0)),
                "history_before_collection": "not_available",
                "accounting_limitations": "retained observations only; collector outages can leave gaps"}


def quota_windows(body):
    result = {}
    if not isinstance(body, dict):
        raise ValueError("invalid quota response")
    for name, value in body.items():
        if not re.fullmatch(r"(?:five_hour|seven_day)(?:_[a-z0-9_]+)?", name) or not isinstance(value, dict):
            continue
        used = value.get("utilization")
        if not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used) or not 0 <= used <= 100:
            continue
        reset = value.get("resets_at")
        try:
            reset = stamp(reset) if reset else None
        except (ValueError, TypeError):
            reset = None
        result[name] = {"used_percent": used, "remaining_percent": round(100 - used, 4), "resets_at": reset}
    if not result:
        raise ValueError("no quota windows returned")
    return result


class Observer:
    def __init__(self, config):
        self.config = config
        prices = json.loads(Path(config["prices_file"]).read_text())
        self.store = Store(config["database"], config["hmac_secret"], config.get("labels", {}), prices)
        self.backend = Backend(config["cpa_base_url"], config["management_key"])
        self.stop = threading.Event()
        self.auth_cache, self.rate = {}, collections.OrderedDict()
        self.lock = threading.Lock()

    def allowed_request(self, key):
        identity = self.store.client(key)
        minute = int(time.monotonic() / 60)
        with self.lock:
            for bucket in ("global", identity):
                last, count_n = self.rate.get(bucket, (minute, 0))
                count_n = count_n if last == minute else 0
                if count_n >= (600 if bucket == "global" else 60):
                    return False
                self.rate[bucket] = (minute, count_n + 1)
            while len(self.rate) > 2048:
                self.rate.popitem(last=False)
        return True

    def authenticate(self, key):
        identity = self.store.client(key)
        with self.lock:
            expires = self.auth_cache.get(identity, 0)
        if expires > time.monotonic():
            return identity
        if not self.backend.valid_key(key):
            return None
        with self.lock:
            self.auth_cache = {k: v for k, v in self.auth_cache.items() if v > time.monotonic()}
            self.auth_cache[identity] = time.monotonic() + 5
        return identity

    def collect(self):
        while not self.stop.is_set():
            try:
                for _ in range(20):
                    rows = self.backend.pop_usage()
                    self.store.ingest(rows)
                    if len(rows) < 100:
                        break
                self.store.meta("collector_last_success", now())
                self.store.meta("collector_status", "ok")
            except Exception as error:
                self.store.meta("collector_status", "error:" + type(error).__name__)
            self.stop.wait(self.config.get("usage_poll_seconds", 3))

    def refresh_quota(self):
        previous = json.loads(self.store.meta("quota_snapshot") or "{}")
        known = {a["id"]: a for a in previous.get("accounts", [])}
        accounts = []
        entries = self.backend.accounts()
        by_name = {entry.get("name"): entry for entry in entries if entry.get("name")}
        bindings = self.config.get("quota_bindings", {})
        sources = {binding.get("credential") for binding in bindings.values() if isinstance(binding, dict)}
        for entry in entries:
            if entry.get("provider") != "claude":
                continue
            if entry.get("name") in sources:
                continue  # Quota-only credentials do not add an account to the pool.
            index = entry.get("auth_index")
            if not index:
                continue
            identity = "account-" + self.store.digest("account:" + index)[:12]
            active = not entry.get("disabled", False)
            item = {"id": identity, "enabled": active, "status": "disabled" if not active else "unknown",
                    "windows": {}, "as_of": None, "stale": True}
            if active:
                old = known.get(identity, {})
                try:
                    source = entry
                    binding = bindings.get(entry.get("name"))
                    if binding is not None:
                        if not isinstance(binding, dict) or not binding.get("credential") or not binding.get("expected_email"):
                            raise QuotaError("invalid_quota_binding")
                        source = by_name.get(binding["credential"])
                        if source is None or source.get("provider") != "claude" or not source.get("disabled"):
                            raise QuotaError("quota_source_must_be_disabled")
                        if str(source.get("email", "")).casefold() != str(binding["expected_email"]).casefold():
                            raise QuotaError("quota_identity_mismatch")
                    source_index = source.get("auth_index")
                    if not source_index:
                        raise QuotaError("quota_source_missing")
                    # Source fingerprint prevents reusing stale windows from a
                    # removed or changed binding after a configuration update.
                    revision = self.store.digest("quota-source:" + str(source_index) + ":" + str(source.get("updated_at", "")))
                    item["source_revision"] = revision
                    retry_at = old.get("retry_at")
                    if old.get("source_revision") == revision and retry_at and dt.datetime.fromisoformat(retry_at) > dt.datetime.now(UTC):
                        item.update(windows=old.get("windows", {}), as_of=old.get("as_of"),
                                    status=old.get("status", "unavailable"), reason=old.get("reason"), retry_at=retry_at)
                    else:
                        try:
                            quota = self.backend.quota(source_index)
                        except QuotaError as error:
                            if error.status != 401 or not source.get("name"):
                                raise
                            self.backend.refresh_credential(source["name"])
                            quota = self.backend.quota(source_index)
                        item.update(windows=quota_windows(quota), status="ok", as_of=now(), stale=False)
                except Exception as error:
                    same_source = old.get("source_revision") == item.get("source_revision")
                    item.update(windows=old.get("windows", {}) if same_source else {},
                                as_of=old.get("as_of") if same_source else None, status="unavailable",
                                reason=error.reason if isinstance(error, QuotaError) else "quota_lookup_failed")
                    if isinstance(error, QuotaError) and error.retry_after:
                        item["retry_at"] = (dt.datetime.now(UTC) + dt.timedelta(seconds=error.retry_after)).isoformat()
            accounts.append(item)
        self.store.meta("quota_snapshot", json.dumps({"checked_at": now(), "accounts": accounts}))

    def quota_worker(self):
        while not self.stop.is_set():
            try:
                self.refresh_quota()
                self.store.meta("quota_status", "ok")
            except Exception as error:
                self.store.meta("quota_status", "error:" + type(error).__name__)
            self.stop.wait(self.config.get("quota_poll_seconds", 300))

    def quota_response(self):
        data = json.loads(self.store.meta("quota_snapshot") or "{}")
        accounts = data.get("accounts", [])
        max_age = self.config.get("quota_poll_seconds", 300) * 2 + 30
        for account in accounts:
            account.pop("source_revision", None)
            as_of = account.get("as_of")
            if not as_of or (dt.datetime.now(UTC) - dt.datetime.fromisoformat(as_of)).total_seconds() > max_age:
                account["stale"] = True
        names = sorted({w for account in accounts for w in account["windows"]})
        windows = {}
        for name in names:
            values = [a["windows"][name]["remaining_percent"] for a in accounts if name in a["windows"] and not a["stale"]]
            windows[name] = {"known_accounts": len(values),
                             "remaining_percent": values[0] if len(accounts) == 1 and len(values) == 1 else None,
                             "min_remaining_percent": min(values) if values else None,
                             "max_remaining_percent": max(values) if values else None,
                             "resets_at": accounts[0]["windows"][name]["resets_at"] if len(accounts) == 1 else None}
        return {"scope": "shared_pool", "provider": "claude", "checked_at": data.get("checked_at"),
                "stale": not accounts or any(a["enabled"] and a["stale"] for a in accounts),
                "accounts_total": len(accounts), "accounts_enabled": sum(a["enabled"] for a in accounts),
                "accounts_with_fresh_quota": sum(not a["stale"] for a in accounts),
                "windows": windows, "accounts": accounts,
                "query_status": self.store.meta("quota_status") or "starting",
                "unit": "percentage_of_provider_usage_window; not tokens or cash balance"}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, observer):
        self.observer = observer
        self.slots = threading.BoundedSemaphore(24)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "CPAObserver/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(35)

    def log_message(self, *args):
        pass  # Never log headers, client-supplied paths or credentials.

    def answer(self, status, body):
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        observer = self.server.observer
        path = urlsplit(self.path)
        if path.path == "/healthz":
            self.answer(200, {"status": "ok", "collector_status": observer.store.meta("collector_status"),
                              "quota_status": observer.store.meta("quota_status")})
            return
        if path.path not in ("/v1/usage", "/v1/quota"):
            self.answer(404, {"error": "not_found"})
            return
        auth = self.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer ") or not 1 <= len(auth[7:]) <= 1024:
            self.answer(401, {"error": "invalid_api_key"})
            return
        key = auth[7:]
        if not observer.allowed_request(key):
            self.answer(429, {"error": "rate_limited"})
            return
        try:
            client = observer.authenticate(key)
            if client is None:
                self.answer(401, {"error": "invalid_api_key"})
                return
            query = parse_qs(path.query, keep_blank_values=True, max_num_fields=8)
            if any(len(v) != 1 for v in query.values()):
                raise ValueError("duplicate parameters")
            if path.path == "/v1/quota":
                if query:
                    raise ValueError("quota accepts no account selectors")
                result = observer.quota_response()
            else:
                if set(query) - {"period", "date", "recent"}:
                    raise ValueError("unsupported query parameter")
                recent = query.get("recent", ["10"])[0]
                if not re.fullmatch(r"[0-9]{1,2}", recent) or int(recent) > 50:
                    raise ValueError("recent must be between 0 and 50")
                period = query.get("period", ["day"])[0]
                if period == "overview":
                    if set(query) != {"period"}:
                        raise ValueError("overview accepts no date or recent")
                    result = observer.store.overview(client)
                else:
                    result = observer.store.summary(client, period, query.get("date", [None])[0], int(recent))
            self.answer(200, result)
        except ValueError:
            self.answer(400, {"error": "invalid_query"})
        except Exception:
            self.answer(503, {"error": "backend_unavailable"})

    def do_POST(self):
        self.answer(405, {"error": "method_not_allowed"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/cpa-observer/config.json")
    parser.add_argument("--import-backlog", nargs="*")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--period", default="day", choices=["day", "month", "all"])
    parser.add_argument("--date")
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(Path(args.config).read_text())
    observer = Observer(config)
    if args.import_backlog is not None:
        for filename in args.import_backlog:
            inserted, rejected = observer.store.ingest(json.loads(Path(filename).read_text()))
            print(json.dumps({"inserted": inserted, "rejected": rejected}))
        return
    if args.report:
        clients = set(config.get("labels", {}))
        with observer.store.db() as con:
            clients.update(row[0] for row in con.execute("SELECT DISTINCT client_id FROM records"))
        print(json.dumps([observer.store.summary(c, args.period, args.date) for c in sorted(clients)], ensure_ascii=False))
        return
    workers = []
    for target in (observer.collect, observer.quota_worker):
        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        workers.append(worker)
    server = Server(("127.0.0.1", config.get("port", 8318)), observer)
    def stop(*_):
        observer.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        observer.stop.set()
        server.server_close()
        for worker in workers:
            worker.join(timeout=35)


if __name__ == "__main__":
    main()
