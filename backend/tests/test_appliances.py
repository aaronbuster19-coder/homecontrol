"""Appliances: furniture linked to plugs — validation, carry/strip, status inference, the washer cycle detector."""
import asyncio
import copy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backend.alerts import DEFAULT_SETTINGS, validate_settings
from backend.appliances import APPLIANCE_TYPES, APPLIANCES, Appliances, status, step, validate_thresholds
from backend.automations import AutoStore
from backend.quiet import HeldStore, Quiet
from backend.store import FURNITURE_TYPES, LayoutError, carry_settings, stored_refs, validate_layout

PLUGS = {"switch.fan", "switch.kettle"}
KNOWN = PLUGS | {"light.a"}


def piece(id="f1", type="washer", **kw):
    return {"id": id, "type": type, "x": 1.0, "y": 1.0, "w": 0.6, "h": 0.6, "rot": 0, **kw}


def lay(*furniture):
    return {"unit": "m", "rooms": [], "placements": [], "furniture": list(furniture)}


def check(*furniture, plugs=PLUGS):
    return validate_layout(lay(*furniture), KNOWN, plugs)["furniture"]


# ---------------- validation ----------------
def test_new_types_are_furniture_and_appliances():
    for t in ("fan", "kettle", "microwave", "dishwasher", "dryer", "tv", "floor_lamp", "heater", "coffee_machine", "toaster",
              "iron", "hair_straightener"):
        assert t in FURNITURE_TYPES and t in APPLIANCE_TYPES, t
    assert {"fridge", "washer"} <= set(APPLIANCE_TYPES) and set(APPLIANCE_TYPES) <= set(FURNITURE_TYPES)
    assert check(piece(type="toaster"))[0]["type"] == "toaster"


def test_link_defaults_and_roundtrip():
    out = check(piece(plug="switch.fan"), piece("f2", "kettle"))
    assert out[0] == {**piece(), "plug": "switch.fan", "hide_marker": True}
    assert "plug" not in out[1] and "hide_marker" not in out[1]
    assert check(*out) == out
    shown = check(piece(plug="switch.fan", hide_marker=False, thresholds={"run_w": 20, "idle_min": 5}))[0]
    assert shown["hide_marker"] is False and shown["thresholds"] == {"run_w": 20.0, "idle_min": 5.0}
    # Unlinked: no link keys survive (hide_marker / thresholds only exist with a plug).
    assert check(piece(plug=None, hide_marker=False, thresholds={"run_w": 20}))[0] == piece()


@pytest.mark.parametrize("bad, match", [
    (piece(plug="switch.nope"), "unknown plug"),
    (piece(plug="light.a"), "unknown plug"),                       # known entity, but not a plug
    (piece(plug=5), "unknown plug"),
    (piece(type="bed", plug="switch.fan"), "can't be linked"),
    (piece(plug="switch.fan", hide_marker="yes"), "hide_marker"),
    (piece(plug="switch.fan", thresholds={"on_w": 50}), "doesn't apply"),  # a washer has no on_w
    (piece(type="kettle", plug="switch.fan", thresholds={"on_w": -1}), "on_w"),
    (piece(type="kettle", plug="switch.fan", thresholds={"on_w": True}), "number"),
    (piece(plug="switch.fan", thresholds={"idle_w": 20}), "quiet level"),   # above run_w (10)
    (piece(plug="switch.fan", thresholds=[1]), "object"),
])
def test_link_rejects(bad, match):
    with pytest.raises(LayoutError, match=match):
        check(bad)


def test_one_plug_one_appliance():
    with pytest.raises(LayoutError, match="already linked"):
        check(piece(plug="switch.fan"), piece("f2", "fan", plug="switch.fan"))
    assert len(check(piece(plug="switch.fan"), piece("f2", "fan", plug="switch.kettle"))) == 2


def test_stored_plug_stays_valid_while_ha_misses_it():
    stored = {**lay(piece(plug="switch.gone")), "settings": {"keep_on": []}}
    assert "switch.gone" in stored_refs(stored)
    from backend.store import stored_plugs
    assert stored_plugs(stored) == {"switch.gone"}
    assert check(piece(plug="switch.gone"), plugs=PLUGS | stored_plugs(stored))[0]["plug"] == "switch.gone"


