import asyncio
import base64
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.automations import AutoStore, WindowHeating
from backend.config import Settings
from backend.discovery import Device
from backend.ha import HAClient
from backend.live import Live
from backend.schedules import (GRACE, MAX_SCHEDULES, ScheduleEngine, ScheduleError, ScheduleStore, next_run, occurrence,
                               validate_schedule, validate_settings)
from backend.sun import sun_event

LON = ZoneInfo("Europe/London")
LAT_LON = (51.5074, -0.1278)


def ts(y, mo, d, h, mi=0, s=0, fold=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=LON, fold=fold).timestamp()


def local(t):
    return datetime.fromtimestamp(t, LON)


# L-shaped lounge (north-east corner cut away) next to a bedroom
LOUNGE = {"id": "lounge", "name": "Lounge", "x": 0, "y": 0, "w": 5, "h": 5, "cut": {"corner": "ne", "w": 2, "h": 2}}
BED = {"id": "bed", "name": "Bedroom", "x": 5, "y": 0, "w": 3, "h": 5}
LAYOUT = {"unit": "m", "rooms": [LOUNGE, BED], "openings": [],
          "placements": [{"entity_id": "light.lounge", "x": 1, "y": 1}, {"entity_id": "light.lounge2", "x": 4, "y": 4},
                         {"entity_id": "light.cutout", "x": 4, "y": 1},   # in the cut-away corner: not in the lounge
                         {"entity_id": "light.bed", "x": 6, "y": 1}, {"entity_id": "switch.tv", "x": 2, "y": 2}]}
DEVICES = {e: Device(e, k, e.split(".")[1].title(), "X") for e, k in (
    ("light.lounge", "light"), ("light.lounge2", "light"), ("light.cutout", "light"), ("light.bed", "light"),
    ("switch.tv", "plug"), ("switch.kettle", "plug"), ("climate.lounge", "valve"), ("climate.bed", "valve"),
    ("binary_sensor.door", "sensor"))}


def sched(**kw):
    s = {"name": "Morning", "days": list(range(7)), "time": {"type": "fixed", "at": "06:30"},
         "action": {"type": "on"}, "target": {"entity_ids": ["light.bed"]}}
    s.update(kw)
    return s


# ---------------- validation ----------------
def test_validate_ok_and_normalised():
    v = validate_schedule(sched(name="  Wake  ", days=[4, 0, 0], action={"type": "brightness", "value": 39.6},
                                target={"entity_ids": ["light.bed", "light.bed", "light.lounge"]}), DEVICES, LAYOUT)
    assert v == {"name": "Wake", "enabled": True, "days": [0, 4], "time": {"type": "fixed", "at": "06:30"},
                 "action": {"type": "brightness", "value": 40}, "target": {"entity_ids": ["light.bed", "light.lounge"]}}
    v = validate_schedule(sched(time={"type": "sunset", "offset": -15}, action={"type": "temperature", "value": 19.3},
                                target={"entity_ids": ["climate.bed"]}), DEVICES, LAYOUT)
    assert v["time"] == {"type": "sunset", "offset": -15} and v["action"] == {"type": "temperature", "value": 19.5}
    assert validate_schedule(sched(target={"room": "lounge"}, action={"type": "off"}), DEVICES, LAYOUT)["target"] == {"room": "lounge"}
    assert validate_schedule(sched(target={"entity_ids": ["switch.tv", "light.bed"]}), DEVICES, LAYOUT)


