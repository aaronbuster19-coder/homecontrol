import asyncio

import pytest

from backend.alerts import Watcher
from backend.automations import AutoStore, Health
from backend.discovery import Device, apply_names
from backend.live import build_device
from backend.store import LayoutError, validate_layout

KNOWN = {"light.a", "switch.p", "binary_sensor.door"}


def layout(**settings):
    return {"rooms": [], "placements": [], "settings": {"keep_on": [], **settings}}


# ---------------- validation ----------------
def test_names_and_hidden_validation():
    out = validate_layout(layout(names={"light.a": "  Reading   lamp ", "switch.p": "", "binary_sensor.door": None},
                                 hidden=["switch.p", "switch.p"]), KNOWN, {"switch.p"})
    assert out["settings"]["names"] == {"light.a": "Reading lamp"}  # trimmed; empty = back to HA's name
    assert out["settings"]["hidden"] == ["switch.p"]
    assert validate_layout(out, KNOWN, {"switch.p"}) == out
    assert "names" not in validate_layout(layout(), KNOWN)["settings"]  # old layouts stay as they were


@pytest.mark.parametrize("settings", [
    {"names": {"light.nope": "X"}},
    {"names": {"light.a": "x" * 41}},
    {"names": {"light.a": 5}},
    {"names": ["light.a"]},
    {"hidden": ["light.nope"]},
    {"hidden": "light.a"},
    {"hidden": [1]},
])
def test_names_and_hidden_invalid(settings):
    with pytest.raises(LayoutError):
        validate_layout(layout(**settings), KNOWN, {"switch.p"})


def test_name_length_limit_is_after_trimming():
    out = validate_layout(layout(names={"light.a": "  " + "x" * 40 + "  "}), KNOWN)
    assert out["settings"]["names"]["light.a"] == "x" * 40


# ---------------- applied to devices ----------------
def test_apply_names_to_device_dicts():
    d = Device("light.a", "light", "Hue 1", "L530")
    devs = {"light.a": d}
    assert apply_names(devs, {"names": {"light.a": "Reading lamp"}, "hidden": ["light.a"]}) == {"light.a"}
    item = build_device(d, {})
    assert item["name"] == "Reading lamp" and item["ha_name"] == "Hue 1" and item["hidden"] is True
    assert apply_names(devs, {"names": {"light.a": "Reading lamp"}, "hidden": ["light.a"]}) == set()  # no change
    assert apply_names(devs, {}) == {"light.a"}
    assert build_device(d, {})["name"] == "Hue 1" and build_device(d, {})["hidden"] is False


def test_notification_texts_use_custom_names(tmp_path):
    door = Device("binary_sensor.door", "sensor", "Contact 1", "T110", {"battery": "sensor.door_bat"})
    apply_names({"binary_sensor.door": door}, {"names": {"binary_sensor.door": "Front door"}})
    # door-open alert
    w = Watcher(lambda: {"enabled": True, "door_open_minutes": 0, "notify_on_close": True}, clock=lambda: 1000.0)
    w.observe(door.entity_id, door.name, "on")
    assert w.check()[0]["title"] == "Front door open"
    # renamed while open: the close message follows the new name
    w.observe(door.entity_id, "Back door", "on")
    w.observe(door.entity_id, "Back door", "off")
    assert w.check()[0]["title"] == "Back door closed"
    # device health (battery)
    pushes = []

    async def notify(p):
        pushes.append(p)
    settings = {"health_battery": True, "health_unavailable": False, "health_unavailable_minutes": 60}
    h = Health(AutoStore(str(tmp_path / "a.db")), lambda: settings, notify, clock=lambda: 1e9)
    asyncio.run(h.tick({door.entity_id: door}, {"sensor.door_bat": {"state": "5", "attributes": {}}}))
    assert pushes[0]["title"] == "Low battery: Front door"


