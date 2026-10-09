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
    for t in ("fan", "kettle", "microwave", "dishwasher", "dryer", "tv", "floor_lamp", "heater", "coffee_machine", "toaster"):
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
        self.clock, self.pushes, self.events = Clock(t), [], []
        self.settings = {**DEFAULT_SETTINGS, **settings}
        self.store = store or AutoStore(str(tmp_path / "a.db"))
        self.layout = lay(*(furniture or [piece(plug="switch.fan", label="Washer")]))
        self.item = {"entity_id": "switch.fan", "kind": "plug", "state": "on", "power": 0.0}
        self.make()

    def make(self):
        async def notify(p):
            self.pushes.append(p)
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

    async def notify(p):
        await quiet.notify(p, "appliance")
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
