import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.discovery import Device
from backend.energy import (Energy, EnergyError, cost_p, night_windows, range_start, standing_total, window_mean,
                            yearly)
from backend.store import LayoutError, validate_energy, validate_layout
from backend.tests.test_summary import FakeHistory

LON = ZoneInfo("Europe/London")
H = 3600


def lon(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LON)


def t(y, mo, d, h=0, mi=0):
    return lon(y, mo, d, h, mi).timestamp()


PLUG = Device("switch.tv", "plug", "TV", "P110", {"power": "sensor.tv_power"})
FRIDGE = Device("switch.fridge", "plug", "Fridge", "P110", {"power": "sensor.fridge_power", "energy_today": "sensor.fridge_today"})
LAYOUT = {"settings": {"keep_on": [], "energy": {"rate_p": 24.5, "standing_p": 60.1}}}


# ---------------- maths ----------------
def test_cost_rounding_and_unknowns():
    assert cost_p(0.42, 24.5) == 10.29
    assert cost_p(1.0, 24.5) == 24.5
    assert cost_p(0.153, 24.5) == 3.75   # 3.7485 -> 3.75
    assert cost_p(0, 24.5) == 0
    assert cost_p(None, 24.5) is None and cost_p(1.0, None) is None


def test_yearly_standby():
    kwh, p = yearly(5.0, 24.5)  # 5 W always on: 43.8 kWh, £10.73 a year
    assert kwh == 43.8 and p == 1073.1
    assert yearly(5.0, None) == (43.8, None)


def test_tariff_validation():
    assert validate_energy({"rate_p": 24.567, "standing_p": 60.1}) == {"rate_p": 24.57, "standing_p": 60.1}
    assert validate_energy({}) == {"rate_p": None, "standing_p": None}
    assert validate_energy({"rate_p": "", "standing_p": None}) == {"rate_p": None, "standing_p": None}
    for bad in ({"rate_p": -1}, {"rate_p": 200.01}, {"rate_p": "abc"}, {"rate_p": True}, {"standing_p": 501}, []):
        with pytest.raises(LayoutError):
            validate_energy(bad)
    out = validate_layout({"rooms": [], "placements": [], "settings": {"energy": {"rate_p": 30}}}, set())
    assert out["settings"]["energy"] == {"rate_p": 30.0, "standing_p": None}


def test_range_boundaries_local_and_standing_days():
    now = lon(2026, 10, 9, 0, 30)  # Fri 9 Oct 00:30 BST = Thu 8 Oct 23:30 UTC: already "today" locally
    assert range_start("today", now, LON) == (lon(2026, 10, 9), 1)
    assert range_start("week", now, LON) == (lon(2026, 10, 5), 5)      # Monday
    assert range_start("month", now, LON) == (lon(2026, 10, 1), 9)
    assert range_start("month", lon(2026, 11, 1, 9), LON) == (lon(2026, 11, 1), 1)  # first of the month
    assert range_start("week", lon(2026, 11, 1, 9), LON) == (lon(2026, 10, 26), 7)  # Sunday: week spans months
    assert standing_total(60.1, 9) == 540.9 and standing_total(None, 9) is None
    with pytest.raises(EnergyError):
        range_start("year", now, LON)


def test_range_boundaries_across_dst():
    # Clocks went back on Sun 25 Oct 2026: 1 Oct 00:00 is BST (23:00 UTC), 26 Oct 00:00 is GMT.
    start, days = range_start("month", lon(2026, 10, 26, 12), LON)
    assert start.timestamp() == datetime(2026, 9, 30, 23, tzinfo=timezone.utc).timestamp() and days == 26
    start, _ = range_start("today", lon(2026, 10, 26, 12), LON)
    assert start.timestamp() == datetime(2026, 10, 26, 0, tzinfo=timezone.utc).timestamp()


def test_night_windows_and_dst_nights():
    w = night_windows(lon(2026, 10, 9, 12), LON)
    assert len(w) == 7 and w[0] == (t(2026, 10, 9, 1) * 1000, t(2026, 10, 9, 5) * 1000)  # this morning counts
    assert night_windows(lon(2026, 10, 9, 4), LON)[0][0] == t(2026, 10, 8, 1) * 1000     # tonight isn't over yet
    autumn = dict((datetime.fromtimestamp(a / 1000, LON).date().day, (b - a) / 3_600_000)
                  for a, b in night_windows(lon(2026, 10, 27, 12), LON))
    assert autumn[25] == 5 and autumn[24] == 4                       # 01:00 BST -> 05:00 GMT is 5 real hours
    spring = dict((datetime.fromtimestamp(a / 1000, LON).date().day, (b - a) / 3_600_000)
                  for a, b in night_windows(lon(2026, 3, 30, 12), LON))
    assert spring[29] == 3                                          # 01:00 GMT -> 05:00 BST is 3


