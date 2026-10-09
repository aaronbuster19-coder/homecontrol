"""Smart preheat (warm-up learning, early start) and damp warnings: backend/climate.py, driven with an injected clock."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.automations import AutoStore
from backend.climate import (DEFAULT_RATE, DEFAULTS, MARGIN, Climate, ClimateError, WarmUp, blend, history_series,
                             lead_seconds, learn_from, validate_room, validate_settings)
from backend.config import Settings
from backend.discovery import Device
from backend.ha import HAClient
from backend.live import Live
from backend.schedules import ScheduleEngine, ScheduleStore
from backend.tests.conftest import FakeHA

LON = ZoneInfo("Europe/London")
H = 3600


def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LON).timestamp()


BED = {"id": "bed", "name": "Bedroom", "x": 0, "y": 0, "w": 4, "h": 4}
LOUNGE = {"id": "lounge", "name": "Lounge", "x": 4, "y": 0, "w": 4, "h": 4}
LAYOUT = {"unit": "m", "rooms": [BED, LOUNGE],
          "placements": [{"entity_id": "climate.bed", "x": 1, "y": 1}, {"entity_id": "climate.lounge", "x": 5, "y": 1},
                         {"entity_id": "humidifier.dehum", "x": 2, "y": 2}],
          "openings": [{"id": "w1", "type": "window", "x": 1, "y": 0, "len": 1, "orient": "h",
                        "entity_id": "binary_sensor.bed_window"}]}
DEVICES = {"climate.bed": Device("climate.bed", "valve", "Bedroom radiator", "KE100"),
           "climate.lounge": Device("climate.lounge", "valve", "Lounge radiator", "KE100"),
           "humidifier.dehum": Device("humidifier.dehum", "dehumidifier", "Dehumidifier", "CS-20L")}


def valve(target, cur, state="heat"):
    return {"state": state, "attributes": {"temperature": target, "current_temperature": cur}}


def base_states():
    return {"climate.bed": valve(17, 16.0), "climate.lounge": valve(20, 19.5),
            "humidifier.dehum": {"state": "off", "attributes": {"current_humidity": 60, "humidity": 50}},
            "binary_sensor.bed_window": {"state": "off", "attributes": {}}}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Rig:
    """Climate with real stores (schedules, AutoStore) and fakes for HA, Live, mode and window heating."""

    def __init__(self, tmp_path, now, held=None, mode="home"):
        self.clock = Clock(now)
        self.calls, self.pushes, self.held, self.mode = [], [], held or {}, {"mode": mode}
        self.live = SimpleNamespace(devices=dict(DEVICES), states=base_states())
        db = str(tmp_path / "t.db")
        self.store = AutoStore(db)
        self.schedules = ScheduleEngine(ScheduleStore(db), None, lambda: LAYOUT, lambda: self.mode, None, self.clock, LON)
        window = SimpleNamespace(held=lambda: self.held)

        async def call(domain, service, data):
            self.calls.append((domain, service, data))

        async def notify(p):
            self.pushes.append(p)
        self.c = Climate(self.store, self.live, call, lambda: LAYOUT, lambda: self.mode, self.schedules, window, notify,
                         asyncio.Lock(), None, self.clock, LON)

    def schedule(self, at="07:00", value=21, ids=("climate.bed",), enabled=True):
        return self.schedules.create({"name": "Morning heat", "enabled": enabled, "days": list(range(7)),
                                      "time": {"type": "fixed", "at": at}, "action": {"type": "temperature", "value": value},
                                      "target": {"entity_ids": list(ids)}})

    def enable(self, room="bed", **settings):
        self.c.put_settings({"preheat_enabled": True, **settings})
        self.c.put_room({"bed": BED, "lounge": LOUNGE}[room], {"preheat": True})

    def run(self, coro):
        return asyncio.run(coro)

    def tick(self):
        self.run(self.c.tick())


# ---------------- validation ----------------
def test_settings_validation():
    s = validate_settings({"preheat_enabled": True, "max_lead_min": 90, "damp_temp": 16.3}, DEFAULTS)
    assert s["preheat_enabled"] and s["max_lead_min"] == 90 and s["damp_temp"] == 16.5
    assert DEFAULTS["preheat_enabled"] is False and DEFAULTS["damp_push"] is False  # off by default
    for bad in ({"preheat_enabled": 1}, {"max_lead_min": 300}, {"max_lead_min": 10}, {"damp_humidity": 99},
                {"damp_temp": "x"}, {"damp_minutes": 5}, {"damp_cooldown_h": 0}, {"nope": 1}, []):
        with pytest.raises(ClimateError):
            validate_settings(bad, DEFAULTS)


def test_room_validation():
    states = {"sensor.bed_humidity": {"state": "71"}}
    assert validate_room({"preheat": True}, {}, states) == {"preheat": True, "humidity_entity": None}
    assert validate_room({"humidity_entity": "sensor.bed_humidity"}, {"preheat": True}, states)["humidity_entity"] == "sensor.bed_humidity"
    for bad in ({}, {"preheat": "yes"}, {"humidity_entity": "light.x"}, {"humidity_entity": "sensor.gone"}, {"x": 1}):
        with pytest.raises(ClimateError):
            validate_room(bad, {}, states)


# ---------------- learning ----------------
def test_warmup_rate_from_a_raised_target():
    w, t0 = WarmUp(), 1000.0
    assert w.feed(t0, 16, 15.0) is None                 # first reading: no rise seen yet
    assert w.feed(t0 + 60, 20, 15.0) is None            # target up 4°, 5° above the room: warm-up starts
    assert w.feed(t0 + 1 * H, 20, 17.0) is None
    r = w.feed(t0 + 2 * H + 60, 20, 19.8)              # within 0.3° of the target: done
    assert r == pytest.approx(4.8 / ((2 * H) / H))
    assert w.feed(t0 + 3 * H, 20, 20.0) is None          # no new warm-up without a new rise


def test_warmup_ignored_when_small_short_or_interrupted():
    assert learn_from([(0, 18, 18.0), (60, 18.5, 18.0), (H, 18.5, 18.4)]) == []          # rise below MIN_GAP
    assert learn_from([(0, 16, 15.0), (60, 20, 15.0), (300, 20, 19.9)]) == []            # 4 min: too short to trust
    assert learn_from([(0, 16, 15.0), (60, 20, 15.0), (H, None, None), (2 * H, 20, 19.9)]) == []  # valve dropped out
    # target changed mid-way (by hand / a schedule): the warm-up so far still counts
    rates = learn_from([(0, 16, 15.0), (60, 20, 15.0), (H + 60, 20, 16.5), (H + 120, 18, 16.6)])
    assert rates == [pytest.approx(1.5)]


def test_rate_is_clamped_and_blended():
    assert learn_from([(0, 10, 10.0), (60, 25, 10.0), (60 + 900, 25, 24.9)]) == [8.0]
    b = blend(None, 2.0, 1)
    b = blend(b, 1.0, 2)
    assert b == {"rate": 1.7, "n": 2, "at": 2}


def test_history_series_parses_rows():
    rows = [{"entity_id": "climate.bed", "state": "heat", "attributes": {"temperature": 20, "current_temperature": 18},
             "last_updated": "2026-10-09T06:30:00+00:00"},
            {"state": "unavailable", "attributes": {}, "last_changed": "2026-10-09T06:00:00+00:00"},
            {"state": "heat", "attributes": {}, "last_changed": "garbage"}]
    out = history_series(rows)
    assert [x[1:] for x in out] == [(None, None), (20, 18)]


def test_learn_history_replaces_rates(tmp_path):
    rig = Rig(tmp_path, ts(2026, 10, 9, 12))
    t = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)
    iso = lambda m: (t + timedelta(minutes=m)).isoformat()
    rows = [[{"entity_id": "climate.bed", "state": "heat", "attributes": {"temperature": tg, "current_temperature": c},
              "last_updated": iso(m)} for m, tg, c in ((0, 16, 15.5), (1, 20, 15.5), (60, 20, 17.5), (121, 20, 19.8))]]

    class HA:
        async def history(self, start, end, ids, attributes=False):
            assert ids == ["climate.bed"] and attributes and end - start == timedelta(days=7)
            return rows
    r = rig.run(rig.c.learn_history(HA(), ["climate.bed"]))
    assert r == {"warmups": 1}
    rate = rig.c.room_rate(["climate.bed"])
    assert rate["n"] == 1 and rate["rate"] == pytest.approx(2.15, abs=0.01)


def test_live_readings_teach_the_rate(tmp_path):
    rig = Rig(tmp_path, ts(2026, 10, 9, 6))
    rig.tick()
    rig.live.states["climate.bed"] = valve(21, 16.0)
    rig.clock.t += 60
    rig.tick()
    for mins, cur in ((30, 17.0), (60, 18.0), (90, 19.0), (120, 20.0), (150, 20.8)):
        rig.clock.t = ts(2026, 10, 9, 6, 1) + mins * 60
        rig.live.states["climate.bed"] = valve(21, cur)
        rig.tick()
    rate = rig.c.room_rate(["climate.bed"])
    assert rate["n"] == 1 and rate["rate"] == pytest.approx(4.8 / 2.5, abs=0.01)
    assert rig.calls == []  # learning never switches anything


# ---------------- preheat ----------------
def test_lead_time_capped():
    assert lead_seconds(21, 17, 2.0, 240) == pytest.approx(2 * H * MARGIN)
    assert lead_seconds(21, 15, 1.0, 120) == 120 * 60
    assert lead_seconds(21, 22, 1.0, 120) == 0


def test_preheat_off_by_default(tmp_path):
    rig = Rig(tmp_path, ts(2026, 10, 9, 6, 30))
    rig.schedule()
    rig.tick()
    rig.c.put_settings({"preheat_enabled": True})   # master on, but the room isn't opted in
    rig.tick()
    rig.c.put_settings({"preheat_enabled": False})
    rig.c.put_room(BED, {"preheat": True})           # room on, master off
    rig.tick()
    assert rig.calls == []


def test_preheat_starts_early_once(tmp_path):
    rig = Rig(tmp_path, ts(2026, 10, 9, 4, 0))
    rig.store.put("climate_rates", {"climate.bed": {"rate": 2.0, "n": 3, "at": 0}})
    rig.schedule(at="07:00", value=21)
    rig.enable()
    # 16° -> 21° at 2 °/h × 1.15 = 172.5 min, capped at 120: starts 05:00
    rig.clock.t = ts(2026, 10, 9, 4, 59)
    rig.tick()
    assert rig.calls == []
    rig.clock.t = ts(2026, 10, 9, 5, 0)
    rig.tick()
    assert rig.calls == [("climate", "set_temperature", {"entity_id": ["climate.bed"], "temperature": 21})]
    # the valve hasn't reported the new target yet / it was turned down by hand: still never a second call
    for m in (1, 2, 30):
        rig.clock.t = ts(2026, 10, 9, 5, m)
        rig.tick()
    assert len(rig.calls) == 1
    # a restart doesn't repeat it either (marker in SQLite)
    rig2 = Rig(tmp_path, ts(2026, 10, 9, 5, 40))
    rig2.tick()
    assert rig2.calls == []
    log = rig.c.status()["log"][0]
    assert log["action"] == "Preheat started" and log["room"] == "Bedroom" and "Morning heat" in log["note"]
    # the next day's occurrence is a new one
    rig.clock.t = ts(2026, 10, 10, 5, 0)
    rig.tick()
    assert len(rig.calls) == 2


def test_preheat_uses_learnt_rate_and_default(tmp_path):
    rig = Rig(tmp_path, ts(2026, 10, 9, 5, 0))
    rig.schedule(at="07:00", value=18)   # 16 -> 18 at the default 1 °/h: 2 × 1.15 h = 138 min, capped 120
    rig.enable(max_lead_min=240)
    p = rig.c.plan(BED, rig.live.states, rig.clock(), 24 * H)
    assert p["lead"] == pytest.approx(2 / DEFAULT_RATE * H * MARGIN) and p["start"] == pytest.approx(ts(2026, 10, 9, 7) - p["lead"])
    rig.store.put("climate_rates", {"climate.bed": {"rate": 4.0, "n": 2, "at": 0}})
    p = rig.c.plan(BED, rig.live.states, rig.clock(), 24 * H)
    assert p["lead"] == pytest.approx(0.5 * H * MARGIN)


@pytest.mark.parametrize("why", ["away", "window", "held", "warm", "already", "schedule off", "master off", "late"])
def test_preheat_never_runs_when(tmp_path, why):
    rig = Rig(tmp_path, ts(2026, 10, 9, 6, 30))
    s = rig.schedule(at="07:00", value=21)
    rig.enable()
    if why == "away":
        rig.mode["mode"] = "away"
    elif why == "window":
        rig.live.states["binary_sensor.bed_window"] = {"state": "on", "attributes": {}}
    elif why == "held":
        rig.held = {"climate.bed": 21}
    elif why == "warm":
        rig.live.states["climate.bed"] = valve(17, 20.8)
    elif why == "already":
        rig.live.states["climate.bed"] = valve(21, 18)
    elif why == "schedule off":
        rig.schedules.update(s, {**{k: s[k] for k in ("name", "days", "time", "action", "target")}, "enabled": False})
    elif why == "master off":
        rig.c.put_settings({"preheat_enabled": False})
    elif why == "late":
        rig.clock.t = ts(2026, 10, 9, 6, 57)  # under 5 min to go: the schedule does it
    rig.tick()
    assert rig.calls == []


def test_preheat_only_touches_the_rooms_own_valves(tmp_path):
    rig = Rig(tmp_path, ts(2026, 10, 9, 6, 0))
    rig.live.states["climate.lounge"] = valve(17, 16.0)
    rig.schedule(at="07:00", value=21, ids=("climate.bed", "climate.lounge"))
    rig.enable("bed")  # the lounge isn't opted in
    rig.tick()
    assert rig.calls == [("climate", "set_temperature", {"entity_id": ["climate.bed"], "temperature": 21})]


def test_preheat_failed_call_is_not_retried(tmp_path):
    rig = Rig(tmp_path, ts(2026, 10, 9, 6, 0))
    rig.schedule()
    rig.enable()

    async def boom(*a):
        rig.calls.append(a)
        raise RuntimeError("HA down")
    rig.c.call = boom
    rig.tick()
    rig.clock.t += 120
    rig.tick()
    assert len(rig.calls) == 1
    assert rig.c.status()["log"][0]["action"] == "Preheat failed"


def test_room_without_valve_cant_enable(tmp_path):
    rig = Rig(tmp_path, ts(2026, 10, 9, 6, 0))
    with pytest.raises(ClimateError):
        rig.c.put_room({"id": "x", "name": "Hall", "x": 20, "y": 20, "w": 1, "h": 1}, {"preheat": True})


# ---------------- damp ----------------
def damp_rig(tmp_path, **settings):
    rig = Rig(tmp_path, ts(2026, 10, 9, 12, 0))
    rig.c.put_settings({"damp_push": True, "damp_minutes": 60, "damp_cooldown_h": 12, **settings})
    rig.live.states["humidifier.dehum"]["attributes"]["current_humidity"] = 78
    rig.live.states["climate.bed"] = valve(17, 15.0)
    return rig


def test_damp_push_after_sustained_and_cooldown(tmp_path):
    rig = damp_rig(tmp_path)
    rig.tick()
    st = rig.c.status()["rooms"][0]["damp"]
    assert st["humidity"] == 78 and st["temperature"] == 15.0 and st["at_risk"] and not st["sustained"]
    rig.clock.t += 59 * 60
    rig.tick()
    assert rig.pushes == []
    rig.clock.t += 60
    rig.tick()
    assert len(rig.pushes) == 1
    p = rig.pushes[0]
    assert p["title"] == "Damp risk: Bedroom" and "78 %" in p["body"] and "Run the Dehumidifier — it's off." in p["body"]
    assert p["tag"] == "damp-bed" and p["url"] == "/?climate"
    assert rig.c.status()["rooms"][0]["damp"]["sustained"]
    rig.clock.t += 11 * H     # still damp, inside the cooldown: no repeat, and not after a restart either
    rig.tick()
    assert len(rig.pushes) == 1
    rig2 = Rig(tmp_path, rig.clock.t)
    rig2.live.states = rig.live.states
    rig2.tick()
    assert rig2.pushes == []
    rig.clock.t += H + 60     # cooldown over, still damp
    rig.tick()
    assert len(rig.pushes) == 2


def test_damp_needs_both_and_has_hysteresis(tmp_path):
    rig = damp_rig(tmp_path)
    rig.live.states["climate.bed"] = valve(20, 19.0)  # humid but warm: fine
    rig.tick()
    assert not rig.c.status()["rooms"][0]["damp"]["at_risk"]
    rig.live.states["climate.bed"] = valve(17, 15.0)
    rig.tick()
    since = rig.c.status()["rooms"][0]["damp"]["since"]
    rig.clock.t += 30 * 60
    rig.live.states["humidifier.dehum"]["attributes"]["current_humidity"] = 68  # just under 70, within the 3 % band
    rig.tick()
    assert rig.c.status()["rooms"][0]["damp"]["since"] == since
    rig.live.states["humidifier.dehum"]["attributes"]["current_humidity"] = 66  # clearly dry again: reset
    rig.tick()
    assert not rig.c.status()["rooms"][0]["damp"]["at_risk"]


def test_damp_unavailable_keeps_timer_and_push_off_by_default(tmp_path):
    rig = damp_rig(tmp_path, damp_push=False)
    rig.tick()
    rig.clock.t += 30 * 60
    rig.live.states["climate.bed"] = valve(17, 15.0, state="unavailable")
    rig.live.states["humidifier.dehum"]["state"] = "unavailable"
    rig.tick()
    assert rig.c.status()["rooms"][0]["damp"]["humidity"] is None
    rig.live.states = {**base_states(), "climate.bed": valve(17, 15.0)}
    rig.live.states["humidifier.dehum"]["attributes"]["current_humidity"] = 78
    rig.clock.t += 31 * 60
    rig.tick()
    st = rig.c.status()["rooms"][0]["damp"]
    assert st["sustained"] and st["dehumidifier"]["entity_id"] == "humidifier.dehum" and st["dehumidifier"]["in_room"]
    assert rig.pushes == [] and rig.calls == []   # push off: shown in the app only; never switches the dehumidifier


def test_damp_uses_chosen_humidity_sensor(tmp_path):
    rig = damp_rig(tmp_path)
    rig.live.states["sensor.bed_humidity"] = {"state": "81.26", "attributes": {"device_class": "humidity", "friendly_name": "Bed hygrometer"}}
    rig.c.put_room(BED, {"humidity_entity": "sensor.bed_humidity"})
    st = rig.c.status()
    assert st["rooms"][0]["damp"]["humidity"] == 81.3 and st["rooms"][0]["damp"]["humidity_source"] == "Bed hygrometer"
    assert {"entity_id": "sensor.bed_humidity", "name": "Bed hygrometer"} in st["humidity_sensors"]


def test_damp_custom_thresholds(tmp_path):
    rig = damp_rig(tmp_path, damp_humidity=80)
    rig.tick()
    assert not rig.c.status()["rooms"][0]["damp"]["at_risk"]
    rig.c.put_settings({"damp_humidity": 75, "damp_temp": 14.5})
    rig.tick()
    assert not rig.c.status()["rooms"][0]["damp"]["at_risk"]   # 15.0° is above 14.5°
    rig.c.put_settings({"damp_temp": 15})
    rig.tick()
    assert rig.c.status()["rooms"][0]["damp"]["at_risk"]


# ---------------- API ----------------
CLIMATE_TEMPLATE = "climate|climate.lounge_valve|Lounge valve|TP-Link|KE100\n"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.climate.STARTUP_DELAY", 3600)
    fake = FakeHA()
    settings = Settings("http://ha.test", "test-token", "aaron", "s3cret", str(tmp_path / "layout.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(fake.handler))
    live = Live(ha, "ws://ha.test/api/websocket", settings.ha_token, use_ws=False)
    with TestClient(create_app(settings, ha, live)) as c:
        c.headers["Authorization"] = "Basic " + base64.b64encode(b"aaron:s3cret").decode()
        layout = {"unit": "m", "rooms": [{"id": "lounge", "name": "Lounge", "x": 0, "y": 0, "w": 4, "h": 4},
                                         {"id": "hall", "name": "Hall", "x": 4, "y": 0, "w": 2, "h": 2}],
                  "placements": [{"entity_id": "climate.lounge_valve", "x": 1, "y": 1}], "furniture": []}
        assert c.put("/api/layout", json=layout).status_code == 200
        yield c, fake


def test_api_status_settings_and_rooms(api):
    c, fake = api
    r = c.get("/api/climate").json()
    assert r["settings"]["preheat_enabled"] is False
    lounge = next(x for x in r["rooms"] if x["id"] == "lounge")
    assert lounge["preheat"] is False and lounge["valves"] == [{"entity_id": "climate.lounge_valve", "name": "Lounge valve"}]
    assert lounge["damp"]["temperature"] == 19.5 and lounge["rate"] is None
    assert c.put("/api/climate/settings", json={"max_lead_min": 1000}).status_code == 400
    assert c.put("/api/climate/settings", json={"preheat_enabled": True}).json()["settings"]["preheat_enabled"] is True
    assert c.put("/api/climate/rooms/nope", json={"preheat": True}).status_code == 404
    assert c.put("/api/climate/rooms/hall", json={"preheat": True}).status_code == 400   # no valve there
    fake.history = []
    r = c.put("/api/climate/rooms/lounge", json={"preheat": True}).json()
    assert next(x for x in r["rooms"] if x["id"] == "lounge")["preheat"] is True
    assert r["learn"] == {"warmups": 0}   # first switch-on learns from history
    assert any(p.startswith("/api/history/period/") for _, p, _ in fake.calls)
    fake.history = None                   # HA history failing: the room stays on, learning reports the error
    r = c.post("/api/climate/rooms/lounge/learn").json()
    assert "error" in r["learn"]
    assert not fake.service_calls()       # nothing switched by any of this
