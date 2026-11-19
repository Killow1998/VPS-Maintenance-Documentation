"""Claude reset grants and safe reporting of local quota cooldowns."""
import datetime as dt
import json
import re
import time
import uuid

UTC = dt.timezone.utc
ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def future(value):
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) > dt.datetime.now(UTC)
    except (ValueError, TypeError, AttributeError):
        return False


def grants(body):
    block = body.get("cedar_ember")
    if not isinstance(block, dict) or type(block.get("eligible")) is not bool:
        return {"status": "unknown", "grants": []}
    result = []
    for raw in block.get("grants", []):
        if not isinstance(raw, dict) or not ID.fullmatch(str(raw.get("id", ""))):
            return {"status": "unknown", "grants": []}
        total, left = raw.get("resets_total"), raw.get("resets_left")
        if type(total) is not int or type(left) is not int or not 0 <= left <= total:
            return {"status": "unknown", "grants": []}
        item = {key: raw.get(key) for key in (
            "id", "resets_total", "resets_left", "starts_at", "ends_at", "paused",
            "usable_now", "use_requires_limit", "gives_back_weekly", "percent_used")}
        item["clears"] = [x for x in raw.get("clears", []) if x in
                          ("five_hour", "seven_day", "seven_day_overage_included")]
        # The upstream 'full' type also covers Fable-only refunds; use its scope flags.
        item["kind"] = "fable" if raw.get("gives_back_weekly") is True else (
            "full" if "seven_day" in item["clears"] else "session")
        result.append(item)
    return {"status": "ok", "eligible": block["eligible"],
            "at_limit": block.get("at_limit") is True, "grants": result}


def usable(status, grant):
    percentages = grant.get("percent_used")
    if grant.get("gives_back_weekly") and isinstance(percentages, dict) and percentages and all(v == 0 for v in percentages.values()):
        return False
    return (status.get("status") == "ok" and status.get("eligible") is True
            and grant.get("usable_now") is True and not grant.get("paused")
            and grant["resets_left"] > 0
            and (not grant.get("ends_at") or future(grant["ends_at"]))
            and (not grant.get("starts_at") or not future(grant["starts_at"]))
            and (grant.get("use_requires_limit") is False or status.get("at_limit") is True))


def recoverable(entry, windows):
    """Report whether an account cooldown appears eligible for manual review."""
    cooling = entry.get("cooldowns") or []
    if not cooling or any(c.get("reason") not in ("quota", "credential_quota") for c in cooling):
        return False
    if any(c.get("scope") != "credential" for c in cooling):
        return False  # Model limits cannot be reliably mapped to account usage windows.
    # Every reported applicable account window must be known and available.
    if not all(k in windows for k in ("five_hour", "seven_day")):
        return False
    return all(type(w.get("remaining_percent")) in (int, float)
               and w["remaining_percent"] > 0 for w in windows.values())


