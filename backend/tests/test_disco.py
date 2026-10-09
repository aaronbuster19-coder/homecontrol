"""Disco mode (backend/disco.py): timing, batching, auto-stop, restore, hand changes, errors, roles — with an injected
clock whose sleep() moves time, so a whole 30-minute disco runs in milliseconds."""
import asyncio
import base64
import json

import pytest

from backend import disco as dm
from backend.disco import Disco, DiscoError, DiscoStore, Forbidden, colours, restore_calls, snapshot
from backend.discovery import Device

from . import conftest


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += max(0.0, s)
        await asyncio.sleep(0)


def st(state, **a):
    return {"state": state, "attributes": a}


def colour_states():
    return {
        "light.a": st("on", brightness=200, supported_color_modes=["hs", "color_temp"], color_mode="color_temp",
                      color_temp_kelvin=2700),
        "light.b": st("on", brightness=120, supported_color_modes=["hs"], color_mode="hs", hs_color=[275, 90]),
        "light.c": st("off", supported_color_modes=["hs", "color_temp"]),
        "light.plain": st("on", brightness=90, supported_color_modes=["brightness"]),
        "light.gone": st("unavailable", supported_color_modes=["hs"]),
    }


LAYOUT = {"rooms": [{"id": "lounge", "name": "Lounge", "x": 0, "y": 0, "w": 4, "h": 4},
                    {"id": "bed", "name": "Bedroom", "x": 4, "y": 0, "w": 4, "h": 4}],
          "placements": [{"entity_id": "light.a", "x": 1, "y": 1}, {"entity_id": "light.c", "x": 2, "y": 2},
                         {"entity_id": "light.b", "x": 5, "y": 1}, {"entity_id": "light.plain", "x": 1, "y": 3}]}


class Rig:
    """A Disco with recorded calls (t, logged?, service, data); fail_raw=n makes the next n raw calls fail."""

    def __init__(self, tmp_path, states=None):
        self.clock = Clock()
        self.states = states or colour_states()
        self.calls, self.events, self.fail_raw, self.fail_logged = [], [], 0, 0
        devs = {e: Device(e, "light", e.split(".")[1].title(), "L530") for e in self.states}

        async def devices():
            return devs

        async def call(domain, service, data):
            if self.fail_logged:
                self.fail_logged -= 1
                raise RuntimeError("HA down")
            self.calls.append((self.clock(), True, service, json.loads(json.dumps(data))))

        async def raw(domain, service, data):
            if self.fail_raw:
                self.fail_raw -= 1
                self.calls.append((self.clock(), False, "FAILED", data))
                raise RuntimeError("HA down")
            self.calls.append((self.clock(), False, service, json.loads(json.dumps(data))))

        self.store = DiscoStore(str(tmp_path / "d.db"))
        self.d = Disco(self.store, call, raw, lambda: self.states, devices, lambda: LAYOUT,
                       broadcast=lambda ev, data: self.events.append(data), record=lambda *a, **k: None,
                       clock=self.clock, sleep=self.clock.sleep)

    async def settle(self, rounds=2000):
        """Let the disco loop (and any stop it spawned) run until it has stopped."""
        for _ in range(rounds):
            if self.d.run is None and not self.d._bg:
                return
            await asyncio.sleep(0)
        raise AssertionError("disco did not stop")

    def steps(self):
        """Colour steps (a restore sends raw "brightness", a step never does)."""
        return [c for c in self.calls if c[2] == "turn_on" and "hs_color" in c[3] and "brightness" not in c[3]]


def run(coro):
    return asyncio.run(coro)


# ---------------- pure helpers ----------------
def test_colours_per_preset():
    assert colours("rainbow", 0, 3) == [(0, 100)] * 3 and colours("rainbow", 1, 2) == [(24, 100)] * 2
    assert colours("flash", 1, 2) == [dm.PARTY[1]] * 2
    chill = colours("chill", 0, 3)
    assert [h for h, _ in chill] == [0, 120, 240] and all(s == 70 for _, s in chill)  # each light its own colour


