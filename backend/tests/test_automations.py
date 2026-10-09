import asyncio
from datetime import datetime, timezone

import pytest

from backend.alerts import DEFAULT_SETTINGS, SettingsError, validate_settings
from backend.automations import RECOVER_SECS, WEEK, AutoStore, Health, WindowHeating
from backend.discovery import Device
from backend.geometry import in_room, opening_rooms, placed_in, room_poly

BED = {"id": "bed", "name": "Bedroom", "x": 0, "y": 0, "w": 4, "h": 3}
# L-shape: the north-east 2 x 2 corner is cut away, so x > 7 and y < 2 is outside
LOUNGE = {"id": "lounge", "name": "Lounge", "x": 4, "y": 0, "w": 5, "h": 5, "cut": {"corner": "ne", "w": 2, "h": 2}}
HALL = {"id": "hall", "name": "Hall", "x": 0, "y": 3, "w": 4, "h": 2}

W_BED, W_LOUNGE, W_SHARED, D_FRONT, W_HALL = (f"binary_sensor.contact_sensor_door_{n}" for n in ("bed", "lounge", "shared", "front", "hall"))
V_BED, V_LOUNGE, V_LOUNGE2, V_HALL = "climate.bed", "climate.lounge", "climate.lounge2", "climate.hall"

LAYOUT = {
    "unit": "m", "rooms": [BED, LOUNGE, HALL],
    "placements": [{"entity_id": V_BED, "x": 2, "y": 1.5}, {"entity_id": V_LOUNGE, "x": 5, "y": 4},
                   {"entity_id": V_LOUNGE2, "x": 8.5, "y": 3}, {"entity_id": V_HALL, "x": 1, "y": 4},
                   {"entity_id": "climate.nowhere", "x": 8, "y": 1}],
    "openings": [
        {"id": "wb", "type": "window", "x": 1, "y": 0, "len": 1.2, "orient": "h", "entity_id": W_BED},       # bedroom outer wall
        {"id": "wl", "type": "window", "x": 7.4, "y": 2.02, "len": 1.2, "orient": "h", "entity_id": W_LOUNGE},  # L inner wall (2 cm off)
        {"id": "ws", "type": "window", "x": 4, "y": 1, "len": 1, "orient": "v", "entity_id": W_SHARED},     # bedroom/lounge wall
        {"id": "df", "type": "door", "x": 1, "y": 3, "len": 0.9, "orient": "h", "entity_id": D_FRONT},      # doors never count
        {"id": "wx", "type": "window", "x": 0, "y": 3.5, "len": 1, "orient": "v"},                          # not linked
    ],
}
VALVES = [V_BED, V_LOUNGE, V_LOUNGE2, V_HALL, "climate.nowhere"]
DEVICES = {**{v: Device(v, "valve", v.split(".")[1].title(), "KE100") for v in VALVES},
           **{s: Device(s, "sensor", s.rsplit("_", 1)[1].title(), "T110") for s in (W_BED, W_LOUNGE, W_SHARED, D_FRONT, W_HALL)}}


# ---------------- geometry ----------------
def test_in_room_l_cut():
    assert in_room(BED, 0, 0) and in_room(BED, 4, 3) and not in_room(BED, 4.01, 1)
    assert in_room(LOUNGE, 5, 1) and in_room(LOUNGE, 8.5, 3) and not in_room(LOUNGE, 8, 1)
    assert in_room(LOUNGE, 7, 1) and in_room(LOUNGE, 8, 2)  # on the inner walls counts as inside (as in floorplan.js)
    for corner, inside, outside in (("nw", (8, 1), (5, 1)), ("sw", (8, 4), (5, 4)), ("se", (5, 4), (8, 4))):
        r = {**LOUNGE, "cut": {"corner": corner, "w": 2, "h": 2}}
        assert in_room(r, *inside) and not in_room(r, *outside), corner
    assert room_poly(LOUNGE) == [(4, 0), (7, 0), (7, 2), (9, 2), (9, 5), (4, 5)]


