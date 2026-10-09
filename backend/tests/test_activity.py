import asyncio
import base64
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.activity import (Activity, ActivityStore, acting, attribute, change_text, fail_bursts, ha_events,
                              room_index)
from backend.discovery import Device

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc).timestamp()  # 16:00 BST


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def row(eid, t, state, **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs, "last_changed": iso(t), "last_updated": iso(t)}


def dev(eid, kind, name, hidden=False):
    return Device(eid, kind, name, "", hidden=hidden)


# ---------------- pure parts ----------------
def test_light_events_skip_carried_in_state_and_unavailable():
    d = dev("light.k", "light", "Kitchen light")
    start = NOW - 3600
    rows = [row("light.k", start, "off"), row("light.k", start + 60, "on"), row("light.k", start + 120, "unavailable"),
            row("light.k", start + 180, "on"), row("light.k", start + 240, "off")]
    evs = ha_events(d, rows, int(start * 1000))
    assert [(e["change"], e["t"]) for e in evs] == [("on", int((start + 60) * 1000)), ("off", int((start + 240) * 1000))]
    assert change_text("light", "Kitchen light", evs[0]) == "Kitchen light turned on"


def test_valve_door_person_dehumidifier_events():
    start = NOW - 3600
    v = ha_events(dev("climate.v", "valve", "Lounge radiator"), [
        row("climate.v", start, "heat", temperature=20, current_temperature=19),
        row("climate.v", start + 60, "heat", temperature=20, current_temperature=19.5),   # only current changes
        row("climate.v", start + 120, "heat", temperature=21.5, current_temperature=19.5),
        row("climate.v", start + 180, "off", temperature=21.5)], int(start * 1000))
    assert [(e["change"], e.get("value")) for e in v] == [("target", 21.5), ("off", None)]
    assert change_text("valve", "Lounge radiator", v[0]) == "Lounge radiator set to 21.5°"
    s = ha_events(dev("binary_sensor.d", "sensor", "Front door"), [
        row("binary_sensor.d", start, "off"), row("binary_sensor.d", start + 5, "on"), row("binary_sensor.d", start + 9, "off")],
        int(start * 1000))
    assert [change_text("sensor", "Front door", e) for e in s] == ["Front door opened", "Front door closed"]
    p = ha_events(dev("person.a", "person", "Alex"), [
        row("person.a", start, "home"), row("person.a", start + 5, "not_home"), row("person.a", start + 9, "Work"),
        row("person.a", start + 20, "home")], int(start * 1000))
    assert [change_text("person", "Alex", e) for e in p] == ["Alex left", "Alex is at Work", "Alex came home"]
    h = ha_events(dev("humidifier.d", "dehumidifier", "Dehumidifier"), [
        row("humidifier.d", start, "on", humidity=50), row("humidifier.d", start + 5, "on", humidity=45),
        row("humidifier.d", start + 9, "off", humidity=45)], int(start * 1000))
    assert [change_text("dehumidifier", "Dehumidifier", e) for e in h] == ["Dehumidifier target 45 %", "Dehumidifier turned off"]


def test_media_player_states_fold_to_on_off():
    start = NOW - 3600
    m = ha_events(dev("media_player.tv", "media", "TV"), [
        row("media_player.tv", start, "off"), row("media_player.tv", start + 5, "playing"),
        row("media_player.tv", start + 9, "paused"), row("media_player.tv", start + 20, "standby")], int(start * 1000))
    assert [e["change"] for e in m] == ["on", "off"]


def test_attribution_window_and_one_change_per_call():
    t = NOW
    evs = [{"t": int((t + 1) * 1000), "entity_id": "light.k", "change": "on"},
           {"t": int((t + 3) * 1000), "entity_id": "light.k", "change": "off"},
           {"t": int((t + 30) * 1000), "entity_id": "light.k", "change": "on"},
           {"t": int((t + 2) * 1000), "entity_id": "climate.v", "change": "target", "value": 21}]
    calls = [{"t": t, "service": "toggle", "entity_ids": ["light.k"], "user": "me", "label": None},
             {"t": t + 1.5, "service": "set_temperature", "entity_ids": ["climate.v"], "user": None, "label": "schedule “Morning”"}]
    hit = attribute(evs, calls)
    assert hit == {0, 1}
    assert evs[0]["by"] == "by you"
    assert "by" not in evs[1]  # the toggle already explained the first change
    assert "by" not in evs[2]  # 30 s later: not this call
    assert evs[3]["by"] == "schedule “Morning”"