def test_window_mean_skips_gaps():
    segs = [(0, 10, 5.0), (10, 20, None), (20, 30, 15.0)]
    assert window_mean(segs, [(0, 30)]) == (10.0, 20)
    assert window_mean(segs, [(10, 20)]) == (None, 0)
    assert window_mean(segs, [(5, 10), (25, 40)]) == (10.0, 10)


# ---------------- from history ----------------
def run(coro):
    return asyncio.run(coro)


def tv_history():
    # 100 W since well before the month; this week (from Mon 5 Oct) 200 W; today (Fri 9 Oct) 50 W from 08:00
    return FakeHistory({"sensor.tv_power": [(t(2026, 9, 1), "100", {}), (t(2026, 10, 5), "200", {}),
                                            (t(2026, 10, 9), "0", {}), (t(2026, 10, 9, 8), "50", {})]})


def test_totals_today_week_month():
    ha = tv_history()
    e = Energy(ha, LON)
    now = lon(2026, 10, 9, 12)
    today = run(e.totals([PLUG], {}, LAYOUT, "today", now))
    assert today["plugs"][0]["kwh"] == 0.2 and today["plugs"][0]["cost_p"] == 4.9 and today["plugs"][0]["source"] == "history"
    assert today["total_kwh"] == 0.2 and today["total_p"] == 4.9 and today["days"] == 1 and today["standing_total_p"] == 60.1
    week = run(e.totals([PLUG], {}, LAYOUT, "week", now))
    assert week["plugs"][0]["kwh"] == pytest.approx(4 * 24 * 0.2 + 0.2) and week["days"] == 5  # Mon–Thu at 200 W
    assert week["standing_total_p"] == 300.5
    month = run(e.totals([PLUG], {}, LAYOUT, "month", now))
    assert month["plugs"][0]["kwh"] == pytest.approx(4 * 24 * 0.1 + 4 * 24 * 0.2 + 0.2)   # 1–4 Oct at 100 W
    assert month["total_p"] == round(month["total_kwh"] * 24.5, 2)
    assert len(ha.calls) == 2  # one fetch for the finished days, one for today; all ranges share them
    run(e.totals([PLUG], {}, LAYOUT, "week", now))
    assert len(ha.calls) == 2


def test_totals_use_the_plug_meter_for_today_and_sort_by_cost():
    ha = FakeHistory({"sensor.tv_power": [(t(2026, 9, 1), "100", {})],
                      "sensor.fridge_power": [(t(2026, 9, 1), "10", {})]})
    e = Energy(ha, LON)
    items = {"switch.fridge": {"energy_today": 0.5}}
    r = run(e.totals([FRIDGE, PLUG], items, LAYOUT, "week", lon(2026, 10, 9, 12)))
    assert [p["name"] for p in r["plugs"]] == ["TV", "Fridge"]
    fridge = r["plugs"][1]
    assert fridge["source"] == "meter" and fridge["kwh"] == pytest.approx(4 * 24 * 0.01 + 0.5)


def test_totals_without_rate():
    r = run(Energy(tv_history(), LON).totals([PLUG], {}, {}, "today", lon(2026, 10, 9, 12)))
    assert r["rate_p"] is None and r["total_p"] is None and r["plugs"][0]["cost_p"] is None and r["standing_total_p"] is None


def test_totals_month_across_dst():
    ha = FakeHistory({"sensor.tv_power": [(t(2026, 9, 1), "1000", {})]})
    r = run(Energy(ha, LON).totals([PLUG], {}, {}, "month", lon(2026, 10, 26, 12)))
    assert r["total_kwh"] == pytest.approx(25 * 24 + 1 + 12)  # 25 Oct had 25 hours
    assert r["days"] == 26


