"""Presence lighting (backend/presence_lighting.py): door opens after dark -> room lights on, off after a quiet period,
never against a manual change, never while Away. Timing driven by an injected clock."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backend.automations import AutoStore
from backend.discovery import Device
from backend.presence_lighting import (GAP, OFF_TRIES, RETRY, LightingError, PresenceLighting, room_sensors,
                                       validate_room, validate_settings)

LON = ZoneInfo("Europe/London")
DOOR, WIN, HALL_DOOR = "binary_sensor.door", "binary_sensor.window", "binary_sensor.hall_door"
K1, K2, LOUNGE, PLUG = "light.k1", "light.k2", "light.lounge", "switch.fridge"
LAYOUT = {
    "rooms": [{"id": "kitchen", "name": "Kitchen", "x": 0, "y": 0, "w": 3, "h": 3},
              {"id": "lounge", "name": "Lounge", "x": 3, "y": 0, "w": 4, "h": 3}],
    "placements": [{"entity_id": K1, "x": 1, "y": 1}, {"entity_id": K2, "x": 2, "y": 2},
                   {"entity_id": LOUNGE, "x": 5, "y": 1}, {"entity_id": PLUG, "x": 1, "y": 2}],
    "openings": [{"id": "d1", "type": "door", "x": 0, "y": 1, "len": 0.9, "orient": "v", "entity_id": DOOR},
                 {"id": "w1", "type": "window", "x": 1, "y": 0, "len": 1, "orient": "h", "entity_id": WIN},
                 {"id": "d2", "type": "door", "x": 3, "y": 1, "len": 0.9, "orient": "v", "entity_id": HALL_DOOR}],
}


def ts(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=LON).timestamp()


NIGHT = ts(2026, 10, 9, 21)   # after sunset in London
DAY = ts(2026, 10, 9, 13)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Rig:
    """A tiny Home Assistant: service calls change the states and are echoed to observe(), like Live does."""

    def __init__(self, tmp_path, t=NIGHT, sun=None):
        self.store = AutoStore(str(tmp_path / "db.sqlite"))
        self.clock = Clock(t)
        self.mode = {"mode": "home"}
        self.layout = LAYOUT
        self.devices = {e: Device(e, "light", e.split(".")[1].title(), "L530") for e in (K1, K2, LOUNGE)}
        self.devices.update({e: Device(e, "sensor", e.split(".")[1].title(), "T110") for e in (DOOR, WIN, HALL_DOOR)})
        self.devices[PLUG] = Device(PLUG, "plug", "Fridge", "P110")
        self.states = {e: {"state": "off"} for e in self.devices}
        if sun:
            self.states["sun.sun"] = {"state": sun, "attributes": {}}
        self.calls, self.fail = [], 0
        self.p = self.make()

    def make(self):
        async def call(domain, service, data):
            self.calls.append((domain, service, data))
            if self.fail:
                self.fail -= 1
                raise RuntimeError("HA down")
            for e in data["entity_id"]:
                self.set(e, "on" if service == "turn_on" else "off")
        p = PresenceLighting(self.store, call, lambda: self.layout, lambda: self.mode, lambda: {"lat": 51.5074, "lon": -0.1278},
                             self.clock, LON)
        for e, d in self.devices.items():  # Live's first load: everything seen once, nothing counts as a change
            p.observe({"entity_id": e, "kind": d.kind, "state": self.states[e]["state"]})
        return p

    def set(self, eid, state):
        self.states[eid] = {**self.states.get(eid, {}), "state": state}
        self.p.observe({"entity_id": eid, "kind": self.devices[eid].kind, "state": state})

    def enable(self, room="kitchen", **cfg):
        self.p.put_settings({"enabled": True})
        self.p.put_room(room, room.title(), validate_room({"enabled": True, **cfg}, self.p.room(room), self.devices))

    def tick(self, advance=0):
        self.clock.t += advance
        return asyncio.run(self.p.tick(self.devices, self.states))

    def door(self, advance=0, eid=DOOR):
        """Open, tick, close (a person walking through)."""
        self.clock.t += advance
        self.set(eid, "on")
        out = self.tick()
        self.set(eid, "off")
        return out

    def on_calls(self):
        return [c for c in self.calls if c[1] == "turn_on"]

    def off_calls(self):
        return [c for c in self.calls if c[1] == "turn_off"]


def test_validation():
    assert validate_settings({"enabled": True}) == {"enabled": True}
    for bad in ({}, {"enabled": 1}, {"enabled": True, "x": 1}, []):
        with pytest.raises(LightingError):
            validate_settings(bad)


def test_room_validation(tmp_path):
    r = Rig(tmp_path)
    cur = r.p.room("kitchen")
    assert cur == {"enabled": False, "sensors": None, "lights": None, "quiet_minutes": 10}
    assert validate_room({"quiet_minutes": 3, "lights": [K2, K1, K1]}, cur, r.devices)["lights"] == [K1, K2]
    assert validate_room({"sensors": None}, {**cur, "sensors": [DOOR]}, r.devices)["sensors"] is None
    for bad in ({}, {"x": 1}, {"enabled": "yes"}, {"quiet_minutes": 0}, {"quiet_minutes": 121}, {"quiet_minutes": 2.5},
                {"quiet_minutes": True}, {"lights": [PLUG]}, {"lights": [DOOR]}, {"sensors": [K1]}, {"lights": "light.k1"},
                {"sensors": ["binary_sensor.nope"]}, {"lights": [f"light.{i}" for i in range(31)]}):
        with pytest.raises(LightingError):
            validate_room(bad, cur, r.devices)


def test_defaults_from_the_plan(tmp_path):
    r = Rig(tmp_path)
    kitchen = LAYOUT["rooms"][0]
    assert room_sensors(LAYOUT, kitchen) == [DOOR, HALL_DOOR]  # doors on its walls (the shared one too), not the window
    assert r.p.resolved(LAYOUT, kitchen, r.p.room("kitchen"), r.devices) == ([DOOR, HALL_DOOR], [K1, K2])  # no plug


def test_off_by_default(tmp_path):
    r = Rig(tmp_path)
    r.door()
    r.p.put_settings({"enabled": True})  # master on, no room enabled
    r.door(5)
    assert r.calls == [] and r.p.next_due() is None


def test_door_after_dark_lights_on_then_off_after_quiet(tmp_path):
    r = Rig(tmp_path)
    r.enable(quiet_minutes=5)
    r.states[K2]["state"] = "on"  # already on: never ours
    r.p.observe({"entity_id": K2, "kind": "light", "state": "on"})
    r.tick(1)
    assert r.calls == []  # K2 by hand before anything was ours: no switch-on yet, room paused only briefly
    r.tick(5 * 60)
    calls = r.door(1)
    assert calls == [{"domain": "light", "service": "turn_on", "data": {"entity_id": [K1]}, "ok": True}]
    assert r.p.state["kitchen"]["owned"] == [K1]
    assert r.p.next_due() == pytest.approx(r.clock.t + 5 * 60)
    r.tick(4 * 60)
    assert r.off_calls() == []
    # door activity (a close counts too) restarts the quiet period
    r.set(DOOR, "on"); r.tick(); r.set(DOOR, "off"); r.tick(30)
    r.tick(4 * 60)
    assert r.off_calls() == []
    r.tick(31)
    assert r.off_calls() == [("light", "turn_off", {"entity_id": [K1]})]  # K2 (already on) is left alone
    assert r.states[K2]["state"] == "on" and r.p.state["kitchen"]["owned"] == []
    r.tick(600)
    assert len(r.calls) == 2  # never repeated
    assert [x["action"] for x in r.store.get("plighting_log")][:2] == ["off", "on"]


def test_daylight_does_nothing_but_counts_as_activity(tmp_path):
    r = Rig(tmp_path, t=DAY)
    r.enable()
    r.door()
    r.door(3600)
    assert r.calls == []
    assert r.p.dark(r.states, r.clock.t)["dark"] is False


def test_computed_dusk_in_london(tmp_path):
    r = Rig(tmp_path, t=ts(2026, 10, 9, 18, 0))  # sunset in London on 9 Oct 2026 is about 18:24 BST
    d = r.p.dark(r.states, r.clock.t)
    assert d["source"] == "computed" and not d["dark"]
    assert ts(2026, 10, 9, 18, 20) < d["until"] < ts(2026, 10, 9, 18, 30)
    d = r.p.dark(r.states, ts(2026, 10, 9, 18, 40))
    assert d["dark"] and ts(2026, 10, 10, 7, 5) < d["until"] < ts(2026, 10, 10, 7, 20)  # next sunrise ~07:13
    assert r.p.dark(r.states, ts(2026, 10, 10, 5, 0))["dark"]  # before sunrise
    r.enable()
    r.door()
    assert r.calls == []
    r.door(40 * 60)  # 18:40: dark now
    assert len(r.on_calls()) == 1


def test_home_assistant_sun_wins(tmp_path):
    r = Rig(tmp_path, t=DAY, sun="below_horizon")  # e.g. HA configured somewhere else: its sun is the truth
    r.states["sun.sun"]["attributes"] = {"next_rising": "2026-10-10T06:13:00+00:00"}
    d = r.p.dark(r.states, r.clock.t)
    assert d == {"dark": True, "source": "ha", "until": datetime(2026, 10, 10, 6, 13, tzinfo=ZoneInfo("UTC")).timestamp()}
    r.enable()
    r.door()
    assert len(r.on_calls()) == 1
    r2 = Rig(tmp_path / "b", t=NIGHT, sun="above_horizon")
    r2.enable()
    r2.door()
    assert r2.calls == []


def test_manual_change_pauses_until_next_quiet_period(tmp_path):
    r = Rig(tmp_path)
    r.enable(quiet_minutes=5)
    r.door()
    assert r.p.state["kitchen"]["owned"] == [K1, K2]
    r.tick(120)
    r.set(K1, "off")  # by hand: the room is the user's now
    r.tick()
    st = r.p.state["kitchen"]
    assert st["owned"] == [] and st["paused_at"] == r.clock.t
    # paused: door openings within the quiet period do nothing, and each one extends it
    r.door(60)
    r.door(4 * 60)
    r.door(4 * 60)
    assert len(r.on_calls()) == 1 and r.off_calls() == []
    assert r.p.next_due() == pytest.approx(r.clock.t + 5 * 60)
    r.tick(10 * 60)
    assert r.off_calls() == []  # K2 stays on: never switched off against the user
    assert r.p.state["kitchen"]["paused_at"] is None  # 5 min with no door activity: automatic again
    r.set(K2, "off"); r.tick()  # the user switches it off later on: paused again (quietly)
    r.door(5 * 60)
    assert len(r.on_calls()) == 2
    acts = [x["action"] for x in r.store.get("plighting_log")]
    assert acts[:4] == ["on", "resumed", "paused", "resumed"], acts


def test_door_after_a_long_pause_resumes_and_switches_on(tmp_path):
    r = Rig(tmp_path)
    r.enable(quiet_minutes=5)
    r.set(K1, "on"); r.tick()
    r.set(K1, "off"); r.tick()
    assert r.p.state["kitchen"]["paused_at"]
    r.door(3600)  # the loop slept through the expiry: the opening itself must not keep it paused
    assert len(r.on_calls()) == 1


def test_our_own_change_is_not_manual(tmp_path):
    r = Rig(tmp_path)
    r.enable(quiet_minutes=1)
    r.door()
    r.tick(61)
    assert len(r.off_calls()) == 1
    assert r.p.state["kitchen"]["paused_at"] is None  # neither the on nor the off counted as a hand change


def test_never_while_away(tmp_path):
    r = Rig(tmp_path)
    r.enable(quiet_minutes=1)
    r.mode["mode"] = "away"
    r.door()
    assert r.calls == []
    r.mode["mode"] = "home"
    r.door(5)
    assert len(r.on_calls()) == 1
    r.mode["mode"] = "away"  # Away switches everything off itself; the room forgets what it owned
    r.set(K1, "off"); r.set(K2, "off")
    r.tick(120)
    assert r.p.state["kitchen"]["owned"] == [] and r.p.state["kitchen"]["paused_at"] is None
    assert r.off_calls() == []
    r.mode["mode"] = "home"
    r.door(5)  # Away's switch-off doesn't count as a hand change: home again, the door works straight away
    assert len(r.on_calls()) == 2


def test_debounced_and_never_loops(tmp_path):
    r = Rig(tmp_path)
    r.enable(quiet_minutes=1)
    r.door()
    r.states[K1]["state"] = "off"  # (HA lost it without telling us)
    r.door(1)
    r.door(1)
    assert len(r.on_calls()) == 1  # within GAP
    r.door(GAP)
    assert len(r.on_calls()) == 2


def test_failed_switch_on_not_retried(tmp_path):
    r = Rig(tmp_path)
    r.enable()
    r.fail = 1
    r.door()
    r.tick(5); r.tick(5)
    assert len(r.on_calls()) == 1  # no retry by itself
    r.door(GAP)
    assert len(r.on_calls()) == 1  # back off RETRY after a failure
    r.door(RETRY)
    assert len(r.on_calls()) == 2 and r.p.state["kitchen"]["owned"] == [K1, K2]


def test_failed_switch_off_backs_off_then_gives_up(tmp_path):
    r = Rig(tmp_path)
    r.enable(quiet_minutes=1)
    r.door()
    r.fail = 10
    r.tick(60)
    assert len(r.off_calls()) == 1
    r.tick(30)
    assert len(r.off_calls()) == 1
    r.tick(RETRY)
    assert len(r.off_calls()) == 2
    r.tick(2 * RETRY)
    assert len(r.off_calls()) == OFF_TRIES
    r.tick(3600)
    assert len(r.off_calls()) == OFF_TRIES and r.p.state["kitchen"]["owned"] == []


def test_restart_keeps_ownership_and_does_not_replay(tmp_path):
    r = Rig(tmp_path)
    r.enable(quiet_minutes=2)
    r.door()
    r.p = r.make()  # restart
    r.tick(1)
    assert len(r.on_calls()) == 1
    r.tick(2 * 60)
    assert len(r.off_calls()) == 1


def test_switching_off_forgets_everything(tmp_path):
    r = Rig(tmp_path)
    r.enable(quiet_minutes=1)
    r.door()
    r.p.put_settings({"enabled": False})
    r.tick(3600)
    r.p.put_settings({"enabled": True})
    r.tick(3600)
    assert r.off_calls() == []  # the lights it had switched on are the user's now
    r.enable(quiet_minutes=1)
    r.p.put_room("kitchen", "Kitchen", {**r.p.room("kitchen"), "enabled": False})
    r.door(5)
    assert len(r.on_calls()) == 1


def test_chosen_sensors_and_lights(tmp_path):
    r = Rig(tmp_path)
    r.enable(sensors=[HALL_DOOR], lights=[K2])
    r.door()
    assert r.calls == []
    r.door(1, eid=HALL_DOOR)
    assert r.on_calls() == [("light", "turn_on", {"entity_id": [K2]})]
    r.set(K1, "off"); r.set(K1, "on"); r.tick()  # a light it doesn't use: not a manual change of this room
    assert r.p.state["kitchen"]["paused_at"] is None


def test_room_removed_from_plan(tmp_path):
    r = Rig(tmp_path)
    r.enable()
    r.layout = {**LAYOUT, "rooms": LAYOUT["rooms"][1:]}
    r.door()
    assert r.calls == []


def test_status(tmp_path):
    r = Rig(tmp_path)
    r.enable(quiet_minutes=3)
    s = r.p.status(r.devices, r.states)
    k = s["rooms"][0]
    assert s["enabled"] and s["dark"]["dark"] and s["dark"]["source"] == "computed"
    assert (k["id"], k["phase"], k["auto_sensors"], k["sensors"], k["lights"]) == ("kitchen", "ready", True, [DOOR, HALL_DOOR], [K1, K2])
    assert s["rooms"][1]["phase"] == "off"
    assert [x["entity_id"] for x in s["all_lights"]] == [K1, K2, LOUNGE]  # never the plug
    r.door()
    k = r.p.status(r.devices, r.states)["rooms"][0]
    assert k["phase"] == "on" and k["off_at"] == int((r.clock.t + 180) * 1000)
    r.mode["mode"] = "away"
    assert r.p.status(r.devices, r.states)["rooms"][0]["phase"] == "away"


# ---------------- API ----------------
def test_api_and_roles(client):
    layout = {"rooms": LAYOUT["rooms"], "placements": [{"entity_id": "light.kitchen_1", "x": 1, "y": 1}],
              "openings": [{**LAYOUT["openings"][0], "entity_id": "binary_sensor.contact_sensor_door"}]}
    r = client.put("/api/layout", json=layout)
    assert r.status_code == 200, r.text
    d = client.get("/api/presence-lighting").json()
    assert d["enabled"] is False and [r["id"] for r in d["rooms"]] == ["kitchen", "lounge"]
    assert all(r["phase"] == "off" for r in d["rooms"])
    r = client.put("/api/presence-lighting/settings", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert client.put("/api/presence-lighting/settings", json={"enabled": "y"}).status_code == 400
    r = client.put("/api/presence-lighting/rooms/kitchen", json={"enabled": True, "quiet_minutes": 7})
    assert r.status_code == 200, r.text
    k = r.json()["rooms"][0]
    assert k["enabled"] and k["quiet_minutes"] == 7 and k["phase"] in ("ready", "daylight")
    assert (k["sensors"], k["lights"]) == (["binary_sensor.contact_sensor_door"], ["light.kitchen_1"])
    assert client.put("/api/presence-lighting/rooms/nope", json={"enabled": True}).status_code == 404
    assert client.put("/api/presence-lighting/rooms/kitchen", json={"lights": ["switch.fan"]}).status_code == 400
    assert client.put("/api/presence-lighting/rooms/kitchen", json={"lights": ["light.strip"]}).json()["rooms"][0]["lights"] == ["light.strip"]
    for name, role, pw in (("mia", "member", "member-pass-1"), ("gus", "guest", "guest-pass-1")):
        assert client.post("/api/users", json={"username": name, "password": pw, "role": role}).status_code == 200
    import base64
    hdr = lambda u, p: {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}
    member, guest = hdr("mia", "member-pass-1"), hdr("gus", "guest-pass-1")
    assert client.get("/api/presence-lighting", headers=member).status_code == 200
    assert client.put("/api/presence-lighting/settings", json={"enabled": False}, headers=member).status_code == 403
    assert client.put("/api/presence-lighting/rooms/kitchen", json={"enabled": False}, headers=member).status_code == 403
    assert client.get("/api/presence-lighting", headers=guest).status_code == 403
    assert client.put("/api/presence-lighting/settings", json={"enabled": False}, headers=guest).status_code == 403
    assert client.get("/api/presence-lighting").json()["enabled"] is True


def test_through_the_automations_loop(client, fake_ha):
    """Live -> observer -> automations tick -> one light.turn_on, attributed to presence lighting."""
    layout = {"rooms": LAYOUT["rooms"], "placements": [{"entity_id": "light.strip", "x": 1, "y": 1}],
              "openings": [{**LAYOUT["openings"][0], "entity_id": "binary_sensor.contact_sensor_door"}]}
    assert client.put("/api/layout", json=layout).status_code == 200
    client.put("/api/presence-lighting/settings", json={"enabled": True})
    client.put("/api/presence-lighting/rooms/kitchen", json={"enabled": True})
    automations, live = client.app.state.automations, client.app.state.automations.live
    client.get("/api/devices")
    live.states["sun.sun"] = {"entity_id": "sun.sun", "state": "below_horizon", "attributes": {}}
    asyncio.run(automations.tick())
    fake_ha.calls.clear()
    door = "binary_sensor.contact_sensor_door"
    for state in ("off", "on"):  # the fixture's door starts open: close it, then a real opening
        live.handle({"type": "event", "event": {"event_type": "state_changed", "data": {
            "entity_id": door, "new_state": {"entity_id": door, "state": state, "attributes": {}}}}})
    assert automations.wake.is_set()
    asyncio.run(automations.tick())
    calls = fake_ha.service_calls()
    assert calls == [("/api/services/light/turn_on", {"entity_id": ["light.strip"]})]
    asyncio.run(automations.tick())
    assert len(fake_ha.service_calls()) == 1
