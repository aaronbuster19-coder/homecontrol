"""Hoover (charge tracker, "Hoover charged", opt-in auto-off), desktop PC and home server (protected, power-loss push)."""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.alerts import DEFAULT_SETTINGS
from backend.appliance_stats import compute, find_charges
from backend.appliances import (APPLIANCE_TYPES, APPLIANCES, PROTECTED, Appliances, protected_plugs, remind_minutes,
                                remind_ok, status)
from backend.automations import AutoStore
from backend.quiet import HeldStore, Quiet
from backend.standby import StandbySaver, blocked_reason
from backend.store import FURNITURE_TYPES, LayoutError, carry_settings, validate_layout

PLUGS = {"switch.fan", "switch.kettle"}
LON = ZoneInfo("Europe/London")
DAY = datetime(2026, 10, 12, 14, 0, tzinfo=LON).timestamp()
NIGHT = datetime(2026, 10, 12, 2, 0, tzinfo=LON).timestamp()


def piece(id="h1", type="hoover", **kw):
    return {"id": id, "type": type, "x": 1.0, "y": 1.0, "w": 0.3, "h": 0.25, "rot": 0, **kw}


def lay(*furniture):
    return {"unit": "m", "rooms": [], "placements": [], "furniture": list(furniture)}


def check(*furniture):
    return validate_layout(lay(*furniture), PLUGS, PLUGS)["furniture"]


# ---------------- validation ----------------
def test_new_types_are_furniture_and_appliances():
    for t in ("hoover", "desktop_pc", "home_server"):
        assert t in FURNITURE_TYPES and t in APPLIANCE_TYPES, t
    assert APPLIANCES["hoover"][0] == "charge" and "home_server" in PROTECTED and "desktop_pc" not in PROTECTED
    out = check(piece(plug="switch.fan"), piece("pc", "desktop_pc", plug="switch.kettle"), piece("s", "home_server"))
    assert [f["type"] for f in out] == ["hoover", "desktop_pc", "home_server"]
    assert out[0] == {**piece(), "plug": "switch.fan", "hide_marker": True}


def test_hoover_thresholds_and_auto_off_validation():
    f = check(piece(plug="switch.fan", thresholds={"charge_w": 20, "trickle_w": 2.5, "charged_min": 15}, auto_off=True))[0]
    assert f["thresholds"] == {"charge_w": 20.0, "trickle_w": 2.5, "charged_min": 15.0} and f["auto_off"] is True
    assert "auto_off" not in check(piece(plug="switch.fan", auto_off=False))[0]   # default off: stored as no key
    assert "auto_off" not in check(piece(auto_off=True))[0]                       # only while linked
    for bad, match in ((piece(plug="switch.fan", auto_off="yes"), "auto_off"),
                       (piece("k", "kettle", plug="switch.fan", auto_off=True), "can't switch itself off"),
                       (piece(plug="switch.fan", thresholds={"trickle_w": 12}), "charged level"),
                       (piece(plug="switch.fan", thresholds={"on_w": 5}), "doesn't apply"),
                       (piece(plug="switch.fan", thresholds={"charged_min": 0}), "charged_min"),
                       (piece("pc", "desktop_pc", plug="switch.fan", thresholds={"charge_w": 5}), "doesn't apply")):
        with pytest.raises(LayoutError, match=match):
            check(bad)


def test_auto_off_is_carried_for_older_clients():
    old = lay(piece(plug="switch.fan", auto_off=True))
    raw = [piece(x=2.0)]  # an older app knows nothing of plugs
    new = carry_settings(validate_layout(lay(*raw), PLUGS, PLUGS), old, lay(*raw))
    assert new["furniture"][0]["auto_off"] is True and new["furniture"][0]["plug"] == "switch.fan"


def test_reminders_per_type():
    assert not remind_ok("hoover") and not remind_ok("home_server")    # a charger / always-on server: none
    assert remind_ok("desktop_pc")
    assert remind_minutes(piece("pc", "desktop_pc", plug="switch.fan")) is None             # available, off by default
    assert remind_minutes(piece("pc", "desktop_pc", plug="switch.fan", remind=120)) == 120
    with pytest.raises(LayoutError, match="no left-on reminder"):
        check(piece(plug="switch.fan", remind=60))