def test_restore_calls_group_identical_payloads():
    saved = {
        "light.a": snapshot(st("on", brightness=200, color_mode="color_temp", color_temp_kelvin=2700, hs_color=[30, 50])),
        "light.a2": snapshot(st("on", brightness=200, color_mode="color_temp", color_temp_kelvin=2700)),
        "light.b": snapshot(st("on", brightness=120, color_mode="hs", hs_color=[275, 90])),
        "light.r": snapshot(st("on", brightness=50, color_mode="rgb", rgb_color=[1, 2, 3], hs_color=[1, 1])),
        "light.c": snapshot(st("off")), "light.d": snapshot(st("off")),
        "light.x": snapshot(st("unavailable")),
    }
    calls = restore_calls(saved)
    assert ("turn_off", {"entity_id": ["light.c", "light.d"]}) in calls
    assert ("turn_on", {"entity_id": ["light.a", "light.a2"], "brightness": 200, "color_temp_kelvin": 2700}) in calls
    assert ("turn_on", {"entity_id": ["light.b"], "brightness": 120, "hs_color": [275, 90]}) in calls
    assert ("turn_on", {"entity_id": ["light.r"], "brightness": 50, "rgb_color": [1, 2, 3]}) in calls
    assert len(calls) == 4  # nothing for the unavailable one


# ---------------- timing ----------------
def test_rainbow_batches_one_call_per_step_and_auto_stops(tmp_path):
    r = Rig(tmp_path)

    async def go():
        await r.d.start({"preset": "rainbow", "speed": "fast", "entity_ids": ["light.a", "light.b"], "minutes": 2})
        t0 = r.clock()
        await r.settle()
        return t0
    t0 = run(go())
    steps = r.steps()
    assert all(c[3]["entity_id"] == ["light.a", "light.b"] for c in steps)  # one call per colour, both lights in it
    assert steps[0][1] is True and all(not c[1] for c in steps[1:])  # only the first step is logged
    times = [c[0] for c in steps]
    assert all(b - a >= dm.MIN_GAP for a, b in zip(times, times[1:]))  # at most one change per light per second
    assert len(steps) == 120  # fast = 1 s, for 2 minutes
    assert max(times) < t0 + 120
    # hard stop at 2 minutes, then everything back, in grouped calls
    tail = [c for c in r.calls if c[0] >= t0 + 120]
    assert [(c[2], c[3]) for c in tail] == [
        ("turn_on", {"entity_id": ["light.a"], "brightness": 200, "color_temp_kelvin": 2700}),
        ("turn_on", {"entity_id": ["light.b"], "brightness": 120, "hs_color": [275, 90]})]
    assert r.d.last["reason"] == "timeout" and r.store.get() is None
    assert r.events[-1]["running"] is False


def test_default_limit_is_thirty_minutes(tmp_path):
    r = Rig(tmp_path)

    async def go():
        s = await r.d.start({"preset": "chill", "entity_ids": ["light.a"]})
        await r.settle()
        return s
    s = run(go())
    assert s["ends_at"] - s["started_at"] == 30 * 60 * 1000
    assert r.d.last["reason"] == "timeout"
    steps = r.steps()
    assert len(steps) == 30 * 60 // 8  # chill, normal: every 8 s
    assert steps[-1][0] < s["ends_at"] / 1000


def test_chill_one_call_per_light_and_min_gap_holds_when_called_early(tmp_path):
    r = Rig(tmp_path)

    async def go():
        await r.d.start({"preset": "chill", "speed": "fast", "entity_ids": ["light.a", "light.b"], "minutes": 1})
        run_ = r.d.run
        n0 = len(r.calls)
        # someone calls step() again half a second later: no light changes twice within MIN_GAP
        made = await r.d.step(run_, r.clock() + 0.5)
        assert made == 0 and len(r.calls) == n0
        await r.d.stop()
    run(go())
    first = r.steps()
    assert len(first) == 2 and {c[3]["entity_id"][0] for c in first} == {"light.a", "light.b"}
    assert first[0][3]["hs_color"] != first[1][3]["hs_color"]


