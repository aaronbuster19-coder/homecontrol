"""Guest links (backend/guest_links.py): admin-made, time-limited links / QR codes for one room's lights or chosen
devices. Tokens are unguessable, stored hashed, rate-limited; a link session reaches its own devices and nothing else,
on every route and on the live stream."""
import asyncio
import base64
import hashlib
import json
import re
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import guest_links as gl
from backend import roles
from backend.app import create_app
from backend.config import Settings
from backend.ha import HAClient
from backend.live import Live, sse
from backend.roles import LINK, POLICY

LAYOUT = {
    "unit": "m",
    "rooms": [{"id": "kitchen", "name": "Kitchen", "x": 0, "y": 0, "w": 4, "h": 3},
              {"id": "lounge", "name": "Lounge", "x": 4, "y": 0, "w": 4, "h": 3},
              {"id": "hall", "name": "Hall", "x": 0, "y": 3, "w": 2, "h": 2}],
    "placements": [{"entity_id": "light.kitchen_1", "x": 1, "y": 1},
                   {"entity_id": "switch.kettle", "x": 2, "y": 1},
                   {"entity_id": "light.strip", "x": 5, "y": 1},
                   {"entity_id": "switch.fan", "x": 6, "y": 1},
                   {"entity_id": "climate.lounge_valve", "x": 7, "y": 2}],
}
# Names of devices that a kitchen link must never reveal.
OTHER_NAMES = ("Strip", "Fan", "Kettle", "Lounge valve", "Front door", "light.strip", "switch.fan", "switch.kettle",
               "climate.lounge_valve", "binary_sensor.contact_sensor_door")


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


