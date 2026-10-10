"""Scenes (backend/scenes.py): validation, capture, batching into one call per payload, protected plugs, skipped and
failed devices, the double-tap guard, disco hand-off, and the API with roles."""
import asyncio
import base64
import json

import pytest

from backend import scenes as sm
from backend.discovery import Device
from backend.scenes import SceneError, SceneStore, Scenes, capture, plan, validate_scene

TV_SF = 128 | 256 | 2048  # turn_on, turn_off, select_source


def st(state, **a):
    return {"state": state, "attributes": a}


DEVS = {e: Device(e, k, n, "x") for e, k, n in (
    ("light.lamp", "light", "Lamp"), ("light.strip", "light", "Strip"), ("light.hall", "light", "Hall"),
    ("switch.fan", "plug", "Fan"), ("switch.fridge", "plug", "Fridge plug"), ("switch.server", "plug", "Server"),
    ("media_player.tv", "media", "TV"), ("climate.valve", "valve", "Valve"))}
STATES = {
    "light.lamp": st("on", brightness=102, supported_color_modes=["hs", "color_temp"], color_mode="color_temp", color_temp_kelvin=2700),
    "light.strip": st("on", brightness=255, supported_color_modes=["hs"], color_mode="hs", hs_color=[275.04, 90]),
    "light.hall": st("off", supported_color_modes=["brightness"]),
    "switch.fan": st("on"), "switch.fridge": st("on"), "switch.server": st("off"),
    "media_player.tv": st("on", supported_features=TV_SF, source="HDMI1", source_list=["TV", "HDMI1", "Netflix"]),
    "climate.valve": st("heat"),
}
LAYOUT = {"rooms": [{"id": "lounge", "name": "Lounge", "x": 0, "y": 0, "w": 4, "h": 4}], "placements": [],
          "furniture": [{"id": "f1", "type": "fridge", "x": 1, "y": 1, "w": 1, "h": 1, "plug": "switch.fridge"}],
          "settings": {"keep_on": ["switch.server"]}}


def run(coro):
    return asyncio.run(coro)


# ---------------- validation ----------------
def test_validate_scene_ok_and_normalised():
    s = validate_scene({"name": "  Movie   night ", "room": "lounge", "actions": [
        {"entity_id": "light.lamp", "on": True, "brightness_pct": 39.6, "color_temp_kelvin": 2700},
        {"entity_id": "light.hall", "on": False}, {"entity_id": "media_player.tv", "on": True, "source": "Netflix"}]},
        DEVS, LAYOUT)
    assert s["name"] == "Movie night" and s["room"] == "lounge" and s["id"].startswith("s") and s["disco"] is None
    assert s["actions"][0] == {"entity_id": "light.lamp", "on": True, "brightness_pct": 40, "color_temp_kelvin": 2700}
    assert s["actions"][2]["source"] == "Netflix"