def test_attribution_needs_a_fitting_service():
    evs = [{"t": int((NOW + 1) * 1000), "entity_id": "light.k", "change": "off"}]
    assert attribute(evs, [{"t": NOW, "service": "turn_on", "entity_ids": ["light.k"], "user": "me"}]) == set()
    assert "by" not in evs[0]


def test_fail_bursts_group_per_address():
    fails = [{"t": 100, "ip": "1.2.3.4"}, {"t": 160, "ip": "1.2.3.4"}, {"t": 200, "ip": "5.6.7.8"},
             {"t": 220, "ip": "1.2.3.4"}, {"t": 2000, "ip": "1.2.3.4"}]
    b = fail_bursts(fails)
    assert [(x["ip"], x["n"]) for x in b] == [("1.2.3.4", 3), ("5.6.7.8", 1), ("1.2.3.4", 1)]


def test_room_index_uses_opening_walls_and_placements():
    layout = {"rooms": [{"id": "hall", "name": "Hall", "x": 0, "y": 0, "w": 2, "h": 2},
                        {"id": "kit", "name": "Kitchen", "x": 2, "y": 0, "w": 2, "h": 2}],
              "placements": [{"entity_id": "light.k", "x": 3, "y": 1}],
              "openings": [{"id": "o", "type": "door", "x": 0, "y": 0.5, "len": 0.9, "orient": "v", "entity_id": "binary_sensor.d"}]}
    idx = room_index(layout)
    assert idx["light.k"]["id"] == "kit" and idx["binary_sensor.d"]["id"] == "hall"


# ---------------- the feed (Activity with a fake HA) ----------------
class HistoryHA:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def history(self, start, end, ids, attributes=False):
        self.calls.append((start, end, tuple(ids), attributes))
        out = {}
        for r in self.rows:
            if r["entity_id"] in ids and start.timestamp() - 1 <= datetime.fromisoformat(r["last_changed"]).timestamp() <= end.timestamp():
                out.setdefault(r["entity_id"], []).append(r)
        return list(out.values())

    async def call_service(self, domain, service, data):
        pass


LAYOUT = {"rooms": [{"id": "kit", "name": "Kitchen", "x": 0, "y": 0, "w": 3, "h": 3},
                    {"id": "hall", "name": "Hall", "x": 3, "y": 0, "w": 3, "h": 3}],
          "placements": [{"entity_id": "light.k", "x": 1, "y": 1}, {"entity_id": "binary_sensor.d", "x": 4, "y": 1}],
          "openings": [], "furniture": []}
DEVS = {"light.k": dev("light.k", "light", "Kitchen light"), "binary_sensor.d": dev("binary_sensor.d", "sensor", "Front door"),
        "light.hidden": dev("light.hidden", "light", "Hidden", hidden=True)}


def make(tmp_path, rows, now=NOW, layout=LAYOUT):
    clock = {"t": now}
    ha = HistoryHA(rows)

    async def devices():
        return DEVS
    a = Activity(ActivityStore(str(tmp_path / "a.db")), ha, devices, lambda: layout, lambda: clock["t"], LONDON)
    return a, ha, clock


def run(coro):
    return asyncio.run(coro)