def make(tmp_path, fake_ha, clock, live_cls=Live):
    settings = Settings("http://ha.test", "test-token", "aaron", "s3cret", str(tmp_path / "layout.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(fake_ha.handler))
    live = live_cls(ha, "ws://ha.test/api/websocket", settings.ha_token, use_ws=False)
    return TestClient(create_app(settings, ha, live, clock=clock)), settings


ADMIN_AUTH = {"Authorization": "Basic " + base64.b64encode(b"aaron:s3cret").decode()}


@pytest.fixture
def app(tmp_path, fake_ha, clock):
    """(admin client, anonymous client) on one app, layout with rooms stored."""
    c, settings = make(tmp_path, fake_ha, clock)
    with c:
        c.headers.update(ADMIN_AUTH)
        assert c.put("/api/layout", json=LAYOUT).status_code == 200
        anon = TestClient(c.app)
        anon.db = settings.db_path
        yield c, anon


def create(c, **body):
    body = {"label": "Sam", "room": "kitchen", "minutes": 120, **body}
    if "devices" in body:
        body.pop("room", None)
    r = c.post("/api/guest/links", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def redeem(anon, token):
    r = anon.post("/api/guest/redeem", json={"token": token})
    assert r.status_code == 200, r.text
    return r


def guest_headers(token):
    return {"Cookie": f"{gl.COOKIE}={token}"}


def ha_calls(fake_ha):
    return [(p, b) for p, b in fake_ha.service_calls()]


# ---------- creating, listing, revoking ----------
def test_create_room_link_token_shown_once_and_hashed_at_rest(app):
    c, anon = app
    link = create(c)
    token = link["token"]
    assert len(token) >= 43 and re.fullmatch(r"[A-Za-z0-9_-]+", token)  # 32 random bytes, URL-safe
    assert link["path"] == f"/guest.html#{token}"  # in the fragment: never sent to a server or in a Referer
    assert link["scope"] == {"room": "kitchen"} and link["room_name"] == "Kitchen"
    assert [d["entity_id"] for d in link["devices"]] == ["light.kitchen_1"]  # the kettle plug is not a light
    assert link["expires"] - link["created"] == 120 * 60 and link["active"] and link["created_by"] == "aaron"
    # At rest: only the SHA-256, nowhere the token itself.
    with sqlite3.connect(anon.db) as db:
        rows = db.execute("SELECT * FROM guest_links").fetchall()
        dump = "\n".join(db.iterdump())
    assert token not in dump
    assert hashlib.sha256(token.encode()).hexdigest() in dump and len(rows) == 1
    # The listing never carries the token or its hash.
    listed = c.get("/api/guest/links").json()["links"]
    assert [x["id"] for x in listed] == [link["id"]]
    text = json.dumps(listed)
    assert token not in text and hashlib.sha256(token.encode()).hexdigest() not in text and "hash" not in listed[0]


def test_two_links_get_different_tokens(app):
    c, _ = app
    a, b = create(c), create(c, label="Jo")
    assert a["token"] != b["token"] and a["id"] != b["id"]


@pytest.mark.parametrize("body,msg", [
    ({"room": "nowhere"}, "unknown room"),
    ({"room": "hall"}, "no lights"),
    ({"devices": ["climate.lounge_valve"]}, "not a light or plug"),
    ({"devices": ["light.nope"]}, "not a light or plug"),
    ({"devices": []}, "devices"),
    ({"devices": "light.strip"}, "devices"),
    ({"minutes": 5}, "minutes"),
    ({"minutes": 31 * 24 * 60}, "minutes"),
    ({"minutes": True}, "minutes"),
    ({"label": ""}, "label"),
    ({"label": "x" * 41}, "label"),
    ({"room": "kitchen", "devices": ["light.strip"]}, "either"),
    ({"extra": 1}, "send"),
])
def test_create_validation(app, body, msg):
    c, _ = app
    data = {"label": "Sam", "minutes": 60, **({"room": "kitchen"} if "room" not in body and "devices" not in body else {}), **body}
    r = c.post("/api/guest/links", json=data)
    assert r.status_code == 400 and msg in r.json()["detail"], r.text


def test_protected_plugs_can_never_be_in_a_link(app):
    c, _ = app
    layout = c.get("/api/layout").json()
    layout["settings"] = {**(layout.get("settings") or {}), "keep_on": ["switch.kettle"]}
    assert c.put("/api/layout", json=layout).status_code == 200
    r = c.post("/api/guest/links", json={"label": "S", "devices": ["switch.kettle"], "minutes": 60})
    assert r.status_code == 400 and "kept on" in r.json()["detail"]
    # A fridge / home server linked to a plug: same.
    layout["furniture"] = [{"id": "f1", "type": "fridge", "x": 2, "y": 2, "w": 0.6, "h": 0.6, "rot": 0, "plug": "switch.fan"}]
    assert c.put("/api/layout", json=layout).status_code == 200, c.put("/api/layout", json=layout).text
    r = c.post("/api/guest/links", json={"label": "S", "devices": ["switch.fan"], "minutes": 60})
    assert r.status_code == 400 and "kept on" in r.json()["detail"]


def test_create_needs_json(app):
    c, _ = app
    r = c.post("/api/guest/links", content="label=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_at_most_20_live_links(app):
    c, _ = app
    for i in range(gl.MAX_ACTIVE):
        create(c, label=f"n{i}")
    r = c.post("/api/guest/links", json={"label": "one more", "room": "kitchen", "minutes": 60})
    assert r.status_code == 400 and "at most" in r.json()["detail"]
    c.delete(f"/api/guest/links/{c.get('/api/guest/links').json()['links'][0]['id']}")
    create(c, label="room again")


def test_members_and_guests_cannot_manage_links(app):
    c, anon = app
    for name, role in (("mia", "member"), ("gus", "guest")):
        assert c.post("/api/users", json={"username": name, "password": "password-1", "role": role}).status_code == 200
    link = create(c)
    for name in ("mia", "gus"):
        h = {"Authorization": "Basic " + base64.b64encode(f"{name}:password-1".encode()).decode()}
        assert anon.get("/api/guest/links", headers=h).status_code == 403
        assert anon.post("/api/guest/links", headers=h, json={"label": "x", "room": "kitchen", "minutes": 60}).status_code == 403
        assert anon.delete(f"/api/guest/links/{link['id']}", headers=h).status_code == 403
    assert anon.get("/api/guest/links").status_code == 401
    assert c.get("/api/guest/links").json()["links"][0]["active"]


# ---------- redeeming ----------
def test_redeem_sets_a_narrow_cookie(app, clock):
    c, anon = app
    link = create(c)
    r = redeem(anon, link["token"])
    assert r.json() == {"label": "Sam", "expires": link["expires"]}
    sc = r.headers["set-cookie"].lower()
    assert f"{gl.COOKIE}={link['token']}".lower() in sc
    for part in ("httponly", "path=/api/guest", "samesite=strict", f"max-age={120 * 60}"):
        assert part in sc, sc
    assert r.headers["cache-control"] == "no-store"
    assert c.get("/api/guest/links").json()["links"][0]["uses"] == 1


@pytest.mark.parametrize("token", ["", "short", "x" * 43, "y" * 200])
def test_redeem_wrong_token(app, token):
    _, anon = app
    r = anon.post("/api/guest/redeem", json={"token": token})
    assert r.status_code == 401 and "set-cookie" not in r.headers


def test_redeem_bad_body(app):
    _, anon = app
    assert anon.post("/api/guest/redeem", json=["x"]).status_code == 400
    assert anon.post("/api/guest/redeem", content="nope").status_code == 400


def test_redeem_is_rate_limited(app):
    c, anon = app
    link = create(c)
    for _ in range(10):
        assert anon.post("/api/guest/redeem", json={"token": "z" * 43}).status_code == 401
    assert anon.post("/api/guest/redeem", json={"token": link["token"]}).status_code == 429  # even the right one now
    # A guessed session cookie counts too, and is blocked the same way.
    assert anon.get(gl.SESSION, headers=guest_headers(link["token"])).status_code == 429


def test_guessed_session_cookies_are_rate_limited(app):
    c, anon = app
    link = create(c)
    for _ in range(10):
        assert anon.get(gl.SESSION, headers=guest_headers("g" * 43)).status_code == 401
    assert anon.get(gl.SESSION, headers=guest_headers(link["token"])).status_code == 429


# ---------- a guest's session ----------
def test_session_shows_only_its_devices(app):
    c, anon = app
    link = create(c)
    r = anon.get(gl.SESSION, headers=guest_headers(link["token"]))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    s = r.json()
    assert s["label"] == "Sam" and s["room"] == "Kitchen" and s["expires"] == link["expires"]
    assert [d["entity_id"] for d in s["devices"]] == ["light.kitchen_1"]
    d = s["devices"][0]
    assert d["state"] == "on" and d["brightness"] == 200 and d["kind"] == "light"
    assert set(d) <= {"entity_id", "kind", "name", "state", *gl.LIGHT_FIELDS}
    for other in OTHER_NAMES:
        assert other not in r.text, other


def test_session_follows_the_layout(app):
    """Moving a light out of the room takes it out of the link at once; deleting the room empties it."""
    c, anon = app
    link = create(c)
    h = guest_headers(link["token"])
    layout = c.get("/api/layout").json()
    layout["placements"] = [{**p, "x": 1.5} if p["entity_id"] == "light.strip" else p for p in layout["placements"]]
    assert c.put("/api/layout", json=layout).status_code == 200
    assert [d["entity_id"] for d in anon.get(gl.SESSION, headers=h).json()["devices"]] == ["light.kitchen_1", "light.strip"]
    layout["rooms"] = [r for r in layout["rooms"] if r["id"] != "kitchen"]
    assert c.put("/api/layout", json=layout).status_code == 200
    s = anon.get(gl.SESSION, headers=h).json()
    assert s["devices"] == [] and s["room"] is None
    assert anon.post(f"{gl.SESSION}/devices/light.kitchen_1/toggle", headers=h).status_code == 404


def test_hidden_lights_are_left_out(app):
    c, anon = app
    link = create(c)
    assert c.put("/api/devices/light.kitchen_1/meta", json={"hidden": True}).status_code == 200
    assert anon.get(gl.SESSION, headers=guest_headers(link["token"])).json()["devices"] == []


def test_toggle_and_dim_in_scope_only(app, fake_ha):
    c, anon = app
    link = create(c)
    h = guest_headers(link["token"])
    assert anon.post(f"{gl.SESSION}/devices/light.kitchen_1/toggle", headers=h).json() == {"ok": True}
    assert anon.post(f"{gl.SESSION}/devices/light.kitchen_1/light", headers=h, json={"brightness_pct": 30}).json() == {"ok": True}
    assert ha_calls(fake_ha) == [("/api/services/light/toggle", {"entity_id": "light.kitchen_1"}),
                                 ("/api/services/light/turn_on", {"entity_id": "light.kitchen_1", "brightness_pct": 30})]
    # Out of scope or unknown: the same 404, nothing reaches HA.
    for eid in ("light.strip", "switch.kettle", "switch.fan", "climate.lounge_valve", "light.nope"):
        for action, body in (("toggle", None), ("light", {"brightness_pct": 10})):
            r = anon.post(f"{gl.SESSION}/devices/{eid}/{action}", headers=h, json=body)
            assert r.status_code == 404 and r.json()["detail"] == "not part of this guest link", (eid, action)
    bad = anon.post(f"{gl.SESSION}/devices/light.kitchen_1/light", headers=h, json={"brightness_pct": 500})
    assert bad.status_code == 400
    assert anon.post(f"{gl.SESSION}/devices/light.kitchen_1/light", headers=h, json={"evil": 1}).status_code == 400
    assert len(ha_calls(fake_ha)) == 2


def test_chosen_devices_link_with_a_plug(app, fake_ha):
    c, anon = app
    link = create(c, devices=["switch.fan", "light.strip"])
    assert link["scope"] == {"devices": ["light.strip", "switch.fan"]}
    h = guest_headers(link["token"])
    s = anon.get(gl.SESSION, headers=h).json()
    assert s["room"] is None and [d["entity_id"] for d in s["devices"]] == ["light.strip", "switch.fan"]
    fan = next(d for d in s["devices"] if d["kind"] == "plug")
    assert set(fan) == {"entity_id", "kind", "name", "state"}  # no power, energy, model …
    assert anon.post(f"{gl.SESSION}/devices/switch.fan/toggle", headers=h).status_code == 200
    assert anon.post(f"{gl.SESSION}/devices/switch.fan/light", headers=h, json={"brightness_pct": 5}).status_code == 400
    assert anon.post(f"{gl.SESSION}/devices/light.kitchen_1/toggle", headers=h).status_code == 404
    # The plug becomes "Keep on" later: it drops out of the link (never switched off by a guest).
    layout = c.get("/api/layout").json()
    layout["settings"] = {**(layout.get("settings") or {}), "keep_on": ["switch.fan"]}
    assert c.put("/api/layout", json=layout).status_code == 200
    assert anon.post(f"{gl.SESSION}/devices/switch.fan/toggle", headers=h).status_code == 404
    assert [d["entity_id"] for d in anon.get(gl.SESSION, headers=h).json()["devices"]] == ["light.strip"]
    assert [p for p, _ in ha_calls(fake_ha)] == ["/api/services/switch/toggle"]


def test_revoke_ends_it_at_once(app, fake_ha):
    c, anon = app
    link = create(c)
    h = guest_headers(link["token"])
    assert anon.get(gl.SESSION, headers=h).status_code == 200
    r = c.delete(f"/api/guest/links/{link['id']}")
    assert r.status_code == 200 and r.json()["revoked"] and not r.json()["active"]
    for call in (lambda: anon.get(gl.SESSION, headers=h),
                 lambda: anon.post(f"{gl.SESSION}/devices/light.kitchen_1/toggle", headers=h),
                 lambda: anon.get(gl.SESSION + "/events", headers=h),
                 lambda: anon.post("/api/guest/redeem", json={"token": link["token"]})):
        r = call()
        assert r.status_code == 401 and r.json()["detail"] == gl.GONE
    assert ha_calls(fake_ha) == []
    assert c.delete("/api/guest/links/nope-nope-nope").status_code == 404
    assert c.delete("/api/guest/links/..").status_code in (404, 405)
    listed = c.get("/api/guest/links").json()["links"][0]
    assert listed["revoked"] and not listed["active"]


def test_expires_automatically(app, clock, fake_ha):
    c, anon = app
    link = create(c, minutes=15)
    h = guest_headers(link["token"])
    clock.t += 14 * 60
    assert anon.post(f"{gl.SESSION}/devices/light.kitchen_1/toggle", headers=h).status_code == 200
    clock.t += 61
    assert anon.post(f"{gl.SESSION}/devices/light.kitchen_1/toggle", headers=h).status_code == 401
    assert anon.post("/api/guest/redeem", json={"token": link["token"]}).status_code == 401
    assert len(ha_calls(fake_ha)) == 1
    assert not c.get("/api/guest/links").json()["links"][0]["active"]
    clock.t += gl.KEEP_ENDED + 60  # a week after it ended it's gone from the list
    assert c.get("/api/guest/links").json()["links"] == []


# ---------- nothing else is reachable ----------
def sample(path, entity="light.kitchen_1"):
    return re.sub(r"\{(\w+)\}", lambda m: {"entity_id": entity, "username": "aaron"}.get(m.group(1), "x"), path)


def test_a_link_cookie_reaches_no_other_route(app, fake_ha):
    """Every route in the table, every method, with nothing but a live guest cookie: only the LINK routes answer; the
    public ones stay public; everything else is 401 (no account) and never calls HA."""
    c, anon = app
    link = create(c)
    h = guest_headers(link["token"])
    public = {("POST", "/api/login"), ("GET", "/healthz"), ("GET", "/sw.js"), ("GET", "/manifest.webmanifest"),
              ("POST", "/api/guest/redeem"), ("*", "")}
    for (method, path), level in sorted(POLICY.items()):
        if (method, path) in public or path == gl.SESSION + "/events":
            continue
        before = len(ha_calls(fake_ha))
        kw = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
        r = anon.request(method, sample(path), headers=h, **kw)
        if level == LINK:
            assert r.status_code not in (401, 403), (method, path, r.status_code)
        else:
            assert r.status_code == 401, (method, path, r.status_code)
            assert len(ha_calls(fake_ha)) == before, (method, path)
    # The app shell and every API answer for the plan: refused too.
    for p in ("/", "/index.html", "/app.js", "/api/devices", "/api/layout", "/api/events", "/api/me"):
        assert anon.get(p, headers=h, follow_redirects=False).status_code in (303, 401), p


def test_link_role_is_refused_everywhere_but_link_routes(app):
    """Belt and braces: even if a link session's state reached another route, RoleMiddleware refuses it."""
    for (method, path), level in POLICY.items():
        ok = roles.allowed(LINK, level, {"entity_id": "light.kitchen_1"})
        assert ok == (level == LINK), (method, path)


def test_session_paths_cannot_escape(app, fake_ha):
    c, anon = app
    link = create(c)
    h = guest_headers(link["token"])
    for p in (gl.SESSION + "/../devices", gl.SESSION + "/%2e%2e/layout", gl.SESSION + "/nope", gl.SESSION + "/devices/light.kitchen_1/turn_off"):
        r = anon.post(p, headers=h)
        assert r.status_code in (401, 403, 404, 405), (p, r.status_code)  # the client may normalise ".."
    r = anon.get(gl.SESSION + "/devices", headers=h)
    assert r.status_code in (403, 404, 405)
    assert ha_calls(fake_ha) == []


def test_accounts_get_403_on_link_routes(app):
    c, _ = app
    for method, path in (("GET", gl.SESSION), ("POST", gl.SESSION + "/devices/light.kitchen_1/toggle")):
        r = c.request(method, path)
        assert r.status_code == 403 and r.json()["detail"] == roles.LINK_ONLY


def test_an_admin_testing_their_own_link_gets_the_guest_view(app):
    """Same browser, both cookies: the guest cookie wins on the link routes, the account everywhere else."""
    c, _ = app
    link = create(c)
    r = c.get(gl.SESSION, headers=guest_headers(link["token"]))
    assert r.status_code == 200 and [d["entity_id"] for d in r.json()["devices"]] == ["light.kitchen_1"]
    assert c.get("/api/devices", headers=guest_headers(link["token"])).status_code == 200


def test_activity_shows_links(app, clock):
    c, anon = app
    link = create(c)
    redeem(anon, link["token"])
    anon.post(f"{gl.SESSION}/devices/light.kitchen_1/toggle", headers=guest_headers(link["token"]))
    c.delete(f"/api/guest/links/{link['id']}")
    clock.t += 1  # the timeline runs up to now
    r = c.get("/api/activity", params={"type": "security"})
    assert r.status_code == 200, r.text
    texts = [i["text"] for i in r.json()["entries"]]
    assert any("made guest link “Sam”" in t for t in texts), texts
    assert any("Guest link “Sam” opened" in t for t in texts), texts
    assert any("revoked guest link “Sam”" in t for t in texts), texts


# ---------- the live stream ----------
class ScriptedLive(Live):
    """The stream sees: a status, its own light changing, a light and a plug it must not see, a disco, the end."""

    def start(self):
        pass

    def subscribe(self):
        c = super().subscribe()
        for msg in (sse("status", {"ws": True}),
                    sse("device", {"entity_id": "light.strip", "name": "Strip", "kind": "light", "state": "on"}),
                    sse("device", {"entity_id": "light.kitchen_1", "name": "Kitchen 1", "kind": "light", "state": "off",
                                   "brightness": None, "power": 3, "model": "L530"}),
                    sse("device", {"entity_id": "switch.fan", "name": "Fan", "kind": "plug", "state": "on", "power": 40}),
                    sse("appliances", {"f1": {"plug": "switch.fan", "status": "Running"}}),
                    sse("disco", {"running": True, "ids": ["light.strip"]})):
            c.queue.put_nowait(msg)
        c.queue.put_nowait(None)
        return c


def test_events_stream_only_its_devices(tmp_path, fake_ha, clock):
    c, _ = make(tmp_path, fake_ha, clock, ScriptedLive)
    with c:
        c.headers.update(ADMIN_AUTH)
        assert c.put("/api/layout", json=LAYOUT).status_code == 200
        link = create(c)
        anon = TestClient(c.app)
        r = anon.get(gl.SESSION + "/events", headers=guest_headers(link["token"]))
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        blocks = r.text.strip().split("\n\n")
        assert blocks[0].startswith("event: snapshot\n")
        assert json.loads(blocks[0].split("data: ", 1)[1])["devices"][0]["entity_id"] == "light.kitchen_1"
        assert blocks[1:] == ['event: device\ndata: {"entity_id":"light.kitchen_1","kind":"light","name":"Kitchen 1",'
                              '"state":"off","brightness":null}']
        for other in OTHER_NAMES + ("power", "L530", "Running", "disco", '"ws"'):
            assert other not in r.text, other
        # an account gets no stream
        assert c.get(gl.SESSION + "/events").status_code == 403


def test_stream_ends_when_the_link_stops(tmp_path, clock):
    """Revoked or expired while open: an `end` event and the stream closes (the page shows "expired")."""
    async def go():
        store = gl.LinkStore(str(tmp_path / "g.db"), clock)
        link, _ = store.create("Sam", {"devices": ["light.a"]}, 15, "aaron")

        class L:
            states, clients = {}, set()

            def unsubscribe(self, c):
                L.clients.discard(c)

        async def devices():
            return {}

        async def ensure_states():
            pass
        links = gl.GuestLinks(store, devices, lambda: {}, L(), ensure_states, clock)
        client = type("C", (), {"queue": asyncio.Queue()})()
        gen = links.stream(link["id"], client, {"light.a"}, {"devices": []})
        assert (await gen.__anext__()).startswith("event: snapshot")
        client.queue.put_nowait(sse("device", {"entity_id": "light.a", "kind": "light", "name": "A", "state": "on"}))
        assert (await gen.__anext__()).startswith("event: device")
        store.revoke(link["id"])
        client.queue.put_nowait(sse("device", {"entity_id": "light.a", "kind": "light", "name": "A", "state": "off"}))
        assert await gen.__anext__() == sse("end", {"detail": gl.GONE})
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
        # the scope changed (layout edit): a fresh snapshot goes out at the next check
        link3, _ = store.create("Al", {"devices": ["light.a"]}, 60, "aaron")
        client3 = type("C", (), {"queue": asyncio.Queue()})()
        every, gl.CHECK_EVERY = gl.CHECK_EVERY, 0  # check at once
        try:
            gen3 = links.stream(link3["id"], client3, {"light.gone"}, {"devices": []})
            await gen3.__anext__()
            nxt = await asyncio.wait_for(gen3.__anext__(), 2)
        finally:
            gl.CHECK_EVERY = every
        assert nxt.startswith("event: snapshot") and '"label":"Al"' in nxt
        await gen3.aclose()
        # expired with nothing to send: ends without waiting for a message
        link2, _ = store.create("Jo", {"devices": ["light.a"]}, 15, "aaron")
        client2 = type("C", (), {"queue": asyncio.Queue()})()
        gen2 = links.stream(link2["id"], client2, {"light.a"}, {"devices": []})
        await gen2.__anext__()
        clock.t = link2["expires"] + 1
        assert await asyncio.wait_for(gen2.__anext__(), 2) == sse("end", {"detail": gl.GONE})
    asyncio.run(go())
