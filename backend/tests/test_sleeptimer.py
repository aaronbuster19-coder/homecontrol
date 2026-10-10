"""Sleep timers (backend/sleeptimer.py): with an injected clock — firing on time, only what is still on, one call per
domain, replace / cancel, rooms, protected plugs, bounded retries, surviving a restart (and dropping stale ones), roles,
and the API."""
import asyncio
import base64

import pytest

from backend import sleeptimer as tm
from backend.discovery import Device
from backend.sleeptimer import SleepTimers, TimerError, TimerStore

TV_SF = 128 | 256


def st(state, **a):
    return {"state": state, "attributes": a}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


DEVS = {e: Device(e, k, n, "x") for e, k, n in (
    ("light.bed", "light", "Bed lamp"), ("light.lounge", "light", "Lounge lamp"), ("switch.fan", "plug", "Fan"),
    ("switch.fridge", "plug", "Fridge plug"), ("media_player.tv", "media", "TV"), ("media_player.dumb", "media", "Speaker"),
    ("climate.valve", "valve", "Valve"))}
LAYOUT = {"rooms": [{"id": "bed", "name": "Bedroom", "x": 0, "y": 0, "w": 4, "h": 4},
                    {"id": "empty", "name": "Box room", "x": 9, "y": 9, "w": 1, "h": 1}],
          "placements": [{"entity_id": "light.bed", "x": 1, "y": 1}, {"entity_id": "switch.fan", "x": 2, "y": 2},
                         {"entity_id": "switch.fridge", "x": 3, "y": 3}, {"entity_id": "climate.valve", "x": 1, "y": 3},
                         {"entity_id": "light.lounge", "x": 6, "y": 6}],
          "furniture": [{"id": "f1", "type": "fridge", "x": 3, "y": 3, "w": 1, "h": 1, "plug": "switch.fridge"},
                        {"id": "f2", "type": "tv", "x": 2, "y": 3.5, "w": 1, "h": 0.2, "media": "media_player.tv"}]}


class Rig:
    def __init__(self, tmp_path):
        self.clock = Clock()
        self.states = {"light.bed": st("on"), "light.lounge": st("on"), "switch.fan": st("on"), "switch.fridge": st("on"),
                       "media_player.tv": st("on", supported_features=TV_SF), "media_player.dumb": st("on", supported_features=0),
                       "climate.valve": st("heat")}
        self.calls, self.fail, self.events, self.records, self.interrupts = [], 0, [], [], []
        self.path = str(tmp_path / "t.db")
        self.layout = LAYOUT
        self.t = self.make()

    def make(self):
        async def call(domain, service, data):
            if self.fail:
                self.fail -= 1
                raise RuntimeError("HA down")
            self.calls.append((domain, service, data))
            for e in data["entity_id"]:
                self.states[e] = {**self.states[e], "state": "off"}

        async def devices():
            return DEVS

        async def interrupt(reason, ids):
            self.interrupts.append((reason, ids))

        return SleepTimers(TimerStore(self.path), call, devices, lambda: self.states, lambda: self.layout,
                           lambda ev, data: self.events.append((ev, data)), lambda kind, **kw: self.records.append(kw),
                           interrupt, self.clock)

    def set(self, role="admin", **body):
        return asyncio.run(self.t.set(body, role, "me"))

    def tick(self):
        return asyncio.run(self.t.tick())


def test_fires_on_time_once(tmp_path):
    r = Rig(tmp_path)
    t = r.set(entity_id="light.bed", minutes=15)
    assert t["ends_at"] == (r.clock.t + 900) * 1000 and t["name"] == "Bed lamp"
    r.clock.t += 899
    assert r.tick() == 0 and r.calls == []
    r.clock.t += 1
    assert r.tick() == 1
    assert r.calls == [("light", "turn_off", {"entity_id": ["light.bed"]})]
    assert r.interrupts == [("manual", ["light.bed"])]
    r.clock.t += 3600
    assert r.tick() == 0 and len(r.calls) == 1  # gone: never fires twice
    assert r.t.status()["timers"] == [] and r.t.last["name"] == "Bed lamp" and r.t.last["ok"]
    assert [x["event"] for x in r.records] == ["set", "fired"]