@pytest.mark.parametrize("body,msg", [
    ({"name": "", "actions": [{"entity_id": "light.lamp", "on": True}]}, "name"),
    ({"name": "x" * 41, "actions": [{"entity_id": "light.lamp", "on": True}]}, "at most 40"),
    ({"name": "A", "actions": []}, "at least one"),
    ({"name": "A", "actions": [{"entity_id": "light.nope", "on": True}]}, "unknown device"),
    ({"name": "A", "actions": [{"entity_id": "climate.valve", "on": True}]}, "can't be in a scene"),
    ({"name": "A", "actions": [{"entity_id": "light.lamp", "on": "yes"}]}, "on must be"),
    ({"name": "A", "actions": [{"entity_id": "light.lamp", "on": True, "brightness_pct": 0}]}, "1–100"),
    ({"name": "A", "actions": [{"entity_id": "light.lamp", "on": True, "hs_color": [1, 2], "color_temp_kelvin": 3000}]}, "only one"),
    ({"name": "A", "actions": [{"entity_id": "switch.fan", "on": True, "brightness_pct": 5}]}, "unexpected field"),
    ({"name": "A", "actions": [{"entity_id": "light.lamp", "on": True}, {"entity_id": "light.lamp", "on": False}]}, "twice"),
    ({"name": "A", "actions": [{"entity_id": "switch.fridge", "on": False}]}, "protected"),
    ({"name": "A", "actions": [{"entity_id": "switch.server", "on": False}]}, "protected"),  # keep on
    ({"name": "A", "room": "attic", "actions": [{"entity_id": "light.lamp", "on": True}]}, "unknown room"),
    ({"name": "A", "actions": [{"entity_id": "light.lamp", "on": True}], "disco": {"preset": "flash"}}, "disco must be one of"),
    ({"name": "A", "actions": [{"entity_id": "light.lamp", "on": False}], "disco": {"preset": "rainbow"}}, "needs at least one light"),
    ({"name": "A", "actions": [{"entity_id": "light.lamp", "on": True}], "disco": {"minutes": 0}}, "minutes"),
    ({"name": "A", "actions": [], "extra": 1}, "unexpected field"),
])
def test_validate_scene_rejects(body, msg):
    with pytest.raises(SceneError, match=msg):
        validate_scene(body, DEVS, LAYOUT)


def test_protected_plug_may_be_switched_on():
    s = validate_scene({"name": "Kitchen", "actions": [{"entity_id": "switch.fridge", "on": True}]}, DEVS, LAYOUT)
    assert s["actions"] == [{"entity_id": "switch.fridge", "on": True}]


def test_device_missing_from_ha_keeps_its_stored_action():
    old = validate_scene({"name": "A", "actions": [{"entity_id": "light.lamp", "on": True, "brightness_pct": 20}]}, DEVS, LAYOUT)
    devs = {k: v for k, v in DEVS.items() if k != "light.lamp"}
    s = validate_scene({"name": "B", "actions": [{"entity_id": "light.lamp", "on": True}]}, devs, LAYOUT, old)
    assert s["id"] == old["id"] and s["actions"] == old["actions"]


# ---------------- capture ----------------
def test_capture_reads_current_state():
    acts, skipped = capture(["light.lamp", "light.strip", "light.hall", "switch.fan", "media_player.tv", "switch.server"],
                            DEVS, STATES, LAYOUT)
    by = {a["entity_id"]: a for a in acts}
    assert by["light.lamp"] == {"entity_id": "light.lamp", "on": True, "brightness_pct": 40, "color_temp_kelvin": 2700}
    assert by["light.strip"] == {"entity_id": "light.strip", "on": True, "brightness_pct": 100, "hs_color": [275.0, 90.0]}
    assert by["light.hall"] == {"entity_id": "light.hall", "on": False}
    assert by["switch.fan"] == {"entity_id": "switch.fan", "on": True}
    assert by["media_player.tv"] == {"entity_id": "media_player.tv", "on": True, "source": "HDMI1"}
    assert skipped == [{"entity_id": "switch.server", "reason": "protected"}]  # keep-on plug that is off: never "off"
    validate_scene({"name": "Now", "actions": acts}, DEVS, LAYOUT)  # a capture always saves


def test_capture_skips_unavailable_and_rejects_others():
    acts, skipped = capture(["light.lamp"], DEVS, {"light.lamp": st("unavailable")}, LAYOUT)
    assert acts == [] and skipped == [{"entity_id": "light.lamp", "reason": "unavailable"}]
    with pytest.raises(SceneError):
        capture(["climate.valve"], DEVS, STATES, LAYOUT)


# ---------------- plan ----------------
def scene(*actions, **kw):
    return {"id": "s1", "name": "S", "actions": list(actions), "disco": None, **kw}