def test_carry_links_for_older_clients():
    old = {**lay(piece(plug="switch.fan", hide_marker=False, thresholds={"run_w": 20}), piece("f2", "kettle", plug="switch.kettle"))}
    old_raw = [piece(x=2.0), piece("f2", "kettle")]  # an older app: moved the washer, knows nothing of plugs
    new = carry_settings(validate_layout(lay(*old_raw), KNOWN, PLUGS), old, lay(*old_raw))
    assert new["furniture"][0] == {**piece(x=2.0), "plug": "switch.fan", "hide_marker": False, "thresholds": {"run_w": 20}}
    assert new["furniture"][1]["plug"] == "switch.kettle"
    # A new app unlinks with an explicit null; a changed type drops the link; a plug relinked elsewhere isn't doubled.
    raw = [piece(plug=None), piece("f2", "toaster"), piece("f3", "fan", plug="switch.kettle")]
    new = carry_settings(validate_layout(lay(*raw), KNOWN, PLUGS), old, lay(*raw))
    assert [f.get("plug") for f in new["furniture"]] == [None, None, "switch.kettle"]


def test_api_link_validation(client):
    base = {"unit": "m", "rooms": [], "placements": []}
    r = client.put("/api/layout", json={**base, "furniture": [piece(plug="switch.fan")]})
    assert r.status_code == 200, r.text
    assert r.json()["furniture"][0]["plug"] == "switch.fan"
    r = client.put("/api/layout", json={**base, "furniture": [piece(plug="light.kitchen_1")]})
    assert r.status_code == 400 and "unknown plug" in r.json()["detail"]
    # An older app's save (no "plug" keys) keeps the link.
    r = client.put("/api/layout", json={**base, "furniture": [piece(x=3.0)]})
    assert r.json()["furniture"][0]["plug"] == "switch.fan"
    assert client.get("/api/appliances").json()["appliances"]["f1"]["plug"] == "switch.fan"


# ---------------- status inference ----------------
def on(p, state="on"):
    return {"state": state, "power": p}


@pytest.mark.parametrize("ftype, item, want", [
    ("kettle", on(2100.0), "Boiling…"),
    ("kettle", on(1000.0), "Idle"),          # over, not at, the threshold
    ("kettle", on(0.0, "off"), "Off"),
    ("kettle", {"state": "unavailable"}, "Offline"),
    ("fan", on(35.04), "On · 35 W"),
    ("fan", on(None), "On"),
    ("fridge", on(85.0), "Cooling"),
    ("fridge", on(30.0), "Idle"),
    ("fridge", on(2.5), "Idle"),
    ("tv", on(0.4), "Standby"),
    ("tv", on(86.0), "On"),
    ("washer", on(0.5), "Idle"),
    ("washer", on(500.0), "Starting…"),
])
def test_status_defaults(ftype, item, want):
    assert status(ftype, item, APPLIANCES[ftype][3]) == want


def test_status_custom_threshold_and_cycle_text():
    assert status("kettle", on(800.0), {"on_w": 500.0}) == "Boiling…"
    th = APPLIANCES["washer"][3]
    assert status("washer", on(400.0), th, {"phase": "running", "run_start": 0}, 47 * 60 + 5) == "Running 47 min"
    assert status("washer", on(400.0), th, {"phase": "running", "run_start": 0}, 92 * 60) == "Running 1 h 32 min"
    assert status("washer", on(1.0), th, {"phase": "finished", "finished_at": 0}, 12 * 60) == "Finished 12 min ago"
    assert status("washer", on(1.0), th, {"phase": "finished", "finished_at": 0}, 3 * 3600) == "Idle"


def test_threshold_ranges():
    assert validate_thresholds(None, "kettle") == {}
    assert validate_thresholds({"on_w": 1500}, "kettle") == {"on_w": 1500.0}
    assert validate_thresholds({"on_w": None}, "kettle") == {}
    for bad in ({"on_w": 0}, {"on_w": 6000}):
        with pytest.raises(ValueError):
            validate_thresholds(bad, "kettle")
    with pytest.raises(ValueError):
        validate_thresholds({"on_w": 10}, "fan")  # "on" rule: no thresholds at all


# ---------------- cycle detector ----------------
class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


LON = ZoneInfo("Europe/London")
DAY = datetime(2026, 10, 12, 14, 0, tzinfo=LON).timestamp()
NIGHT = datetime(2026, 10, 12, 2, 0, tzinfo=LON).timestamp()