# ---------------- status ----------------
def on(p, state="on"):
    return {"state": state, "power": p}


HTH = APPLIANCES["hoover"][3]


@pytest.mark.parametrize("ftype, item, cyc, want", [
    ("hoover", on(45.0), None, "Charging"),
    ("hoover", on(5.0), {"phase": "charging"}, "Charging"),       # tapering: still the same charge
    ("hoover", on(1.2), {"phase": "charged"}, "Charged"),
    ("hoover", on(1.2), None, "Not charging"),
    ("hoover", on(0.0, "off"), {"phase": "charged"}, "Not charging"),
    ("hoover", {"state": "unavailable"}, None, "Offline"),
    ("desktop_pc", on(120.0), None, "On"),
    ("desktop_pc", on(4.0), None, "Sleep"),
    ("desktop_pc", on(0.0, "off"), None, "Off"),
    ("home_server", on(35.0), None, "Running · 35 W"),
    ("home_server", on(None), None, "Running"),
])
def test_status(ftype, item, cyc, want):
    assert status(ftype, item, APPLIANCES[ftype][3], cyc, DAY) == want


# ---------------- charge tracker ----------------
class Clock:
    def __init__(self, t=DAY):
        self.t = t

    def __call__(self):
        return self.t


class Rig:
    """Appliances with pieces on switch.fan; feed() sets the power and advances the clock in 15 s automation ticks."""

    def __init__(self, tmp_path, *furniture, t=DAY, fail_off=False, **settings):
        self.clock, self.pushes, self.categories, self.offs = Clock(t), [], [], []
        self.settings = {**DEFAULT_SETTINGS, **settings}
        self.store = AutoStore(str(tmp_path / "a.db"))
        self.layout = lay(*(furniture or [piece(plug="switch.fan")]))
        self.item = {"entity_id": "switch.fan", "kind": "plug", "state": "on", "power": 0.0}
        self.fail_off = fail_off
        self.make()

    def make(self):
        async def notify(p, category):
            self.pushes.append(p)
            self.categories.append(category)

        async def turn_off(eid):
            self.offs.append(eid)
            if self.fail_off:
                raise RuntimeError("HA said no")
            self.item = {**self.item, "state": "off", "power": 0.0}
        self.app = Appliances(self.store, lambda: self.settings, lambda: self.layout, notify, None, self.clock, turn_off)

    def feed(self, power, mins=0.0, state="on"):
        self.item = {**self.item, "power": power, "state": state}
        self.app.observe(self.item)
        self.tick()
        end = self.clock.t + mins * 60
        while self.clock.t < end:
            self.clock.t = min(end, self.clock.t + 15)
            self.tick()

    def tick(self):
        asyncio.run(self.app.tick({"switch.fan": self.item}))

    def charge(self, key="h1|switch.fan"):
        return self.app.charges[key]

    @property
    def phase(self):
        return self.charge()["phase"]


def test_charging_then_charged_after_ten_minutes_of_trickle(tmp_path):
    r = Rig(tmp_path)
    r.feed(1.0, 5)
    assert r.phase == "idle"
    r.feed(45, 50)                    # docked: charging straight away
    assert r.phase == "charging"
    r.feed(6, 20)                     # tapering between 3 and 10 W: still charging
    assert r.phase == "charging" and r.pushes == []
    r.feed(1.5, 9.5)
    assert r.phase == "charging" and r.pushes == []
    r.feed(1.5, 0.75)                 # under 3 W for 10 min: charged
    assert r.phase == "charged" and len(r.pushes) == 1
    p = r.pushes[0]
    assert p["title"] == "Hoover charged" and p["body"].startswith("Charged in 1 h 10 min")
    assert p["actions"] == [{"action": "off", "title": "Turn off"}] and p["entity_id"] == "switch.fan"
    assert p["url"] == "/?dev=switch.fan" and r.categories == ["appliance"]
    assert r.offs == []               # auto-off is opt-in: nothing switched
    r.feed(1.5, 60)
    assert len(r.pushes) == 1         # one push per charge
    data = r.app.public({"switch.fan": r.item})["appliances"]["h1"]
    assert data["phase"] == "charged" and data["status"] == "Charged" and data["charged_at"] - data["charge_start"] == 70 * 60


