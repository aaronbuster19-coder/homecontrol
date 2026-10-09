import asyncio
import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.alerts import DEFAULT_SETTINGS, SettingsError, validate_settings
from backend.app import create_app
from backend.automations import AutoStore, Health
from backend.config import Settings
from backend.discovery import Device
from backend.ha import HAClient
from backend.live import Live
from backend.quiet import HeldStore, Quiet, in_window, next_local

LON = ZoneInfo("Europe/London")


def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LON).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Pusher:
    def __init__(self):
        self.sent = []

    async def notify(self, p):
        self.sent.append(p)
        return {"sent": 1}


class Rig:
    def __init__(self, tmp_path, now, **settings):
        self.clock, self.pusher = Clock(now), Pusher()
        self.settings = {**DEFAULT_SETTINGS, **settings}
        self.held = HeldStore(str(tmp_path / "q.db"))
        self.quiet = Quiet(self.held, lambda: self.settings, self.pusher, self.clock, LON)

    def push(self, title, category, tag=None, body="b"):
        asyncio.run(self.quiet.notify({"title": title, "body": body, "tag": tag or title, "url": "/"}, category))

    def tick(self, at=None):
        if at is not None:
            self.clock.t = at
        return asyncio.run(self.quiet.tick())


NIGHT = ts(2026, 10, 12, 2, 0)


def test_window_across_midnight_and_same_day():
    assert in_window(ts(2026, 10, 12, 23, 0), LON, "23:00", "07:00")
    assert in_window(ts(2026, 10, 12, 0, 30), LON, "23:00", "07:00")
    assert in_window(ts(2026, 10, 12, 6, 59), LON, "23:00", "07:00")
    assert not in_window(ts(2026, 10, 12, 7, 0), LON, "23:00", "07:00")
    assert not in_window(ts(2026, 10, 12, 22, 59), LON, "23:00", "07:00")
    assert in_window(ts(2026, 10, 12, 13, 0), LON, "12:00", "14:00") and not in_window(ts(2026, 10, 12, 14, 0), LON, "12:00", "14:00")
    assert not in_window(ts(2026, 10, 12, 3, 0), LON, "07:00", "07:00")   # same start and end: never
    # local wall clock on the DST days too
    assert in_window(ts(2026, 3, 29, 6, 30), LON, "23:00", "07:00") and not in_window(ts(2026, 3, 29, 7, 0), LON, "23:00", "07:00")
    assert next_local(ts(2026, 3, 29, 0, 30), LON, "07:00") == ts(2026, 3, 29, 7, 0)
    assert next_local(ts(2026, 10, 12, 8, 0), LON, "07:00") == ts(2026, 10, 13, 7, 0)


def test_door_and_test_always_go_through(tmp_path):
    r = Rig(tmp_path, NIGHT)
    r.push("Front door open", "door")
    r.push("homecontrol", "test")
    assert [p["title"] for p in r.pusher.sent] == ["Front door open", "homecontrol"]
    assert r.quiet.state()["held"] == 0


def test_held_then_one_digest_at_the_end(tmp_path):
    r = Rig(tmp_path, NIGHT)
    r.push("Fan is offline", "health", tag="health-fan", body="Unavailable for 30 min.")
    r.push("Bedroom window open — radiator off", "window", tag="window-w1")
    r.push("Fan is offline", "health", tag="health-fan", body="Unavailable for 31 min.")   # duplicate: kept once
    assert r.pusher.sent == [] and r.quiet.state()["held"] == 2 and r.quiet.state()["active"]
    assert r.tick(ts(2026, 10, 12, 6, 59)) is None and r.pusher.sent == []
    r.tick(ts(2026, 10, 12, 7, 0))
    assert len(r.pusher.sent) == 1
    d = r.pusher.sent[0]
    assert d["title"] == "While you were asleep" and d["tag"] == "digest"
    assert d["body"].splitlines() == ["Fan is offline — Unavailable for 31 min.", "Bedroom window open — radiator off — b"]
    r.tick(ts(2026, 10, 12, 7, 1))
    assert len(r.pusher.sent) == 1 and r.quiet.state()["held"] == 0   # once only
    r.push("Low battery: Valve", "health")   # daytime: straight through
    assert r.pusher.sent[-1]["title"] == "Low battery: Valve"