class Rig:
    """Appliances with a washer on switch.fan; feed() sets the power and advances the clock like the live loop would."""

    def __init__(self, tmp_path, t=DAY, store=None, furniture=None, **settings):
        self.clock, self.pushes, self.events, self.categories = Clock(t), [], [], []
        self.settings = {**DEFAULT_SETTINGS, **settings}
        self.store = store or AutoStore(str(tmp_path / "a.db"))
        self.layout = lay(*(furniture or [piece(plug="switch.fan", label="Washer")]))
        self.item = {"entity_id": "switch.fan", "kind": "plug", "state": "on", "power": 0.0}
        self.make()

    def make(self):
        async def notify(p, category="appliance"):
            self.pushes.append(p)
            self.categories.append(category)
        self.app = Appliances(self.store, lambda: self.settings, lambda: self.layout, notify, self.events.append, self.clock)

    def feed(self, power, mins=0.0, state="on"):
        """Change the reading (as an SSE event would), then let `mins` pass in 15 s automation ticks."""
        self.item = {**self.item, "power": power, "state": state}
        self.app.observe(self.item)
        self.tick()
        end = self.clock.t + mins * 60
        while self.clock.t < end:
            self.clock.t = min(end, self.clock.t + 15)
            self.tick()

    def tick(self):
        asyncio.run(self.app.tick({"switch.fan": self.item}))

    @property
    def phase(self):
        return self.app.cycles["f1|switch.fan"]["phase"]


def test_cycle_start_needs_two_minutes_over_10w(tmp_path):
    r = Rig(tmp_path)
    r.feed(500, 1.5)
    assert r.phase == "idle"
    r.feed(2, 0.25)                 # a dip resets the start timer
    r.feed(500, 1.75)
    assert r.phase == "idle"
    r.feed(500, 0.25)
    assert r.phase == "running"
    start = r.app.cycles["f1|switch.fan"]["run_start"]
    assert start == r.clock.t - 120  # the cycle started when the power went up
    assert r.events and r.events[-1]["appliances"]["f1"]["phase"] == "running"


def test_mid_cycle_pause_is_not_finished_then_finished_after_3_min_low(tmp_path):
    r = Rig(tmp_path)
    r.feed(500, 3)
    assert r.phase == "running"
    r.feed(2, 2.75)                  # soaking: 2 W for 2¾ min
    r.feed(8, 0.5)                   # 8 W: between the levels counts as still running
    r.feed(2, 2.75)
    assert r.phase == "running" and r.pushes == []
    r.feed(1800, 20)                 # heating, spinning…
    r.feed(1.5, 2.9)
    assert r.phase == "running"
    r.feed(1.5, 0.1)
    assert r.phase == "finished" and len(r.pushes) == 1
    c = r.app.cycles["f1|switch.fan"]
    assert c["finished_at"] == r.clock.t - 180  # finished when it went quiet
    p = r.pushes[0]
    assert p["title"] == "Washing finished" and p["body"].startswith("Washer ran ") and p["tag"] == "appliance-f1"
    st = r.app.public({"switch.fan": r.item})["appliances"]["f1"]
    assert st["phase"] == "finished" and st["status"] == "Finished 3 min ago"
    r.feed(1.5, 150)
    assert r.app.public({"switch.fan": r.item})["appliances"]["f1"]["status"] == "Idle"


def test_never_twice_and_next_cycle_pushes_again(tmp_path):
    r = Rig(tmp_path)
    r.feed(500, 5)
    r.feed(0.5, 4)
    r.feed(0.5, 30)
    r.feed(3, 10)
    assert len(r.pushes) == 1
    r.make()                         # restart after finishing: nothing again
    r.feed(0.5, 10)
    assert len(r.pushes) == 1 and r.phase == "finished"
    r.feed(600, 3)                   # the next load
    r.feed(0.4, 3)
    assert len(r.pushes) == 2


def test_restart_mid_cycle_is_not_a_false_finish(tmp_path):
    r = Rig(tmp_path)
    r.feed(500, 10)
    r.feed(1, 2.5)                   # quiet for 2½ min, then the app restarts
    r.make()
    assert r.phase == "running"
    r.item = {**r.item, "power": None, "state": "unavailable"}  # HA not loaded yet
    r.clock.t += 600
    r.tick()
    assert r.phase == "running" and r.pushes == []
    r.feed(1, 1)                     # the quiet timer starts again after the restart, from what we actually see
    assert r.phase == "running" and r.pushes == []
    r.feed(1, 2)
    assert r.phase == "finished" and len(r.pushes) == 1