def test_plan_batches_one_call_per_payload_ons_then_sources_then_offs():
    calls, skipped = plan(scene(
        {"entity_id": "light.lamp", "on": True, "brightness_pct": 40},
        {"entity_id": "light.strip", "on": True, "brightness_pct": 40},
        {"entity_id": "light.hall", "on": True},
        {"entity_id": "switch.fan", "on": False},
        {"entity_id": "media_player.tv", "on": True, "source": "Netflix"},
        {"entity_id": "light.gone", "on": False}), DEVS, STATES, LAYOUT)
    assert calls == [
        ("light", "turn_on", {"entity_id": ["light.lamp", "light.strip"], "brightness_pct": 40}),
        ("light", "turn_on", {"entity_id": ["light.hall"]}),
        ("media_player", "select_source", {"entity_id": ["media_player.tv"], "source": "Netflix"}),
        ("switch", "turn_off", {"entity_id": ["switch.fan"]}),
    ]
    assert skipped == [{"entity_id": "light.gone", "name": "light.gone", "reason": "not in Home Assistant"}]


def test_plan_tv_off_and_on():
    off = dict(STATES, **{"media_player.tv": st("off", supported_features=TV_SF, source_list=["TV", "HDMI1"])})
    calls, skipped = plan(scene({"entity_id": "media_player.tv", "on": True, "source": "HDMI1"}), DEVS, off, LAYOUT)
    assert calls == [("media_player", "turn_on", {"entity_id": ["media_player.tv"]}),
                     ("media_player", "select_source", {"entity_id": ["media_player.tv"], "source": "HDMI1"})]
    calls, _ = plan(scene({"entity_id": "media_player.tv", "on": False}), DEVS, off, LAYOUT)
    assert calls == []  # already off
    calls, _ = plan(scene({"entity_id": "media_player.tv", "on": False}), DEVS, STATES, LAYOUT)
    assert calls == [("media_player", "turn_off", {"entity_id": ["media_player.tv"]})]
    same = plan(scene({"entity_id": "media_player.tv", "on": True, "source": "HDMI1"}), DEVS, STATES, LAYOUT)
    assert same == ([], [])  # on, on that source: nothing to send
    calls, skipped = plan(scene({"entity_id": "media_player.tv", "on": True, "source": "Disney+"}), DEVS, STATES, LAYOUT)
    assert calls == [] and skipped[0]["reason"] == "no source Disney+"
    dumb = dict(STATES, **{"media_player.tv": st("on", supported_features=0)})
    calls, skipped = plan(scene({"entity_id": "media_player.tv", "on": False}), DEVS, dumb, LAYOUT)
    assert calls == [] and skipped[0]["reason"] == "can't be turned off"


def test_plan_never_switches_off_a_plug_that_became_protected():
    calls, skipped = plan(scene({"entity_id": "switch.fan", "on": False}, {"entity_id": "light.hall", "on": False}), DEVS, STATES,
                          {**LAYOUT, "settings": {"keep_on": ["switch.fan"]}})
    assert calls == [("light", "turn_off", {"entity_id": ["light.hall"]})]
    assert skipped == [{"entity_id": "switch.fan", "name": "Fan", "reason": "protected"}]


def test_plan_skips_unavailable():
    calls, skipped = plan(scene({"entity_id": "light.lamp", "on": True}), DEVS, {"light.lamp": st("unavailable")}, LAYOUT)
    assert calls == [] and skipped[0]["reason"] == "unavailable"


# ---------------- run ----------------
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeDisco:
    def __init__(self):
        self.interrupted, self.started, self.fail = [], [], None

    async def interrupt(self, reason, ids):
        self.interrupted.append((reason, list(ids)))

    async def start(self, body, role, user):
        if self.fail:
            raise self.fail
        self.started.append((body, role, user))


def rig(tmp_path, states=None):
    calls, fail = [], set()

    async def call(domain, service, data):
        if service in fail:
            raise RuntimeError("HA down")
        calls.append((domain, service, data))

    async def devices():
        return DEVS

    clock, disco = Clock(), FakeDisco()
    sc = Scenes(SceneStore(str(tmp_path / "s.db")), call, devices, lambda: states or STATES, lambda: LAYOUT, disco, clock)
    return sc, calls, fail, clock, disco


