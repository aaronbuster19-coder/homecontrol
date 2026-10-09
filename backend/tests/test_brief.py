"""Morning brief (backend/brief.py) and the monthly energy report (backend/energy_report.py), with an injected clock."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.brief import Brief, door_events, left_on, night_window, weather_part
from backend.discovery import Device
from backend.energy_report import (DailyStore, ReportError, Rollup, appliance_names, change_pct, month_first, month_last,
                                   monthly, parse_month)
from backend.tests.test_summary import FakeHistory, iso

LON = ZoneInfo("Europe/London")
H = 3600


def ts(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LON).timestamp()


def run(coro):
    return asyncio.run(coro)


TV = Device("switch.tv", "plug", "TV plug", "P110", {"power": "sensor.tv_power"})
FRIDGE = Device("switch.fridge", "plug", "Plug 2", "P110", {"power": "sensor.fridge_power"})
LAMP = Device("light.lamp", "light", "Lamp", "L530")
DOOR = Device("binary_sensor.door", "sensor", "Front door", "T110")
WIN = Device("binary_sensor.win", "sensor", "Bedroom window", "T110")
DEVS = {d.entity_id: d for d in (TV, FRIDGE, LAMP, DOOR, WIN)}
LAYOUT = {"settings": {"keep_on": [], "energy": {"rate_p": 25.0, "standing_p": 50.0}},
          "furniture": [{"id": "f1", "type": "fridge", "plug": "switch.fridge"}, {"id": "f2", "type": "tv", "plug": "switch.tv", "label": "Telly"}]}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


# ---------------- pure helpers ----------------
def test_months():
    assert month_first(date(2026, 10, 9)) == date(2026, 10, 1)
    assert month_first(date(2026, 1, 15), 1) == date(2025, 12, 1)
    assert month_first(date(2026, 12, 1), -1) == date(2027, 1, 1)
    assert month_last(date(2026, 2, 1)) == date(2026, 2, 28) and month_last(date(2028, 2, 1)) == date(2028, 2, 29)
    assert parse_month("2026-09") == date(2026, 9, 1)
    for bad in ("2026-9", "2026-13", "x", "", "2026-09-01"):
        with pytest.raises(ReportError):
            parse_month(bad)
    assert change_pct(12, 10) == 20 and change_pct(5, 10) == -50 and change_pct(1, 0) is None and change_pct(1, None) is None


def test_night_window_local_and_dst():
    a, b = night_window(ts(2026, 10, 9, 8, 30), LON)
    assert (a, b) == (ts(2026, 10, 8, 22), ts(2026, 10, 9, 7))
    assert night_window(ts(2026, 10, 9, 5), LON)[1] == ts(2026, 10, 9, 5)       # before 07:00: up to now
    assert night_window(ts(2026, 10, 9, 21), LON)[0] == ts(2026, 10, 8, 22)    # evening: still last night
    a, b = night_window(ts(2026, 10, 25, 9), LON)                              # clocks went back: a 10 h night
    assert (b - a) / H == 10


def test_door_events_skip_initial_state_and_unavailable():
    rows = [{"state": "off", "last_changed": iso(ts(2026, 10, 8, 21))},
            {"state": "on", "last_changed": iso(ts(2026, 10, 8, 23, 10))},
            {"state": "off", "last_changed": iso(ts(2026, 10, 8, 23, 12))},
            {"state": "unavailable", "last_changed": iso(ts(2026, 10, 9, 2))},
            {"state": "off", "last_changed": iso(ts(2026, 10, 9, 2, 5))},   # back from unavailable: not an event
            {"state": "on", "last_changed": iso(ts(2026, 10, 9, 6, 45))}]
    ev = door_events(rows, int(ts(2026, 10, 8, 22) * 1000), int(ts(2026, 10, 9, 7) * 1000))
    assert [(datetime.fromtimestamp(t / 1000, LON).strftime("%H:%M"), v) for t, v in ev] == \
        [("23:10", "open"), ("23:12", "closed"), ("06:45", "open")]


def test_left_on_skips_protected_keep_on_hidden_and_off():
    hidden = Device("light.hall", "light", "Hall", "L530", hidden=True)
    tvset = Device("media_player.tv", "media", "TV", "QE55")
    kettle = Device("switch.kettle", "plug", "Kettle", "P110")
    devs = {**DEVS, hidden.entity_id: hidden, tvset.entity_id: tvset, kettle.entity_id: kettle}
    states = {"light.lamp": {"state": "on", "last_changed": iso(ts(2026, 10, 8, 19))},
              "switch.tv": {"state": "on", "last_changed": iso(ts(2026, 10, 8, 18))},
              "sensor.tv_power": {"state": "45.5"},
              "switch.fridge": {"state": "on"}, "light.hall": {"state": "on"},
              "media_player.tv": {"state": "standby"}, "switch.kettle": {"state": "on"}}
    layout = {**LAYOUT, "settings": {**LAYOUT["settings"], "keep_on": ["switch.kettle"]}}
    rows = left_on(devs, states, layout)
    assert [r["name"] for r in rows] == ["Telly", "Lamp"]  # oldest first; fridge (protected), kettle (keep on), hidden, standby TV out
    assert rows[0]["power"] == 45.5 and rows[0]["since"] == ts(2026, 10, 8, 18) * 1000
    states["media_player.tv"] = {"state": "playing"}
    assert "TV" in [r["name"] for r in left_on(devs, states, layout)]


def test_weather_part_rain_and_unavailable():
    now = ts(2026, 10, 9, 8)
    w = {"available": True, "current": {"condition": "rainy", "text": "Rain", "temperature": 11.2},
         "today": {"high": 14, "low": 8}, "tonight": {"cold": False},
         "hourly": [{"t": ts(2026, 10, 9, 9) * 1000, "precipitation_probability": 20},
                    {"t": ts(2026, 10, 9, 15) * 1000, "precipitation_probability": 70},
                    {"t": ts(2026, 10, 9, 16) * 1000, "precipitation_probability": 90}]}
    p = weather_part(w, now, LON)
    assert p["available"] and p["high"] == 14 and p["rain_from"] == "15:00" and p["rain_pct"] == 70 and p["tonight"] is None
    w["hourly"] = [{"t": ts(2026, 10, 10, 1) * 1000, "precipitation_probability": 90}]  # tomorrow doesn't count
    assert "rain_from" not in weather_part(w, now, LON)
    assert weather_part({"available": False, "entity_id": None}, now, LON) == {"available": False, "entity_id": None}


def test_appliance_names():
    names = appliance_names(LAYOUT)
    assert names["switch.fridge"]["name"] == "Fridge" and names["switch.tv"]["name"] == "Telly"


# ---------------- rollup ----------------
def power_history():
    # TV: 100 W all of September, 200 W from 1 Oct; unavailable on 3 Oct 00:00–12:00. Fridge: 50 W throughout.
    return FakeHistory({"sensor.tv_power": [(ts(2026, 8, 1), "100", {}), (ts(2026, 10, 1), "200", {}),
                                            (ts(2026, 10, 3), "unavailable", {}), (ts(2026, 10, 3, 12), "200", {})],
                        "sensor.fridge_power": [(ts(2026, 8, 1), "50", {})]})


def make(tmp_path, now, ha=None):
    clock = Clock(now)
    return Rollup(DailyStore(str(tmp_path / "db.sqlite")), ha or power_history(), LON, clock), clock


def test_rollup_newest_first_in_batches_and_never_twice(tmp_path):
    r, clock = make(tmp_path, ts(2026, 10, 9, 8))
    assert r.missing([TV, FRIDGE])[0] == date(2026, 10, 8) and r.missing([TV, FRIDGE])[-1] == date(2026, 9, 1)
    assert run(r.run([TV, FRIDGE])) == 7
    assert len(r.ha.calls) == 1  # one HA call for the 7 newest days
    a, b = r.ha.calls[0][0], r.ha.calls[0][1]
    assert a == datetime(2026, 10, 2, tzinfo=LON) and b == datetime(2026, 10, 9, tzinfo=LON)
    rows = {(d, e): (k, c) for d, e, k, c in r.store.between(date(2026, 10, 1), date(2026, 10, 8))}
    assert rows[("2026-10-08", "switch.tv")] == (4.8, 24 * H * 1000)
    assert rows[("2026-10-03", "switch.tv")] == (2.4, 12 * H * 1000)   # the gap isn't counted as 0 W
    assert ("2026-10-01", "switch.tv") not in rows
    assert run(r.run([TV, FRIDGE], 100)) == 38 - 7 and len(r.missing([TV, FRIDGE])) == 0  # 1 Sep – 8 Oct
    assert run(r.run([TV, FRIDGE])) == 0 and len(r.ha.calls) == 1 + 5  # 31 days in batches of 7
    clock.t += 24 * H  # a day later: just yesterday
    assert r.missing([TV, FRIDGE]) == [date(2026, 10, 9)]


def test_rollup_dst_day_and_plug_found_later(tmp_path):
    r, clock = make(tmp_path, ts(2026, 10, 27, 8))
    run(r.run([FRIDGE], 3))
    rows = {d: k for d, e, k, c in r.store.between(date(2026, 10, 24), date(2026, 10, 26))}
    assert rows["2026-10-25"] == 1.25 and rows["2026-10-26"] == 1.2  # 25 h on the day the clocks went back
    # a new plug: its days are fetched, the fridge's stored days are kept as they were
    r.store.put_day("2026-10-26", {"switch.fridge": (9.0, 1)})
    run(r.run([TV, FRIDGE], 3))
    rows = {(d, e): k for d, e, k, c in r.store.between(date(2026, 10, 24), date(2026, 10, 26))}
    assert rows[("2026-10-26", "switch.fridge")] == 1.2 and rows[("2026-10-26", "switch.tv")] == 4.8


def test_rollup_waits_after_failure_and_needs_plugs(tmp_path):
    class Down:
        calls = 0

        async def history(self, *a, **k):
            Down.calls += 1
            raise RuntimeError("HA down")
    r, clock = make(tmp_path, ts(2026, 10, 9, 8), Down())
    assert run(r.run([TV])) == 0 and run(r.run([TV])) == 0 and Down.calls == 1
    clock.t += 301
    run(r.run([TV]))
    assert Down.calls == 2
    assert run(r.run([LAMP])) == 0  # no plug with a power sensor (not discovered yet): nothing marked


def test_days_without_history_are_stored_empty(tmp_path):
    r, _ = make(tmp_path, ts(2026, 10, 9, 8), FakeHistory({"sensor.tv_power": [(ts(2026, 10, 5), "10", {})]}))
    run(r.run([TV], 100))
    rows = {d: c for d, e, k, c in r.store.between(date(2026, 9, 1), date(2026, 10, 8))}
    assert rows["2026-10-04"] == 0 and rows["2026-10-05"] > 0 and len(rows) == 38
    assert r.store.first_day() == "2026-10-05"


# ---------------- monthly report ----------------
def test_report_this_month_vs_same_days_last_month(tmp_path):
    r, _ = make(tmp_path, ts(2026, 10, 9, 8))
    rep = run(monthly(r, [TV, FRIDGE], DEVS, LAYOUT))
    assert rep["month"] == "2026-10" and rep["current"] and rep["from"] == "2026-10-01" and rep["to"] == "2026-10-08"
    assert rep["days"] == 8 and rep["days_with_data"] == 8
    # TV: 7.5 days at 200 W = 36 kWh; fridge 8 × 1.2 = 9.6 kWh
    tv = next(x for x in rep["rows"] if x["entity_id"] == "switch.tv")
    fr = next(x for x in rep["rows"] if x["entity_id"] == "switch.fridge")
    assert tv["kwh"] == 36.0 and tv["name"] == "Telly" and tv["plug_name"] == "TV plug" and tv["appliance"]["type"] == "tv"
    assert fr["name"] == "Fridge" and fr["kwh"] == 9.6
    assert rep["total_kwh"] == 45.6 and rep["total_p"] == 1140.0 and rep["standing_total_p"] == 400.0
    assert tv["share_pct"] == round(36 / 45.6 * 100, 1) and tv["cost_p"] == 900.0
    prev = rep["previous"]
    assert prev["from"] == "2026-09-01" and prev["to"] == "2026-09-08" and not prev["full"] and prev["days"] == 8
    assert tv["last_kwh"] == 8 * 2.4 and tv["change_pct"] == round((36 - 19.2) / 19.2 * 100) and fr["change_pct"] == 0
    assert rep["change_pct"] == round((45.6 - 28.8) / 28.8 * 100)
    assert rep["prev_month"] == "2026-09" and rep["next_month"] is None and rep["rows"][0]["entity_id"] == "switch.tv"


def test_report_finished_month_and_navigation_limits(tmp_path):
    r, _ = make(tmp_path, ts(2026, 10, 9, 8))
    run(r.run([TV, FRIDGE], 100))
    rep = run(monthly(r, [TV, FRIDGE], DEVS, LAYOUT, "2026-09"))
    assert not rep["current"] and rep["days"] == 30 and rep["to"] == "2026-09-30" and rep["next_month"] == "2026-10"
    assert rep["previous"]["month"] == "2026-08" and rep["previous"]["full"] and rep["previous"]["days"] == 31
    assert rep["total_kwh"] == 30 * 3.6
    # August wasn't rolled up by the default window; this request fills up to 14 of its days, newest first
    assert rep["previous"]["days_with_data"] == 14
    with pytest.raises(ReportError):
        run(monthly(r, [TV], DEVS, LAYOUT, "2026-11"))
    with pytest.raises(ReportError):
        run(monthly(r, [TV], DEVS, LAYOUT, "2025-09"))
    assert run(monthly(r, [TV], DEVS, LAYOUT, "2025-10"))["prev_month"] is None


def test_report_on_the_first_defaults_to_last_month(tmp_path):
    r, _ = make(tmp_path, ts(2026, 11, 1, 9))
    rep = run(monthly(r, [TV, FRIDGE], DEVS, {}))
    assert rep["month"] == "2026-10" and rep["days"] == 31 and rep["total_p"] is None and rep["rows"][0]["cost_p"] is None
    cur = run(monthly(r, [TV, FRIDGE], DEVS, {}, "2026-11"))
    assert cur["days"] == 0 and cur["rows"] == [] and cur["change_pct"] is None


# ---------------- brief ----------------
class FakeWeather:
    async def get(self):
        return {"available": True, "entity_id": "weather.home", "current": {"condition": "sunny", "text": "Sunny", "temperature": 9},
                "today": {"high": 15, "low": 6}, "tonight": {"cold": True, "text": "Cold night ahead (1°)"}, "hourly": []}


class Live:
    def __init__(self, states):
        self.states = states


def make_brief(tmp_path, now, states=None, ha=None):
    ha = ha or FakeHistory({"sensor.tv_power": [(ts(2026, 10, 1), "100", {})],
                            "binary_sensor.door": [(ts(2026, 10, 8, 12), "off", {}), (ts(2026, 10, 8, 23, 30), "on", {}),
                                                   (ts(2026, 10, 8, 23, 31), "off", {}), (ts(2026, 10, 9, 9), "on", {})],
                            "binary_sensor.win": [(ts(2026, 10, 1), "off", {})]})
    clock = Clock(now)

    async def devices():
        return DEVS
    r = Rollup(DailyStore(str(tmp_path / "db.sqlite")), ha, LON, clock)
    return Brief(ha, Live(states or {}), lambda: LAYOUT, devices, FakeWeather(), r, LON, clock), ha


def test_brief_builds_every_part(tmp_path):
    b, ha = make_brief(tmp_path, ts(2026, 10, 9, 7, 30), {"light.lamp": {"state": "on"}, "binary_sensor.win": {"state": "on"}})
    out = run(b.build([TV, FRIDGE]))
    assert out["date"] == "2026-10-09" and out["label"] == "Friday 9 October" and out["errors"] == {}
    n = out["night"]
    assert n["label"] == "22:00–07:00" and n["sensors"] == 2
    assert [(e["name"], e["state"]) for e in n["events"]] == [("Front door", "open"), ("Front door", "closed")]
    assert n["doors"] == [{"entity_id": "binary_sensor.door", "name": "Front door", "opens": 1}]
    assert out["weather"]["temperature"] == 9 and out["weather"]["tonight"] == "Cold night ahead (1°)"
    e = out["energy"]
    assert e["date"] == "2026-10-08" and e["available"] and e["kwh"] == 2.4 and e["cost_p"] == 60.0 and e["prev_kwh"] == 2.4
    assert e["top"] == [{"entity_id": "switch.tv", "name": "Telly", "kwh": 2.4, "cost_p": 60.0}] and e["standing_p"] == 50.0
    assert [x["name"] for x in out["left_on"]] == ["Lamp"] and out["open_now"] == [{"entity_id": "binary_sensor.win", "name": "Bedroom window"}]
    calls = len(ha.calls)
    run(b.build([TV, FRIDGE]))
    assert len(ha.calls) == calls  # door log cached for a minute, yesterday already rolled up


def test_brief_part_failing_keeps_the_rest(tmp_path):
    class Flaky(FakeHistory):
        async def history(self, start, end, ids, attributes=False):
            if any(i.startswith("binary_sensor") for i in ids):
                raise RuntimeError("history down")
            return await super().history(start, end, ids, attributes)
    b, _ = make_brief(tmp_path, ts(2026, 10, 9, 8), ha=Flaky({"sensor.tv_power": [(ts(2026, 10, 1), "100", {})]}))
    out = run(b.build([TV]))
    assert out["night"] is None and out["errors"] == {"night": "history down"}
    assert out["energy"]["kwh"] == 2.4 and out["weather"]["available"]


# ---------------- endpoints ----------------
def test_endpoints(client, fake_ha):
    rollup = client.app.state.energy_rollup
    assert rollup._task is not None and not rollup._task.done()  # the hourly rollup runs with the app
    start = (datetime.now(timezone.utc) - timedelta(days=70)).isoformat()
    fake_ha.history = [[{"entity_id": "sensor.fan_current_consumption", "state": "100", "last_changed": start}]]
    b = client.get("/api/brief")
    assert b.status_code == 200
    body = b.json()
    assert body["energy"]["available"] and body["energy"]["kwh"] == 2.4 and body["energy"]["top"][0]["name"] == "Fan"
    assert {x["entity_id"] for x in body["left_on"]} == {"light.kitchen_1", "switch.kettle"}  # STATES: kitchen light, kettle on
    assert body["open_now"][0]["name"] == "Front door"
    rep = client.get("/api/energy/report")
    assert rep.status_code == 200 and rep.json()["rows"][0]["entity_id"] == "switch.fan"
    assert client.get("/api/energy/report?month=1999-01").status_code == 400
    assert client.get("/api/energy/report?month=junk").status_code == 400
    import base64
    client.headers["Authorization"] = "Basic " + base64.b64encode(b"aaron:wrong").decode()
    assert client.get("/api/brief").status_code == 401 and client.get("/api/energy/report").status_code == 401