def test_switched_off_mid_cycle_aborts_without_push(tmp_path):
    r = Rig(tmp_path)
    r.feed(500, 5)
    r.feed(0, 10, state="off")
    assert r.phase == "idle" and r.pushes == []


def test_unknown_power_holds(tmp_path):
    r = Rig(tmp_path)
    r.feed(500, 5)
    r.feed(None, 10)
    assert r.phase == "running"


def test_push_setting_off(tmp_path):
    r = Rig(tmp_path, appliance_done=False)
    r.feed(500, 5)
    r.feed(0.5, 4)
    assert r.phase == "finished" and r.pushes == []
    assert validate_settings({"appliance_done": False}, DEFAULT_SETTINGS)["appliance_done"] is False
    assert DEFAULT_SETTINGS["appliance_done"] is True


def test_dryer_and_dishwasher_titles_and_custom_thresholds(tmp_path):
    r = Rig(tmp_path, furniture=[piece(type="dryer", plug="switch.fan", thresholds={"run_w": 100, "idle_min": 1})])
    r.feed(50, 5)
    assert r.phase == "idle"         # under its own 100 W running level
    r.feed(800, 2.5)
    r.feed(1, 1)
    assert [p["title"] for p in r.pushes] == ["Dryer finished"] and r.pushes[0]["body"].startswith("Tumble dryer ran")
    r = Rig(tmp_path / "x", furniture=[piece(type="dishwasher", plug="switch.fan")])
    r.feed(1500, 3)
    r.feed(0, 3)
    assert [p["title"] for p in r.pushes] == ["Dishwasher finished"]


def test_unlinking_forgets_the_cycle(tmp_path):
    r = Rig(tmp_path)
    r.feed(500, 5)
    r.layout = lay(piece())
    r.tick()
    assert r.app.cycles == {} and r.store.get("appliance_cycles") == {}


def test_only_cycle_types_are_tracked(tmp_path):
    r = Rig(tmp_path, furniture=[piece(type="kettle", plug="switch.fan")])
    r.feed(2000, 5)
    r.feed(0, 5)
    assert r.app.cycles == {} and r.pushes == []


def test_finished_push_is_held_in_quiet_hours(tmp_path):
    """Category "appliance" goes through quiet hours: held at night, delivered in the morning digest."""
    sent = []

    class Pusher:
        async def notify(self, p):
            sent.append(p)

    r = Rig(tmp_path, t=NIGHT)
    quiet = Quiet(HeldStore(str(tmp_path / "a.db")), lambda: r.settings, Pusher(), r.clock, LON)

    async def notify(p, category):
        await quiet.notify(p, category)
    r.app.notify = notify
    r.feed(500, 5)
    r.feed(0.5, 4)
    assert r.phase == "finished" and sent == [] and quiet.state()["held"] == 1
    r.clock.t = datetime(2026, 10, 12, 7, 0, tzinfo=LON).timestamp()
    asyncio.run(quiet.tick())
    assert len(sent) == 1 and sent[0]["title"] == "While you were asleep" and "Washing finished" in sent[0]["body"]


def test_automations_feed_and_api(client, fake_ha):
    """Through the app: the Live observer feeds the tracker and GET /api/appliances reports it."""
    base = {"unit": "m", "rooms": [], "placements": []}
    assert client.put("/api/layout", json={**base, "furniture": [piece(plug="switch.fan"),
                                                                    piece("k", "kettle", plug="switch.kettle")]}).status_code == 200
    data = client.get("/api/appliances").json()
    assert set(data["appliances"]) == {"f1", "k"} and isinstance(data["now"], float)
    assert data["appliances"]["f1"]["status"] == "Off"          # switch.fan is off in the fake states
    assert data["appliances"]["k"]["status"] == "On"            # kettle on, power sensor unavailable
    assert "phase" not in data["appliances"]["k"]