def test_room_start_takes_only_colour_lights_that_are_on(tmp_path):
    r = Rig(tmp_path)

    async def go():
        s = await r.d.start({"preset": "rainbow", "room": "lounge"})
        await r.d.stop()
        return s
    s = run(go())
    assert s["entity_ids"] == ["light.a"]  # light.c is off (not picked), light.plain has no colour, light.b elsewhere
    assert not any("light.c" in json.dumps(c[3]) for c in r.calls)
    assert not any("light.plain" in json.dumps(c[3]) for c in r.calls)


def test_whole_home_and_errors_for_nothing_on(tmp_path):
    r = Rig(tmp_path)
    for e in ("light.a", "light.b"):
        r.states[e]["state"] = "off"
    with pytest.raises(DiscoError, match="No colour lights are on"):
        run(r.d.start({"preset": "rainbow"}))
    with pytest.raises(DiscoError, match="not a colour light"):
        run(r.d.start({"preset": "rainbow", "entity_ids": ["light.plain"]}))
    with pytest.raises(DiscoError, match="unavailable"):
        run(r.d.start({"preset": "rainbow", "entity_ids": ["light.gone"]}))
    with pytest.raises(DiscoError, match="unknown room"):
        run(r.d.start({"preset": "rainbow", "room": "attic"}))
    for bad in ({"preset": "strobe"}, {"speed": "ludicrous"}, {"minutes": 0}, {"minutes": 121}, {"minutes": 2.5},
                {"minutes": True}, {"extra": 1}, {"entity_ids": "light.a"}):
        with pytest.raises(DiscoError):
            run(r.d.start({"entity_ids": ["light.a"], **bad}))
    assert r.calls == [] and r.d.run is None


def test_picked_light_that_was_off_goes_on_then_back_off(tmp_path):
    r = Rig(tmp_path)

    async def go():
        s = await r.d.start({"preset": "rainbow", "entity_ids": ["light.c", "light.b"], "minutes": 1})
        await r.d.stop()
        return s
    s = run(go())
    assert s["turned_on"] == ["light.c"]
    first = [c for c in r.calls if "hs_color" in c[3]][:2]
    assert {"entity_id": ["light.c"], "hs_color": [0, 100], "brightness_pct": dm.ON_BRIGHTNESS} in [c[3] for c in first]
    assert ("turn_off", {"entity_id": ["light.c"]}) in [(c[2], c[3]) for c in r.calls]


def test_flash_needs_the_warning_acknowledged(tmp_path):
    r = Rig(tmp_path)
    with pytest.raises(DiscoError, match="photosensitivity"):
        run(r.d.start({"preset": "flash", "entity_ids": ["light.a"]}))

    async def go():
        await r.d.start({"preset": "flash", "entity_ids": ["light.a"], "warning_ok": True})
        await r.d.stop()
    run(go())
    assert r.steps()


# ---------------- errors ----------------
def test_errors_back_off_then_stop_and_restore(tmp_path):
    r = Rig(tmp_path)

    async def go():
        await r.d.start({"preset": "rainbow", "speed": "fast", "entity_ids": ["light.a"], "minutes": 30})
        r.fail_raw = 99  # HA starts failing after the first (logged) step
        t0 = r.clock()
        await r.settle()
        return t0
    t0 = run(go())
    failed = [c[0] for c in r.calls if c[2] == "FAILED"]
    assert len(failed) == dm.MAX_FAILS  # never loops on errors
    gaps = [b - a for a, b in zip([t0] + failed, failed)]
    assert gaps == [1.0, 2.0, 4.0]  # interval, then doubling back-off
    assert r.d.last["reason"] == "errors" and r.d.run is None
    assert r.calls[-1][2:] == ("turn_on", {"entity_id": ["light.a"], "brightness": 200, "color_temp_kelvin": 2700})


def test_ha_down_at_start_fails_the_start_and_leaves_nothing_running(tmp_path):
    r = Rig(tmp_path)
    r.fail_logged = 1
    with pytest.raises(RuntimeError):
        run(r.d.start({"preset": "rainbow", "entity_ids": ["light.a"]}))
    assert r.d.run is None and r.d.last["reason"] == "errors" and r.store.get() is None
    assert [(c[2], c[3]) for c in r.calls] == [  # the restore still went out
        ("turn_on", {"entity_id": ["light.a"], "brightness": 200, "color_temp_kelvin": 2700})]