# ---------------- API ----------------
def test_meta_endpoint_names_hidden_and_device_list(client):
    r = client.put("/api/devices/light.kitchen_1/meta", json={"name": "  Ceiling  "})
    assert r.status_code == 200 and r.json()["settings"]["names"] == {"light.kitchen_1": "Ceiling"}
    devs = {d["entity_id"]: d for d in client.get("/api/devices").json()}
    assert devs["light.kitchen_1"]["name"] == "Ceiling" and devs["light.kitchen_1"]["ha_name"] == "Kitchen 1"
    r = client.put("/api/devices/switch.fan/meta", json={"hidden": True})
    assert r.json()["settings"]["hidden"] == ["switch.fan"]
    devs = {d["entity_id"]: d for d in client.get("/api/devices").json()}
    assert devs["switch.fan"]["hidden"] is True and devs["light.kitchen_1"]["hidden"] is False
    # empty name / HA's own name = back to HA's name
    assert client.put("/api/devices/light.kitchen_1/meta", json={"name": ""}).json()["settings"]["names"] == {}
    client.put("/api/devices/light.kitchen_1/meta", json={"name": "Ceiling"})
    assert client.put("/api/devices/light.kitchen_1/meta", json={"name": "Kitchen 1"}).json()["settings"]["names"] == {}
    assert client.put("/api/devices/switch.fan/meta", json={"hidden": False}).json()["settings"]["hidden"] == []
    for bad in ({"name": "x" * 41}, {"name": 3}, {"hidden": "yes"}, {"colour": "red"}, []):
        assert client.put("/api/devices/switch.fan/meta", json=bad).status_code == 400
    assert client.put("/api/devices/light.nope/meta", json={"name": "X"}).status_code == 404


def test_hidden_keeps_placement_and_all_off_still_includes_it(client, fake_ha):
    L = {"unit": "m", "rooms": [], "placements": [{"entity_id": "switch.fan", "x": 1, "y": 1}]}
    client.put("/api/layout", json=L)
    out = client.put("/api/devices/switch.fan/meta", json={"hidden": True}).json()
    assert out["placements"] == [{"entity_id": "switch.fan", "x": 1.0, "y": 1.0}]
    assert client.post("/api/bulk", json={"action": "turn_off", "entity_ids": ["switch.fan", "light.strip"]}).status_code == 200
    assert ("/api/services/switch/turn_off", {"entity_id": ["switch.fan"]}) in fake_ha.service_calls()


def test_export_import_round_trip_and_older_clients(client):
    client.put("/api/layout", json={"unit": "m", "rooms": [], "placements": [{"entity_id": "switch.fan", "x": 1, "y": 1}]})
    client.put("/api/devices/switch.fan/meta", json={"name": "Desk fan", "hidden": True})
    client.put("/api/energy/settings", json={"rate_p": 24.5, "standing_p": 60})
    exported = client.get("/api/layout").json()
    assert exported["settings"] == {"keep_on": [], "names": {"switch.fan": "Desk fan"}, "hidden": ["switch.fan"],
                                    "energy": {"rate_p": 24.5, "standing_p": 60.0}}
    # wipe, then import the export: everything comes back
    client.put("/api/layout", json={"unit": "m", "rooms": [], "placements": [],
                                    "settings": {"keep_on": [], "names": {}, "hidden": [], "energy": {}}})
    assert {d["entity_id"]: d for d in client.get("/api/devices").json()}["switch.fan"]["name"] == "Fan"
    assert client.put("/api/layout", json=exported).json() == exported
    assert {d["entity_id"]: d for d in client.get("/api/devices").json()}["switch.fan"]["name"] == "Desk fan"
    # an older client's PUT (no names/hidden/energy) leaves them alone
    old = {"unit": "ft", "rooms": [], "placements": exported["placements"], "settings": {"keep_on": []}}
    assert client.put("/api/layout", json=old).json()["settings"] == exported["settings"]
    assert client.put("/api/layout", json={"unit": "m", "rooms": [], "placements": []}).json()["settings"] == exported["settings"]


def test_names_survive_rediscovery(client):
    client.put("/api/devices/light.strip/meta", json={"name": "TV glow"})
    assert client.post("/api/devices/refresh").status_code == 200
    assert {d["entity_id"]: d for d in client.get("/api/devices").json()}["light.strip"]["name"] == "TV glow"


def test_rename_reaches_sse_stream(client):
    """A rename republishes the item to Live's observers and SSE clients straight away."""
    seen = []
    client.get("/api/devices")
    client.app.state.automations.live.add_observer(lambda item, raw: seen.append(item))
    client.put("/api/devices/light.strip/meta", json={"name": "TV glow"})
    assert [i["name"] for i in seen if i["entity_id"] == "light.strip"] == ["TV glow"]