def test_away_leaves_a_linked_fridge_on(client, fake_ha):
    base = {"unit": "m", "rooms": [], "placements": []}
    assert client.put("/api/layout", json={**base, "furniture": [piece("fr", "fridge", plug="switch.kettle")]}).status_code == 200
    r = client.post("/api/mode", json={"mode": "away"}).json()
    assert "switch.kettle" in r["kept_on"] and "switch.kettle" not in r["turned_off"]
    assert "switch.fan" in r["turned_off"]


def test_rules_match_the_frontend():
    """APPLIANCE in frontend/appliances.js mirrors APPLIANCES here: same types, rules, texts and default thresholds."""
    import json
    import re
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "frontend" / "appliances.js").read_text()
    cyc = json.loads(re.sub(r"(\w+):", r'"\1":', re.search(r"const CYCLE_TH = (\{[^}]*\})", js).group(1)))
    block = js[js.index("const APPLIANCE = {"):]
    block = block[:block.index("\n};")]
    got = {}
    for m in re.finditer(r'^  (\w+): \{ rule: "(\w+)"(?:, busy: "([^"]*)", idle: "([^"]*)")?, th: (\{[^}]*\}|CYCLE_TH) \},', block, re.M):
        th = cyc if m.group(5) == "CYCLE_TH" else json.loads(re.sub(r"(\w+):", r'"\1":', m.group(5)))
        got[m.group(1)] = (m.group(2), m.group(3), m.group(4), {k: float(v) for k, v in th.items()})
    assert got == {t: (r, b, i, {k: float(v) for k, v in th.items()}) for t, (r, b, i, th) in APPLIANCES.items()}
    assert list(got) == list(APPLIANCE_TYPES)


# ---------------- left-on reminders ----------------
def heater(**kw):
    return piece(type="heater", plug="switch.fan", **kw)


def test_remind_validation_and_defaults():
    from backend.appliances import remind_minutes
    assert "remind" not in check(heater())[0]
    assert check(heater(remind=False))[0]["remind"] is False
    assert check(heater(remind=90))[0]["remind"] == 90
    for bad in (10, 1441, True, "60", 60.5):
        with pytest.raises(LayoutError, match="remind"):
            check(heater(remind=bad))
    with pytest.raises(LayoutError, match="no left-on"):
        check(piece(plug="switch.fan", remind=60))            # a washer has cycles, not reminders
    with pytest.raises(LayoutError, match="no left-on"):
        check(piece(type="fridge", plug="switch.fan", remind=60))
    assert remind_minutes(heater()) == 180 and remind_minutes(piece(type="fan")) == 180
    assert remind_minutes(piece(type="iron")) == 60 and remind_minutes(piece(type="hair_straightener")) == 60
    assert remind_minutes(piece(type="kettle")) is None and remind_minutes(piece(type="kettle", remind=30)) == 30
    assert remind_minutes(heater(remind=False)) is None


def test_heater_left_on_reminds_once_and_bypasses_quiet(tmp_path):
    r = Rig(tmp_path, furniture=[heater(label="Bedroom heater")])
    r.feed(1500, 179)
    assert r.pushes == []
    r.feed(1500, 1.5)
    assert len(r.pushes) == 1 and r.categories == ["safety"]
    p = r.pushes[0]
    assert p["title"] == "Bedroom heater has been on for 3 h" and p["entity_id"] == "switch.fan"
    assert p["actions"] == [{"action": "off", "title": "Turn off"}] and p["url"] == "/?dev=switch.fan"
    r.feed(1500, 300)
    r.feed(0.5, 30)                  # thermostat satisfied for a long time: still no second push
    r.feed(1500, 300)
    assert len(r.pushes) == 1
    r.feed(0, 1, state="off")        # off re-arms
    r.feed(1500, 181)
    assert len(r.pushes) == 2


def test_thermostat_dips_dont_reset_but_a_long_quiet_does(tmp_path):
    r = Rig(tmp_path, furniture=[heater(remind=60)])
    for _ in range(6):               # 6 × (8 min heating + 2 min thermostat off) = 60 min
        r.feed(1500, 8)
        r.feed(0.4, 2)
    r.feed(1500, 0.5)
    assert len(r.pushes) == 1
    r2 = Rig(tmp_path / "b", furniture=[heater(remind=60)])
    r2.feed(1500, 40)
    r2.feed(0.4, 11)                 # quiet for over 10 min: the run is over
    r2.feed(1500, 40)
    assert r2.pushes == []