def test_feed_merges_attributes_and_filters(tmp_path):
    midnight = datetime(2026, 10, 9, tzinfo=LONDON).timestamp()
    rows = [row("light.k", midnight, "off"), row("light.k", NOW - 600 + 1, "on"), row("light.k", NOW - 300, "off"),
            row("binary_sensor.d", midnight, "off"), row("binary_sensor.d", NOW - 100, "on"),
            row("light.hidden", midnight, "off"), row("light.hidden", NOW - 50, "on")]
    a, ha, clock = make(tmp_path, rows)
    call = a.wrap_call(ha.call_service)
    clock["t"] = NOW - 600

    async def by_user():
        from backend.activity import USER
        tok = USER.set("me")
        try:
            await call("light", "toggle", {"entity_id": "light.k"})
        finally:
            USER.reset(tok)
    run(by_user())
    clock["t"] = NOW
    p = run(a.page())
    texts = [(e["text"], e["by"]) for e in p["entries"]]
    assert texts == [("Front door opened", None), ("Kitchen light turned off", "manually / other"),
                     ("Kitchen light turned on", "by you")]
    assert [d["label"] for d in p["days"]] == ["Today"]
    assert p["entries"][2]["entity_id"] == "light.k" and p["entries"][2]["room"] == "kit"
    assert p["next_before"] == int(midnight * 1000)
    assert ha.calls[0][3] is True and "light.hidden" not in ha.calls[0][2]  # one call, with attributes; hidden skipped
    assert [e["text"] for e in run(a.page(room="hall"))["entries"]] == ["Front door opened"]
    assert [e["type"] for e in run(a.page(type_="lights"))["entries"]] == ["lights", "lights"]
    assert run(a.page(type_="security"))["entries"] == []


def test_recent_call_shows_until_history_catches_up(tmp_path):
    a, ha, clock = make(tmp_path, [])
    clock["t"] = NOW - 10
    with acting("All off"):
        run(a.wrap_call(ha.call_service)("light", "turn_off", {"entity_id": ["light.k", "light.unknown"]}))
    clock["t"] = NOW
    e = run(a.page())["entries"]
    assert [(x["text"], x["by"]) for x in e] == [("Kitchen light turned off", "All off")]
    clock["t"] = NOW + 600  # long past and HA never saw a change (already off): nothing to show
    a.cache.clear()
    assert run(a.page())["entries"] == []


def test_paging_back_seven_days_and_cache(tmp_path):
    a, ha, clock = make(tmp_path, [])
    p = run(a.page())
    pages = [p]
    while p["next_before"]:
        p = run(a.page(before=p["next_before"] / 1000))
        pages.append(p)
    assert len(pages) == 7
    assert [pg["days"][0]["label"] for pg in pages[:3]] == ["Today", "Yesterday", "Wed 7 Oct"]
    assert pages[-1]["start"] == int(datetime(2026, 10, 3, tzinfo=LONDON).timestamp() * 1000)
    n = len(ha.calls)
    run(a.page(before=pages[2]["end"] / 1000))  # a past day comes from the cache
    run(a.page())                               # and so does today, for a few seconds
    assert len(ha.calls) == n
    assert run(a.page(before=pages[-1]["start"] / 1000))["entries"] == []
    two = run(a.page(days=2))
    assert [d["label"] for d in two["days"]] == ["Today", "Yesterday"]


def test_history_failure_still_shows_the_apps_own_log(tmp_path):
    a, ha, clock = make(tmp_path, [])

    async def broken(*a, **k):
        raise RuntimeError("HA down")
    ha.history = broken
    clock["t"] = NOW - 3000
    run(a.wrap_call(ha.call_service)("light", "turn_on", {"entity_id": "light.k", "brightness_pct": 40}))
    clock["t"] = NOW
    p = run(a.page())
    assert "HA down" in p["error"]
    assert [(e["text"], e["by"]) for e in p["entries"]] == [("Kitchen light set to 40 %", "by the app")]