def test_one_failure_then_recovery_keeps_going(tmp_path):
    r = Rig(tmp_path)

    async def go():
        await r.d.start({"preset": "rainbow", "speed": "fast", "entity_ids": ["light.a"], "minutes": 1})
        r.fail_raw = 1
        await r.settle()
    run(go())
    assert r.d.last["reason"] == "timeout"
    assert len([c for c in r.calls if c[2] == "FAILED"]) == 1


# ---------------- stops from outside ----------------
def item(eid, state="on", **kw):
    return {"entity_id": eid, "kind": "light", "state": state, **kw}


def test_a_change_by_hand_stops_and_keeps_that_light(tmp_path):
    r = Rig(tmp_path)

    async def go():
        await r.d.start({"preset": "rainbow", "speed": "fast", "entity_ids": ["light.a", "light.b"], "minutes": 5})
        r.clock.t += 5
        await asyncio.sleep(0)
        sent = r.d.run.recent["light.a"][-1]
        # our own echo (HA rounds a little, lags a step) is not a hand change
        assert not r.d.by_hand(item("light.a", brightness=203, color_mode="hs", hs_color=[sent[0] + 3, sent[1] - 4]))
        # someone switches light.b off on the wall
        r.d.observe(item("light.b", "off"))
        assert r.d.run.halted  # no further colour goes out before the stop runs
        n = len(r.calls)
        await r.settle()
        return n
    n = run(go())
    after = r.calls[n:]
    assert [(c[2], c[3]) for c in after] == [
        ("turn_on", {"entity_id": ["light.a"], "brightness": 200, "color_temp_kelvin": 2700})]  # light.b left alone
    assert r.d.last["reason"] == "manual"


@pytest.mark.parametrize("change", [
    {"color_mode": "color_temp", "color_temp_kelvin": 4000},
    {"color_mode": "hs", "hs_color": [180, 20]},
    {"brightness": 40},
])
def test_hand_changes_detected(tmp_path, change):
    r = Rig(tmp_path)

    async def go():
        await r.d.start({"preset": "rainbow", "entity_ids": ["light.a"], "minutes": 5})
        r.clock.t += dm.GRACE + 1
        sent = r.d.run.recent["light.a"][-1]
        base = {"brightness": 200, "color_mode": "hs", "hs_color": list(sent)}
        assert not r.d.by_hand(item("light.a", **base))
        assert r.d.by_hand(item("light.a", **{**base, **change}))
        assert not r.d.by_hand(item("light.a", "unavailable"))  # offline is not a hand change
        await r.d.stop()
    run(go())


def test_all_off_and_away_stop_without_switching_those_lights_back_on(tmp_path):
    r = Rig(tmp_path)

    async def go():
        await r.d.start({"preset": "rainbow", "entity_ids": ["light.a", "light.b", "light.c"], "minutes": 5})
        n = len(r.calls)
        await r.d.interrupt("all_off", ["light.a", "light.b", "light.c", "switch.tv"])
        return n
    n = run(go())
    assert r.calls[n:] == [] and r.d.last["reason"] == "all_off" and r.d.run is None

    async def away():
        await r.d.start({"preset": "rainbow", "entity_ids": ["light.a", "light.b"], "minutes": 5})
        n = len(r.calls)
        await r.d.interrupt("away", ["light.b"])  # light.a kept on (e.g. keep_on): it goes back to normal
        return n
    n = run(away())
    assert [(c[2], c[3]["entity_id"]) for c in r.calls[n:]] == [("turn_on", ["light.a"])]
    assert r.d.last["reason"] == "away"


def test_tapping_another_light_does_not_stop_it(tmp_path):
    r = Rig(tmp_path)

    async def go():
        await r.d.start({"preset": "rainbow", "entity_ids": ["light.a"], "minutes": 5})
        await r.d.interrupt("manual", ["light.plain"])
        assert r.d.run is not None
        await r.d.interrupt("manual", ["light.a"])
        assert r.d.run is None and r.d.last["reason"] == "manual"
    run(go())


