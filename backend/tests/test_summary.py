import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend.alerts import DEFAULT_SETTINGS
from backend.automations import AutoStore
from backend.discovery import Device
from backend.discovery import apply_names
from backend.summary import WeeklySummary, build_summary, summary_text, week_window

LON = ZoneInfo("Europe/London")
H = 3600


def ts(y, mo, d, h=0, mi=0, tz=LON):
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def test_week_window_and_dst():
    # Sun 11 Oct 2026 (BST): due 19:00 local = 18:00 UTC, catch-up until Mon 12:00 local
    week, sunday, due, deadline = week_window(ts(2026, 10, 11, 19), LON)
    assert week == "2026-W41" and str(sunday) == "2026-10-11"
    assert due == ts(2026, 10, 11, 18, tz=timezone.utc) and deadline == ts(2026, 10, 12, 11, tz=timezone.utc)
    assert week_window(ts(2026, 10, 11, 18, 59), LON)[0] == "2026-W40"  # a minute early: still last week's
    assert week_window(ts(2026, 10, 14, 9), LON)[0] == "2026-W41"
    # Sun 25 Oct 2026: clocks went back at 02:00 that morning -> 19:00 GMT = 19:00 UTC
    assert week_window(ts(2026, 10, 25, 20), LON)[2] == ts(2026, 10, 25, 19, tz=timezone.utc)
    # Sun 29 Mar 2026: clocks went forward -> 19:00 BST = 18:00 UTC
    assert week_window(ts(2026, 3, 29, 20), LON)[2] == ts(2026, 3, 29, 18, tz=timezone.utc)
    # Year boundary: Sun 3 Jan 2027 belongs to ISO week 2026-W53
    assert week_window(ts(2027, 1, 3, 19), LON)[0] == "2026-W53"


class FakeHistory:
    """HA /api/history/period semantics: per entity, the state at `start` first, then changes up to `end`."""

    def __init__(self, series):
        self.series, self.calls = series, []  # entity -> [(t, state, attrs)]

    async def history(self, start, end, ids, attributes=False):
        self.calls.append((start, end, tuple(ids), attributes))
        s, e = start.timestamp(), end.timestamp()
        out = []
        for eid in ids:
            pts = sorted(self.series.get(eid, []))
            before = [p for p in pts if p[0] <= s]
            rows = ([(s, before[-1][1], before[-1][2])] if before else []) + [p for p in pts if s < p[0] < e]
            if rows:
                out.append([{"entity_id": eid, "state": st, "attributes": a if attributes else {},
                             "last_changed": iso(t), "last_updated": iso(t)} for t, st, a in rows])
        return out


PLUG = Device("switch.kettle", "plug", "Kettle", "P110", {"power": "sensor.kettle_power"})
PLUG2 = Device("switch.fan", "plug", "Fan", "P110", {"power": "sensor.fan_power"})
DOOR = Device("binary_sensor.contact_sensor_door", "sensor", "Front door", "T110")
DOOR2 = Device("binary_sensor.contact_sensor_door_2", "sensor", "Back door", "T110")
VALVE = Device("climate.lounge", "valve", "Lounge valve", "KE100")
DEVS = {d.entity_id: d for d in (PLUG, PLUG2, DOOR, DOOR2, VALVE)}
LAYOUT = {"rooms": [{"id": "l", "name": "Lounge", "x": 0, "y": 0, "w": 4, "h": 4}, {"id": "k", "name": "Kitchen", "x": 4, "y": 0, "w": 2, "h": 2}],
          "placements": [{"entity_id": "climate.lounge", "x": 1, "y": 1}], "openings": []}
SUN = ts(2026, 10, 11, 19)
START, PREV = SUN - 7 * 24 * H, SUN - 14 * 24 * H


def fake_history():
    return FakeHistory({
        "sensor.kettle_power": [(PREV - H, "50", {}), (START, "100", {})],     # 8.4 kWh last week, 16.8 this week
        "sensor.fan_power": [(PREV - H, "0", {}), (START + 24 * H, "unavailable", {}), (START + 48 * H, "10", {}),
                             (START + 49 * H, "0", {})],                         # 0.01 kWh this week
        DOOR.entity_id: [(PREV - H, "off", {}), (PREV + H, "on", {}), (PREV + 2 * H, "off", {}),  # last week: not counted
                         (START + H, "on", {}), (START + 2 * H, "off", {}), (START + 3 * H, "on", {}),
                         (START + 4 * H, "unavailable", {}), (START + 5 * H, "on", {}),  # dropout isn't a new opening
                         (START + 6 * H, "off", {}), (SUN - H, "on", {})],      # 3 opens this week
        DOOR2.entity_id: [(PREV - H, "on", {})],  # open the whole time: 0 opens
        "climate.lounge": [(PREV, "heat", {"current_temperature": 18}), (START, "heat", {"current_temperature": 20}),
                           (START + 84 * H, "heat", {"current_temperature": 22})],  # half 20, half 22 -> 21
    })


