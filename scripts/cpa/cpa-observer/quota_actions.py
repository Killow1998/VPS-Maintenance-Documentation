"""Claude reset grants and conservative recovery of local quota cooldowns."""
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
    """CPA's reset clears ALL model states: refuse mixed/non-quota failures."""
    cooling = entry.get("cooldowns") or []
    if not cooling or any(c.get("reason") not in ("quota", "credential_quota") for c in cooling):
        return False
    if not any(c.get("scope") == "credential" for c in cooling):
        return False  # A model-only limit may be absent from the account usage response.
    # Every reported applicable limit must be known and available, including model windows.
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
            try:
                self.observer.backend.request("/v8/management/routing/cooldown/reset",
                                              {"auth_index": entry["auth_index"]})
                item["routing"] = {"blocked": False, "retry_at": None, "recovery": "cleared"}
            except Exception:
                item["routing"]["recovery"] = "failed"

    def redeem(self, account_id, grant_id, operation_id, actor="admin"):
        if not all(isinstance(v, str) and ID.fullmatch(v) for v in (account_id, grant_id, operation_id)):
            raise ValueError("invalid_reset_selection")
        obs = self.observer
        key = "reset_operation:" + account_id
        saved = json.loads(obs.store.meta(key) or "{}")
        if saved.get("id") == operation_id:
            if saved.get("grant_id") != grant_id:
                raise ValueError("reset_selection_changed")
            return saved
        if saved.get("result") == "unknown" or time.time() - saved.get("created", 0) < 30:
            raise ValueError("reset_pending_or_recent")
        last_attempt = float(obs.store.meta("reset_attempt:" + account_id) or "0")
        if time.time() - last_attempt < 30:
            raise ValueError("reset_preflight_rate_limited")
        obs.store.meta("reset_attempt:" + account_id, str(time.time()))
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
        operation = {"id": operation_id, "grant_id": grant_id, "created": time.time(), "actor": str(actor)[:120],
                     "request_id": uuid.uuid4().hex, "result": "unknown"}
        # Commit BEFORE sending. Ambiguous outcomes survive process restarts and never re-spend.
        obs.store.meta(key, json.dumps(operation))
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
        obs.store.meta(key, json.dumps(operation))
        return operation