def test_already_off_sends_nothing(tmp_path):
    r = Rig(tmp_path)
    r.set(entity_id="switch.fan", minutes=1)
    r.states["switch.fan"] = st("off")
    r.clock.t += 60
    assert r.tick() == 1 and r.calls == []


def test_replace_and_cancel(tmp_path):
    r = Rig(tmp_path)
    a = r.set(entity_id="light.bed", minutes=60)
    b = r.set(entity_id="light.bed", minutes=15)
    assert a["id"] == b["id"] and len(r.t.status()["timers"]) == 1
    assert r.t.status()["timers"][0]["minutes"] == 15
    assert asyncio.run(r.t.cancel(b["id"]))
    assert not asyncio.run(r.t.cancel(b["id"]))
    r.clock.t += 7200
    assert r.tick() == 0 and r.calls == []
    assert any(ev == "timers" for ev, _ in r.events)


def test_room_timer_takes_what_can_be_switched_off(tmp_path):
    r = Rig(tmp_path)
    t = r.set(room="bed", minutes=30)
    assert t["target"] == "room" and t["name"] == "Bedroom"
    assert t["entity_ids"] == ["light.bed", "media_player.tv", "switch.fan"]  # not the fridge, not the valve
    r.clock.t += 1800
    r.tick()
    assert r.calls == [("light", "turn_off", {"entity_id": ["light.bed"]}),
                       ("media_player", "turn_off", {"entity_id": ["media_player.tv"]}),
                       ("switch", "turn_off", {"entity_id": ["switch.fan"]})]
    assert r.states["switch.fridge"]["state"] == "on"
    with pytest.raises(TimerError, match="Nothing in Box room"):
        r.set(room="empty", minutes=5)
    assert r.set(room="bed", minutes=5, role="guest")["entity_ids"] == ["light.bed"]  # guests: the lights only


@pytest.mark.parametrize("body,msg", [
    ({"entity_id": "switch.fridge", "minutes": 15}, "protected"),
    ({"entity_id": "climate.valve", "minutes": 15}, "can't have a sleep timer"),
    ({"entity_id": "media_player.dumb", "minutes": 15}, "can't be turned off"),
    ({"entity_id": "light.nope", "minutes": 15}, "unknown device"),
    ({"room": "attic", "minutes": 15}, "unknown room"),
    ({"minutes": 15}, "entity_id or room"),
    ({"entity_id": "light.bed", "room": "bed", "minutes": 15}, "entity_id or room"),
    ({"entity_id": "light.bed", "minutes": 0}, "minutes"),
    ({"entity_id": "light.bed", "minutes": 721}, "minutes"),
    ({"entity_id": "light.bed", "minutes": 1.5}, "minutes"),
    ({"entity_id": "light.bed", "minutes": True}, "minutes"),
])
def test_rejects(tmp_path, body, msg):
    with pytest.raises(TimerError, match=msg):
        Rig(tmp_path).set(**body)


def test_plug_that_becomes_protected_is_skipped(tmp_path):
    r = Rig(tmp_path)
    r.set(entity_id="switch.fan", minutes=1)
    r.layout = {**LAYOUT, "settings": {"keep_on": ["switch.fan"]}}
    r.clock.t += 60
    assert r.tick() == 1 and r.calls == []


def test_failures_retry_a_bounded_number_of_times(tmp_path):
    r = Rig(tmp_path)
    r.set(entity_id="light.bed", minutes=1)
    r.fail = 99
    r.clock.t += 60
    for i in range(tm.TRIES):
        assert r.tick() == (1 if i == tm.TRIES - 1 else 0)
        assert r.t.status()["timers"] == [] or r.t.status()["timers"][0]["retrying"]
        r.clock.t += tm.RETRY - 1
        assert r.tick() == 0  # not before RETRY
        r.clock.t += 1
    assert r.t.status()["timers"] == [] and r.t.last == {**r.t.last, "ok": False}
    r.clock.t += 3600
    assert r.tick() == 0
    assert r.records[-1]["event"] == "failed"