@pytest.mark.parametrize("bad, msg", [
    ({"name": " "}, "name"),
    ({"days": []}, "days"), ({"days": [7]}, "days"), ({"days": [True]}, "days"), ({"days": "mon"}, "days"),
    ({"time": {"type": "fixed", "at": "24:00"}}, "HH:MM"), ({"time": {"type": "fixed", "at": "6:30"}}, "HH:MM"),
    ({"time": {"type": "noon"}}, "time.type"), ({"time": {"type": "sunrise", "offset": 181}}, "offset"),
    ({"time": {"type": "sunrise", "offset": 1.5}}, "offset"),
    ({"action": {"type": "toggle"}}, "action.type"),
    ({"action": {"type": "brightness", "value": 0}}, "brightness"),
    ({"action": {"type": "temperature", "value": 40}, "target": {"entity_ids": ["climate.bed"]}}, "temperature"),
    ({"action": {"type": "temperature", "value": 20}}, "light"),                       # light can't take a temperature
    ({"action": {"type": "brightness", "value": 50}, "target": {"entity_ids": ["switch.tv"]}}, "plug"),
    ({"target": {"entity_ids": ["binary_sensor.door"]}}, "sensor"),
    ({"target": {"entity_ids": ["light.nope"]}}, "unknown device"),
    ({"target": {"entity_ids": []}}, "at least one"),
    ({"target": {"entity_ids": [f"light.x{i}" for i in range(51)]}}, "at most"),
    ({"target": {"room": "attic"}}, "unknown room"),
    ({"target": {"room": "lounge"}, "action": {"type": "temperature", "value": 20}}, "room"),
    ({"enabled": "yes"}, "enabled"),
])
def test_validate_rejects(bad, msg):
    with pytest.raises(ScheduleError, match=msg):
        validate_schedule(sched(**bad), DEVICES, LAYOUT)


def test_validate_keeps_devices_already_in_the_schedule():
    old = sched(target={"entity_ids": ["light.gone"]})
    assert validate_schedule(sched(target={"entity_ids": ["light.gone"]}, enabled=False), DEVICES, LAYOUT, old)["enabled"] is False
    with pytest.raises(ScheduleError):
        validate_schedule(sched(target={"entity_ids": ["light.gone"]}), DEVICES, LAYOUT)


def test_validate_settings():
    cur = {"enabled": True, "lat": 51.5074, "lon": -0.1278}
    assert validate_settings({"lat": 53.48096, "lon": -2.23743}, cur) == {"enabled": True, "lat": 53.481, "lon": -2.2374}
    for bad in ({"lat": 91}, {"lon": "x"}, {"enabled": 1}, []):
        with pytest.raises(ScheduleError):
            validate_settings(bad, cur)


# ---------------- timing ----------------
def test_fixed_time_days_and_next():
    s = sched(days=[1], time={"type": "fixed", "at": "06:30"})   # Tuesdays
    assert occurrence(s, date(2026, 10, 12), LON, *LAT_LON) is None          # Monday
    assert occurrence(s, date(2026, 10, 13), LON, *LAT_LON) == ts(2026, 10, 13, 6, 30)
    assert next_run(s, ts(2026, 10, 9, 12), LON, *LAT_LON) == ts(2026, 10, 13, 6, 30)   # Fri -> Tue
    assert next_run(s, ts(2026, 10, 13, 6, 30), LON, *LAT_LON) == ts(2026, 10, 20, 6, 30)  # strictly after
    assert next_run(sched(days=[0, 1, 2, 3, 4]), ts(2026, 10, 9, 7), LON, *LAT_LON) == ts(2026, 10, 12, 6, 30)  # weekdays