def test_short_dips_dont_count(tmp_path):
    r = Rig(tmp_path)
    r.feed(40, 30)
    for _ in range(3):                # the charger pausing for 8 min at a time
        r.feed(0.0, 8)
        r.feed(40, 5)
    assert r.phase == "charging" and r.pushes == []
    r.feed(0.0, 10)
    assert r.phase == "charged" and len(r.pushes) == 1


def test_label_and_custom_thresholds(tmp_path):
    r = Rig(tmp_path, piece(plug="switch.fan", label="Dyson", thresholds={"charge_w": 20, "charged_min": 2}))
    r.feed(15, 10)
    assert r.phase == "idle"          # under its own 20 W charging level
    r.feed(30, 10)
    r.feed(1, 2)
    assert [p["title"] for p in r.pushes] == ["Dyson charged"]


def test_restart_mid_trickle_is_not_a_false_charged(tmp_path):
    r = Rig(tmp_path)
    r.feed(40, 30)
    r.feed(1, 8)                      # 8 min of trickle, then the app restarts
    r.make()
    assert r.phase == "charging"
    r.item = {**r.item, "power": None, "state": "unavailable"}  # HA not loaded yet
    r.clock.t += 900
    r.tick()
    assert r.phase == "charging" and r.pushes == []
    r.feed(1, 5)                      # the trickle timer starts again from what we actually see
    assert r.phase == "charging" and r.pushes == []
    r.feed(1, 5)
    assert r.phase == "charged" and len(r.pushes) == 1
    r.make()                          # restart after charged: no second push
    r.feed(1, 30)
    assert r.phase == "charged" and len(r.pushes) == 1


def test_unplugged_mid_charge_aborts_without_push(tmp_path):
    r = Rig(tmp_path)
    r.feed(40, 10)
    r.feed(0, 30, state="off")
    assert r.phase == "idle" and r.pushes == []


def test_next_charge_pushes_again(tmp_path):
    r = Rig(tmp_path)
    r.feed(40, 30)
    r.feed(1, 10)
    r.feed(1, 60)
    r.feed(55, 40)                    # used, docked again
    assert r.phase == "charging"
    r.feed(0.5, 10)
    assert len(r.pushes) == 2


def test_push_setting_off_still_tracks(tmp_path):
    r = Rig(tmp_path, appliance_done=False)
    r.feed(40, 10)
    r.feed(1, 10)
    assert r.phase == "charged" and r.pushes == []


def test_charged_push_is_held_in_quiet_hours(tmp_path):
    sent = []

    class Pusher:
        async def notify(self, p):
            sent.append(p)

    r = Rig(tmp_path, t=NIGHT)
    quiet = Quiet(HeldStore(str(tmp_path / "a.db")), lambda: r.settings, Pusher(), r.clock, LON)

    async def notify(p, category):
        await quiet.notify(p, category)
    r.app.notify = notify
    r.feed(40, 30)
    r.feed(1, 10)
    assert r.phase == "charged" and sent == [] and quiet.state()["held"] == 1


