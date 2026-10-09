"""User accounts: admin / member / guest, scrypt password hashes, per-user session ids, admin API.

The account named by APP_USER / APP_PASSWORD is the *owner*: always an admin, its password lives in .env, and it is
re-synced on every start (so an existing single-user setup simply becomes that admin, and changing APP_PASSWORD still
signs that account out everywhere). Other accounts live in the `users` table next to the layout.

Each account has a random session id (`sid`) that goes into its cookies; a new password or removing the account gives a
new sid / no row, so every cookie issued before stops working at once. Guests can carry an expiry (epoch seconds).
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time

ROLES = ("admin", "member", "guest")
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,31}$")
PW_MIN, PW_MAX = 8, 256
MAX_USERS = 50
MAX_EXPIRY = 366 * 86400

# scrypt: N=2^15, r=8, p=1 -> 32 MiB and ~0.1 s per check. Stored with its parameters so they can be raised later.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1


class UserError(ValueError):
    pass


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    n, r, p = SCRYPT_N, SCRYPT_R, SCRYPT_P  # read at call time: the unit tests lower N to stay fast
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * n * r + (1 << 20), dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt, want = encoded.split("$")
        n, r, p = int(n), int(r), int(p)
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=n, r=r, p=p,
                            maxmem=256 * n * r + (1 << 20), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(want))


def check_username(name) -> str:
    if not isinstance(name, str) or not USERNAME_RE.match(name):
        raise UserError("username: 1–32 letters, digits or . _ @ -, starting with a letter or digit")
    return name


def check_password(pw) -> str:
    if not isinstance(pw, str) or not PW_MIN <= len(pw) <= PW_MAX:
        raise UserError(f"password must be {PW_MIN}–{PW_MAX} characters")
    return pw


class UserStore:
    """SQLite-backed accounts. Usernames are unique ignoring case; a login matches them ignoring case too."""

    def __init__(self, path: str, clock=time.time, kdf=hash_password):
        self.path, self.clock, self.kdf, self._lock = path, clock, kdf, threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY COLLATE NOCASE, role TEXT NOT NULL, "
                      "pw TEXT NOT NULL, sid TEXT NOT NULL, expires REAL, created REAL NOT NULL, "
                      "owner INTEGER NOT NULL DEFAULT 0)")
        # Same work for an unknown username as for a wrong password: verify against this throwaway hash.
        self._dummy = self.kdf(secrets.token_hex(16))

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    # ---- reads ----
    def get(self, username: str) -> dict | None:
        if not isinstance(username, str) or not username:
            return None
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def all(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM users ORDER BY owner DESC, username COLLATE NOCASE")]

    def expired(self, u: dict) -> bool:
        return u.get("expires") is not None and u["expires"] <= self.clock()

    def public(self, u: dict) -> dict:
        return {"username": u["username"], "role": u["role"], "expires": u["expires"], "owner": bool(u["owner"]),
                "created": u["created"], "expired": self.expired(u)}

    def listing(self) -> list[dict]:
        return [self.public(u) for u in self.all()]

    def active(self, username: str, sid: str | None = None) -> dict | None:
        """The account if it exists, hasn't expired and (given a sid) that session is still current."""
        u = self.get(username)
        if u is None or self.expired(u):
            return None
        if sid is not None and not hmac.compare_digest(str(sid).encode(), u["sid"].encode()):
            return None
        return u

    def authenticate(self, username: str, password: str) -> dict | None:
        """Constant work whether or not the user exists; None for unknown user, wrong password or an expired guest."""
        u = self.get(username) if isinstance(username, str) and len(username) <= 64 else None
        ok = verify_password(password if isinstance(password, str) else "", u["pw"] if u else self._dummy)
        if not (u and ok and password) or self.expired(u):
            return None
        return u

    # ---- writes ----
    def _admins(self, c) -> int:
        return c.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]

    def sync_owner(self, username: str, password: str) -> None:
        """APP_USER / APP_PASSWORD -> the owner account (admin). Called at start; empty env = no owner account."""
        with self._lock, self._conn() as c:
            if not username or not password:
                return
            c.execute("DELETE FROM users WHERE owner = 1 AND username != ?", (username,))  # APP_USER was renamed
            row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if row and row["owner"] and row["role"] == "admin" and verify_password(password, row["pw"]):
                return
            pw, sid = self.kdf(password), secrets.token_urlsafe(16)
            if row:  # new APP_PASSWORD (or an existing account became APP_USER): sign it out everywhere
                c.execute("UPDATE users SET username = ?, role = 'admin', pw = ?, sid = ?, expires = NULL, owner = 1 "
                          "WHERE username = ?", (username, pw, sid, username))
            else:
                c.execute("INSERT INTO users VALUES (?, 'admin', ?, ?, NULL, ?, 1)", (username, pw, sid, self.clock()))

    def check_expiry(self, role: str, expires) -> float | None:
        if expires is None:
            return None
        if role != "guest":
            raise UserError("only guests can have an expiry")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            raise UserError("expires must be a time (epoch seconds) or null")
        now = self.clock()
        if not now < expires <= now + MAX_EXPIRY:
            raise UserError("expiry must be in the future and within a year")
        return float(expires)

    def create(self, data) -> dict:
        if not isinstance(data, dict) or not set(data) <= {"username", "password", "role", "expires"}:
            raise UserError("send {username, password, role, expires}")
        name, pw = check_username(data.get("username")), check_password(data.get("password"))
        role = data.get("role")
        if role not in ROLES:
            raise UserError("role must be admin, member or guest")
        expires = self.check_expiry(role, data.get("expires"))
        hashed = self.kdf(pw)
        with self._lock, self._conn() as c:
            if c.execute("SELECT 1 FROM users WHERE username = ?", (name,)).fetchone():
                raise UserError("that username is taken")
            if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= MAX_USERS:
                raise UserError(f"at most {MAX_USERS} users")
            c.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, 0)",
                      (name, role, hashed, secrets.token_urlsafe(16), expires, self.clock()))
        return self.public(self.get(name))

    def _editable(self, c, username: str):
        row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            raise LookupError(username)
        if row["owner"]:
            raise UserError("this account comes from APP_USER / APP_PASSWORD in .env: change it there")
        return row

    def update(self, username: str, data) -> dict:
        """Role and/or expiry. Both are read on every request, so a change applies to open sessions at once."""
        if not isinstance(data, dict) or not data or not set(data) <= {"role", "expires"}:
            raise UserError("send {role, expires}")
        with self._lock, self._conn() as c:
            row = self._editable(c, username)
            role = data.get("role", row["role"])
            if role not in ROLES:
                raise UserError("role must be admin, member or guest")
            if row["role"] == "admin" and role != "admin" and self._admins(c) <= 1:
                raise UserError("keep at least one admin")
            expires = self.check_expiry(role, data["expires"]) if "expires" in data else \
                (row["expires"] if role == "guest" else None)
            c.execute("UPDATE users SET role = ?, expires = ? WHERE username = ?", (role, expires, row["username"]))
            name = row["username"]
        return self.public(self.get(name))

    def set_password(self, username: str, password, *, allow_owner: bool = False) -> dict:
        """New password + new session id: every existing session of that account ends."""
        hashed = self.kdf(check_password(password))
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone() if allow_owner \
                else self._editable(c, username)
            if row is None:
                raise LookupError(username)
            c.execute("UPDATE users SET pw = ?, sid = ? WHERE username = ?",
                      (hashed, secrets.token_urlsafe(16), row["username"]))
        return self.get(row["username"])

    def delete(self, username: str) -> None:
        with self._lock, self._conn() as c:
            row = self._editable(c, username)
            if row["role"] == "admin" and self._admins(c) <= 1:
                raise UserError("keep at least one admin")
            c.execute("DELETE FROM users WHERE username = ?", (row["username"],))