def test_window_to_rooms():
    ops = {o["id"]: o for o in LAYOUT["openings"]}
    names = lambda oid: [r["id"] for r in opening_rooms(LAYOUT, ops[oid])]
    assert names("wb") == ["bed"]               # outer wall
    assert names("wl") == ["lounge"]            # L inner wall, 2 cm off still counts
    assert names("ws") == ["bed", "lounge"]     # shared wall: both
    assert opening_rooms(LAYOUT, {**ops["wl"], "y": 2.1}) == []  # 10 cm off: no room
    assert opening_rooms(LAYOUT, {**ops["wb"], "orient": "v", "x": 1.6, "y": -0.6}) == []  # crossing, not along, a wall
    assert placed_in(LAYOUT, LOUNGE, VALVES) == [V_LOUNGE, V_LOUNGE2]  # climate.nowhere sits in the cut-away corner


# ---------------- window heating ----------------
class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class Rig:
    """WindowHeating with a fake HA: calls apply to `states` like a real valve would."""

    def __init__(self, tmp_path, store=None, **settings):
        self.clock, self.calls, self.pushes = Clock(), [], []
        self.settings = {**DEFAULT_SETTINGS, **settings}
        self.mode = {"mode": "home", "away_temp": 16.0}
        self.store = store or AutoStore(str(tmp_path / "a.db"))
        self.states = {v: {"state": "heat", "attributes": {"temperature": 21.0}} for v in VALVES}
        for s in (W_BED, W_LOUNGE, W_SHARED, D_FRONT, W_HALL):
            self.set(s, "off")
        self.layout = LAYOUT
        self.w = self.make()

    def make(self):
        return WindowHeating(self.store, lambda: self.settings, self._call, self._notify, lambda: self.mode, self.clock)

    async def _call(self, v, t):
        self.calls.append((v, t))
        self.states[v]["attributes"]["temperature"] = t

    async def _notify(self, p):
        self.pushes.append(p)

    def set(self, eid, state, ago=0):
        self.states[eid] = {"state": state, "attributes": {}, "last_changed": iso(self.clock.t - ago)}

    def target(self, v, t):
        self.states[v]["attributes"]["temperature"] = t

    def tick(self, advance=0):
        self.clock.t += advance
        asyncio.run(self.w.tick(self.layout, DEVICES, self.states))


def test_threshold_hold_and_restore(tmp_path):
    r = Rig(tmp_path)
    r.set(W_BED, "on")
    r.tick(119)
    assert r.calls == [] and r.w.holds == {}
    r.tick(1)
    assert r.calls == [(V_BED, 7.0)]
    assert [p["title"] for p in r.pushes] == ["Bedroom window open — radiator off"]
    assert r.w.held() == {V_BED: 21.0}
    r.tick(60)
    r.tick(60)
    assert r.calls == [(V_BED, 7.0)] and len(r.pushes) == 1  # no repeats while open
    r.set(W_BED, "off")
    r.tick(1)
    assert r.calls == [(V_BED, 7.0), (V_BED, 21.0)] and r.w.holds == {}
    r.tick(60)
    assert len(r.calls) == 2
    # reopening starts a new episode with a new notification
    r.set(W_BED, "on")
    r.tick(120)
    assert r.calls[-1] == (V_BED, 7.0) and len(r.pushes) == 2


def test_doors_and_short_openings_never_trigger(tmp_path):
    r = Rig(tmp_path)
    r.set(D_FRONT, "on")
    r.tick(3600)
    r.set(W_BED, "on")
    r.tick(100)
    r.set(W_BED, "off")
    r.tick(10)
    r.set(W_BED, "on")
    r.tick(100)  # never 2 min continuously
    assert r.calls == [] and r.pushes == []


def test_last_changed_counts_from_ha(tmp_path):
    r = Rig(tmp_path, window_open_minutes=5)
    r.set(W_BED, "on", ago=299)
    r.tick()
    assert r.calls == []
    r.tick(1)
    assert r.calls == [(V_BED, 7.0)]