def test_fixed_time_dst_days():
    # spring forward, Sun 29 Mar 2026: 06:30 is 06:30 BST (05:30 UTC), not 06:30 GMT
    s = sched(time={"type": "fixed", "at": "06:30"})
    assert local(occurrence(s, date(2026, 3, 29), LON, *LAT_LON)).strftime("%H:%M %Z") == "06:30 BST"
    assert occurrence(s, date(2026, 3, 29), LON, *LAT_LON) - occurrence(s, date(2026, 3, 28), LON, *LAT_LON) == 23 * 3600
    # 01:30 doesn't exist that night: it runs once, at 02:30 BST
    gap = occurrence(sched(time={"type": "fixed", "at": "01:30"}), date(2026, 3, 29), LON, *LAT_LON)
    assert local(gap).strftime("%H:%M %Z") == "02:30 BST"
    # fall back, Sun 25 Oct 2026: 01:30 happens twice; it runs at the first (BST) one only
    s130 = sched(time={"type": "fixed", "at": "01:30"})
    t = occurrence(s130, date(2026, 10, 25), LON, *LAT_LON)
    assert t == ts(2026, 10, 25, 1, 30, fold=0) and ts(2026, 10, 25, 1, 30, fold=1) - t == 3600
    assert next_run(s130, t, LON, *LAT_LON) == ts(2026, 10, 26, 1, 30)   # not the repeated 01:30 GMT
    assert local(occurrence(s, date(2026, 10, 25), LON, *LAT_LON)).strftime("%H:%M %Z") == "06:30 GMT"


@pytest.mark.parametrize("day, sunrise, sunset", [
    # London, published times (timeanddate.com / HM Nautical Almanac Office), local clock
    (date(2026, 3, 20), "06:03", "18:13"),
    (date(2026, 6, 21), "04:43", "21:21"),
    (date(2026, 10, 25), "06:41", "16:46"),
    (date(2026, 12, 21), "08:04", "15:53"),
])
def test_sun_times_london(day, sunrise, sunset):
    for ev, want in (("sunrise", sunrise), ("sunset", sunset)):
        got = sun_event(day, ev, *LAT_LON)
        h, m = map(int, want.split(":"))
        assert abs(got - ts(day.year, day.month, day.day, h, m)) <= 120, (ev, local(got))


def test_sun_polar_and_offset():
    assert sun_event(date(2026, 6, 21), "sunset", 78.2, 15.6) is None   # Svalbard, midnight sun
    s = sched(time={"type": "sunset", "offset": -30})
    assert occurrence(s, date(2026, 6, 21), LON, *LAT_LON) == sun_event(date(2026, 6, 21), "sunset", *LAT_LON) - 1800
    # sunrise on the spring-forward day is still the real sunrise (about 05:45 BST), not shifted an hour
    t = occurrence(sched(time={"type": "sunrise", "offset": 0}), date(2026, 3, 29), LON, *LAT_LON)
    assert abs(t - ts(2026, 3, 29, 6, 45)) < 300 and local(t).tzname() == "BST"
    assert sched(time={"type": "sunset", "offset": 0}) and next_run(
        sched(time={"type": "sunset", "offset": 0}, days=[3]), ts(2026, 10, 9, 12), LON, *LAT_LON) > ts(2026, 10, 15, 17)


# ---------------- engine ----------------
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Rig:
    def __init__(self, tmp_path, now, store=None, fail=False):
        self.clock, self.calls, self.fail = Clock(now), [], fail
        self.mode = {"mode": "home", "away_temp": 16.0}
        self.layout = LAYOUT
        self.auto = AutoStore(str(tmp_path / "s.db"))
        self.store = store or ScheduleStore(str(tmp_path / "s.db"))
        self.window = WindowHeating(self.auto, lambda: {"window_heating_enabled": True}, None, None, lambda: self.mode, self.clock)
        self.engine = ScheduleEngine(self.store, self.call, lambda: self.layout, lambda: self.mode, self.window, self.clock, LON)

    async def call(self, domain, service, data):
        self.calls.append((domain, service, data))
        if self.fail:
            raise RuntimeError("HA down")

    def add(self, **kw):
        return self.engine.create(validate_schedule(sched(**kw), DEVICES, LAYOUT))

    def tick(self, at=None):
        if at is not None:
            self.clock.t = at
        n = len(self.calls)
        asyncio.run(self.engine.tick(DEVICES, {}))
        return self.calls[n:]

    def last(self, s):
        return next(x for x in self.engine.listing()["schedules"] if x["id"] == s["id"])["last"]


T0 = ts(2026, 10, 12, 6, 30)   # Monday 06:30


