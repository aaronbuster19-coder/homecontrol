import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.discovery import Device
from backend.history import (History, RangeError, by_entity, build, check_range, door_log, downsample, energy_kwh,
                             local_midnight, ms, numeric_state, runs, samples, segments, state_of, attr)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
H = 3_600_000


def iso(dt):
    return dt.isoformat()


def row(dt, state, **attrs):
    r = {"state": state, "last_changed": iso(dt)}
    if attrs:
        r["attributes"] = attrs
        r["last_updated"] = iso(dt)
    return r


def test_check_range():
    assert check_range("7d") == "7d"
    with pytest.raises(RangeError):
        check_range("1y")
    with pytest.raises(RangeError):
        check_range("30d", ("24h", "7d"))


def test_raw_steps_and_gap_on_unavailable():
    t0 = NOW - timedelta(hours=3)
    rows = [row(t0, "10"), row(t0 + timedelta(hours=1), "unavailable"), row(t0 + timedelta(hours=2), "20")]
    segs = segments(samples(rows, numeric_state), ms(t0), ms(NOW))
    assert segs == [(ms(t0), ms(t0) + H, 10.0), (ms(t0) + H, ms(t0) + 2 * H, None), (ms(t0) + 2 * H, ms(NOW), 20.0)]
    pts = downsample(segs, ms(t0), ms(NOW))
    assert pts == [[ms(t0), 10.0], [ms(t0) + H, 10.0], [ms(t0) + H, None], [ms(t0) + 2 * H, 20.0], [ms(NOW), 20.0]]


def test_bucket_means_time_weighted_and_capped():
    start, end = 0, 1000 * 600
    segs = [(i * 1000, (i + 1) * 1000, float(i % 2) * 100) for i in range(600)]  # 600 alternating 0/100 W segments
    pts = downsample(segs, start, end, n=300)
    assert len(pts) == 300
    assert all(p[1] == 50.0 for p in pts)


def test_bucket_gaps():
    segs = [(0, 400, 5.0), (400, 800, None), (800, 1000, 7.0)] + [(1000 + i, 1001 + i, 1.0) for i in range(400)]
    pts = downsample(segs, 0, 1400, n=14)
    vals = [p[1] for p in pts]
    assert vals[:4] == [5.0] * 4 and vals[4] is None and 7.0 in vals
    assert sum(v is None for v in vals) == 1  # one break marker, not one per empty bucket


def test_runs_merge_and_drop_gaps():
    segs = [(0, 10, "on"), (10, 20, "on"), (20, 30, None), (30, 40, "off")]
    assert runs(segs) == [{"state": "on", "start": 0, "end": 20}, {"state": "off", "start": 30, "end": 40}]


def test_runs_capped():
    segs = [(i * 10, i * 10 + 10, "on" if i % 2 else "off") for i in range(2000)]
    segs[1000] = (10000, 10010 + 5000, "on")  # one long run among many blips
    out = runs(sorted(segs), n=300)
    assert len(out) <= 300 and any(r["end"] - r["start"] >= 5000 for r in out)


def test_energy_integration():
    segs = [(0, H, 100.0), (H, 2 * H, None), (2 * H, 3 * H, 1000.0)]
    assert energy_kwh(segs) == pytest.approx(1.1)


def test_valve_attributes_parsed():
    t0 = NOW - timedelta(hours=24)
    rows = [row(t0, "heat", current_temperature=17.5, temperature=21),
            row(t0 + timedelta(hours=1), "heat", current_temperature="19", temperature=21),
            row(t0 + timedelta(hours=2), "off", current_temperature=20.5, temperature=None),
            row(t0 + timedelta(hours=3), "unavailable")]
    rows[0]["entity_id"] = "climate.v"
    dev = Device("climate.v", "valve", "V", "KE100")
    out = build(dev, "24h", ms(t0), ms(NOW), by_entity([rows]))
    cur, tgt = out["series"]
    assert [p[1] for p in cur["points"]] == [17.5, 19.0, 20.5, 20.5, None]  # smooth line through samples
    assert tgt["step"] and [p[1] for p in tgt["points"]] == [21.0, 21.0, None]
    assert cur["unit"] == "°C"


def test_plug_build_energy_and_timeline():
    t0 = NOW - timedelta(hours=24)
    plug = [dict(row(t0, "on"), entity_id="switch.fan"), row(t0 + timedelta(hours=12), "off")]
    power = [dict(row(t0, "100"), entity_id="sensor.p"), row(t0 + timedelta(hours=12), "0")]
    dev = Device("switch.fan", "plug", "Fan", "P110", {"power": "sensor.p"})
    out = build(dev, "24h", ms(t0), ms(NOW), by_entity([plug, power]), {"energy_today": 0.9})
    assert out["energy_kwh"] == 1.2 and out["energy_today_kwh"] == 0.9
    assert [r["state"] for r in out["timeline"]] == ["on", "off"]
    assert out["series"][0]["unit"] == "W"


def door(rows):
    return [{"state": s, "last_changed": iso(t)} for t, s in rows]


def test_door_log_pairing_and_summary():
    start = NOW - timedelta(hours=24)
    rows = door([(start, "off"), (NOW - timedelta(hours=5), "on"), (NOW - timedelta(hours=5, minutes=-3), "off"),
                 (NOW - timedelta(hours=2), "unavailable"), (NOW - timedelta(hours=1), "on"),
                 (NOW - timedelta(minutes=50), "off")])
    out = door_log(rows, ms(start), ms(NOW), ms(NOW.replace(hour=0)))
    ev = out["events"]
    assert [e["state"] for e in ev] == ["closed", "open", "closed", "open"]  # newest first; unavailable skipped
    assert ev[0]["open_ms"] == 10 * 60_000 and ev[2]["open_ms"] == 3 * 60_000
    assert out["summary"] == {"opens_today": 2, "longest_open_ms": 600_000, "open_since": None, "open_since_before_range": False}
    assert out["state"] == "closed"