def test_shared_and_l_windows_hit_the_right_valves(tmp_path):
    r = Rig(tmp_path, window_off_temp=8)
    r.set(W_SHARED, "on")
    r.tick(120)
    assert sorted(r.calls) == [(V_BED, 8.0), (V_LOUNGE, 8.0), (V_LOUNGE2, 8.0)]
    assert r.pushes[0]["title"] == "Bedroom & Lounge window open — radiators off"
    r.calls.clear()
    r.set(W_SHARED, "off")
    r.set(W_LOUNGE, "on")
    r.tick(1)  # lounge window still under threshold; bedroom has no open window -> back, lounge valves still held
    assert r.calls == [(V_BED, 21.0)] and set(r.w.holds) == {V_LOUNGE, V_LOUNGE2}
    r.tick(120)
    r.set(W_LOUNGE, "off")
    r.tick(1)
    assert sorted(r.calls[1:]) == [(V_LOUNGE, 21.0), (V_LOUNGE2, 21.0)] and r.w.holds == {}


def test_several_windows_on_one_valve(tmp_path):
    r = Rig(tmp_path)
    r.set(W_BED, "on")
    r.set(W_SHARED, "on")
    r.tick(120)
    assert r.calls.count((V_BED, 7.0)) == 1
    r.set(W_BED, "off")
    r.tick(5)
    assert V_BED in r.w.holds and (V_BED, 21.0) not in r.calls  # shared window still open
    r.set(W_SHARED, "unavailable")
    r.tick(5)
    assert V_BED in r.w.holds  # unknown is not "closed"
    r.set(W_SHARED, "off")
    r.tick(5)
    assert (V_BED, 21.0) in r.calls and r.w.holds == {}



def test_unavailable_sensor_releases_after_grace(tmp_path):
    # A window sensor whose battery dies while the window is open must not hold the radiator at 7° for days.
    r = Rig(tmp_path)
    r.set(W_BED, "on")
    r.tick(120)
    assert r.calls == [(V_BED, 7.0)]
    r.set(W_BED, "unavailable")
    r.tick(30 * 60)
    assert V_BED in r.w.holds and r.calls == [(V_BED, 7.0)]  # still held inside the grace period
    r.tick(31 * 60)
    assert r.calls == [(V_BED, 7.0), (V_BED, 21.0)] and r.w.holds == {}
    r.tick(15)
    assert len(r.calls) == 2  # and nothing more while it stays unavailable


def test_away_interplay(tmp_path):
    r = Rig(tmp_path)
    r.set(W_BED, "on")
    r.tick(120)
    r.mode = {"mode": "away", "away_temp": 15.0}
    r.set(W_BED, "off")
    r.tick(1)
    assert r.calls == [(V_BED, 7.0), (V_BED, 15.0)]  # closed while away -> away temperature
    # Home pressed while open: app.go_home adopts the home target; it is applied when the window closes
    r.calls.clear()
    r.set(W_BED, "on")
    r.tick(120)
    assert r.w.held() == {V_BED: 15.0}
    r.mode = {"mode": "home", "away_temp": 15.0}
    r.w.adopt(V_BED, 21.5)
    r.tick(30)
    assert r.calls == [(V_BED, 7.0)]  # still held
    r.set(W_BED, "off")
    r.tick(1)
    assert r.calls == [(V_BED, 7.0), (V_BED, 21.5)]


def test_user_change_while_held(tmp_path):
    r = Rig(tmp_path)
    r.set(W_BED, "on")
    r.tick(120)
    r.target(V_BED, 19.0)  # changed by hand in the app or on the valve
    r.tick(15)
    r.tick(15)
    assert r.calls == [(V_BED, 7.0)] and r.w.held() == {V_BED: 19.0}  # nothing sent
    r.set(W_BED, "off")
    r.tick(1)
    assert r.calls == [(V_BED, 7.0)] and r.w.holds == {}  # already at 19: no duplicate call