def test_fires_once_and_not_again_after_restart(tmp_path):
    r = Rig(tmp_path, T0 - 3600)
    s = r.add()
    assert r.tick(T0 - 1) == []
    assert r.tick(T0) == [("light", "turn_on", {"entity_id": ["light.bed"]})]
    assert r.tick(T0 + 20) == [] and r.tick(T0 + 100) == []
    r2 = Rig(tmp_path, T0 + 30, store=r.store)   # restart within the grace window
    assert r2.tick() == []
    assert r.last(s)["status"] == "1 light on" and r.last(s)["at"] == T0 * 1000
    assert r.tick(T0 + 86400) == [("light", "turn_on", {"entity_id": ["light.bed"]})]   # next day: again, once


def test_grace_two_minutes_and_no_replay_after_downtime(tmp_path):
    r = Rig(tmp_path, T0 - 3600)
    r.add()
    assert len(r.tick(T0 + GRACE - 1)) == 1        # 1:59 late (e.g. restart): still fires
    r = Rig(tmp_path / "b", T0 - 3600)
    s = r.add()
    assert r.tick(T0 + GRACE) == []                # 2 min late: missed, never replayed
    assert r.tick(T0 + 3 * 3600) == []
    assert r.last(s) is None


def test_created_or_enabled_after_the_time_does_not_catch_up(tmp_path):
    r = Rig(tmp_path, T0 + 30)
    s = r.add()                                    # made at 06:30:30 for 06:30
    assert r.tick(T0 + 40) == []
    off = r.engine.update(s, {**validate_schedule(sched(), DEVICES, LAYOUT), "enabled": False})
    r.clock.t = T0 + 86400 - 60
    r.engine.update(off, validate_schedule(sched(), DEVICES, LAYOUT))   # back on a minute before: fires
    assert len(r.tick(T0 + 86400)) == 1


def test_per_schedule_toggle_and_master_switch(tmp_path):
    r = Rig(tmp_path, T0 - 3600)
    s = r.add()
    r.engine.update(s, {**validate_schedule(sched(), DEVICES, LAYOUT), "enabled": False})
    assert r.tick(T0) == []
    r = Rig(tmp_path / "m", T0 - 3600)
    r.add()
    r.engine.put_settings({**r.store.settings(), "enabled": False})
    assert r.tick(T0) == [] and r.engine.listing()["schedules"][0]["next"] is None
    r.engine.put_settings({**r.store.settings(), "enabled": True})   # back on at 06:30:00: no catch-up
    r.clock.t = T0 + 10
    r.engine.put_settings({**r.store.settings(), "enabled": False})
    r.engine.put_settings({**r.store.settings(), "enabled": True})
    assert r.tick(T0 + 20) == []
    assert len(r.tick(T0 + 86400)) == 1


def test_edit_after_firing_runs_the_new_time_today(tmp_path):
    r = Rig(tmp_path, T0 - 3600)
    s = r.add()
    assert len(r.tick(T0)) == 1
    r.clock.t = T0 + 600
    s = r.engine.update(s, validate_schedule(sched(time={"type": "fixed", "at": "07:00"}), DEVICES, LAYOUT))
    assert r.tick(T0 + 1800) == [("light", "turn_on", {"entity_id": ["light.bed"]})]
    assert r.tick(T0 + 1830) == []
    r.engine.update(s, {**validate_schedule(sched(time={"type": "fixed", "at": "07:00"}), DEVICES, LAYOUT), "name": "Renamed"})
    assert r.tick(T0 + 1850) == []   # renaming isn't a new occurrence


