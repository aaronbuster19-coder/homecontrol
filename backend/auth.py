"""Login sessions: signed cookie, Basic fallback, login rate limit, ASGI auth guard (accounts: backend/users.py)."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from starlette.concurrency import run_in_threadpool

COOKIE = "hc_session"
SESSION_TTL = 90 * 24 * 3600
MAX_FAILS = 10
MAX_FAILS_GLOBAL = 50  # across all clients, since client keys come partly from headers
FAIL_WINDOW = 600
PUBLIC_PATHS = {"/healthz", "/login.html", "/login.js", "/style.css", "/manifest.webmanifest", "/sw.js", "/api/login",
                "/guest.html", "/guest.js", "/guest.css", "/api/guest/redeem"}  # guest links: backend/guest_links.py
PUBLIC_PREFIXES = ("/icons/",)
PAGE_PATHS = {"/", "/index.html"}


def check_credentials(user: str, password: str, want_user: str, want_password: str) -> bool:
    if not want_user or not want_password:
        return False
    # Evaluate both comparisons so timing doesn't reveal which one failed.
    ok_user = secrets.compare_digest(user.encode(), want_user.encode())
    ok_pw = secrets.compare_digest(password.encode(), want_password.encode())
    return ok_user and ok_pw


def check_basic_auth(header: str | None, user: str, password: str) -> bool:
    creds = basic_credentials(header)
    return bool(creds) and check_credentials(*creds, user, password)


def load_secret(db_path: str) -> bytes:
    env = os.environ.get("SESSION_SECRET")
    if env:
        return env.encode()
    path = Path(db_path).resolve().parent / "session_secret"
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(value)
    return value


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Sessions:
    """Signed cookie {u: user, s: that account's session id, exp}. The session id (backend/users.py) is what a new
    password or removing the account changes, so those end every session of the account."""

    def __init__(self, secret: bytes, password: str = ""):
        self.key = hmac.new(secret, b"sessions:v2", hashlib.sha256).digest()
        # Cookies from before multi-user logins were signed with APP_PASSWORD mixed in and carry no session id; they
        # stay valid for the APP_USER account only (and still end when APP_PASSWORD changes).
        self.legacy_key = hmac.new(secret, b"pw:" + hashlib.sha256(password.encode()).digest(), hashlib.sha256).digest()

    @staticmethod
    def _sig(key: bytes, payload: str) -> str:
        return _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())

    def issue(self, user: str, sid: str = "", now: float | None = None) -> str:
        payload = _b64(json.dumps({"u": user, "s": sid, "exp": int((now or time.time()) + SESSION_TTL)}).encode())
        return f"{payload}.{self._sig(self.key, payload)}"

    def read(self, token: str | None, now: float | None = None) -> dict | None:
        """{"u", "s", "legacy"} of a valid, unexpired cookie, else None."""
        if not token or "." not in token:
            return None
        payload, sig = token.rsplit(".", 1)
        legacy = False
        if not hmac.compare_digest(sig, self._sig(self.key, payload)):
            if not hmac.compare_digest(sig, self._sig(self.legacy_key, payload)):
                return None
            legacy = True
        try:
            data = json.loads(_unb64(payload))
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("u"), str) or data.get("exp", 0) < (now or time.time()):
            return None
        if legacy == ("s" in data):  # a new-style payload under the old key (or vice versa) is not a cookie we issued
            return None
        return {"u": data["u"], "s": data.get("s"), "legacy": legacy}

    def verify(self, token: str | None, now: float | None = None) -> str | None:
        data = self.read(token, now)
        return data["u"] if data else None


class RateLimiter:
    GLOBAL = "*"

    def __init__(self, max_fails: int = MAX_FAILS, window: float = FAIL_WINDOW, max_global: int = MAX_FAILS_GLOBAL):
        self.max_fails, self.window, self.max_global, self.fails = max_fails, window, max_global, {}

    def _recent(self, key: str, now: float) -> list[float]:
        hits = [t for t in self.fails.get(key, []) if now - t < self.window]
        if hits:
            self.fails[key] = hits
        else:
            self.fails.pop(key, None)
        return hits

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        return len(self._recent(key, now)) >= self.max_fails or len(self._recent(self.GLOBAL, now)) >= self.max_global

    def fail(self, key: str):
        now = time.monotonic()
        if len(self.fails) > 10000:  # keep memory bounded, but keep the global count
            self.fails = {self.GLOBAL: self.fails.get(self.GLOBAL, [])}
        self.fails[key] = self._recent(key, now) + [now]
        self.fails[self.GLOBAL] = self._recent(self.GLOBAL, now) + [now]


def client_key(headers, client_host: str | None) -> str:
    if headers.get("cf-connecting-ip"):
        return headers["cf-connecting-ip"].strip()
    if headers.get("x-forwarded-for"):
        return headers["x-forwarded-for"].split(",")[0].strip()
    return client_host or "?"


def is_https(request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def cookie_value(raw: str, name: str = COOKIE) -> str | None:
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


def basic_credentials(header: str | None) -> tuple[str, str] | None:
    if not header:
        return None
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        user, sep, pw = base64.b64decode(encoded, validate=True).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    return (user, pw) if sep else None


class AuthMiddleware:
    """Pure ASGI guard: only short-circuits, so streaming responses (SSE) pass through untouched.
    Puts the signed-in account's name and role into scope["state"] ("user", "role") for backend/roles.py."""

    BASIC_CACHE_TTL = 300  # a good Basic header is re-checked (scrypt) at most every 5 min, or at once on a new sid

    def __init__(self, app, sessions: Sessions, users, limiter: RateLimiter | None = None):
        self.app, self.sessions, self.users = app, sessions, users
        self.limiter = limiter or RateLimiter()
        self.basic_ok: dict[bytes, tuple[str, str, float]] = {}

    async def authed(self, headers: dict, client: str) -> dict | None:
        data = self.sessions.read(cookie_value(headers.get("cookie", "")))
        if data:
            u = self.users.active(data["u"]) if data["legacy"] else self.users.active(data["u"], data["s"])
            if u and (u["owner"] or not data["legacy"]):
                return u
        if headers.get("authorization"):
            # Basic attempts count against the same limit as the login form.
            if self.limiter.blocked(client):
                return None
            key = hashlib.sha256(headers["authorization"].encode("latin-1")).digest()
            hit = self.basic_ok.get(key)
            if hit and time.monotonic() - hit[2] < self.BASIC_CACHE_TTL:
                u = self.users.active(hit[0], hit[1])
                if u:
                    return u
            creds = basic_credentials(headers["authorization"])
            u = await run_in_threadpool(self.users.authenticate, *creds) if creds else None
            if u:
                if len(self.basic_ok) > 100:
                    self.basic_ok.clear()
                self.basic_ok[key] = (u["username"], u["sid"], time.monotonic())
                return u
            self.limiter.fail(client)
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if ".." in scope["path"].split("/"):
            # e.g. /icons/../app.js would match a public prefix but be served as app.js
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        if is_public(scope["path"]) or (scope.get("state") or {}).get("role") == "link":  # GuestLinkMiddleware vouched
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        client = client_key(headers, (scope.get("client") or (None,))[0])
        user = await self.authed(headers, client)
        if user:
            scope.setdefault("state", {}).update(user=user["username"], role=user["role"])
            return await self.app(scope, receive, send)
        if scope["path"] in PAGE_PATHS:
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", b"/login.html"), (b"cache-control", b"no-store")]})
        else:
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"detail":"not authenticated"}'})
            return
        await send({"type": "http.response.body", "body": b""})
