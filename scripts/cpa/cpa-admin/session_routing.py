"""Persistent, caller-scoped session affinity; never stores prompts or raw IDs."""
import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass


SESSION_HEADERS = ("X-Claude-Code-Session-Id", "Session-Id", "X-Http-Session-Id", "X-Session-ID")


def explicit_id(value):
    if not isinstance(value, str) or len(value) > 256 or not value.isprintable():
        return ""
    return value.strip()


def session_id(headers, body):
    for name in SESSION_HEADERS:
        value = explicit_id(headers.get(name))
        if value:
            return value
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    user = metadata.get("user_id")
    if isinstance(user, str) and len(user) <= 8192:
        try:
            parsed = json.loads(user)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and explicit_id(parsed.get("session_id")):
            return explicit_id(parsed["session_id"])
        legacy = re.search(r"_session_([a-fA-F0-9-]+)$", user)
        if legacy:
            return explicit_id(legacy[1])
    return explicit_id(metadata.get("session_id"))


@dataclass(frozen=True)
class Selection:
    key: tuple
    primary: str
    candidates: list
    generation: str


class SessionRouter:
    def __init__(self, store, secret, *, ttl=86400, limit=10000):
        self.store, self.secret, self.ttl, self.limit = store, secret.encode(), ttl, limit
        with store.db() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS session_routes (
                    client_id TEXT NOT NULL, session_hash TEXT NOT NULL, model TEXT NOT NULL,
                    account_id TEXT NOT NULL, expires REAL NOT NULL, generation TEXT NOT NULL,
                    PRIMARY KEY(client_id, session_hash, model));
                CREATE INDEX IF NOT EXISTS session_routes_expiry ON session_routes(expires);
                CREATE TABLE IF NOT EXISTS session_route_cursors (
                    pool_hash TEXT PRIMARY KEY, sequence INTEGER NOT NULL, expires REAL NOT NULL);
            """)

    def select(self, client, model, headers, body, candidates):
        raw = session_id(headers, body)
        # Missing identity is a stable per-Key binding, never a per-request identity.
        identity = "session:" + raw if raw else "key-fallback"
        digest = hmac.new(self.secret, identity.encode(), hashlib.sha256).hexdigest()
        key = (client, digest, model)
        by_id = {a["id"]: a for a in candidates}
        ids = sorted(by_id)
        pool = hashlib.sha256(json.dumps([model, ids]).encode()).hexdigest()
        at = time.time()
        with self.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM session_routes WHERE expires<=?", (at,))
            con.execute("DELETE FROM session_route_cursors WHERE expires<=?", (at,))
            row = con.execute("SELECT account_id, generation FROM session_routes WHERE client_id=? AND session_hash=? AND model=?", key).fetchone()
            primary = row[0] if row and row[0] in by_id else None
            generation = row[1] if primary else uuid.uuid4().hex
            if primary is None:
                cursor = con.execute("SELECT sequence FROM session_route_cursors WHERE pool_hash=?", (pool,)).fetchone()
                sequence = cursor[0] if cursor else 0
                primary = ids[sequence % len(ids)]
                con.execute("INSERT OR REPLACE INTO session_route_cursors VALUES (?,?,?)", (pool, sequence + 1, at + self.ttl))
            con.execute("INSERT OR REPLACE INTO session_routes VALUES (?,?,?,?,?,?)", (*key, primary, at + self.ttl, generation))
            con.execute("DELETE FROM session_routes WHERE rowid IN (SELECT rowid FROM session_routes ORDER BY expires DESC, rowid DESC LIMIT -1 OFFSET ?)", (self.limit,))
            con.execute("DELETE FROM session_route_cursors WHERE rowid IN (SELECT rowid FROM session_route_cursors ORDER BY expires DESC LIMIT -1 OFFSET ?)", (self.limit,))
        return Selection(key, primary, [by_id[primary]] + [by_id[a] for a in ids if a != primary], generation)

    def succeeded(self, selection, account, *, migrate):
        if account == selection.primary or not migrate:
            return
        # A late response from an older concurrent attempt must not undo a migration.
        with self.store.db() as con:
            con.execute("UPDATE session_routes SET account_id=?, expires=?, generation=? WHERE client_id=? AND session_hash=? AND model=? AND generation=?",
                        (account, time.time() + self.ttl, uuid.uuid4().hex, *selection.key, selection.generation))