# ---------------- auto-off ----------------
def test_auto_off_only_when_opted_in_and_only_once(tmp_path):
    r = Rig(tmp_path, piece(plug="switch.fan", auto_off=True))
    r.feed(40, 30)
    r.feed(1, 9)
    assert r.offs == []
    r.feed(1, 1.5)
    assert r.offs == ["switch.fan"] and r.phase == "charged" and r.charge()["off_done"] is True
    assert r.store.get("appliance_charges")["h1|switch.fan"]["off_done"] is True
    assert len(r.pushes) == 1 and "actions" not in r.pushes[0] and "Switching its plug off" in r.pushes[0]["body"]
    r.feed(0, 10, state="off")        # our call landed
    r.feed(1, 30)                     # switched back on by hand, still docked and full: left alone
    r.make()                          # and after a restart
    r.feed(1, 30)
    assert r.offs == ["switch.fan"] and len(r.pushes) == 1
    r.feed(45, 20)                    # a new charge: one more call when it's done
    r.feed(1, 10)
    assert r.offs == ["switch.fan"] * 2 and len(r.pushes) == 2


def test_auto_off_failure_is_not_retried(tmp_path):
    r = Rig(tmp_path, piece(plug="switch.fan", auto_off=True), fail_off=True)
    r.feed(40, 10)
    r.feed(1, 10)
    r.feed(1, 60)
    assert r.offs == ["switch.fan"] and r.phase == "charged"


def test_auto_off_switched_off_before_charged_never_calls(tmp_path):
    r = Rig(tmp_path, piece(plug="switch.fan", auto_off=False))
    r.feed(40, 10)
    r.feed(1, 30)
    assert r.offs == [] and len(r.pushes) == 1


def test_unlinking_forgets_the_charge(tmp_path):
    r = Rig(tmp_path)
    r.feed(40, 5)
    r.layout = lay(piece())
    r.tick()
    assert r.app.charges == {} and r.store.get("appliance_charges") == {}


# ---------------- home server ----------------
def server(**kw):
    return piece("srv", "home_server", **{"w": 0.4, "h": 0.4, "plug": "switch.fan", **kw})


def test_server_power_loss_pushes_once(tmp_path):
    r = Rig(tmp_path, server())
    r.feed(35, 30)
    assert r.pushes == []
    r.feed(0.0, 4.5)                  # 0 W for 4½ min: nothing yet
    assert r.pushes == []
    r.feed(0.0, 1)
    assert [p["title"] for p in r.pushes] == ["Home server lost power"] and r.categories == ["safety"]
    r.feed(0.0, 60)
    r.make()                          # a restart while still down: no repeat
    r.feed(None, 30, state="unavailable")
    assert len(r.pushes) == 1
    r.feed(36, 1)                     # back: re-armed
    r.feed(36, 0, state="unavailable")
    r.feed(None, 5, state="unavailable")
    assert len(r.pushes) == 2 and r.categories == ["safety", "safety"]


def test_server_watchdog_holds_without_a_reading_and_ignores_others(tmp_path):
    r = Rig(tmp_path, server())
    asyncio.run(r.app.tick({}))       # its plug missing from HA's list for a moment: hold
    r.clock.t += 3600
    asyncio.run(r.app.tick({}))
    assert r.pushes == []
    r.feed(None, 30)                  # on, plug without a power sensor: fine
    assert r.pushes == []
    r2 = Rig(tmp_path / "pc", piece("pc", "desktop_pc", plug="switch.fan"))
    r2.feed(0, 60)                    # a PC at 0 W is just off: no push
    assert r2.pushes == []


def test_server_safety_push_bypasses_quiet_hours(tmp_path):
    sent = []

    class Pusher:
        async def notify(self, p):
            sent.append(p)

    r = Rig(tmp_path, server(), t=NIGHT)
    quiet = Quiet(HeldStore(str(tmp_path / "a.db")), lambda: r.settings, Pusher(), r.clock, LON)

    async def notify(p, category):
        await quiet.notify(p, category)
    r.app.notify = notify
    r.feed(0, 0, state="off")
    r.feed(0, 6, state="off")
    assert [p["title"] for p in sent] == ["Home server lost power"]


