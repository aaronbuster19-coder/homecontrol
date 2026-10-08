"""Login sessions: signed cookie, Basic fallback, login rate limit, ASGI auth guard."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

COOKIE = "hc_session"
SESSION_TTL = 90 * 24 * 3600
MAX_FAILS = 10
FAIL_WINDOW = 600
PUBLIC_PATHS = {"/healthz", "/login.html", "/login.js", "/style.css", "/manifest.webmanifest", "/sw.js", "/api/login"}
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
    if not header:
        return False
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        given_user, sep, given_pw = base64.b64decode(encoded, validate=True).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return bool(sep) and check_credentials(given_user, given_pw, user, password)


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
    def __init__(self, secret: bytes, password: str):
        # Mixing the password in means changing APP_PASSWORD invalidates every session.
        self.key = hmac.new(secret, b"pw:" + hashlib.sha256(password.encode()).digest(), hashlib.sha256).digest()

    def _sig(self, payload: str) -> str:
        return _b64(hmac.new(self.key, payload.encode(), hashlib.sha256).digest())

    def issue(self, user: str, now: float | None = None) -> str:
        payload = _b64(json.dumps({"u": user, "exp": int((now or time.time()) + SESSION_TTL)}).encode())
        return f"{payload}.{self._sig(payload)}"

    def verify(self, token: str | None, now: float | None = None) -> str | None:
        if not token or "." not in token:
            return None
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sig(payload)):
            return None
        try:
            data = json.loads(_unb64(payload))
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("exp", 0) < (now or time.time()):
            return None
        return data.get("u")


class RateLimiter:
    def __init__(self, max_fails: int = MAX_FAILS, window: float = FAIL_WINDOW):
        self.max_fails, self.window, self.fails = max_fails, window, {}

    def _recent(self, key: str, now: float) -> list[float]:
        hits = [t for t in self.fails.get(key, []) if now - t < self.window]
        if hits:
            self.fails[key] = hits
        else:
            self.fails.pop(key, None)
        return hits

    def blocked(self, key: str) -> bool:
        return len(self._recent(key, time.monotonic())) >= self.max_fails

    def fail(self, key: str):
        now = time.monotonic()
        self.fails[key] = self._recent(key, now) + [now]
        if len(self.fails) > 10000:  # keep memory bounded
            self.fails.clear()


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


class AuthMiddleware:
    """Pure ASGI guard: only short-circuits, so streaming responses (SSE) pass through untouched."""

    def __init__(self, app, sessions: Sessions, user: str, password: str):
        self.app, self.sessions, self.user, self.password = app, sessions, user, password

    def authed(self, headers: dict) -> str | None:
        who = self.sessions.verify(cookie_value(headers.get("cookie", "")))
        if who:
            return who
        if check_basic_auth(headers.get("authorization"), self.user, self.password):
            return self.user
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or is_public(scope["path"]):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        user = self.authed(headers)
        if user:
            scope.setdefault("state", {})["user"] = user
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