def test_run_sends_batched_calls_and_guards_double_taps(tmp_path):
    sc, calls, _, clock, disco = rig(tmp_path)
    s = run(sc.create({"name": "Night", "actions": [{"entity_id": "light.lamp", "on": False}, {"entity_id": "light.strip", "on": False},
                                                     {"entity_id": "switch.fan", "on": False}]}))
    r = run(sc.run(s["id"]))
    assert r["ok"] and r["calls"] == 2 and not r["skipped"]
    assert calls == [("light", "turn_off", {"entity_id": ["light.lamp", "light.strip"]}), ("switch", "turn_off", {"entity_id": ["switch.fan"]})]
    assert disco.interrupted == [("manual", ["light.lamp", "light.strip"])]
    clock.t += 0.5
    assert run(sc.run(s["id"]))["repeat"] and len(calls) == 2  # a double tap: nothing sent twice
    clock.t += sm.RERUN_GAP
    run(sc.run(s["id"]))
    assert len(calls) == 4


def test_run_continues_after_a_failed_group(tmp_path):
    sc, calls, fail, _, _ = rig(tmp_path)
    s = run(sc.create({"name": "Mix", "actions": [{"entity_id": "light.hall", "on": True}, {"entity_id": "switch.fan", "on": False}]}))
    fail.add("turn_on")
    r = run(sc.run(s["id"]))
    assert not r["ok"] and r["failed"][0]["entity_id"] == "light.hall"
    assert calls == [("switch", "turn_off", {"entity_id": ["switch.fan"]})]


def test_run_starts_a_disco_with_the_colour_lights_it_switched_on(tmp_path):
    sc, calls, _, _, disco = rig(tmp_path)
    s = run(sc.create({"name": "Party", "actions": [{"entity_id": "light.strip", "on": True}, {"entity_id": "light.hall", "on": True},
                                                     {"entity_id": "light.lamp", "on": False}],
                       "disco": {"preset": "chill", "speed": "slow", "minutes": 20}}))
    r = run(sc.run(s["id"], "member", "mia"))
    assert r["disco"] == {"started": True, "n": 1}
    assert disco.started == [({"preset": "chill", "speed": "slow", "minutes": 20, "entity_ids": ["light.strip"]}, "member", "mia")]
    from backend.disco import DiscoError
    disco.fail = DiscoError("nope")
    sc._last.clear()
    assert run(sc.run(s["id"]))["disco"] == {"started": False, "reason": "nope"}


def test_guests_run_only_light_scenes(tmp_path):
    sc, calls, _, _, _ = rig(tmp_path)
    lights = run(sc.create({"name": "Lights", "actions": [{"entity_id": "light.hall", "on": True}]}))
    mixed = run(sc.create({"name": "Mixed", "actions": [{"entity_id": "light.hall", "on": True}, {"entity_id": "switch.fan", "on": True}]}))
    assert [s["name"] for s in sc.listing("guest")["scenes"]] == ["Lights"]
    assert [s["guest_ok"] for s in sc.listing("member")["scenes"]] == [True, False]
    with pytest.raises(sm.Forbidden):
        run(sc.run(mixed["id"], "guest"))
    assert run(sc.run(lights["id"], "guest"))["ok"]


def test_store_limits_update_delete(tmp_path, monkeypatch):
    sc, *_ = rig(tmp_path)
    monkeypatch.setattr(sm, "MAX_SCENES", 2)
    a = run(sc.create({"name": "A", "actions": [{"entity_id": "light.hall", "on": True}]}))
    run(sc.create({"name": "B", "actions": [{"entity_id": "light.hall", "on": True}]}))
    with pytest.raises(SceneError, match="at most 2"):
        run(sc.create({"name": "C", "actions": [{"entity_id": "light.hall", "on": True}]}))
    u = run(sc.update(a["id"], {"name": "A2", "actions": [{"entity_id": "light.hall", "on": False}]}))
    assert u["id"] == a["id"] and sc.store.get(a["id"])["name"] == "A2"
    assert sc.delete(a["id"]) and not sc.delete(a["id"])
    with pytest.raises(KeyError):
        run(sc.update("nope", {"name": "x", "actions": []}))