def test_server_is_protected_from_all_off_away_and_standby(tmp_path):
    layout = {**lay(server(), piece("pc", "desktop_pc", plug="switch.kettle")), "settings": {"keep_on": []}}
    assert protected_plugs(layout) == {"switch.fan"}
    assert blocked_reason("switch.fan", layout) == "server" and blocked_reason("switch.kettle", layout) is None
    # A plug enabled for the saver before the server was linked to it is switched off the saver, never the plug.
    from backend.discovery import Device
    store = AutoStore(str(tmp_path / "s.db"))
    calls = []

    async def call(domain, service, data):
        calls.append((domain, service, data))
    saver = StandbySaver(store, call, None, lambda: layout, lambda: {"mode": "home"}, Clock(), LON)
    saver.put_plug("switch.fan", {"enabled": True, "threshold_w": 5.0, "off_at": "01:00", "on_at": "07:00"})
    devices = {"switch.fan": Device("switch.fan", "plug", "Server plug", "P110", related={"power": "sensor.fan_power"})}
    asyncio.run(saver.tick(devices, {"switch.fan": {"state": "on"}}))
    assert calls == [] and not saver.plug("switch.fan")["enabled"]
    assert saver.logs()[0]["note"] == "a home server is linked to it"


def test_api_away_and_standby_leave_the_server_alone(client, fake_ha):
    base = {"unit": "m", "rooms": [], "placements": []}
    assert client.put("/api/layout", json={**base, "furniture": [server(plug="switch.kettle")]}).status_code == 200
    r = client.put("/api/standby/switch.kettle", json={"enabled": True})
    assert r.status_code == 400 and "home server" in r.json()["detail"]
    r = client.post("/api/mode", json={"mode": "away"}).json()
    assert "switch.kettle" in r["kept_on"] and "switch.kettle" not in r["turned_off"]
    assert client.get("/api/appliances").json()["appliances"]["srv"]["status"].startswith("Running")


def test_automations_wire_auto_off_to_a_turn_off_call(client, fake_ha):
    """The app's Appliances get a turn_off that calls HA's <domain>.turn_off on just that plug."""
    autos = client.app.state.automations
    asyncio.run(autos.appliances.turn_off("switch.kettle"))
    assert fake_ha.service_calls()[-1] == ("/api/services/switch/turn_off", {"entity_id": "switch.kettle"})


# ---------------- stats ----------------
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=LON)


def segs_from(points, end=NOW):
    out = []
    for i, (t, v) in enumerate(points):
        t1 = points[i + 1][0] if i + 1 < len(points) else end
        out.append((int(t.timestamp() * 1000), int(t1.timestamp() * 1000), v))
    return out


def test_charge_stats():
    pts = [(datetime(2026, 10, 5, 0, tzinfo=LON), 0.5)]
    for d, h, mins in ((8, 9, 120), (12, 10, 90), (13, 18, 150)):   # one last week, two this week
        t = datetime(2026, 10, d, h, tzinfo=LON)
        pts += [(t, 45.0), (t + timedelta(minutes=mins - 30), 6.0), (t + timedelta(minutes=mins), 1.0)]
    t = datetime(2026, 10, 13, 20, tzinfo=LON)
    pts += [(t, 0.5)]
    pts += [(datetime(2026, 10, 14, 11, 30, tzinfo=LON), 40.0)]       # charging now: not counted
    segs = segs_from(pts)
    assert len(find_charges(segs, HTH)) == 3
    out = compute(piece(plug="switch.fan"), segs, NOW, LON, 25.0)
    assert out["charges"] == {"this_week": 2, "avg_min": 120}


def test_desktop_pc_hours_count_only_above_sleep():
    pts = [(datetime(2026, 10, 12, 0, tzinfo=LON), 4.0),               # asleep at 4 W: not "on"
           (datetime(2026, 10, 12, 9, tzinfo=LON), 150.0), (datetime(2026, 10, 12, 13, tzinfo=LON), 4.0)]
    out = compute(piece("pc", "desktop_pc", plug="switch.fan"), segs_from(pts), NOW, LON, None)
    assert out["hours_week"] == 4.0
    out = compute(server(), segs_from([(datetime(2026, 10, 12, 0, tzinfo=LON), 30.0)]), NOW, LON, None)
    assert out["hours_week"] == 60.0