def test_left_on_survives_restart_without_double_send(tmp_path):
    r = Rig(tmp_path, furniture=[piece(type="iron", plug="switch.fan")])
    r.feed(1200, 40)
    r.make()                         # restart after 40 min: the timer carries on
    r.feed(1200, 21)
    assert len(r.pushes) == 1 and r.categories == ["safety"] and r.pushes[0]["title"] == "Iron has been on for 1 h"
    r.make()
    r.feed(1200, 120)
    assert len(r.pushes) == 1


def test_fan_reminder_goes_through_quiet_hours_and_can_be_off(tmp_path):
    r = Rig(tmp_path, furniture=[piece(type="fan", plug="switch.fan")])
    r.feed(35, 181)
    assert r.categories == ["appliance"] and r.pushes[0]["title"] == "Fan has been on for 3 h"
    r = Rig(tmp_path / "b", furniture=[piece(type="fan", plug="switch.fan", remind=False)])
    r.feed(35, 600)
    assert r.pushes == [] and r.app.left == {}


def test_safety_category_bypasses_quiet_hours(tmp_path):
    from backend.quiet import BYPASS
    assert "safety" in BYPASS and "appliance" not in BYPASS


def test_turn_off_endpoint_only_turns_off(client, fake_ha):
    r = client.post("/api/devices/switch.fan/turn_off")
    assert r.status_code == 200
    assert fake_ha.service_calls()[-1] == ("/api/services/switch/turn_off", {"entity_id": "switch.fan"})
    assert client.post("/api/devices/switch.nope/turn_off").status_code == 404
    assert client.post("/api/devices/climate.lounge_valve/turn_off").status_code == 400
    import base64
    from fastapi.testclient import TestClient
    anon = TestClient(client.app)
    assert anon.post("/api/devices/switch.fan/turn_off").status_code == 401


# ---------------- stats from history ----------------
from datetime import timedelta, timezone  # noqa: E402

from backend.appliance_stats import ApplianceStats, compute, find_cycles  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=LON)  # Wednesday; this week from Mon 12 Oct, last week from Mon 5 Oct


def segs_from(points, end=NOW):
    """[(datetime, W)] -> power segments in ms."""
    out = []
    for i, (t, v) in enumerate(points):
        t1 = points[i + 1][0] if i + 1 < len(points) else end
        out.append((int(t.timestamp() * 1000), int(t1.timestamp() * 1000), v))
    return out


def wash(day, hour, mins=90, kw=1.0):
    """A cycle: 10 min at 2 kW, a 2 min soak at 2 W (under the 3 min that would end it), then at kw kW, then 1 W."""
    t = datetime(2026, 10, day, hour, tzinfo=LON)
    return [(t, 2000.0), (t + timedelta(minutes=10), 2.0), (t + timedelta(minutes=12), kw * 1000),
            (t + timedelta(minutes=mins), 1.0)]


def test_cycle_stats_this_and_last_week():
    pts = [(datetime(2026, 10, 5, 0, tzinfo=LON), 1.0)]
    for d, h in ((6, 9), (8, 18), (10, 10)):        # last week: 3 cycles
        pts += wash(d, h)
    for d, h in ((12, 8), (13, 20)):                # this week: 2
        pts += wash(d, h)
    pts += [(datetime(2026, 10, 14, 11, 50, tzinfo=LON), 500.0)]  # running now: not counted
    segs = segs_from(pts)
    assert len(find_cycles(segs, APPLIANCES["washer"][3])) == 5
    out = compute(piece(plug="switch.fan"), segs, NOW, LON, 25.0)
    c = out["cycles"]
    assert (c["this_week"], c["last_week"], c["avg_min"]) == (2, 3, 90)
    # 10 min × 2 kW + 2 min × 2 W + 78 min × 1 kW = 0.3333 + 0.0001 + 1.3
    assert c["kwh_per_cycle"] == pytest.approx(1.633, abs=0.001) and c["cost_per_cycle_p"] == pytest.approx(40.84, abs=0.01)
    assert compute(piece(plug="switch.fan"), segs, NOW, LON, None)["cycles"]["cost_per_cycle_p"] is None