class QuotaActions:
    def __init__(self, observer):
        self.observer = observer

    def upstream(self, source, path, data=None):
        response = self.observer.backend.request("/v8/management/requests/api-call", {
            "auth_index": source["auth_index"], "method": "GET" if data is None else "POST",
            "url": "https://api.anthropic.com" + path,
            "header": {"Authorization": "Bearer $TOKEN$", "Accept": "application/json",
                       "Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20",
                       "User-Agent": "claude-cli/2.1.321 (external, cli)"},
            **({"data": json.dumps(data)} if data is not None else {})})
        body = response.get("body")
        try:
            body = json.loads(body) if isinstance(body, str) else body
        except ValueError:
            body = None
        return response.get("status_code"), body

    def recover(self, entry, item):
        item["routing"] = {"blocked": bool(entry.get("unavailable")),
                           "retry_at": entry.get("next_retry_after"), "recovery": "unchanged"}
        if recoverable(entry, item["windows"]):
            # CPA's reset is unconditional; a snapshot cannot rule out a new model cooldown.
            item["routing"]["recovery"] = "manual"

    @staticmethod
    def _get(con, key):
        row = con.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _put(con, key, value):
        con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, value))

    def _reserve(self, account_id, grant_id, operation_id, actor):
        latest_key = "reset_operation:" + account_id
        attempt_key = "reset_attempt:" + account_id
        record_key = "reset_operation_id:" + account_id + ":" + operation_id
        at = time.time()
        with self.observer.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            legacy_raw = self._get(con, latest_key)
            legacy = json.loads(legacy_raw or "{}")
            if isinstance(legacy.get("id"), str) and ID.fullmatch(legacy["id"]):
                legacy_key = "reset_operation_id:" + account_id + ":" + legacy["id"]
                if not self._get(con, legacy_key):
                    self._put(con, legacy_key, legacy_raw)
            saved_raw = self._get(con, record_key)
            if saved_raw:
                saved = json.loads(saved_raw)
                if saved.get("grant_id") != grant_id:
                    raise ValueError("reset_selection_changed")
                return saved, False, None
            if legacy.get("result") == "unknown" or at - legacy.get("created", 0) < 30:
                raise ValueError("reset_pending_or_recent")
            if at - float(self._get(con, attempt_key) or "0") < 30:
                raise ValueError("reset_preflight_rate_limited")
            operation = {"id": operation_id, "grant_id": grant_id, "created": at, "actor": str(actor)[:120],
                         "request_id": uuid.uuid4().hex, "result": "unknown"}
            raw = json.dumps(operation)
            self._put(con, record_key, raw)
            self._put(con, latest_key, raw)
            self._put(con, attempt_key, str(at))
        return operation, True, legacy_raw

    def _finish(self, account_id, operation):
        latest_key = "reset_operation:" + account_id
        record_key = "reset_operation_id:" + account_id + ":" + operation["id"]
        with self.observer.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            current = json.loads(self._get(con, latest_key) or "{}")
            if current.get("request_id") != operation["request_id"]:
                raise ValueError("reset_operation_changed")
            raw = json.dumps(operation)
            self._put(con, record_key, raw)
            self._put(con, latest_key, raw)

    def _cancel_preflight(self, account_id, operation, legacy_raw):
        latest_key = "reset_operation:" + account_id
        record_key = "reset_operation_id:" + account_id + ":" + operation["id"]
        with self.observer.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            current = json.loads(self._get(con, latest_key) or "{}")
            if current.get("request_id") != operation["request_id"]:
                return
            con.execute("DELETE FROM metadata WHERE key=?", (record_key,))
            if legacy_raw is None:
                con.execute("DELETE FROM metadata WHERE key=?", (latest_key,))
            else:
                self._put(con, latest_key, legacy_raw)

    def redeem(self, account_id, grant_id, operation_id, actor="admin"):
        if not all(isinstance(v, str) and ID.fullmatch(v) for v in (account_id, grant_id, operation_id)):
            raise ValueError("invalid_reset_selection")
        obs = self.observer
        operation, fresh, legacy_raw = self._reserve(account_id, grant_id, operation_id, actor)
        if not fresh:
            return operation
        try:
            entries = obs.backend.accounts()
            helpers = {b.get("credential") for b in obs.config.get("quota_bindings", {}).values()}
            targets = [e for e in entries if e.get("provider") == "claude" and e.get("name") not in helpers
                       and not e.get("disabled") and e.get("auth_index")
                       and "account-" + obs.store.digest("account:" + e["auth_index"])[:12] == account_id]
            if len(targets) != 1:
                raise ValueError("account_not_available")
            entry = targets[0]
            source = obs.quota_source(entry, {e["name"]: e for e in entries})
            status, body = self.upstream(source, "/api/oauth/usage?cedar_ember=1&skip_spend=1")
            if status != 200 or not isinstance(body, dict):
                raise ValueError("reset_status_unavailable")
            current = grants(body)
            grant = next((g for g in current["grants"] if g["id"] == grant_id), None)
            if not grant or not usable(current, grant):
                raise ValueError("reset_not_usable")
            status, profile = self.upstream(source, "/api/oauth/profile")
            organization = profile.get("organization", {}).get("uuid") if isinstance(profile, dict) else None
            try:
                organization = str(uuid.UUID(organization))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("reset_identity_unavailable") from None
            if status != 200:
                raise ValueError("reset_identity_unavailable")
        except Exception:
            self._cancel_preflight(account_id, operation, legacy_raw)
            raise
        try:
            status, answer = self.upstream(source, "/api/organizations/" + organization + "/reset_rate_limits",
                {"program": "cedar_ember", "grant_id": grant_id, "request_id": operation["request_id"]})
            result = answer.get("result") if isinstance(answer, dict) else None
            if status in (401, 403, 429):
                operation["result"] = "rate_limited" if status == 429 else "auth_error"
            elif status == 200 and result in ("reset", "already_used", "not_limited", "cooldown", "ineligible", "unavailable"):
                operation["result"] = result
        except Exception:
            pass
        self._finish(account_id, operation)
        return operation