def test_new_disco_replaces_old_and_keeps_the_real_snapshot(tmp_path):
    r = Rig(tmp_path)

    async def go():
        await r.d.start({"preset": "rainbow", "entity_ids": ["light.a", "light.c"], "minutes": 5})
        # the live state now shows disco colours; light.c is on
        r.states["light.a"] = st("on", brightness=200, supported_color_modes=["hs", "color_temp"], color_mode="hs", hs_color=[0, 100])
        r.states["light.c"] = st("on", brightness=204, supported_color_modes=["hs", "color_temp"], color_mode="hs", hs_color=[0, 100])
        n = len(r.calls)
        await r.d.start({"preset": "chill", "entity_ids": ["light.a", "light.c"], "minutes": 5})
        assert not [c for c in r.calls[n:] if "hs_color" not in c[3]]  # nothing restored in between (no flicker)
        await r.d.stop()
    run(go())
    tail = [(c[2], c[3]) for c in r.calls if "hs_color" not in c[3] or "brightness" in c[3]][-2:]
    assert ("turn_off", {"entity_id": ["light.c"]}) in tail
    assert ("turn_on", {"entity_id": ["light.a"], "brightness": 200, "color_temp_kelvin": 2700}) in tail


# ---------------- restart ----------------
def test_restart_mid_disco_restores_from_sqlite(tmp_path):
    r = Rig(tmp_path)

    async def go():
        await r.d.start({"preset": "rainbow", "entity_ids": ["light.a", "light.c"], "minutes": 5})
        r.d.run.halted = True  # the process dies: no stop ran
        r.d.run = None
    run(go())
    assert r.store.get()
    r2 = Rig(tmp_path)
    r2.clock.t = r.clock.t + 60
    run(r2.d.recover())
    assert [(c[2], c[3]) for c in r2.calls] == [
        ("turn_on", {"entity_id": ["light.a"], "brightness": 200, "color_temp_kelvin": 2700}),
        ("turn_off", {"entity_id": ["light.c"]})]
    assert r2.store.get() is None and r2.d.last["reason"] == "restart"


def test_failed_restore_keeps_the_snapshot_for_the_next_start(tmp_path):
    r = Rig(tmp_path)

    async def go():
        await r.d.start({"preset": "rainbow", "entity_ids": ["light.a"], "minutes": 5})
        r.fail_logged = 1  # HA is down when it stops
        await r.d.stop()
    run(go())
    assert r.d.run is None and r.store.get()["saved"]["light.a"]["color_temp_kelvin"] == 2700
    r2 = Rig(tmp_path)
    r2.clock.t = r.clock.t + 30
    run(r2.d.recover())
    assert [c[3]["entity_id"] for c in r2.calls] == [["light.a"]] and r2.store.get() is None


def test_stale_snapshot_is_dropped(tmp_path):
    r = Rig(tmp_path)
    r.store.put({"saved": {"light.a": snapshot(st("off"))}, "ends": r.clock() - dm.RESTORE_STALE - 1, "started": 0})
    run(r.d.recover())
    assert r.calls == [] and r.store.get() is None


# ---------------- roles ----------------
def test_guest_only_for_lights_they_may_control(tmp_path, monkeypatch):
    r = Rig(tmp_path)
    monkeypatch.setattr(dm, "allowed", lambda role, level, params: role != "guest" or params["entity_id"] != "light.b")
    with pytest.raises(Forbidden):
        run(r.d.start({"preset": "rainbow", "entity_ids": ["light.a", "light.b"]}, role="guest"))

    async def go():
        await r.d.start({"preset": "rainbow", "entity_ids": ["light.a", "light.b"]}, role="member")
        assert not r.d.may_stop("guest") and r.d.may_stop("member")
        with pytest.raises(Forbidden):  # nor replace a disco they couldn't stop
            await r.d.start({"preset": "rainbow", "entity_ids": ["light.a"]}, role="guest")
        await r.d.stop()
    run(go())


# ---------------- API ----------------
API_STATES = [
    {"entity_id": "light.kitchen_1", "state": "on",
     "attributes": {"brightness": 200, "supported_color_modes": ["hs", "color_temp"], "color_mode": "color_temp",
                    "color_temp_kelvin": 3000}},
    {"entity_id": "light.strip", "state": "off", "attributes": {"supported_color_modes": ["hs"]}},
    {"entity_id": "switch.fan", "state": "off", "attributes": {}},
]