def test_already_low_and_unknown_targets(tmp_path):
    r = Rig(tmp_path)
    r.target(V_BED, 5.0)
    r.states[V_LOUNGE] = {"state": "unavailable", "attributes": {}}
    r.set(W_SHARED, "on")
    r.tick(120)
    assert sorted(r.calls) == [(V_LOUNGE2, 7.0)]  # bedroom already below 7: nothing to send; lounge offline: skipped
    assert V_BED in r.w.holds and V_LOUNGE not in r.w.holds
    r.states[V_LOUNGE] = {"state": "heat", "attributes": {"temperature": 20.0}}
    r.tick(15)
    assert (V_LOUNGE, 7.0) in r.calls  # picked up once it reports a target
    r.set(W_SHARED, "off")
    r.tick(1)
    assert sorted(r.calls[2:]) == [(V_LOUNGE, 20.0), (V_LOUNGE2, 21.0)]  # bedroom stays at 5, no call


def test_restart_persistence(tmp_path):
    r = Rig(tmp_path)
    r.set(W_BED, "on")
    r.tick(120)
    r.w = r.make()  # restart: same database
    assert r.w.held() == {V_BED: 21.0}
    r.tick(30)
    assert r.calls == [(V_BED, 7.0)] and len(r.pushes) == 1  # not re-sent, not re-notified
    r.w = r.make()
    r.set(W_BED, "off")  # closed while we were down
    r.tick(1)
    assert r.calls == [(V_BED, 7.0), (V_BED, 21.0)] and AutoStore(str(tmp_path / "a.db")).get("window_holds") == {}


def test_failures_back_off_and_no_duplicates(tmp_path):
    r = Rig(tmp_path)
    attempts = []

    async def failing(v, t):
        attempts.append((v, t))
        raise RuntimeError("HA down")
    r.w.call = failing
    r.set(W_BED, "on")
    r.tick(120)
    for _ in range(10):
        r.tick(15)
    assert attempts == [(V_BED, 7.0)]  # one try, then backs off
    r.w.call = r._call
    r.tick(300)
    assert r.calls == [(V_BED, 7.0)]
    # lagging HA: the target hasn't updated yet, but we don't send it again
    r.set(W_BED, "off")

    async def lagging(v, t):
        r.calls.append((v, t))
    r.w.call = lagging
    r.target(V_BED, 7.0)
    r.tick(1)
    assert r.calls[-1] == (V_BED, 21.0) and r.w.holds == {}


def test_switch_off_restores_and_stops(tmp_path):
    r = Rig(tmp_path)
    r.set(W_BED, "on")
    r.tick(120)
    r.settings["window_heating_enabled"] = False
    r.tick(1)
    assert r.calls == [(V_BED, 7.0), (V_BED, 21.0)] and r.w.holds == {}
    r.tick(600)
    assert len(r.calls) == 2
    r.set(W_BED, "off")
    r.settings.update(window_heating_enabled=True, window_notify=False)
    r.set(W_HALL, "on")  # not linked to any opening
    r.tick(600)
    assert len(r.calls) == 2


def test_unlinked_window_releases(tmp_path):
    r = Rig(tmp_path)
    r.set(W_BED, "on")
    r.tick(120)
    r.layout = {**LAYOUT, "openings": [o for o in LAYOUT["openings"] if o["id"] != "wb"]}
    r.tick(1)
    assert r.calls[-1] == (V_BED, 21.0) and r.w.holds == {}


def test_settings_validation_new_fields():
    s = validate_settings({"window_open_minutes": 30, "window_off_temp": 7.3, "health_unavailable_minutes": 10,
                           "weekly_summary": False}, DEFAULT_SETTINGS)
    assert s["window_open_minutes"] == 30 and s["window_off_temp"] == 7.5 and not s["weekly_summary"]
    for bad in ({"window_open_minutes": 31}, {"window_open_minutes": 0}, {"window_off_temp": 4.5}, {"window_off_temp": 16},
                {"window_off_temp": "7"}, {"health_unavailable_minutes": 9}, {"health_unavailable_minutes": 241},
                {"health_battery": "on"}, {"window_heating_enabled": 1}):
        with pytest.raises(SettingsError):
            validate_settings(bad, DEFAULT_SETTINGS)


