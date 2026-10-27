"""Admin sessions and one-use login tickets; only password hashes are stored."""
import hashlib
import hmac
import secrets
import time

from server import PolicyError


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


class Authentication:
    def __init__(self, store):
        self.store = store
        with store.db() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS administrators (
                    name TEXT PRIMARY KEY, salt TEXT NOT NULL, password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    hash TEXT PRIMARY KEY, actor TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (
                    hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS login_limits (
                    bucket TEXT PRIMARY KEY, started REAL NOT NULL, attempts INTEGER NOT NULL);
            ''')

    def set_password(self, username, password):
        if not isinstance(username, str) or not username.isascii() or not username.replace('_', '').replace('-', '').isalnum() or not 1 <= len(username) <= 64:
            raise PolicyError(400, "管理员名称只允许英文字母、数字、下划线或连字符。")
        if not isinstance(password, str) or not 14 <= len(password) <= 256:
            raise PolicyError(400, "密码长度需为 14–256 字符。")
        salt = secrets.token_hex(16)
        hashed = password_hash(password, salt)
        with self.store.db() as con:
            con.execute("INSERT OR REPLACE INTO administrators VALUES (?,?,?)", (username, salt, hashed))
            con.execute("DELETE FROM sessions")
        return {"message": "管理员密码已设置；原网页会话已退出。"}

    def ticket(self):
        token = secrets.token_urlsafe(32)
        with self.store.db() as con:
            con.execute("DELETE FROM tickets WHERE expires < ?", (time.time(),))
            con.execute("INSERT INTO tickets VALUES (?,?)", (digest(token), time.time() + 120))
        return token

    def login(self, body, bucket):
        now = time.time()
        # Limits are persistent, so restarting the process cannot reset a guessing window.
        with self.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM login_limits WHERE started < ?", (now - 600,))
            for name, limit in (("global", 60), (digest(bucket), 12)):
                row = con.execute("SELECT attempts FROM login_limits WHERE bucket=?", (name,)).fetchone()
                if row and row[0] >= limit:
                    raise PolicyError(429, "登录尝试过多，请 10 分钟后再试。")
            for name in ("global", digest(bucket)):
                con.execute("INSERT INTO login_limits VALUES (?,?,1) ON CONFLICT(bucket) DO UPDATE SET attempts=attempts+1", (name, now))
        actor = None
        if isinstance(body.get("ticket"), str):
            with self.store.db() as con:
                con.execute("BEGIN IMMEDIATE")
                token = digest(body["ticket"])
                row = con.execute("SELECT expires FROM tickets WHERE hash=?", (token,)).fetchone()
                if row and row[0] > now:
                    con.execute("DELETE FROM tickets WHERE hash=?", (token,))
                    actor = "ssh-admin"
        elif isinstance(body.get("username"), str) and isinstance(body.get("password"), str) and len(body["password"]) <= 256:
            with self.store.db() as con:
                row = con.execute("SELECT salt,password_hash FROM administrators WHERE name=?", (body["username"],)).fetchone()
            salt, expected = row if row else ("00" * 16, "00" * 64)
            valid = hmac.compare_digest(password_hash(body["password"], salt), expected)
            if row and valid:
                actor = "web:" + body["username"]
        if actor is None:
            raise PolicyError(401, "登录信息无效或链接已过期。")
        session = secrets.token_urlsafe(32)
        with self.store.db() as con:
            con.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            con.execute("INSERT INTO sessions VALUES (?,?,?)", (digest(session), actor, now + 8 * 3600))
        return session

    def actor(self, token):
        with self.store.db() as con:
            row = con.execute("SELECT actor FROM sessions WHERE hash=? AND expires>?", (digest(token), time.time())).fetchone()
        if not row:
            raise PolicyError(401, "请先登录管理员账号。")
        return row[0]

    def logout(self, token):
        with self.store.db() as con:
            con.execute("DELETE FROM sessions WHERE hash=?", (digest(token),))
