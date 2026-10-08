import base64
import time

import httpx
import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.auth import COOKIE, RateLimiter, Sessions, client_key, load_secret
from backend.config import Settings
from backend.ha import HAClient


def make(tmp_path, fake_ha, password="s3cret", **kw):
    settings = Settings("http://ha.test", "test-token", "aaron", password, str(tmp_path / "layout.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(fake_ha.handler))
    return TestClient(create_app(settings, ha), **kw)


@pytest.fixture
def anon(tmp_path, fake_ha):
    with make(tmp_path, fake_ha) as c:
        yield c


def login(c, pw="s3cret", **kw):
    return c.post("/api/login", json={"username": "aaron", "password": pw}, **kw)


def test_login_success_sets_cookie(anon):
    r = login(anon)
    assert r.status_code == 200 and r.json() == {"user": "aaron"}
    sc = r.headers["set-cookie"]
    assert sc.startswith(COOKIE + "=") and "HttpOnly" in sc and "SameSite=lax" in sc and "Path=/" in sc
    assert "Max-Age=7776000" in sc and "Secure" not in sc
    assert anon.get("/api/me").json() == {"user": "aaron"}
    assert anon.get("/api/devices").status_code == 200
    assert anon.get("/").status_code == 200


def test_secure_behind_https(anon):
    assert "Secure" in login(anon, headers={"X-Forwarded-Proto": "https"}).headers["set-cookie"]


def test_secure_on_https_scheme(tmp_path, fake_ha):
    with make(tmp_path, fake_ha, base_url="https://testserver") as c:
        assert "Secure" in login(c).headers["set-cookie"]


def test_login_failure(anon):
    r = login(anon, "nope")
    assert r.status_code == 401 and "www-authenticate" not in r.headers and "set-cookie" not in r.headers
    assert anon.post("/api/login", json={"username": "x", "password": "s3cret"}).status_code == 401
    assert anon.get("/api/me").status_code == 401


def test_empty_creds_never_authenticate(tmp_path, fake_ha):
    with make(tmp_path, fake_ha, password="") as c:
        assert c.post("/api/login", json={"username": "aaron", "password": ""}).status_code == 401
        h = {"Authorization": "Basic " + base64.b64encode(b"aaron:").decode()}
        assert c.get("/api/me", headers=h).status_code == 401


def test_logout(anon):
    login(anon)
    r = anon.post("/api/logout")
    assert r.status_code == 200 and "Max-Age=0" in r.headers["set-cookie"]
    assert anon.get("/api/me").status_code == 401


def test_session_expiry_and_tamper():
    s = Sessions(b"k", "pw")
    tok = s.issue("aaron")
    assert s.verify(tok) == "aaron"
    assert s.verify(tok, now=time.time() + 89 * 86400) == "aaron"
    assert s.verify(tok, now=time.time() + 91 * 86400) is None
    payload, sig = tok.rsplit(".", 1)
    assert s.verify(payload + "." + ("A" if sig[0] != "A" else "B") + sig[1:]) is None
    assert s.verify("x" + tok) is None
    assert s.verify("garbage") is None and s.verify(None) is None
    assert Sessions(b"other", "pw").verify(tok) is None


def test_tampered_cookie_rejected(anon):
    anon.cookies.set(COOKIE, "eyJ1IjoiYWFyb24iLCJleHAiOjk5OTk5OTk5OTl9.bad")
    assert anon.get("/api/me").status_code == 401


def test_password_change_invalidates(tmp_path, fake_ha):
    with make(tmp_path, fake_ha) as c:
        login(c)
        tok = c.cookies[COOKIE]
    with make(tmp_path, fake_ha) as c:  # same secret file -> survives restart
        c.cookies.set(COOKIE, tok)
        assert c.get("/api/me").status_code == 200
    with make(tmp_path, fake_ha, password="new") as c:
        c.cookies.set(COOKIE, tok)
        assert c.get("/api/me").status_code == 401


def test_secret_persisted(tmp_path, monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    db = str(tmp_path / "d" / "layout.db")
    a = load_secret(db)
    assert a == load_secret(db) and len(a) == 64
    assert (tmp_path / "d" / "session_secret").stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("SESSION_SECRET", "envsecret")
    assert load_secret(db) == b"envsecret"


def test_rate_limit(anon):
    for _ in range(10):
        assert login(anon, "bad").status_code == 401
    assert login(anon).status_code == 429
    assert login(anon, headers={"CF-Connecting-IP": "1.2.3.4"}).status_code == 200  # other client unaffected


def test_rate_limiter_window(monkeypatch):
    rl, now = RateLimiter(2, 60), [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    rl.fail("a")
    rl.fail("a")
    assert rl.blocked("a") and not rl.blocked("b")
    now[0] += 61
    assert not rl.blocked("a")


def test_client_key():
    assert client_key({"cf-connecting-ip": "1.1.1.1", "x-forwarded-for": "2.2.2.2"}, "3.3.3.3") == "1.1.1.1"
    assert client_key({"x-forwarded-for": "2.2.2.2, 9.9.9.9"}, "3.3.3.3") == "2.2.2.2"
    assert client_key({}, "3.3.3.3") == "3.3.3.3"


def test_public_paths(anon):
    for p in ("/healthz", "/login.html", "/login.js", "/style.css", "/manifest.webmanifest", "/sw.js",
              "/icons/icon-192.png"):
        assert anon.get(p).status_code == 200, p
    assert anon.get("/manifest.webmanifest").headers["content-type"].startswith("application/manifest+json")
    r = anon.get("/sw.js")
    assert r.headers["cache-control"] == "no-cache" and "javascript" in r.headers["content-type"]


def test_protected_paths(anon):
    for p in ("/api/devices", "/api/layout", "/api/me", "/app.js", "/api/events"):
        r = anon.get(p, follow_redirects=False)
        assert r.status_code == 401 and "www-authenticate" not in r.headers, p
    for p in ("/", "/index.html"):
        r = anon.get(p, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login.html"


def test_basic_header_accepted(anon):
    h = {"Authorization": "Basic " + base64.b64encode(b"aaron:s3cret").decode()}
    assert anon.get("/api/me", headers=h).json() == {"user": "aaron"}


def test_streaming_passes_through(tmp_path, fake_ha):
    c = make(tmp_path, fake_ha)

    async def gen():
        for i in range(3):
            yield f"data: {i}\n\n"

    c.app.add_api_route("/api/stream-test", lambda: StreamingResponse(gen(), media_type="text/event-stream"))
    c.app.router.routes.insert(0, c.app.router.routes.pop())  # ahead of the static mount
    with c:
        assert c.get("/api/stream-test").status_code == 401
        login(c)
        with c.stream("GET", "/api/stream-test") as r:
            assert r.status_code == 200 and "".join(r.iter_text()) == "data: 0\n\ndata: 1\n\ndata: 2\n\n"