def test_held_items_survive_a_restart(tmp_path):
    r = Rig(tmp_path, NIGHT)
    r.push("Your week at home", "summary", tag="weekly-summary", body="12 kWh")
    r2 = Rig(tmp_path, ts(2026, 10, 12, 7, 5))
    r2.tick()
    assert len(r2.pusher.sent) == 1 and r2.pusher.sent[0]["body"] == "Your week at home"
    assert r2.pusher.sent[0]["url"] == "/?summary"


def test_quiet_off_and_custom_window(tmp_path):
    r = Rig(tmp_path, NIGHT, quiet_hours=False)
    r.push("Fan is offline", "health")
    assert len(r.pusher.sent) == 1
    r = Rig(tmp_path / "b", ts(2026, 10, 12, 13, 0), quiet_from="12:00", quiet_to="14:00")
    r.push("Fan is offline", "health")
    assert r.pusher.sent == [] and r.quiet.state()["until"] == ts(2026, 10, 12, 14, 0) * 1000
    r.settings["quiet_hours"] = False   # switched off while something is held: delivered on the next tick
    r.tick()
    assert len(r.pusher.sent) == 1


def test_mute_and_expiry(tmp_path):
    day = ts(2026, 10, 12, 15, 0)
    r = Rig(tmp_path, day)
    r.settings["mute_until"] = r.quiet.mute_until("1h")
    assert r.settings["mute_until"] == day + 3600
    r.push("Fan is offline", "health")
    r.push("Front door open", "door")
    assert [p["title"] for p in r.pusher.sent] == ["Front door open"]
    st = r.quiet.state()
    assert st["muted"] and not st["quiet"] and st["mute_until"] == (day + 3600) * 1000
    r.tick(day + 3599)
    assert len(r.pusher.sent) == 1
    r.tick(day + 3600)   # mute expired: digest
    assert len(r.pusher.sent) == 2 and r.pusher.sent[1]["title"] == "While you were asleep"
    assert not r.quiet.state()["active"]
    assert r.quiet.mute_until("morning") == ts(2026, 10, 13, 7, 0)
    assert r.quiet.mute_until("off") is None


def test_mute_extends_past_quiet_hours(tmp_path):
    r = Rig(tmp_path, ts(2026, 10, 12, 6, 30))
    r.settings["mute_until"] = ts(2026, 10, 12, 8, 0)
    assert r.quiet.state()["until"] == ts(2026, 10, 12, 8, 0) * 1000
    r.push("Fan is offline", "health")
    r.tick(ts(2026, 10, 12, 7, 30))
    assert r.pusher.sent == []
    r.tick(ts(2026, 10, 12, 8, 0))
    assert len(r.pusher.sent) == 1


def test_digest_caps_lines(tmp_path):
    r = Rig(tmp_path, NIGHT)
    for i in range(12):
        r.push(f"Device {i} is offline", "health")
    r.tick(ts(2026, 10, 12, 7, 0))
    lines = r.pusher.sent[0]["body"].splitlines()
    assert len(lines) == 8 and lines[-1] == "and 5 more"


def test_health_push_goes_through_quiet(tmp_path):
    """The automations' own pushes carry a category and are held at night."""
    r = Rig(tmp_path, NIGHT)
    store = AutoStore(str(tmp_path / "a.db"))
    h = Health(store, lambda: r.settings, lambda p: r.quiet.notify(p, "health"), r.clock)
    dev = {"climate.v": Device("climate.v", "valve", "Valve", "KE100", related={"battery": "sensor.v_battery"})}
    asyncio.run(h.tick(dev, {"climate.v": {"state": "heat"}, "sensor.v_battery": {"state": "5"}}))
    assert r.pusher.sent == [] and r.quiet.state()["held"] == 1
    r.tick(ts(2026, 10, 12, 7, 0))
    assert r.pusher.sent[0]["body"].startswith("Low battery: Valve")