def test_standby_overnight_average_with_gaps():
    series = [(t(2026, 9, 25), "5", {})]
    # Night of 3 Oct: unavailable 01:00–03:00 (gap is skipped, not counted as 0 W)
    series += [(t(2026, 10, 3, 1), "unavailable", {}), (t(2026, 10, 3, 3), "5", {})]
    # Night of 6 Oct: 25 W from 03:00 to 05:00
    series += [(t(2026, 10, 6, 3), "25", {}), (t(2026, 10, 6, 5), "5", {})]
    ha = FakeHistory({"sensor.tv_power": series, "sensor.fridge_power": [(t(2026, 10, 8, 2), "60", {})]})
    r = run(Energy(ha, LON).standby([PLUG, FRIDGE], LAYOUT, lon(2026, 10, 9, 12)))
    assert r["nights"] == 7 and r["from"] == "01:00" and r["to"] == "05:00"
    tv = next(p for p in r["plugs"] if p["entity_id"] == "switch.tv")
    # covered: 7 nights × 4 h minus the 2 h gap = 26 h; 24 h at 5 W + 2 h at 25 W
    assert tv["coverage_h"] == 26.0 and tv["avg_w"] == round((24 * 5 + 2 * 25) / 26, 1)
    assert tv["year_kwh"] == round((24 * 5 + 2 * 25) / 26 * 8.76, 1)  # W × 24 × 365 / 1000
    fridge = next(p for p in r["plugs"] if p["entity_id"] == "switch.fridge")
    assert fridge["avg_w"] == 60 and fridge["coverage_h"] == 7.0  # only data from 8 Oct 02:00: 3 h + 4 h
    assert r["plugs"][0]["entity_id"] == "switch.fridge"  # biggest standby first


def test_standby_needs_an_hour_of_data():
    ha = FakeHistory({"sensor.tv_power": [(t(2026, 10, 9, 4, 30), "8", {})]})
    tv = run(Energy(ha, LON).standby([PLUG], LAYOUT, lon(2026, 10, 9, 12)))["plugs"][0]
    assert tv["avg_w"] is None and tv["year_p"] is None and tv["coverage_h"] == 0.5


# ---------------- endpoints ----------------
def test_energy_settings_endpoint(client):
    assert client.get("/api/energy/settings").json() == {"rate_p": None, "standing_p": None}
    r = client.put("/api/energy/settings", json={"rate_p": 24.5, "standing_p": 60.1})
    assert r.status_code == 200 and r.json() == {"rate_p": 24.5, "standing_p": 60.1}
    assert client.get("/api/layout").json()["settings"]["energy"] == {"rate_p": 24.5, "standing_p": 60.1}
    assert client.put("/api/energy/settings", json={"rate_p": 250}).status_code == 400
    # a layout PUT from an older client (no energy key) keeps the tariff
    L = client.get("/api/layout").json()
    del L["settings"]["energy"]
    assert client.put("/api/layout", json=L).json()["settings"]["energy"]["rate_p"] == 24.5


def test_energy_endpoints_with_mocked_history(client, fake_ha):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=40)).isoformat()
    fake_ha.history = [[{"entity_id": "sensor.fan_current_consumption", "state": "100", "last_changed": start}],
                       [{"entity_id": "sensor.kettle_current_consumption", "state": "unavailable", "last_changed": start}]]
    client.put("/api/energy/settings", json={"rate_p": 24.5, "standing_p": 60})
    r = client.get("/api/energy?range=today").json()
    names = [p["name"] for p in r["plugs"]]
    assert names == ["Fan", "Kettle"]
    fan = r["plugs"][0]
    assert fan["kwh"] == 0.153 and fan["source"] == "meter" and fan["cost_p"] == 3.75  # HA's today's-energy sensor
    assert r["plugs"][1]["kwh"] == 0 and r["standing_total_p"] == 60 and r["days"] == 1
    month = client.get("/api/energy?range=month").json()
    tz = ZoneInfo("Europe/London")
    loc = now.astimezone(tz)
    first = datetime(loc.year, loc.month, 1, tzinfo=tz)
    mid = datetime(loc.year, loc.month, loc.day, tzinfo=tz)
    assert month["plugs"][0]["kwh"] == pytest.approx((mid - first).total_seconds() / 3600 * 0.1 + 0.153, abs=1e-3)
    assert month["days"] == loc.day and month["standing_total_p"] == 60 * loc.day
    assert client.get("/api/energy?range=year").status_code == 400
    sb = client.get("/api/energy/standby").json()
    fan_sb = next(p for p in sb["plugs"] if p["entity_id"] == "switch.fan")
    assert fan_sb["avg_w"] == 100 and fan_sb["year_kwh"] == 876.0 and fan_sb["year_p"] == 21462.0
    assert next(p for p in sb["plugs"] if p["entity_id"] == "switch.kettle")["avg_w"] is None