def test_numbers_from_history():
    ha = fake_history()
    s = asyncio.run(build_summary(ha, DEVS, LAYOUT, START, SUN))
    assert s["energy"] == {"this_kwh": 16.81, "last_kwh": 8.4, "change_pct": 100, "rate_p": None, "this_p": None, "last_p": None}
    assert [(p["name"], p["kwh"]) for p in s["plugs"]] == [("Kettle", 16.8), ("Fan", 0.01)]
    assert s["biggest_plug"]["name"] == "Kettle"
    assert [(d["name"], d["opens"]) for d in s["doors"]] == [("Front door", 3), ("Back door", 0)]
    assert s["top_door"]["name"] == "Front door"
    assert s["rooms"] == [{"id": "l", "name": "Lounge", "avg_temp": 21.0}]  # Kitchen has no valve
    assert s["text"] == "16.8 kWh (+100% vs last week) · Kettle used most · Front door opened 3× · Lounge 21.0°"
    assert len(ha.calls) == 2 and ha.calls[1][3] is True  # valves with attributes, only this week
    empty = asyncio.run(build_summary(FakeHistory({}), {}, {"rooms": []}, START, SUN))
    assert empty["energy"]["this_kwh"] is None and empty["text"] == "Not much to report this week."


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def rig(tmp_path, t, store=None, **settings):
    clock, pushes = Clock(t), []
    store = store or AutoStore(str(tmp_path / "s.db"))
    st = {**DEFAULT_SETTINGS, **settings}

    async def notify(p):
        pushes.append(p)
    ws = WeeklySummary(store, lambda: st, fake_history(), lambda: DEVS, lambda: LAYOUT, notify, clock, LON)
    return ws, clock, pushes, store


def test_sends_sunday_once(tmp_path):
    ws, clock, pushes, store = rig(tmp_path, SUN - 60)
    asyncio.run(ws.tick())
    assert pushes == [] and store.latest_summary() is None
    clock.t = SUN
    asyncio.run(ws.tick())
    assert len(pushes) == 1 and pushes[0]["url"] == "/?summary" and pushes[0]["title"] == "Your week at home"
    latest = store.latest_summary()
    assert latest["week"] == "2026-W41" and latest["start"] == START * 1000 and latest["end"] == SUN * 1000
    clock.t += 3600
    asyncio.run(ws.tick())
    ws2, _, pushes2, _ = rig(tmp_path, SUN + 2 * H, store=store)  # restart
    asyncio.run(ws2.tick())
    assert len(pushes) == 1 and pushes2 == []


def test_catch_up_until_monday_noon(tmp_path):
    ws, clock, pushes, store = rig(tmp_path, ts(2026, 10, 12, 11, 59))
    asyncio.run(ws.tick())
    assert len(pushes) == 1 and store.latest_summary()["end"] == SUN * 1000  # covers the week up to Sunday 19:00
    ws, clock, pushes, store = rig(tmp_path / "b", ts(2026, 10, 12, 12, 0))
    asyncio.run(ws.tick())
    assert pushes == []  # too late: skipped, wait for next Sunday


def test_dst_sunday_and_switch(tmp_path):
    sun = ts(2026, 10, 25, 19)
    ws, clock, pushes, store = rig(tmp_path, sun - 1)
    asyncio.run(ws.tick())
    assert pushes == []
    clock.t = sun
    asyncio.run(ws.tick())
    assert len(pushes) == 1 and store.latest_summary()["week"] == "2026-W43"
    assert store.latest_summary()["start"] == ts(2026, 10, 18, 19) * 1000  # 7 local days, 169 real hours
    off, clock2, pushes2, _ = rig(tmp_path / "off", SUN, weekly_summary=False)
    asyncio.run(off.tick())
    assert pushes2 == []


def test_failed_build_retries_later(tmp_path):
    ws, clock, pushes, store = rig(tmp_path, SUN)

    class Down:
        n = 0

        async def history(self, *a, **k):
            Down.n += 1
            raise RuntimeError("HA down")
    ws.ha = Down()
    asyncio.run(ws.tick())
    clock.t += 60
    asyncio.run(ws.tick())
    assert Down.n == 1 and pushes == []
    ws.ha = fake_history()
    clock.t += 300
    asyncio.run(ws.tick())
    assert len(pushes) == 1


def test_preview_not_stored(tmp_path):
    ws, clock, pushes, store = rig(tmp_path, SUN + 3 * H)
    p = asyncio.run(ws.preview())
    assert p["preview"] and p["end"] == (SUN + 3 * H) * 1000 and pushes == [] and store.latest_summary() is None


def test_cost_line_when_a_rate_is_set():
    layout = {**LAYOUT, "settings": {"keep_on": [], "energy": {"rate_p": 24.5, "standing_p": 60}}}
    s = asyncio.run(build_summary(fake_history(), DEVS, layout, START, SUN))
    e = s["energy"]
    assert e["rate_p"] == 24.5 and e["this_p"] == 411.85 and e["last_p"] == 205.8  # from unrounded kWh
    # 16.81 kWh × 24.5p = £4.12; last week 8.4 × 24.5 = £2.06 -> +£2.06 (standing charge not included)
    assert s["text"] == ("16.8 kWh (+100% vs last week) · This week ≈ £4.12 (+£2.06 vs last week) · Kettle used most · "
                         "Front door opened 3× · Lounge 21.0°")
    cheaper = summary_text({**s, "energy": {**e, "this_p": 50.0, "last_p": 125.0}})
    assert "This week ≈ £0.50 (−£0.75 vs last week)" in cheaper
    # no comparison when last week had (almost) nothing
    assert "This week ≈ £0.50 ·" in summary_text({**s, "energy": {**e, "this_p": 50.0, "last_p": 0.0, "change_pct": None}})


def test_custom_names_in_summary_text():
    renamed = Device("switch.kettle", "plug", "Kettle", "P110", {"power": "sensor.kettle_power"})
    devs = {**DEVS, renamed.entity_id: renamed}
    apply_names(devs, {"names": {"switch.kettle": "Big kettle"}})
    s = asyncio.run(build_summary(fake_history(), devs, LAYOUT, START, SUN))
    assert "Big kettle used most" in s["text"] and s["plugs"][0]["name"] == "Big kettle"
