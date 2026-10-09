"""Accounts (backend/users.py): hashing, migration from the single APP_USER login, session invalidation, guest expiry
(injected clock), no user enumeration, and the admin API's guard rails."""
import base64
import hashlib
import hmac
import json
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import users as users_mod
from backend.app import create_app
from backend.auth import COOKIE, Sessions, _b64, load_secret
from backend.config import Settings
from backend.ha import HAClient
from backend.users import UserError, UserStore, hash_password, verify_password


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, fake_ha, user="aaron", password="s3cret", clock=time.time):
    settings = Settings("http://ha.test", "test-token", user, password, str(tmp_path / "layout.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(fake_ha.handler))
    return TestClient(create_app(settings, ha, clock=clock))


def login(c, user="aaron", pw="s3cret"):
    return c.post("/api/login", json={"username": user, "password": pw})


def add(c, name, role="member", pw="password-1", **kw):
    r = c.post("/api/users", json={"username": name, "password": pw, "role": role, **kw})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def admin(tmp_path, fake_ha):
    with make(tmp_path, fake_ha) as c:
        assert login(c).status_code == 200
        yield c


# ---------- hashing ----------
def test_hash_is_salted_scrypt():
    a, b = hash_password("pw"), hash_password("pw")
    assert a != b and a.startswith("scrypt$") and "pw" not in a.split("$", 4)[-1]
    assert verify_password("pw", a) and not verify_password("pW", a) and not verify_password("", a)
    for bad in ("", "x", "md5$1$2$3$4$5", "scrypt$a$b$c$d$e"):
        assert not verify_password("pw", bad)


def test_production_strength(monkeypatch):
    monkeypatch.undo()  # the conftest cheapens hashes for speed: check the real setting
    assert users_mod.SCRYPT_N >= 2 ** 15 and users_mod.SCRYPT_R >= 8
    assert hash_password("x").startswith(f"scrypt${2 ** 15}$8$1$")


def test_passwords_are_not_stored(admin, tmp_path):
    add(admin, "mia", pw="correct horse")
    rows = sqlite3.connect(tmp_path / "layout.db").execute("SELECT username, pw FROM users").fetchall()
    assert {r[0] for r in rows} == {"aaron", "mia"}
    assert all(r[1].startswith("scrypt$") and "s3cret" not in r[1] and "correct horse" not in r[1] for r in rows)


# ---------- migration from the single login ----------
def test_existing_login_becomes_admin(tmp_path, fake_ha):
    with make(tmp_path, fake_ha) as c:
        assert login(c).status_code == 200
        assert c.get("/api/me").json() == {"user": "aaron", "role": "admin", "expires": None, "owner": True}
        assert [u["username"] for u in c.get("/api/users").json()["users"]] == ["aaron"]


def legacy_cookie(tmp_path, user, password, exp=None):
    """A cookie exactly as the single-user version issued it (key mixed with APP_PASSWORD, no session id)."""
    secret = load_secret(str(tmp_path / "layout.db"))
    key = hmac.new(secret, b"pw:" + hashlib.sha256(password.encode()).digest(), hashlib.sha256).digest()
    payload = _b64(json.dumps({"u": user, "exp": int(exp or time.time() + 3600)}).encode())
    return f"{payload}.{_b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())}"


def test_old_cookie_survives_the_upgrade(tmp_path, fake_ha):
    with make(tmp_path, fake_ha) as c:
        add_c = login(c)
        assert add_c.status_code == 200
        add(c, "mia")
        c.cookies.clear()
        c.cookies.set(COOKIE, legacy_cookie(tmp_path, "aaron", "s3cret"))
        assert c.get("/api/me").json()["role"] == "admin"
        # Only for the APP_USER account, and only with the current APP_PASSWORD.
        c.cookies.set(COOKIE, legacy_cookie(tmp_path, "mia", "s3cret"))
        assert c.get("/api/me").status_code == 401
        c.cookies.set(COOKIE, legacy_cookie(tmp_path, "aaron", "old-password"))
        assert c.get("/api/me").status_code == 401
        c.cookies.set(COOKIE, legacy_cookie(tmp_path, "aaron", "s3cret", exp=time.time() - 1))
        assert c.get("/api/me").status_code == 401


def test_new_cookie_needs_new_key():
    s = Sessions(b"k", "pw")
    tok = s.issue("aaron", "sid1")
    assert s.read(tok) == {"u": "aaron", "s": "sid1", "legacy": False}
    payload, _ = tok.rsplit(".", 1)  # a new-style payload signed with the legacy key is not accepted
    forged = f"{payload}.{Sessions._sig(s.legacy_key, payload)}"
    assert s.read(forged) is None


def test_app_user_renamed(tmp_path, fake_ha):
    with make(tmp_path, fake_ha) as c:
        login(c)
        add(c, "mia")
    with make(tmp_path, fake_ha, user="aaron2") as c:
        assert login(c).status_code == 401
        assert login(c, "aaron2").status_code == 200
        assert [(u["username"], u["role"], u["owner"]) for u in c.get("/api/users").json()["users"]] == \
            [("aaron2", "admin", True), ("mia", "member", False)]


def test_app_user_takes_over_existing_account(tmp_path, fake_ha):
    with make(tmp_path, fake_ha) as c:
        login(c)
        add(c, "mia", role="guest")
        c.post("/api/logout")
        assert login(c, "mia", "password-1").status_code == 200
        mia = c.cookies[COOKIE]
    with make(tmp_path, fake_ha, user="mia", password="env-pass") as c:
        c.cookies.set(COOKIE, mia)
        assert c.get("/api/me").status_code == 401  # new password: signed out
        c.cookies.clear()
        assert login(c, "mia", "env-pass").status_code == 200
        assert c.get("/api/me").json()["role"] == "admin"


def test_no_env_login_keeps_db_users(tmp_path, fake_ha):
    with make(tmp_path, fake_ha) as c:
        login(c)
        add(c, "mia", role="admin")
    with make(tmp_path, fake_ha, user="", password="") as c:
        assert login(c, "aaron", "").status_code == 401
        assert login(c, "mia", "password-1").status_code == 200


# ---------- sessions end on removal / password change ----------
def test_removed_user_is_signed_out_everywhere(admin, tmp_path, fake_ha):
    add(admin, "mia")
    with make(tmp_path, fake_ha) as other:
        assert login(other, "mia", "password-1").status_code == 200
        basic = {"Authorization": "Basic " + base64.b64encode(b"mia:password-1").decode()}
        assert other.get("/api/devices").status_code == 200
        assert other.get("/api/me", headers=basic).status_code == 200  # cached Basic too
        assert admin.delete("/api/users/mia").json() == {"ok": True}
        assert other.get("/api/devices").status_code == 401
        other.cookies.clear()
        assert other.get("/api/me", headers=basic).status_code == 401
        assert login(other, "mia", "password-1").status_code == 401


def test_password_reset_ends_sessions(admin, tmp_path, fake_ha):
    add(admin, "mia")
    with make(tmp_path, fake_ha) as other:
        login(other, "mia", "password-1")
        assert admin.put("/api/users/mia/password", json={"password": "short"}).status_code == 400
        assert admin.put("/api/users/mia/password", json={"password": "brand-new-1"}).status_code == 200
        assert other.get("/api/me").status_code == 401
        assert login(other, "mia", "password-1").status_code == 401
        assert login(other, "mia", "brand-new-1").status_code == 200


def test_own_password_change(admin, tmp_path, fake_ha):
    add(admin, "mia")
    with make(tmp_path, fake_ha) as a, make(tmp_path, fake_ha) as b:
        login(a, "mia", "password-1")
        login(b, "mia", "password-1")
        assert a.post("/api/me/password", json={"current": "nope", "password": "brand-new-1"}).status_code == 400
        assert a.post("/api/me/password", json={"current": "password-1", "password": "x"}).status_code == 400
        r = a.post("/api/me/password", json={"current": "password-1", "password": "brand-new-1"})
        assert r.status_code == 200 and COOKIE in r.headers["set-cookie"]
        assert a.get("/api/me").json()["user"] == "mia"  # this session carries on
        assert b.get("/api/me").status_code == 401  # the other one ends
    # APP_USER's password lives in .env
    assert admin.post("/api/me/password", json={"current": "s3cret", "password": "brand-new-1"}).status_code == 400


def test_own_password_change_is_rate_limited(admin):
    add(admin, "mia")
    admin.post("/api/logout")
    login(admin, "mia", "password-1")
    for _ in range(10):
        assert admin.post("/api/me/password", json={"current": "bad", "password": "brand-new-1"}).status_code == 400
    assert admin.post("/api/me/password", json={"current": "password-1", "password": "brand-new-1"}).status_code == 429


def test_role_change_applies_at_once(admin, tmp_path, fake_ha):
    add(admin, "mia")
    with make(tmp_path, fake_ha) as other:
        login(other, "mia", "password-1")
        assert other.post("/api/devices/switch.fan/toggle").status_code == 200
        assert admin.patch("/api/users/mia", json={"role": "guest"}).json()["role"] == "guest"
        assert other.post("/api/devices/switch.fan/toggle").status_code == 403
        assert other.get("/api/me").json()["role"] == "guest"


# ---------- guests expire (injected clock) ----------
def test_guest_expiry(tmp_path, fake_ha):
    clock = Clock()
    with make(tmp_path, fake_ha, clock=clock) as c, make(tmp_path, fake_ha, clock=clock) as g:
        login(c)
        u = add(c, "gus", role="guest", expires=clock.t + 3600)
        assert u["expires"] == clock.t + 3600 and not u["expired"]
        assert login(g, "gus", "password-1").status_code == 200
        assert g.get("/api/me").json()["expires"] == clock.t + 3600
        clock.t += 3599
        assert g.get("/api/devices").status_code == 200
        clock.t += 1
        assert g.get("/api/devices").status_code == 401
        r = login(g, "gus", "password-1")
        assert r.status_code == 401 and r.json() == {"detail": "wrong username or password"}
        assert [x["expired"] for x in c.get("/api/users").json()["users"]] == [False, True]
        # The admin can extend it, and the guest can sign in again.
        assert c.patch("/api/users/gus", json={"expires": clock.t + 86400}).status_code == 200
        assert login(g, "gus", "password-1").status_code == 200
        assert c.patch("/api/users/gus", json={"expires": None}).json()["expires"] is None


def test_expiry_rules(tmp_path):
    clock = Clock()
    s = UserStore(str(tmp_path / "u.db"), clock)
    for role, exp in (("member", clock.t + 60), ("guest", clock.t), ("guest", clock.t - 1),
                      ("guest", clock.t + 367 * 86400), ("guest", "tomorrow"), ("guest", True)):
        with pytest.raises(UserError):
            s.create({"username": "g", "password": "password-1", "role": role, "expires": exp})
    s.create({"username": "g", "password": "password-1", "role": "guest", "expires": clock.t + 60})
    assert s.update("g", {"role": "member"})["expires"] is None  # promoted: no expiry


# ---------- no user enumeration ----------
def test_unknown_user_and_wrong_password_look_the_same(admin, monkeypatch):
    add(admin, "mia")
    admin.post("/api/logout")
    checks = []
    real = users_mod.verify_password
    monkeypatch.setattr(users_mod, "verify_password", lambda pw, enc: checks.append(enc.split("$")[1]) or real(pw, enc))
    answers = []
    for name, pw in (("mia", "wrong-pw"), ("nobody", "wrong-pw"), ("MIA", "wrong-pw"), ("x" * 500, "pw"), ("", "")):
        checks.clear()
        r = login(admin, name, pw)
        answers.append((r.status_code, r.text, r.headers.get("set-cookie")))
        assert len(checks) == 1, name  # one scrypt check either way: same work for unknown users
    assert len(set(answers)) == 1 and answers[0][0] == 401


def test_username_case_insensitive(admin):
    add(admin, "Mia")
    assert admin.post("/api/users", json={"username": "mia", "password": "password-1", "role": "guest"}).status_code == 400
    admin.post("/api/logout")
    assert login(admin, "mia", "password-1").json() == {"user": "Mia"}


# ---------- admin API guard rails ----------
def test_create_validation(admin):
    bad = [{"username": "", "password": "password-1", "role": "member"},
           {"username": "has space", "password": "password-1", "role": "member"},
           {"username": "-dash", "password": "password-1", "role": "member"},
           {"username": "x" * 33, "password": "password-1", "role": "member"},
           {"username": "ok", "password": "short", "role": "member"},
           {"username": "ok", "password": "x" * 257, "role": "member"},
           {"username": "ok", "password": "password-1", "role": "root"},
           {"username": "ok", "password": "password-1", "role": "member", "extra": 1},
           ["ok"]]
    for b in bad:
        assert admin.post("/api/users", json=b).status_code == 400, b
    assert admin.post("/api/users", json={"username": "aaron", "password": "password-1", "role": "member"}).status_code == 400


def test_writes_need_json(admin):
    """A cross-site form post (text/plain or urlencoded) can't create a user or change a password."""
    form = '{"username": "eve", "password": "password-1", "role": "admin"}'
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
        assert admin.post("/api/users", content=form, headers={"Content-Type": ctype}).status_code == 415
        assert admin.post("/api/me/password", content="{}", headers={"Content-Type": ctype}).status_code == 415
    assert [u["username"] for u in admin.get("/api/users").json()["users"]] == ["aaron"]


def test_owner_and_self_are_protected(admin):
    for r in (admin.delete("/api/users/aaron"), admin.patch("/api/users/aaron", json={"role": "member"}),
              admin.put("/api/users/aaron/password", json={"password": "password-2"})):
        assert r.status_code == 400
    add(admin, "ada", role="admin")
    admin.post("/api/logout")
    login(admin, "ada", "password-1")
    assert admin.delete("/api/users/ada").status_code == 400
    assert admin.patch("/api/users/ada", json={"role": "guest"}).status_code == 400
    assert admin.delete("/api/users/nobody").status_code == 404
    assert admin.patch("/api/users/nobody", json={"role": "guest"}).status_code == 404
    assert admin.put("/api/users/nobody/password", json={"password": "password-2"}).status_code == 404


def test_last_admin_stays(tmp_path):
    s = UserStore(str(tmp_path / "u.db"))
    s.create({"username": "a", "password": "password-1", "role": "admin"})
    s.create({"username": "b", "password": "password-1", "role": "member"})
    with pytest.raises(UserError):
        s.update("a", {"role": "member"})
    with pytest.raises(UserError):
        s.delete("a")
    s.create({"username": "c", "password": "password-1", "role": "admin"})
    s.update("a", {"role": "member"})
    s.delete("b")
    assert [u["username"] for u in s.all()] == ["a", "c"]


def test_user_changes_show_in_activity(admin):
    add(admin, "mia")
    admin.patch("/api/users/mia", json={"role": "guest"})
    admin.delete("/api/users/mia")
    texts = [e["text"] for d in [admin.get("/api/activity?type=security").json()] for e in d["entries"]]
    assert "aaron added user mia" in texts and "aaron removed user mia" in texts and "aaron changed user mia" in texts