def test_batched_per_domain_and_last_schedule_wins(tmp_path):
    r = Rig(tmp_path, T0 - 3600)
    r.add(target={"entity_ids": ["light.bed", "switch.tv"]})
    r.add(target={"entity_ids": ["light.lounge", "switch.kettle"]})
    r.add(action={"type": "off"}, target={"entity_ids": ["light.lounge"]})          # conflicts: this one wins
    r.add(action={"type": "brightness", "value": 30}, target={"entity_ids": ["light.lounge2"]})
    r.add(action={"type": "temperature", "value": 20}, target={"entity_ids": ["climate.bed", "climate.lounge"]})
    calls = r.tick(T0)
    assert sorted(calls, key=str) == sorted([
        ("light", "turn_on", {"entity_id": ["light.bed"]}),
        ("switch", "turn_on", {"entity_id": ["switch.kettle", "switch.tv"]}),
        ("light", "turn_off", {"entity_id": ["light.lounge"]}),
        ("light", "turn_on", {"entity_id": ["light.lounge2"], "brightness_pct": 30}),
        ("climate", "set_temperature", {"entity_id": ["climate.bed", "climate.lounge"], "temperature": 20.0}),
    ], key=str)
    ids = [e for _, _, d in calls for e in d["entity_id"]]
    assert len(ids) == len(set(ids))   # one call per device


def test_room_target_uses_l_shape(tmp_path):
    r = Rig(tmp_path, T0 - 3600)
    s = r.add(target={"room": "lounge"}, action={"type": "brightness", "value": 60})
    assert r.tick(T0) == [("light", "turn_on", {"entity_id": ["light.lounge", "light.lounge2"], "brightness_pct": 60})]
    assert r.last(s)["status"] == "2 lights at 60 %"
    r.layout = {**LAYOUT, "rooms": [BED]}   # room deleted later: skipped, nothing sent
    assert r.tick(T0 + 86400) == [] and r.last(s)["status"] == "skipped: no devices"


def test_away_skips_valves_only(tmp_path):
    r = Rig(tmp_path, T0 - 3600)
    v = r.add(action={"type": "temperature", "value": 21}, target={"entity_ids": ["climate.bed"]})
    r.add()
    r.mode = {"mode": "away", "away_temp": 16.0}
    assert r.tick(T0) == [("light", "turn_on", {"entity_id": ["light.bed"]})]
    assert r.last(v)["status"] == "skipped: away"
    assert r.tick(T0 + 60) == []   # not caught up later


def test_window_held_valve_gets_the_value_when_the_window_closes(tmp_path):
    r = Rig(tmp_path, T0 - 3600)
    r.window.holds["climate.bed"] = {"restore": 21.0, "temp": 7.0, "since": T0 - 600, "windows": ["w"], "pending": False}
    s = r.add(action={"type": "temperature", "value": 18}, target={"entity_ids": ["climate.bed", "climate.lounge"]})
    assert r.tick(T0) == [("climate", "set_temperature", {"entity_id": ["climate.lounge"], "temperature": 18.0})]
    assert r.window.holds["climate.bed"]["restore"] == 18.0
    assert r.auto.get("window_holds")["climate.bed"]["restore"] == 18.0     # persisted
    assert "held by an open window" in r.last(s)["status"]


def test_failed_call_is_not_retried(tmp_path):
    r = Rig(tmp_path, T0 - 3600, fail=True)
    s = r.add()
    assert len(r.tick(T0)) == 1
    assert r.tick(T0 + 15) == [] and r.tick(T0 + 60) == []
    assert r.last(s)["status"].startswith("failed")


def test_late_evening_grace_crosses_midnight(tmp_path):
    t = ts(2026, 10, 12, 23, 59)
    r = Rig(tmp_path, t - 3600)
    r.add(days=[0], time={"type": "fixed", "at": "23:59"})   # Mondays only; checked at 00:00:30 Tuesday
    assert len(r.tick(t + 90)) == 1 and r.tick(t + 100) == []


def test_dst_day_fires_once_at_the_right_instant(tmp_path):
    t = ts(2026, 10, 25, 1, 30, fold=0)   # the first 01:30 (BST)
    r = Rig(tmp_path, t - 3600)
    r.add(time={"type": "fixed", "at": "01:30"})
    assert r.tick(t - 1) == [] and len(r.tick(t)) == 1
    assert r.tick(t + 3600) == []         # the repeated 01:30 (GMT) doesn't fire again


