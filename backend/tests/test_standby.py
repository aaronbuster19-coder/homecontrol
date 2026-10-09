"""Standby saver (backend/standby.py): night off only when idle, morning on only if it was ours, never replayed."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backend.automations import AutoStore
from backend.discovery import Device
from backend.standby import (DEFAULTS, SETTLE, StandbyError, StandbySaver, blocked_reason, default_threshold, hours_off,
                             validate_plug)

LON = ZoneInfo("Europe/London")
TV, LAMP, FRIDGE = "switch.tv", "switch.lamp", "switch.fridge"
PW = {TV: "sensor.tv_power", LAMP: "sensor.lamp_power", FRIDGE: "sensor.fridge_power"}


def ts(y, mo, d, h, mi=0, s=0, fold=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=LON, fold=fold).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeHA:
    """history(): one row per power sensor holding `hist[sensor]` W for the whole window (None: no data)."""

    def __init__(self):
        self.hist = {p: 3.0 for p in PW.values()}
        self.history_calls, self.fail_history = [], False

    async def history(self, start, end, ids, attributes=False):
        self.history_calls.append((start, end, list(ids)))
        if self.fail_history:
            raise RuntimeError("HA down")
        return [[{"entity_id": p, "state": str(self.hist[p]), "last_changed": start.isoformat()}]
                for p in ids if self.hist.get(p) is not None]


class Rig:
    def __init__(self, tmp_path, t=None):
        self.store = AutoStore(str(tmp_path / "db.sqlite"))
        self.clock = Clock(t or ts(2026, 10, 9, 20))
        self.ha = FakeHA()
        self.calls, self.fail_calls = [], set()
        self.mode = {"mode": "home"}
        self.layout = {"rooms": [], "placements": [], "settings": {"keep_on": []}, "furniture": []}
        self.devices = {e: Device(e, "plug", e.split(".")[1].title(), "P110", related={"power": PW[e]}) for e in PW}
        self.devices["light.x"] = Device("light.x", "light", "X", "L530")
        self.states = {}
        for e, p in PW.items():
            self.states[e] = {"state": "on"}
            self.states[p] = {"state": "3.0", "attributes": {"unit_of_measurement": "W"}}
        self.s = self.make()

    def make(self):
        return StandbySaver(self.store, self._call, self.ha, lambda: self.layout, lambda: self.mode, self.clock, LON)

    async def _call(self, domain, service, data):
        self.calls.append((domain, service, data["entity_id"]))
        if service in self.fail_calls:
            raise RuntimeError("HA error")
        for e in data["entity_id"]:
            self.states[e] = {"state": "on" if service == "turn_on" else "off"}

    def enable(self, eid=TV, **kw):
        p = validate_plug({"enabled": True, "threshold_w": 8.0, **kw}, self.s.plug(eid))
        return self.s.put_plug(eid, p, eid)

    def power(self, eid, w, history=...):
        self.states[PW[eid]] = {"state": str(w), "attributes": {"unit_of_measurement": "W"}}
        self.ha.hist[PW[eid]] = w if history is ... else history

    def at(self, h, mi=0, s=0, day=10, month=10, fold=0):
        self.clock.t = ts(2026, month, day, h, mi, s, fold)
        return asyncio.run(self.s.tick(self.devices, self.states))

    def logs(self):
        return [(x["entity_id"], x["action"]) for x in self.s.logs()]


# ---------------- validation / helpers ----------------
def test_validation_and_helpers():
    assert DEFAULTS == {"enabled": False, "threshold_w": None, "off_at": "01:00", "on_at": "07:00"}
    v = validate_plug({"enabled": True, "threshold_w": 7.26, "off_at": "00:30"}, {})
    assert v == {"enabled": True, "threshold_w": 7.3, "off_at": "00:30", "on_at": "07:00"}
    for bad in ({"enabled": 1}, {"threshold_w": 1}, {"threshold_w": 501}, {"threshold_w": "5"}, {"off_at": "1:00"},
                {"on_at": "01:00"}, {"nope": 1}, []):
        with pytest.raises(StandbyError):
            validate_plug(bad, {})
    assert default_threshold(4.0) == 9.0 and default_threshold(None) == 5.0 and default_threshold(0.0) == 5.0
    assert hours_off("01:00", "07:00") == 6 and hours_off("23:30", "06:00") == 6.5
    layout = {"settings": {"keep_on": [LAMP]}, "furniture": [{"id": "f", "type": "fridge", "plug": FRIDGE},
                                                             {"id": "g", "type": "sofa", "plug": TV}]}
    assert blocked_reason(LAMP, layout) == "keep on" and blocked_reason(FRIDGE, layout) == "fridge"
    assert blocked_reason(TV, layout) is None


# ---------------- night off / morning on ----------------
def test_off_when_idle_on_in_the_morning(tmp_path):
    r = Rig(tmp_path)
    r.enable()
    assert r.s.next_due() == ts(2026, 10, 10, 1)
    assert r.at(0, 59, 59) == [] and r.calls == []
    calls = r.at(1, 0)
    assert calls == [{"domain": "switch", "service": "turn_off", "data": {"entity_id": [TV]}, "ok": True}]
    start, end, ids = r.ha.history_calls[-1]
    assert ids == [PW[TV]] and (end - start).total_seconds() == 900  # the last 15 min
    for m in (0, 1):  # same occurrence again within the grace: nothing
        assert r.at(1, m, 30) == []
    assert r.s.next_due() == ts(2026, 10, 10, 7)
    assert r.at(7, 0) == [{"domain": "switch", "service": "turn_on", "data": {"entity_id": [TV]}, "ok": True}]
    assert r.at(7, 1) == [] and r.calls == [("switch", "turn_off", [TV]), ("switch", "turn_on", [TV])]
    assert r.logs()[:2] == [(TV, "on"), (TV, "off")]


def test_never_cuts_something_in_use(tmp_path):
    r = Rig(tmp_path)
    r.enable()
    r.power(TV, 3.0, history=60.0)  # watched TV until 00:55: the last 15 min weren't idle
    assert r.at(1) == [] and r.calls == []
    assert r.s.logs()[0]["note"].startswith("in use (60.0 W")
    assert r.at(7) == [] and r.calls == []  # not ours: never switched on either
    r.power(TV, 85.0, history=3.0)  # on right now
    assert r.at(1, day=11) == [] and r.calls == []
    r.power(TV, 3.0, history=None)  # no history: unknown is not idle
    assert r.at(1, day=12) == [] and r.calls == []
    r.ha.fail_history = True
    r.power(TV, 3.0)
    assert r.at(1, day=13) == [] and r.calls == []
    r.ha.fail_history = False
    r.states[PW[TV]] = {"state": "unavailable"}
    assert r.at(1, day=14) == [] and r.calls == []
    assert r.at(1, day=14) == []


def test_already_off_is_left_alone(tmp_path):
    r = Rig(tmp_path)
    r.enable()
    r.states[TV] = {"state": "off"}
    assert r.at(1) == [] and r.at(7) == [] and r.calls == []
    assert (TV, "skipped") in r.logs()


def test_manual_on_in_between_is_respected(tmp_path):
    r = Rig(tmp_path)
    r.enable()
    r.at(1)
    r.clock.t = ts(2026, 10, 10, 1, 0, 30)
    r.s.observe({"entity_id": TV, "state": "on", "name": "Tv"})  # our own call landing late: still ours
    assert r.s.state[TV]["owned"]
    r.clock.t = ts(2026, 10, 10, 3)
    r.s.observe({"entity_id": TV, "state": "on", "name": "Tv"})  # turned on by hand at 03:00 ...
    r.states[TV] = {"state": "off"}                              # ... and off again
    assert r.at(7) == [] and r.calls == [("switch", "turn_off", [TV])]
    assert (TV, "released") in r.logs()
    # the same seen only by a tick (no live event): still released
    r.states[TV] = {"state": "on"}
    assert r.at(1, day=11)[0]["service"] == "turn_off"
    r.states[TV] = {"state": "on"}
    assert r.at(2, day=11) == []
    r.states[TV] = {"state": "off"}
    assert r.at(7, day=11) == [] and len(r.calls) == 2 and r.calls[-1][1] == "turn_off"


def test_keep_on_and_fridge_plugs_can_never_run(tmp_path):
    r = Rig(tmp_path)
    r.enable(TV)
    r.enable(FRIDGE)
    r.layout["settings"]["keep_on"] = [TV]  # became keep-on after it was enabled
    r.layout["furniture"] = [{"id": "f1", "type": "fridge", "plug": FRIDGE}]
    assert r.at(1) == [] and r.calls == []
    assert not r.s.plug(TV)["enabled"] and not r.s.plug(FRIDGE)["enabled"]
    assert (TV, "disabled") in r.logs() and (FRIDGE, "disabled") in r.logs()


def test_away_keeps_saving_but_waits_to_switch_back_on(tmp_path):
    r = Rig(tmp_path)
    r.enable()
    r.mode = {"mode": "away"}
    assert r.at(1)[0]["service"] == "turn_off"  # saving runs while Away
    assert r.at(7) == [] and len(r.calls) == 1  # ... but nothing comes on in an empty flat
    assert r.s.state[TV]["owed"] and (TV, "waiting") in r.logs()
    assert r.at(12) == []
    r.mode = {"mode": "home"}
    assert r.at(18) == [{"domain": "switch", "service": "turn_on", "data": {"entity_id": [TV]}, "ok": True}]
    assert r.at(18, 1) == [] and len(r.calls) == 2


def test_several_plugs_one_call(tmp_path):
    r = Rig(tmp_path)
    r.enable(TV)
    r.enable(LAMP)
    assert r.at(1) == [{"domain": "switch", "service": "turn_off", "data": {"entity_id": [LAMP, TV]}, "ok": True}]
    assert r.at(7)[0]["data"] == {"entity_id": [LAMP, TV]}
    assert len(r.calls) == 2


def test_no_replay_after_downtime_or_enabling_late(tmp_path):
    r = Rig(tmp_path, t=ts(2026, 10, 10, 1, 30))
    r.enable()  # enabled at 01:30: tonight's 01:00 is not caught up
    assert r.at(1, 30, 5) == [] and r.calls == []
    r.at(0, 30, day=11)
    assert r.at(1, 5, day=11) == [] and r.calls == []  # app was down at 01:00, 5 min late is too late
    assert r.at(1, 0, 30, day=12)[0]["service"] == "turn_off"  # 30 s late is fine


def test_restart_keeps_ownership_without_replay(tmp_path):
    r = Rig(tmp_path)
    r.enable()
    r.at(1)
    r.s = r.make()  # restart a few seconds later, still inside the grace
    assert r.at(1, 1) == [] and len(r.calls) == 1
    r.s = r.make()
    assert r.at(7)[0]["service"] == "turn_on"
    r.s = r.make()
    assert r.at(7, 1) == [] and len(r.calls) == 2


def test_dst_spring_forward_and_fall_back(tmp_path):
    # 29 Mar 2026: 01:00 GMT -> 02:00 BST, so 01:00 doesn't exist: it runs at 02:00 BST, once
    r = Rig(tmp_path, t=ts(2026, 3, 28, 12))
    r.enable()
    assert r.s.next_due() == ts(2026, 3, 29, 2)
    assert r.at(0, 59, day=29, month=3) == []
    assert r.at(2, 0, day=29, month=3)[0]["service"] == "turn_off"
    assert r.at(2, 1, day=29, month=3) == []
    assert r.at(7, day=29, month=3)[0]["service"] == "turn_on"
    # 25 Oct 2026: 01:00 happens twice; only the first one switches
    r2 = Rig(tmp_path / "autumn", t=ts(2026, 10, 24, 12))
    r2.enable()
    assert r2.at(1, 0, day=25)[0]["service"] == "turn_off"
    r2.states[TV] = {"state": "on"}  # turned back on by hand during the first 01:xx
    r2.clock.t = ts(2026, 10, 25, 1, 30)
    r2.s.observe({"entity_id": TV, "state": "on"})
    assert r2.at(1, 0, day=25, fold=1) == [] and len(r2.calls) == 1  # the second 01:00 (GMT): no second off
    assert r2.at(7, day=25) == [] and len(r2.calls) == 1


def test_failures(tmp_path):
    r = Rig(tmp_path)
    r.enable()
    r.fail_calls = {"turn_off"}
    assert r.at(1)[0]["ok"] is False
    assert r.at(1, 1) == [] and len(r.calls) == 1  # a failed switch-off is not retried
    assert r.at(7) == [] and not r.s.state[TV]["owned"]
    r.fail_calls = {"turn_on"}
    r.at(1, day=11)
    r.states[TV] = {"state": "off"}
    assert r.at(7, day=11)[0]["ok"] is False
    assert r.at(7, 4, day=11) == []  # back-off 5 min
    assert r.at(7, 5, day=11)[0]["ok"] is False
    assert r.at(7, 14, day=11) == []  # then 10 min
    r.fail_calls = set()
    assert r.at(7, 15, day=11)[0]["ok"] is True
    assert len([c for c in r.calls if c[1] == "turn_on"]) == 3


def test_turn_on_gives_up(tmp_path):
    r = Rig(tmp_path)
    r.enable()
    r.fail_calls = {"turn_on"}
    r.at(1)
    for t in ((7, 0), (7, 5), (7, 15), (7, 30), (8, 0)):
        r.at(*t)
    assert len([c for c in r.calls if c[1] == "turn_on"]) == 3 and not r.s.state[TV]["owned"]
    assert "gave up" in r.s.logs()[0]["note"]


def test_disable_and_edit(tmp_path):
    r = Rig(tmp_path)
    r.enable()
    r.at(1)
    r.s.put_plug(TV, validate_plug({"enabled": False}, r.s.plug(TV)), "Tv")
    assert r.at(7) == [] and len(r.calls) == 1 and TV not in r.s.state  # off: no more automatic switching
    r.enable(off_at="02:00", on_at="06:30")
    r.states[TV] = {"state": "on"}
    assert r.at(1, day=11) == [] and r.at(2, day=11)[0]["service"] == "turn_off"
    assert r.at(6, 30, day=11)[0]["service"] == "turn_on"
    assert r.s.next_due() == ts(2026, 10, 12, 2)


def test_listing(tmp_path):
    r = Rig(tmp_path)
    r.enable()
    r.at(1)
    L = r.s.listing(r.devices, r.states, {TV: 4.0, LAMP: None}, r.layout, 25.0)
    tv = next(p for p in L["plugs"] if p["entity_id"] == TV)
    assert tv["enabled"] and tv["threshold_w"] == 8.0 and tv["owned"] and tv["standby_w"] == 4.0 and tv["suggested_w"] == 9.0
    assert tv["year_kwh"] == round(4.0 * 6 * 365 / 1000, 1) and tv["year_p"] == round(4.0 * 6 * 365 / 1000 * 25, 2)
    assert tv["next"] == {"off_at": int(ts(2026, 10, 11, 1) * 1000), "on_at": int(ts(2026, 10, 10, 7) * 1000)}
    lamp = next(p for p in L["plugs"] if p["entity_id"] == LAMP)
    assert lamp["year_p"] is None and lamp["suggested_w"] == 5.0 and not lamp["enabled"]
    assert "light.x" not in [p["entity_id"] for p in L["plugs"]]
    assert L["log"][0]["action"] == "off"


# ---------------- API ----------------
def test_api(client, fake_ha):
    fake_ha.history = []
    L = client.get("/api/standby").json()
    assert {p["entity_id"] for p in L["plugs"]} == {"switch.fan", "switch.kettle"} and not any(p["enabled"] for p in L["plugs"])
    r = client.put("/api/standby/switch.fan", json={"enabled": True})
    assert r.status_code == 200
    fan = next(p for p in r.json()["plugs"] if p["entity_id"] == "switch.fan")
    assert fan["enabled"] and fan["threshold_w"] == 5.0 and fan["off_at"] == "01:00" and fan["next"]
    assert client.put("/api/standby/switch.fan", json={"threshold_w": 1}).status_code == 400
    assert client.put("/api/standby/light.kitchen_1", json={"enabled": True}).status_code == 400
    assert client.put("/api/standby/switch.nope", json={"enabled": True}).status_code == 404
    lay = {"rooms": [], "placements": [], "settings": {"keep_on": ["switch.kettle"]}}
    assert client.put("/api/layout", json=lay).status_code == 200
    r = client.put("/api/standby/switch.kettle", json={"enabled": True})
    assert r.status_code == 400 and "keep-on" in r.json()["detail"]
    assert not [c for c in fake_ha.service_calls()]  # settings never switch anything