# ---------------- API ----------------
def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def api(client):
    for name, role, pw in (("mia", "member", "member-pass-1"), ("gus", "guest", "guest-pass-1")):
        assert client.post("/api/users", json={"username": name, "password": pw, "role": role}).status_code == 200
    return client


def test_api_crud_capture_run(api, fake_ha):
    cap = api.post("/api/scenes/capture", json={"entity_ids": ["light.kitchen_1", "switch.fan"]})
    assert cap.status_code == 200, cap.text
    assert cap.json()["actions"] == [{"entity_id": "light.kitchen_1", "on": True}, {"entity_id": "switch.fan", "on": False}]
    r = api.post("/api/scenes", json={"name": "Evening", "actions": cap.json()["actions"]})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert [s["name"] for s in api.get("/api/scenes").json()["scenes"]] == ["Evening"]
    fake_ha.calls.clear()
    ran = api.post(f"/api/scenes/{sid}/run").json()
    assert ran["ok"] and ran["calls"] == 2
    assert fake_ha.service_calls() == [("/api/services/light/turn_on", {"entity_id": ["light.kitchen_1"]}),
                                       ("/api/services/switch/turn_off", {"entity_id": ["switch.fan"]})]
    assert api.put(f"/api/scenes/{sid}", json={"name": "Late", "actions": [{"entity_id": "light.strip", "on": True}]}).json()["name"] == "Late"
    assert api.post("/api/scenes", json={"name": ""}).status_code == 400
    assert api.post("/api/scenes/nope/run").status_code == 404
    assert api.put("/api/scenes/nope", json={"name": "x"}).status_code == 404
    assert api.delete(f"/api/scenes/{sid}").status_code == 200
    assert api.delete(f"/api/scenes/{sid}").status_code == 404


def test_api_roles(api, fake_ha):
    g, m = basic("gus", "guest-pass-1"), basic("mia", "member-pass-1")
    body = {"name": "Lamps", "actions": [{"entity_id": "light.strip", "on": True}]}
    assert api.post("/api/scenes", json=body, headers=g).status_code == 403
    assert api.post("/api/scenes/capture", json={"entity_ids": ["light.strip"]}, headers=g).status_code == 403
    lamps = api.post("/api/scenes", json=body, headers=m).json()["id"]  # members make scenes
    fan = api.post("/api/scenes", json={"name": "Fan", "actions": [{"entity_id": "switch.fan", "on": True}]}).json()["id"]
    assert [s["id"] for s in api.get("/api/scenes", headers=g).json()["scenes"]] == [lamps]
    assert api.post(f"/api/scenes/{fan}/run", headers=g).status_code == 403
    assert api.post(f"/api/scenes/{lamps}/run", headers=g).json()["ok"]
    assert api.put(f"/api/scenes/{lamps}", json=body, headers=g).status_code == 403
    assert api.delete(f"/api/scenes/{lamps}", headers=g).status_code == 403
    assert api.post(f"/api/scenes/{fan}/run", headers=m).json()["ok"]


def test_api_fridge_never_switched_off(api, fake_ha):
    layout = api.get("/api/layout").json()
    layout["settings"] = {**(layout.get("settings") or {}), "keep_on": ["switch.kettle"]}
    assert api.put("/api/layout", json=layout).status_code == 200
    r = api.post("/api/scenes", json={"name": "Off", "actions": [{"entity_id": "switch.kettle", "on": False}]})
    assert r.status_code == 400 and "protected" in r.json()["detail"]
    assert json.loads(json.dumps(api.post("/api/scenes/capture", json={"entity_ids": ["switch.kettle"]}).json()))["actions"] == \
        [{"entity_id": "switch.kettle", "on": True}]