def test_sunset_schedule_fires(tmp_path):
    d = date(2026, 10, 12)
    t = sun_event(d, "sunset", *LAT_LON) - 15 * 60
    r = Rig(tmp_path, t - 3600)
    r.add(time={"type": "sunset", "offset": -15}, target={"room": "lounge"})
    assert r.tick(t - 1) == [] and len(r.tick(t)) == 1


def test_limit_and_next_due(tmp_path):
    r = Rig(tmp_path, T0 - 3600)
    for _ in range(MAX_SCHEDULES):
        r.add()
    with pytest.raises(ScheduleError, match="at most"):
        r.add()
    assert r.engine.next_due() == T0


# ---------------- API ----------------
@pytest.fixture
def app_client(tmp_path, fake_ha):
    clock = Clock(T0 - 3600)
    settings = Settings("http://ha.test", "test-token", "aaron", "s3cret", str(tmp_path / "layout.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(fake_ha.handler))
    live = Live(ha, "ws://ha.test/api/websocket", settings.ha_token, use_ws=False)
    with TestClient(create_app(settings, ha, live, clock=clock)) as c:
        c.headers["Authorization"] = "Basic " + base64.b64encode(b"aaron:s3cret").decode()
        c.clock = c.app.state.automations.schedules.clock = clock
        yield c


def test_api_crud(app_client, fake_ha):
    c = app_client
    body = {"name": "Kitchen on", "days": [0, 1, 2, 3, 4], "time": {"type": "fixed", "at": "06:30"},
            "action": {"type": "on"}, "target": {"entity_ids": ["light.kitchen_1", "switch.fan"]}}
    r = c.post("/api/schedules", json=body)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["next"] == T0 * 1000 and s["last"] is None and s["enabled"] and "rev" not in s
    lst = c.get("/api/schedules").json()
    assert lst["enabled"] and lst["lat"] == 51.5074 and lst["tz"] == "Europe/London" and [x["id"] for x in lst["schedules"]] == [s["id"]]
    assert c.post("/api/schedules", json={**body, "target": {"entity_ids": ["climate.lounge_valve"]}}).status_code == 400
    assert c.post("/api/schedules", json={**body, "target": {"entity_ids": ["switch.fan_led"]}}).status_code == 400  # not a device
    assert c.post("/api/schedules", content=b"nope").status_code == 400
    r = c.put(f"/api/schedules/{s['id']}", json={**body, "enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["next"] is None
    assert c.put("/api/schedules/abc", json=body).status_code == 404
    r = c.put("/api/schedules/settings", json={"enabled": False, "lat": 55.95, "lon": -3.19})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["lat"] == 55.95
    assert c.put("/api/schedules/settings", json={"lat": 100}).status_code == 400
    assert c.delete(f"/api/schedules/{s['id']}").json() == {"ok": True}
    assert c.delete(f"/api/schedules/{s['id']}").status_code == 404
    assert c.get("/api/schedules").json()["schedules"] == []


def test_api_schedule_runs_through_the_loop(app_client, fake_ha):
    c = app_client
    c.post("/api/schedules", json={"name": "Valve", "days": [0], "time": {"type": "fixed", "at": "06:30"},
                                   "action": {"type": "temperature", "value": 19}, "target": {"entity_ids": ["climate.lounge_valve"]}})
    c.get("/api/devices")   # loads HA states into Live
    c.clock.t = T0 + 5
    auto = c.app.state.automations
    c.portal.call(auto.tick)
    c.portal.call(auto.tick)
    calls = [b for p, b in fake_ha.service_calls() if p == "/api/services/climate/set_temperature"]
    assert calls == [{"entity_id": ["climate.lounge_valve"], "temperature": 19.0}]
    assert c.get("/api/schedules").json()["schedules"][0]["last"]["status"] == "1 radiator to 19°"