# ---------------- device health ----------------
def health_rig(tmp_path, **settings):
    clock, pushes = Clock(), []
    st = {**DEFAULT_SETTINGS, **settings}
    store = AutoStore(str(tmp_path / "h.db"))

    async def notify(p):
        pushes.append(p)
    make = lambda: Health(store, lambda: st, notify, clock)
    return make, clock, pushes, st


VALVE = Device("climate.v", "valve", "Lounge valve", "KE100", {"battery": "sensor.v_battery", "battery_low": "binary_sensor.v_low"})


def vstates(clock, battery="80", low="off", state="heat", ago=0):
    return {"climate.v": {"state": state, "attributes": {}, "last_changed": iso(clock.t - ago)},
            "sensor.v_battery": {"state": battery, "attributes": {"unit_of_measurement": "%"}},
            "binary_sensor.v_low": {"state": low, "attributes": {}}}


def test_battery_once_weekly_reset(tmp_path):
    make, clock, pushes, st = health_rig(tmp_path)
    h = make()
    run = lambda **kw: asyncio.run(h.tick({"climate.v": VALVE}, vstates(clock, **kw)))
    run(battery="15")
    assert pushes == []
    run(battery="14")
    assert [p["title"] for p in pushes] == ["Low battery: Lounge valve"] and "14 %" in pushes[0]["body"]
    clock.t += WEEK - 1
    run(battery="14")
    h = make()  # restart keeps the marker
    run(battery="16")  # 16 % is not "recovered" (hysteresis), not low either
    assert len(pushes) == 1
    clock.t += 1
    run(battery="13")
    assert len(pushes) == 2  # a week later, still low
    run(battery="100")  # replaced
    run(battery="90", low="on")
    assert len(pushes) == 3 and pushes[-1]["body"] == "Battery at 90 % — replace it soon."
    run(battery="90")
    st["health_battery"] = False
    run(battery="5")
    assert len(pushes) == 3


def test_unavailable_threshold_flap_back_online(tmp_path):
    make, clock, pushes, st = health_rig(tmp_path)
    h = make()
    run = lambda **kw: asyncio.run(h.tick({"climate.v": VALVE}, vstates(clock, **kw)))
    run(state="unavailable", ago=29 * 60)
    assert pushes == []
    run(state="heat", ago=0)  # flaps back: timer restarts, no "back online"
    clock.t += 20 * 60
    run(state="unavailable", ago=20 * 60)
    assert pushes == []
    clock.t += 10 * 60
    run(state="unavailable", ago=30 * 60)
    assert [p["title"] for p in pushes] == ["Lounge valve is offline"]
    clock.t += 3600
    run(state="unavailable", ago=90 * 60)
    h = make()  # restart: the marker is remembered
    run(state="heat", ago=10)  # back, but only briefly so far
    run(state="unavailable", ago=0)
    assert len(pushes) == 1
    run(state="heat", ago=RECOVER_SECS)
    assert [p["title"] for p in pushes] == ["Lounge valve is offline", "Lounge valve is back online"]
    run(state="heat", ago=3600)
    assert len(pushes) == 2


def test_unavailable_switch_and_no_back_online_without_alert(tmp_path):
    make, clock, pushes, st = health_rig(tmp_path, health_unavailable=False, health_unavailable_minutes=10)
    h = make()
    run = lambda **kw: asyncio.run(h.tick({"climate.v": VALVE}, vstates(clock, **kw)))
    run(state="unavailable", ago=3600)
    run(state="heat", ago=3600)
    assert pushes == []
    st["health_unavailable"] = True
    run(state="unavailable", ago=9 * 60)
    run(state="heat", ago=3600)
    assert pushes == []