def test_kettle_boils_today_and_week():
    pts = [(datetime(2026, 10, 11, 0, tzinfo=LON), 0.0)]
    for d, h in ((11, 8), (12, 7), (12, 17), (13, 7), (14, 7), (14, 10)):   # Sunday's is last week
        t = datetime(2026, 10, d, h, tzinfo=LON)
        pts += [(t, 2100.0), (t + timedelta(minutes=3), 0.0)]
    pts.insert(5, (datetime(2026, 10, 12, 17, 1, tzinfo=LON), None))         # a gap mid-boil is still one boil
    out = compute(piece(type="kettle", plug="switch.fan"), segs_from(pts), NOW, LON, 25.0)
    assert out["uses"] == {"label": "Boils", "today": 2, "week": 5}


def test_heater_hours_and_cost():
    pts = [(datetime(2026, 10, 11, 0, tzinfo=LON), 0.0), (datetime(2026, 10, 12, 18, tzinfo=LON), 1000.0),
           (datetime(2026, 10, 12, 21, tzinfo=LON), 0.0), (datetime(2026, 10, 14, 9, tzinfo=LON), 1000.0),
           (datetime(2026, 10, 14, 9, 30, tzinfo=LON), 0.0)]
    out = compute(heater(), segs_from(pts), NOW, LON, 30.0)
    assert out["hours_week"] == 3.5 and out["week"] == {"kwh": 3.5, "cost_p": 105.0}


def test_fridge_average_per_day():
    pts, t = [], datetime(2026, 10, 7, 0, tzinfo=LON)
    while t < NOW:                                    # 15 min an hour at 80 W, else 2 W: 0.5 + 0.036 kWh a day
        pts += [(t, 80.0), (t + timedelta(minutes=15), 2.0)]
        t += timedelta(hours=1)
    out = compute(piece(type="fridge", plug="switch.fan"), segs_from(pts), NOW, LON, 20.0)
    assert out["daily"]["days"] == 7.0 and out["daily"]["kwh"] == pytest.approx(0.516, abs=0.001)
    assert out["daily"]["cost_p"] == pytest.approx(10.32, abs=0.01)


def test_stats_fetch_and_cache():
    class HA:
        calls = []

        async def history(self, start, end, ids):
            self.calls.append((start, end, ids))
            t = datetime(2026, 10, 13, 7, tzinfo=LON)
            return [[{"entity_id": "sensor.k_power", "state": "0", "last_changed": datetime(2026, 10, 5, tzinfo=LON).isoformat()},
                     {"state": "2100", "last_changed": t.isoformat()},
                     {"state": "0", "last_changed": (t + timedelta(minutes=3)).isoformat()}]]
    from backend.discovery import Device
    ha, mono = HA(), Clock(0.0)
    s = ApplianceStats(ha, LON, mono, lambda: NOW)
    dev = Device("switch.k", "plug", "Kettle", "P110", related={"power": "sensor.k_power"})
    f = piece(type="kettle", plug="switch.k")
    out = asyncio.run(s.stats(f, dev, {"settings": {"energy": {"rate_p": 24.0}}}))
    assert out["uses"] == {"label": "Boils", "today": 0, "week": 1} and out["rate_p"] == 24.0
    assert ha.calls[0][0] == datetime(2026, 10, 5, tzinfo=LON).astimezone(UTC) and ha.calls[0][2] == ["sensor.k_power"]
    asyncio.run(s.stats(f, dev, {"settings": {"energy": {"rate_p": 24.0}}}))
    assert len(ha.calls) == 1                         # cached
    mono.t = 121
    asyncio.run(s.stats(f, dev, {"settings": {"energy": {"rate_p": 24.0}}}))
    assert len(ha.calls) == 2
    nop = asyncio.run(s.stats(f, Device("switch.x", "plug", "X", "TP11"), {}))
    assert nop["no_power"] is True


def test_stats_api(client, fake_ha):
    base = {"unit": "m", "rooms": [], "placements": []}
    assert client.put("/api/layout", json={**base, "furniture": [piece("k", "kettle", plug="switch.kettle")]}).status_code == 200
    fake_ha.history = [[{"entity_id": "sensor.kettle_current_consumption", "state": "0",
                         "last_changed": "2020-01-01T00:00:00+00:00"}]]
    r = client.get("/api/appliances/k/stats")
    assert r.status_code == 200, r.text
    assert r.json()["uses"] == {"label": "Boils", "today": 0, "week": 0}
    assert client.get("/api/appliances/nope/stats").status_code == 404