def test_door_still_open_and_across_midnight():
    start = NOW - timedelta(hours=24)
    midnight = NOW.replace(hour=0)
    rows = door([(start, "off"), (midnight - timedelta(minutes=30), "on"), (midnight + timedelta(minutes=30), "off"),
                 (NOW - timedelta(minutes=20), "on")])
    out = door_log(rows, ms(start), ms(NOW), ms(midnight))
    s = out["summary"]
    assert s["opens_today"] == 1  # the one that opened before midnight belongs to yesterday
    assert out["events"][1]["open_ms"] == 3_600_000  # closed after midnight: paired across the day boundary
    assert s["open_since"] == ms(NOW - timedelta(minutes=20)) and s["longest_open_ms"] == 3_600_000
    assert out["state"] == "open"


def test_door_open_since_before_range():
    start = NOW - timedelta(hours=24)
    out = door_log(door([(start, "on")]), ms(start), ms(NOW), ms(NOW.replace(hour=0)))
    assert out["events"] == [] and out["summary"]["open_since_before_range"] is True
    assert out["summary"]["longest_open_ms"] == 24 * H


def test_local_midnight():
    assert local_midnight(NOW, "UTC") == ms(NOW.replace(hour=0))
    assert local_midnight(datetime(2026, 10, 8, 23, 30, tzinfo=timezone.utc), "Europe/London") == ms(datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc))
    assert local_midnight(NOW, "Not/AZone") is not None


class CountingHA:
    def __init__(self):
        self.calls = 0

    async def history(self, start, end, ids, attributes=False):
        self.calls += 1
        return []


def test_cache_ttl():
    ha, clock = CountingHA(), [0.0]
    h = History(ha, clock=lambda: clock[0])
    dev = Device("light.a", "light", "A", "L530")
    run = lambda rng: asyncio.run(h.device(dev, rng))
    run("24h"); run("24h")
    assert ha.calls == 1
    clock[0] = 61; run("24h")
    assert ha.calls == 2
    run("7d"); clock[0] = 61 + 500; run("7d")
    assert ha.calls == 3
    clock[0] = 61 + 601; run("7d")
    assert ha.calls == 4


# ---- endpoints ----
def hist_requests(fake_ha):
    return [r for r in fake_ha.requests if r.url.path.startswith("/api/history/period/")]


def test_plug_endpoint_requests_power_entity(client, fake_ha):
    t = datetime.now(timezone.utc) - timedelta(hours=1)
    fake_ha.history = [[dict(row(t, "on"), entity_id="switch.fan")],
                       [dict(row(t, "50"), entity_id="sensor.fan_current_consumption")]]
    r = client.get("/api/history/switch.fan?range=24h")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "plug" and body["series"][0]["points"][0][1] == 50.0
    assert body["energy_kwh"] == pytest.approx(0.05, abs=0.002) and body["energy_today_kwh"] == 0.153
    req = hist_requests(fake_ha)[0]
    q = req.url.params
    assert q["filter_entity_id"] == "switch.fan,sensor.fan_current_consumption"
    assert "minimal_response" in q and "no_attributes" in q and "end_time" in q
    start = datetime.fromisoformat(req.url.path.rsplit("/", 1)[1])
    end = datetime.fromisoformat(q["end_time"])
    assert end - start == timedelta(hours=24)
    client.get("/api/history/switch.fan?range=24h")
    assert len(hist_requests(fake_ha)) == 1  # cached


def test_valve_endpoint_requests_attributes(client, fake_ha):
    t = datetime.now(timezone.utc) - timedelta(days=2)
    fake_ha.history = [[dict(row(t, "heat", current_temperature=19, temperature=21), entity_id="climate.lounge_valve")]]
    body = client.get("/api/history/climate.lounge_valve?range=7d").json()
    assert [s["name"] for s in body["series"]] == ["Current", "Target"]
    q = hist_requests(fake_ha)[0].url.params
    assert q["filter_entity_id"] == "climate.lounge_valve"
    assert "no_attributes" not in q and "minimal_response" not in q


def test_history_errors(client, fake_ha):
    assert client.get("/api/history/switch.fan?range=1y").status_code == 400
    assert client.get("/api/history/light.nope").status_code == 404
    fake_ha.history = None
    assert client.get("/api/history/light.kitchen_1?range=30d").status_code == 502


def test_door_log_endpoint(client, fake_ha):
    now = datetime.now(timezone.utc)
    fake_ha.history = [[{"entity_id": "binary_sensor.contact_sensor_door", "state": "off", "last_changed": iso(now - timedelta(hours=24))},
                        {"state": "on", "last_changed": iso(now - timedelta(minutes=10))},
                        {"state": "off", "last_changed": iso(now - timedelta(minutes=7))}]]
    r = client.get("/api/doors/log?range=24h&tz=Europe/London")
    assert r.status_code == 200, r.text
    d = r.json()["doors"][0]
    assert d["name"] == "Front door" and [e["state"] for e in d["events"]] == ["closed", "open"]
    assert d["events"][0]["open_ms"] == 180_000
    assert hist_requests(fake_ha)[0].url.params["filter_entity_id"] == "binary_sensor.contact_sensor_door"
    assert client.get("/api/doors/log?range=30d").status_code == 400
