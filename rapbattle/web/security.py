"""Passwords (argon2id), sessions, CSRF, IP hashing and rate limiting."""
import hashlib
import hmac
import re
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError

_ph = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
SESSION_DAYS = 14
_DUMMY = _ph.hash(secrets.token_urlsafe(16))


def hash_password(pw):
    return _ph.hash(pw)


def verify_password(stored, pw):
    try:
        return _ph.verify(stored or _DUMMY, pw) and bool(stored)
    except (VerificationError, InvalidHashError):
        return False


def password_problem(pw):
    if not isinstance(pw, str) or len(pw) < 10:
        return "Password must be at least 10 characters."
    if len(pw) > 256:
        return "Password is too long."
    return None


def norm_email(e):
    return str(e or "").strip().lower()


def valid_email(e):
    return bool(re.fullmatch(r"[^\s@<>\"]{1,64}@[^\s@<>\"]{1,190}\.[A-Za-z]{2,}", e))


def sha256(v):
    return hashlib.sha256(v.encode() if isinstance(v, str) else v).hexdigest()


class Keyring:
    def __init__(self, secret):
        self.ephemeral = not (secret and len(secret) >= 32)
        root = (secret if not self.ephemeral else secrets.token_urlsafe(48)).encode()
        self._csrf = hmac.new(root, b"rap-csrf", hashlib.sha256).digest()
        self._ip = hmac.new(root, b"rap-ip", hashlib.sha256).digest()

    def csrf_for(self, seed):
        return hmac.new(self._csrf, str(seed).encode(), hashlib.sha256).hexdigest()

    def ip_hash(self, ip):
        return hmac.new(self._ip, str(ip or "unknown").encode(), hashlib.sha256).hexdigest()[:32]


class Sessions:
    def __init__(self, db):
        self.db = db

    def create(self, user_id):
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.db.run(f"INSERT INTO sessions(id,user_id,csrf,expires_at) VALUES (%s,%s,%s, now() + interval '{SESSION_DAYS} days')", (sha256(raw), user_id, csrf))
        self.db.run("DELETE FROM sessions WHERE expires_at < now()")
        return raw

    def load(self, raw):
        if not isinstance(raw, str) or not 20 <= len(raw) <= 100:
            return None
        row = self.db.one("""SELECT s.id, s.csrf, u.id AS user_id, u.email, u.display_name, u.created_at, u.bootstrap,
            COALESCE(array_agg(DISTINCT r.role) FILTER (WHERE r.role IS NOT NULL), '{}') AS roles
            FROM sessions s JOIN users u ON u.id=s.user_id LEFT JOIN user_roles r ON r.user_id=u.id
            WHERE s.id=%s AND s.expires_at > now() AND NOT u.disabled GROUP BY s.id, u.id""", (sha256(raw),))
        if not row:
            return None
        leagues = {}
        for m in self.db.all("SELECT league_slug, role FROM league_members WHERE user_id=%s", (row["user_id"],)):
            leagues.setdefault(m["league_slug"], set()).add(m["role"])
        return {"session_id": row["id"], "csrf": row["csrf"], "user": User(row["user_id"], row["email"], row["display_name"], row["created_at"], row["bootstrap"], set(row["roles"]), leagues)}

    def destroy(self, raw):
        if raw:
            self.db.run("DELETE FROM sessions WHERE id=%s", (sha256(raw),))

    def destroy_all(self, user_id):
        self.db.run("DELETE FROM sessions WHERE user_id=%s", (user_id,))


class User:
    def __init__(self, uid, email, name, created_at, bootstrap, roles, leagues):
        self.id, self.email, self.name, self.created_at, self.bootstrap = int(uid), email, name, created_at, bootstrap
        self.roles, self.leagues = roles, leagues

    @property
    def is_admin(self):
        return "admin" in self.roles

    def has_league_role(self, slug, *roles):
        return self.is_admin or bool(self.leagues.get(slug, set()) & set(roles))

    def produces(self, slug):
        return self.has_league_role(slug, "producer")


class RateLimiter:
    """Fixed-window in-memory limiter (single instance, thread-safe)."""

    def __init__(self):
        self.hits, self.lock, self.sweep = {}, threading.Lock(), time.time()

    def allow(self, key, limit, window):
        now = time.time()
        with self.lock:
            if now - self.sweep > 60:
                self.hits = {k: v for k, v in self.hits.items() if v[1] > now}
                self.sweep = now
            count, reset = self.hits.get(key, (0, now + window))
            if reset < now:
                count, reset = 0, now + window
            count += 1
            self.hits[key] = (count, reset)
            return count <= limit


def client_ip(environ, trust_proxy):
    if trust_proxy:
        xff = (environ.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
        if xff:
            return xff
    return environ.get("REMOTE_ADDR") or "unknown"