def add_routes(app, users: UserStore, sessions, limiter, on_change=None) -> None:
    """Admin: GET/POST /api/users, PATCH/DELETE /api/users/{username}, PUT /api/users/{username}/password.
    Anyone signed in: POST /api/me/password {current, password} (their own; a fresh cookie comes back)."""
    from fastapi import HTTPException, Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    from .auth import COOKIE, SESSION_TTL, client_key, is_https

    async def body(request: Request):
        # JSON only: a cross-site <form> can't send application/json, so these writes can't be forged from another page.
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "send JSON")
        try:
            return await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON")

    def not_self(request: Request, username: str):
        if username.lower() == request.state.user.lower():
            raise HTTPException(400, "you can't do that to your own account here")

    def changed(what: str, request: Request, username: str):
        if on_change:
            on_change(what, by=request.state.user, username=username)

    @app.get("/api/users")
    async def list_users():
        return {"users": users.listing()}

    @app.post("/api/users")
    async def create_user(request: Request):
        data = await body(request)
        try:
            u = await run_in_threadpool(users.create, data)
        except UserError as e:
            raise HTTPException(400, str(e))
        changed("user_added", request, u["username"])
        return u

    @app.patch("/api/users/{username}")
    async def update_user(username: str, request: Request):
        not_self(request, username)
        data = await body(request)
        try:
            u = users.update(username, data)
        except LookupError:
            raise HTTPException(404, "no such user")
        except UserError as e:
            raise HTTPException(400, str(e))
        changed("user_changed", request, u["username"])
        return u

    @app.put("/api/users/{username}/password")
    async def reset_password(username: str, request: Request):
        not_self(request, username)
        data = await body(request)
        try:
            u = await run_in_threadpool(users.set_password, username, (data or {}).get("password") if isinstance(data, dict) else None)
        except LookupError:
            raise HTTPException(404, "no such user")
        except UserError as e:
            raise HTTPException(400, str(e))
        changed("user_password_reset", request, u["username"])
        return users.public(u)

    @app.delete("/api/users/{username}")
    async def delete_user(username: str, request: Request):
        not_self(request, username)
        try:
            users.delete(username)
        except LookupError:
            raise HTTPException(404, "no such user")
        except UserError as e:
            raise HTTPException(400, str(e))
        changed("user_removed", request, username)
        return {"ok": True}

    @app.post("/api/me/password")
    async def change_own_password(request: Request):
        data = await body(request)
        if not isinstance(data, dict):
            raise HTTPException(400, "send {current, password}")
        me = users.get(request.state.user)
        if me is None:
            raise HTTPException(401, "not authenticated")
        if me["owner"]:
            raise HTTPException(400, "this account's password is APP_PASSWORD in .env: change it there")
        key = client_key(request.headers, request.client.host if request.client else None)
        if limiter.blocked(key):
            raise HTTPException(429, "too many attempts, try again later")
        if not await run_in_threadpool(users.authenticate, me["username"], data.get("current")):
            limiter.fail(key)
            raise HTTPException(400, "current password is wrong")
        try:
            u = await run_in_threadpool(users.set_password, me["username"], data.get("password"))
        except UserError as e:
            raise HTTPException(400, str(e))
        changed("password_changed", request, u["username"])
        # Every other session of this account ends; this one carries on with a fresh cookie.
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, sessions.issue(u["username"], u["sid"]), max_age=SESSION_TTL, path="/",
                        httponly=True, samesite="lax", secure=is_https(request))
        return resp