def test_settings_validation():
    s = validate_settings({"quiet_hours": False, "quiet_from": "22:30", "quiet_to": "06:45"}, DEFAULT_SETTINGS)
    assert (s["quiet_hours"], s["quiet_from"], s["quiet_to"]) == (False, "22:30", "06:45")
    assert DEFAULT_SETTINGS["quiet_hours"] is True and DEFAULT_SETTINGS["quiet_from"] == "23:00"
    for bad in ({"quiet_from": "7:00"}, {"quiet_to": "25:00"}, {"quiet_hours": "on"}, {"mute_until": "soon"},
                {"mute_until": 1.0}, {"mute_until": 4e12}):
        with pytest.raises(SettingsError):
            validate_settings(bad, DEFAULT_SETTINGS)
    assert validate_settings({"mute_until": None}, DEFAULT_SETTINGS)["mute_until"] is None


@pytest.fixture
def night_client(tmp_path, fake_ha):
    clock, sent = Clock(NIGHT), []
    settings = Settings("http://ha.test", "test-token", "aaron", "s3cret", str(tmp_path / "layout.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(fake_ha.handler))
    live = Live(ha, "ws://ha.test/api/websocket", settings.ha_token, use_ws=False)

    def sender(sub, payload):
        sent.append(payload)
        return 201
    with TestClient(create_app(settings, ha, live, push_sender=sender, clock=clock)) as c:
        c.headers["Authorization"] = "Basic " + base64.b64encode(b"aaron:s3cret").decode()
        c.clock, c.sent = clock, sent
        c.post("/api/push/subscribe", json={"endpoint": "https://push.test/1", "keys": {"p256dh": "k", "auth": "a"}})
        yield c


def test_api_quiet_mute_and_test_bypass(night_client):
    c = night_client
    s = c.get("/api/alerts/settings").json()
    assert (s["quiet_hours"], s["quiet_from"], s["quiet_to"], s["mute_until"]) == (True, "23:00", "07:00", None)
    q = c.get("/api/alerts/quiet").json()
    assert q["active"] and q["quiet"] and q["until"] == ts(2026, 10, 12, 7, 0) * 1000
    assert c.post("/api/push/test").json()["sent"] == 1 and c.sent[-1]["tag"] == "test"   # test bypasses quiet hours
    auto = c.app.state.automations
    c.portal.call(auto._notify, {"title": "Fan is offline", "body": "x", "tag": "health-fan"}, "health")
    assert len(c.sent) == 1 and c.get("/api/alerts/quiet").json()["held"] == 1
    c.clock.t = ts(2026, 10, 12, 15, 0)
    r = c.post("/api/alerts/mute", json={"for": "morning"}).json()
    assert r["muted"] and r["mute_until"] == ts(2026, 10, 13, 7, 0) * 1000
    assert c.get("/api/alerts/settings").json()["mute_until"] == ts(2026, 10, 13, 7, 0)
    assert c.post("/api/alerts/mute", json={"for": "2h"}).status_code == 400
    r = c.post("/api/alerts/mute", json={"for": "off"}).json()
    assert not r["active"] and r["held"] == 1
    c.portal.call(auto.quiet.tick)
    assert c.sent[-1]["title"] == "While you were asleep" and c.get("/api/alerts/quiet").json()["held"] == 0
    r = c.put("/api/alerts/settings", json={"quiet_from": "22:00", "quiet_to": "06:30"})
    assert r.status_code == 200 and r.json()["quiet_from"] == "22:00" and r.json()["door_open_minutes"] == 5
    assert c.put("/api/alerts/settings", json={"quiet_to": "6:30"}).status_code == 400