def test_retry_after_a_failure_then_succeeds(tmp_path):
    r = Rig(tmp_path)
    r.set(entity_id="light.bed", minutes=1)
    r.fail = 1
    r.clock.t += 60
    assert r.tick() == 0
    r.clock.t += tm.RETRY
    assert r.tick() == 1 and r.calls == [("light", "turn_off", {"entity_id": ["light.bed"]})]


def test_survives_a_restart(tmp_path):
    r = Rig(tmp_path)
    t = r.set(entity_id="light.bed", minutes=60)
    r.clock.t += 1200
    r.t = r.make()  # a new process: same database, same clock
    assert r.t.recover() == []
    assert r.t.status()["timers"][0]["ends_at"] == t["ends_at"]  # the countdown didn't restart
    r.clock.t += 2400
    assert r.tick() == 1 and r.calls


def test_restart_fires_recent_and_drops_stale(tmp_path):
    r = Rig(tmp_path)
    r.set(entity_id="light.bed", minutes=10)
    r.set(entity_id="switch.fan", minutes=60)
    r.clock.t += 600 + tm.LATE_OK + 1  # the app was down: the lamp's came due over LATE_OK ago, the fan's not yet
    r.t = r.make()
    dropped = r.t.recover()
    assert [d["name"] for d in dropped] == ["Bed lamp"] and r.records[-1]["event"] == "missed"
    r.clock.t = 1_000_000.0 + 3600 + tm.LATE_OK - 1  # the fan's is overdue, but less than LATE_OK: still fires
    r.t = r.make()
    assert r.t.recover() == []
    assert r.tick() == 1 and r.calls == [("switch", "turn_off", {"entity_id": ["switch.fan"]})]


def test_guest_roles(tmp_path):
    r = Rig(tmp_path)
    with pytest.raises(tm.Forbidden):
        r.set(entity_id="switch.fan", minutes=5, role="guest")
    fan = r.set(entity_id="switch.fan", minutes=5)
    with pytest.raises(tm.Forbidden):
        asyncio.run(r.t.cancel(fan["id"], "guest"))
    lamp = r.set(entity_id="light.bed", minutes=5, role="guest")
    assert {t["id"]: t["may_cancel"] for t in r.t.status("guest")["timers"]} == {fan["id"]: False, lamp["id"]: True}
    assert asyncio.run(r.t.cancel(lamp["id"], "guest"))


def test_next_wait_is_capped(tmp_path):
    r = Rig(tmp_path)
    assert r.t.next_wait() == tm.TICK
    r.set(entity_id="light.bed", minutes=1)
    assert r.t.next_wait() == tm.TICK
    r.clock.t += 59.5
    assert r.t.next_wait() == pytest.approx(0.5)
    r.clock.t += 10
    assert r.t.next_wait() == 0.05


# ---------------- API ----------------
def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_api(client, fake_ha):
    assert client.post("/api/users", json={"username": "gus", "password": "guest-pass-1", "role": "guest"}).status_code == 200
    g = basic("gus", "guest-pass-1")
    r = client.post("/api/timers", json={"entity_id": "switch.kettle", "minutes": 30})
    assert r.status_code == 200, r.text
    kettle = r.json()
    assert kettle["minutes"] == 30 and kettle["ends_at"] > kettle["created_at"]
    assert client.post("/api/timers", json={"entity_id": "switch.kettle", "minutes": 30}, headers=g).status_code == 403
    assert client.post("/api/timers", json={"entity_id": "climate.lounge_valve", "minutes": 5}).status_code == 400
    lamp = client.post("/api/timers", json={"entity_id": "light.kitchen_1", "minutes": 15}, headers=g)
    assert lamp.status_code == 200, lamp.text
    listing = client.get("/api/timers", headers=g).json()
    assert {t["name"]: t["may_cancel"] for t in listing["timers"]} == {"Kettle": False, "Kitchen 1": True}
    assert listing["presets"] == [15, 30, 60]
    assert client.delete(f"/api/timers/{kettle['id']}", headers=g).status_code == 403
    assert client.delete(f"/api/timers/{lamp.json()['id']}", headers=g).status_code == 200
    assert client.delete(f"/api/timers/{kettle['id']}").status_code == 200
    assert client.delete(f"/api/timers/{kettle['id']}").status_code == 404
