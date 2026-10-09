import asyncio
import base64
import os
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.alerts import (DEFAULT_SETTINGS, Alerts, AlertStore, Pusher, SettingsError, Watcher, load_vapid,
                            validate_settings, validate_subscription, webpush_sender)
from backend.app import create_app
from backend.config import Settings
from backend.discovery import parse_template_output
from backend.ha import HAClient
from backend.live import Live, sse
from backend.tests.conftest import TEMPLATE_OUTPUT

SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "BPkey", "auth": "authkey"}}
DOOR = "binary_sensor.contact_sensor_door"


def test_vapid_generated_once_and_persisted(tmp_path):
    db = str(tmp_path / "layout.db")
    a = load_vapid(db, env={})
    path = tmp_path / "vapid_private.pem"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    b = load_vapid(db, env={})
    assert a.public_key == b.public_key and a.private_pem == b.private_pem
    assert len(base64.urlsafe_b64decode(a.public_key + "=")) == 65  # uncompressed P-256 point
    assert a.subject == "mailto:admin@localhost"
    a.signer()  # loadable by the push library


def test_vapid_env_override(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    raw = base64.urlsafe_b64encode(key.private_numbers().private_value.to_bytes(32, "big")).rstrip(b"=").decode()
    v = load_vapid(str(tmp_path / "x.db"), env={"VAPID_PRIVATE_KEY": raw, "VAPID_SUBJECT": "mailto:me@x.test"})
    assert v.subject == "mailto:me@x.test"
    assert not (tmp_path / "vapid_private.pem").exists()
    v2 = load_vapid(str(tmp_path / "x.db"), env={"VAPID_PRIVATE_KEY": raw, "VAPID_PUBLIC_KEY": "PUB"})
    assert v2.public_key == "PUB" and v2.private_pem == v.private_pem


def test_store_subscriptions_dedupe(tmp_path):
    s = AlertStore(str(tmp_path / "layout.db"))
    s.add(SUB)
    s.add({**SUB, "keys": {"p256dh": "new", "auth": "a"}})
    s.add({**SUB, "endpoint": "https://push.example/def"})
    subs = s.subs()
    assert len(subs) == 2 and subs[0]["keys"]["p256dh"] == "new"
    assert s.remove(SUB["endpoint"]) and not s.remove(SUB["endpoint"])
    assert [x["endpoint"] for x in s.subs()] == ["https://push.example/def"]


def test_subscription_validation():
    assert validate_subscription({**SUB, "expirationTime": None}) == SUB
    for bad in (None, {}, {"endpoint": "ftp://x", "keys": SUB["keys"]}, {"endpoint": SUB["endpoint"]},
                {"endpoint": SUB["endpoint"], "keys": {"p256dh": "x"}}):
        with pytest.raises(SettingsError):
            validate_subscription(bad)


def test_settings_validation(tmp_path):
    s = AlertStore(str(tmp_path / "layout.db"))
    assert s.settings() == DEFAULT_SETTINGS
    new = validate_settings({"door_open_minutes": 10, "notify_on_close": True}, s.settings())
    assert new == {**DEFAULT_SETTINGS, "enabled": True, "door_open_minutes": 10, "notify_on_close": True}
    for bad in ([], {"door_open_minutes": 0}, {"door_open_minutes": 121}, {"door_open_minutes": 2.5},
                {"door_open_minutes": True}, {"enabled": "yes"}, {"notify_on_close": 1}):
        with pytest.raises(SettingsError):
            validate_settings(bad, DEFAULT_SETTINGS)
    s.put_settings(new)
    assert s.settings() == new


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def watcher(**settings):
    clock = Clock()
    return Watcher(lambda: {**DEFAULT_SETTINGS, **settings}, clock), clock


def test_watcher_timing():
    w, clock = watcher()
    w.observe(DOOR, "Front door", "on")
    clock.t += 4 * 60 + 59
    assert w.check() == []
    clock.t += 1
    [p] = w.check()
    assert p["body"] == "Front door has been open for 5 min" and p["tag"] == f"door-{DOOR}"
    clock.t += 600
    w.observe(DOOR, "Front door", "on")  # e.g. a battery update re-publishes the device
    assert w.check() == []  # one alert per opening
    w.observe(DOOR, "Front door", "off")
    assert w.check() == []  # notify_on_close off
    w.observe(DOOR, "Front door", "on")
    clock.t += 299
    assert w.check() == []
    clock.t += 1
    assert len(w.check()) == 1  # reopened: alerts again


def test_watcher_unavailable_and_disabled():
    w, clock = watcher()
    w.observe(DOOR, "Front door", "unavailable")
    clock.t += 3600
    assert w.check() == [] and w.open == {}
    w.observe(DOOR, "Front door", "on")
    w.observe(DOOR, "Front door", "unknown")  # dropout pauses, doesn't count as open
    clock.t += 3600
    assert w.check() == []
    w.observe(DOOR, "Front door", "on")
    assert len(w.check()) == 1
    off, clock2 = watcher(enabled=False)
    off.observe(DOOR, "Front door", "on")
    clock2.t += 3600
    assert off.check() == []


def test_watcher_notify_on_close_and_last_changed():
    w, clock = watcher(notify_on_close=True, door_open_minutes=1)
    w.observe(DOOR, "Front door", "on")
    w.observe(DOOR, "Front door", "off")
    assert w.check() == []  # closed before any alert: no "closed" message
    w.observe(DOOR, "Front door", "on")
    clock.t += 60
    assert len(w.check()) == 1
    w.observe(DOOR, "Front door", "off")
    [p] = w.check()
    assert p["title"] == "Front door closed"
    # startup with HA's last_changed: already open for 2 min
    w2, c2 = watcher(door_open_minutes=5)
    from datetime import datetime, timezone
    lc = datetime.fromtimestamp(c2.t - 4 * 60, timezone.utc).isoformat()
    w2.observe(DOOR, "Front door", "on", lc)
    c2.t += 60
    assert len(w2.check()) == 1


def test_pusher_prunes_gone(tmp_path):
    s = AlertStore(str(tmp_path / "layout.db"))
    for i, ep in enumerate(("ok", "gone", "missing", "err", "boom")):
        s.add({"endpoint": f"https://push.example/{ep}", "keys": {"p256dh": "k", "auth": "a"}})
    codes = {"ok": 201, "gone": 410, "missing": 404, "err": 500}

    def send(sub, payload):
        ep = sub["endpoint"].rsplit("/", 1)[1]
        if ep == "boom":
            raise OSError("network")
        return codes[ep]
    res = asyncio.run(Pusher(s, send).notify({"title": "t"}))
    assert res == {"sent": 1, "pruned": 2, "failed": 2}
    assert sorted(x["endpoint"].rsplit("/", 1)[1] for x in s.subs()) == ["boom", "err", "ok"]


def test_webpush_sender_maps_status(tmp_path, monkeypatch):
    import pywebpush
    vapid = load_vapid(str(tmp_path / "x.db"), env={})

    class Resp:
        status_code = 410
    def fake(sub, data, **kw):
        assert kw["vapid_claims"]["sub"] == "mailto:admin@localhost"
        raise pywebpush.WebPushException("gone", response=Resp())
    monkeypatch.setattr(pywebpush, "webpush", fake)
    assert webpush_sender(vapid)(SUB, {"title": "x"}) == 410


def test_live_observer_feeds_alerts(tmp_path):
    live = Live(None, "ws://x", "t", use_ws=False)
    live.set_devices({d.entity_id: d for d in parse_template_output(TEMPLATE_OUTPUT)})
    clock = Clock()
    sent = []
    store = AlertStore(str(tmp_path / "l.db"))
    store.put_settings({**DEFAULT_SETTINGS, "door_open_minutes": 1, "notify_on_close": True})
    store.add(SUB)
    alerts = Alerts(store, Pusher(store, lambda s, p: sent.append(p) or 201), live, clock=clock)
    live.load_states([{"entity_id": DOOR, "state": "on", "attributes": {}}])
    assert DOOR in alerts.watcher.open
    clock.t += 61
    asyncio.run(alerts.tick())
    assert [p["title"] for p in sent] == ["Front door open"]
    live.handle({"type": "event", "event": {"event_type": "state_changed", "data": {
        "entity_id": DOOR, "new_state": {"entity_id": DOOR, "state": "off", "attributes": {}}}}})
    assert alerts.wake.is_set()
    asyncio.run(alerts.tick())
    assert [p["title"] for p in sent] == ["Front door open", "Front door closed"]


def test_status_event():
    live = Live(None, "ws://x", "t", use_ws=False)
    c = live.subscribe()

    async def first_two():
        gen = live.stream(c, [])
        return [await gen.__anext__(), await gen.__anext__()]
    assert asyncio.run(first_two())[1] == sse("status", {"ws": False})
    c2 = live.subscribe()
    live.set_ws(True)
    assert c2.queue.get_nowait() == sse("status", {"ws": True})
    live.set_ws(True)
    assert c2.queue.empty()  # only on change


def test_push_api(tmp_path, fake_ha):
    sent = []
    settings = Settings("http://ha.test", "test-token", "aaron", "s3cret", str(tmp_path / "layout.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(fake_ha.handler))
    live = Live(ha, "ws://ha.test/api/websocket", settings.ha_token, use_ws=False)
    with TestClient(create_app(settings, ha, live, push_sender=lambda s, p: sent.append(p) or 201)) as c:
        assert c.get("/api/push/key").status_code == 401
        c.headers["Authorization"] = "Basic " + base64.b64encode(b"aaron:s3cret").decode()
        assert len(c.get("/api/push/key").json()["publicKey"]) == 87
        assert c.post("/api/push/subscribe", json={"endpoint": "x"}).status_code == 400
        assert c.post("/api/push/subscribe", json=SUB).json() == {"ok": True}
        assert c.post("/api/push/test").json() == {"sent": 1, "pruned": 0, "failed": 0}
        assert sent[-1]["tag"] == "test"
        assert c.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]}).json() == {"removed": True}
        assert c.get("/api/alerts/settings").json() == DEFAULT_SETTINGS
        assert c.put("/api/alerts/settings", json={"door_open_minutes": 500}).status_code == 400
        r = c.put("/api/alerts/settings", json={"door_open_minutes": 2, "enabled": False})
        assert r.json() == {**DEFAULT_SETTINGS, "enabled": False, "door_open_minutes": 2, "notify_on_close": False}
        assert c.get("/api/alerts/settings").json()["door_open_minutes"] == 2