@pytest.fixture
def colour_ha(monkeypatch):
    monkeypatch.setattr(conftest, "STATES", API_STATES)  # before the app starts and polls the states


@pytest.fixture
def api(colour_ha, client):
    for name, role, pw in (("mia", "member", "member-pass-1"), ("gus", "guest", "guest-pass-1")):
        assert client.post("/api/users", json={"username": name, "password": pw, "role": role}).status_code == 200
    return client


def hdr(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_api_start_status_stop(api, fake_ha):
    assert api.get("/api/disco").json()["running"] is False
    r = api.post("/api/disco/start", json={"preset": "rainbow", "entity_ids": ["light.kitchen_1", "light.strip"], "minutes": 10})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["running"] and s["entity_ids"] == ["light.kitchen_1", "light.strip"] and s["turned_on"] == ["light.strip"]
    assert s["started_by"] == "aaron" and s["ends_at"] - s["started_at"] == 600_000
    assert api.get("/api/disco").json()["running"]
    r = api.post("/api/disco/stop")
    assert r.json()["stopped"] and not r.json()["running"]
    svc = fake_ha.service_calls()
    assert ("/api/services/light/turn_off", {"entity_id": ["light.strip"]}) in svc
    assert ("/api/services/light/turn_on", {"entity_id": ["light.kitchen_1"], "brightness": 200,
                                            "color_temp_kelvin": 3000}) in svc
    assert api.post("/api/disco/stop").json()["stopped"] is False


def test_api_errors(api):
    assert api.post("/api/disco/start", json={"preset": "rainbow", "entity_ids": ["switch.fan"]}).status_code == 400
    assert api.post("/api/disco/start", json={"preset": "flash", "entity_ids": ["light.kitchen_1"]}).status_code == 400
    assert api.post("/api/disco/start", content=b"nope").status_code == 400
    assert api.post("/api/disco/start", json=[]).status_code == 400


def test_api_guest_can_start_and_stop_light_discos(api):
    g = hdr("gus", "guest-pass-1")
    assert api.get("/api/disco", headers=g).status_code == 200
    r = api.post("/api/disco/start", json={"preset": "chill", "entity_ids": ["light.kitchen_1"]}, headers=g)
    assert r.status_code == 200, r.text
    assert api.post("/api/disco/stop", headers=g).json()["stopped"]


def test_api_all_off_and_light_tap_stop_the_disco(api, fake_ha):
    assert api.post("/api/disco/start", json={"preset": "rainbow", "entity_ids": ["light.kitchen_1"]}).status_code == 200
    n = len(fake_ha.service_calls())
    r = api.post("/api/bulk", json={"action": "turn_off", "entity_ids": ["light.kitchen_1"], "source": "all_off"})
    assert r.status_code == 200
    s = api.get("/api/disco").json()
    assert not s["running"] and s["last"]["reason"] == "all_off"
    assert fake_ha.service_calls()[n:] == [("/api/services/light/turn_off", {"entity_id": ["light.kitchen_1"]})]
    assert api.post("/api/disco/start", json={"preset": "rainbow", "entity_ids": ["light.kitchen_1"]}).status_code == 200
    api.post("/api/devices/light.kitchen_1/light", json={"brightness_pct": 30})
    assert api.get("/api/disco").json()["last"]["reason"] == "manual"


def test_api_away_stops_the_disco(api):
    assert api.post("/api/disco/start", json={"preset": "rainbow", "entity_ids": ["light.kitchen_1"]}).status_code == 200
    assert api.post("/api/mode", json={"mode": "away"}).status_code == 200
    assert api.get("/api/disco").json()["last"]["reason"] == "away"


def test_api_activity_shows_disco(api):
    api.post("/api/disco/start", json={"preset": "rainbow", "entity_ids": ["light.kitchen_1"]})
    api.post("/api/disco/stop")
    texts = [e["text"] for e in api.get("/api/activity").json()["entries"]]
    assert "Disco off" in texts and "Disco on · 1 light" in texts