def test_appliance_kettle_push_and_sign_in_entries(tmp_path):
    layout = {**LAYOUT, "furniture": [{"id": "f1", "type": "washer", "plug": "switch.w"},
                                      {"id": "f2", "type": "kettle", "plug": "switch.k"}]}
    a, ha, clock = make(tmp_path, [], layout=layout)
    clock["t"] = NOW - 900
    a.record_appliance(layout["furniture"][0], "finished", {"run_start": NOW - 900 - 4320, "finished_at": NOW - 900})
    a.observe({"kind": "plug", "entity_id": "switch.k", "state": "on", "power": 2000})
    clock["t"] = NOW - 700
    a.observe({"kind": "plug", "entity_id": "switch.k", "state": "on", "power": 0.4})
    a.record_push({"title": "Washing finished", "body": "Washing machine ran 1 h 12 min."}, {"sent": 0})
    a.record("push_held", title="Low battery: Front door", body="")
    a.record("login", user="me", ip="1.2.3.4")
    for _ in range(3):
        a.record("login_failed", ip="9.9.9.9")
    clock["t"] = NOW
    e = run(a.page())["entries"]
    got = {(x["type"], x["text"], x["by"], x["detail"]) for x in e}
    assert ("appliances", "Washing finished · 1 h 12 min", None, None) in got
    assert ("appliances", "Kettle boiled · 3 min", None, None) in got
    assert ("alerts", "Notification: Washing finished", "no devices subscribed", "Washing machine ran 1 h 12 min.") in got
    assert ("alerts", "Held for the digest: Low battery: Front door", None, None) in got
    assert ("security", "me signed in", None, "from 1.2.3.4") in got
    assert ("security", "3 failed sign-in attempts", None, "from 9.9.9.9") in got


def test_dedupe_same_event_twice(tmp_path):
    a, ha, clock = make(tmp_path, [])
    clock["t"] = NOW - 60
    a.record("appliance", plug="switch.w", type="washer", name="Washer", event="started")
    a.record("appliance", plug="switch.w", type="washer", name="Washer", event="started")
    clock["t"] = NOW
    assert [x["text"] for x in run(a.page())["entries"]] == ["Washer started"]


# ---------------- through the API ----------------
def auth(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_api_toggle_by_you_then_ha_change(client, fake_ha):
    now = time.time()
    r = client.post("/api/devices/light.kitchen_1/toggle")
    assert r.status_code == 200
    fake_ha.history = [[row("light.kitchen_1", now - 7200, "off"), row("light.kitchen_1", time.time(), "on")]]
    e = client.get("/api/activity").json()["entries"]
    hit = [x for x in e if x["text"] == "Kitchen 1 turned on"]
    assert hit and hit[0]["by"] == "by you" and hit[0]["entity_id"] == "light.kitchen_1" and hit[0]["type"] == "lights"


def test_api_bulk_all_off_and_away_are_named(client, fake_ha):
    fake_ha.history = []
    assert client.post("/api/bulk", json={"action": "turn_off", "entity_ids": ["light.strip"], "source": "all_off"}).status_code == 200
    assert client.post("/api/mode", json={"mode": "away"}).status_code == 200
    e = client.get("/api/activity").json()["entries"]
    got = {(x["text"], x["by"]) for x in e}
    assert ("Strip turned off", "All off") in got
    assert ("Kitchen 1 turned off", "Away mode") in got
    assert ("Lounge valve set to 16°", "Away mode") in got
    assert ("Away mode on", "by you") in got


def test_api_logins_and_validation(client):
    client.headers.pop("Authorization")
    assert client.post("/api/login", json={"username": "aaron", "password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"username": "aaron", "password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"username": "aaron", "password": "s3cret"}).status_code == 200
    e = client.get("/api/activity?type=security").json()["entries"]
    assert [x["text"] for x in e] == ["aaron signed in", "2 failed sign-in attempts"]
    assert client.get("/api/activity?type=nope").status_code == 400
    assert client.get("/api/activity?days=9").status_code == 400


def test_api_test_push_is_logged(client):
    client.post("/api/push/test")
    e = client.get("/api/activity?type=alerts").json()["entries"]
    assert e[0]["text"] == "Notification: homecontrol" and e[0]["by"] == "no devices subscribed"


def test_schedule_label_reaches_the_log(tmp_path):
    a, ha, clock = make(tmp_path, [])
    clock["t"] = NOW - 5
    with acting("schedule “Morning”"):
        run(a.wrap_call(ha.call_service)("climate", "set_temperature", {"entity_id": ["light.k"], "temperature": 21}))
    clock["t"] = NOW
    assert run(a.page())["entries"][0]["by"] == "schedule “Morning”"
