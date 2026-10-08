import pytest

from backend.live import light_caps
from backend.store import LayoutError, validate_layout

LIGHT = "/api/devices/light.kitchen_1/light"


def svc(fake_ha):
    return fake_ha.service_calls()


def test_light_caps():
    c = light_caps({"supported_color_modes": ["color_temp", "hs"], "brightness": 128, "color_mode": "hs",
                    "hs_color": [30, 80], "min_color_temp_kelvin": 2500, "max_color_temp_kelvin": 6500})
    assert c["supports_color"] and c["supports_color_temp"] and c["supports_brightness"]
    assert c["brightness"] == 128 and c["hs_color"] == [30, 80] and c["max_color_temp_kelvin"] == 6500
    c = light_caps({"supported_color_modes": ["brightness"]})
    assert c["supports_brightness"] and not c["supports_color"] and not c["supports_color_temp"]
    assert "min_color_temp_kelvin" not in c
    assert not light_caps({"supported_color_modes": ["onoff"]})["supports_brightness"]
    assert light_caps({})["supported_color_modes"] == []


def test_devices_keep_light_keys(client):
    d = {x["entity_id"]: x for x in client.get("/api/devices").json()}["light.kitchen_1"]
    assert d["brightness"] == 200 and d["state"] == "on" and "supports_color" in d


@pytest.mark.parametrize("body,data", [
    ({"brightness_pct": 40}, {"brightness_pct": 40}),
    ({"hs_color": [120, 75.5]}, {"hs_color": [120, 75.5]}),
    ({"rgb_color": [255, 0, 10]}, {"rgb_color": [255, 0, 10]}),
    ({"color_temp_kelvin": 3000, "brightness_pct": 1}, {"brightness_pct": 1, "color_temp_kelvin": 3000}),
])
def test_light(client, fake_ha, body, data):
    assert client.post(LIGHT, json=body).status_code == 200
    assert svc(fake_ha) == [("/api/services/light/turn_on", {"entity_id": "light.kitchen_1", **data})]


@pytest.mark.parametrize("body", [
    {}, {"brightness_pct": 0}, {"brightness_pct": 101}, {"hs_color": [400, 10]}, {"hs_color": [1]},
    {"rgb_color": [0, 0, 256]}, {"rgb_color": [1, 2]}, {"color_temp_kelvin": 500},
    {"hs_color": [1, 1], "rgb_color": [1, 1, 1]}, {"bogus": 1},
])
def test_light_invalid(client, fake_ha, body):
    assert client.post(LIGHT, json=body).status_code in (400, 422)
    assert svc(fake_ha) == []


def test_light_only_lights(client, fake_ha):
    assert client.post("/api/devices/switch.fan/light", json={"brightness_pct": 5}).status_code == 400
    assert client.post("/api/devices/light.ghost/light", json={"brightness_pct": 5}).status_code == 404
    assert svc(fake_ha) == []


def test_bulk(client, fake_ha):
    r = client.post("/api/bulk", json={"action": "turn_off",
                                       "entity_ids": ["light.kitchen_1", "switch.fan", "light.strip", "switch.fan"]})
    assert r.status_code == 200 and r.json()["count"] == 3
    assert svc(fake_ha) == [("/api/services/light/turn_off", {"entity_id": ["light.kitchen_1", "light.strip"]}),
                            ("/api/services/switch/turn_off", {"entity_id": ["switch.fan"]})]
    fake_ha.calls.clear()
    assert client.post("/api/bulk", json={"action": "turn_on", "entity_ids": ["light.strip"]}).status_code == 200
    assert svc(fake_ha) == [("/api/services/light/turn_on", {"entity_id": ["light.strip"]})]


@pytest.mark.parametrize("body", [
    {"action": "toggle", "entity_ids": ["light.strip"]},
    {"action": "turn_off", "entity_ids": ["light.strip", "climate.lounge_valve"]},
    {"action": "turn_off", "entity_ids": ["switch.fan_led"]},
    {"action": "turn_off", "entity_ids": ["light.ghost"]},
])
def test_bulk_invalid(client, fake_ha, body):
    assert client.post("/api/bulk", json=body).status_code == 400
    assert svc(fake_ha) == []


def test_valves(client, fake_ha):
    assert client.post("/api/valves/temperature", json={"temperature": 19}).json()["count"] == 1
    assert svc(fake_ha) == [("/api/services/climate/set_temperature",
                             {"entity_id": ["climate.lounge_valve"], "temperature": 19})]
    fake_ha.calls.clear()
    body = {"temperature": 20.5, "entity_ids": ["climate.lounge_valve"]}
    assert client.post("/api/valves/temperature", json=body).status_code == 200
    assert svc(fake_ha)[0][1]["temperature"] == 20.5


@pytest.mark.parametrize("body", [
    {"temperature": 4}, {"temperature": 36}, {"temperature": 20, "entity_ids": ["switch.fan"]},
])
def test_valves_invalid(client, fake_ha, body):
    assert client.post("/api/valves/temperature", json=body).status_code == 400
    assert svc(fake_ha) == []


def test_keep_on_layout(client):
    base = {"unit": "m", "rooms": [], "placements": []}
    r = client.put("/api/layout", json={**base, "settings": {"keep_on": ["switch.fan"]}})
    assert r.status_code == 200 and client.get("/api/layout").json()["settings"] == {"keep_on": ["switch.fan"]}
    for bad in (["light.strip"], ["switch.ghost"], "switch.fan", [1]):
        assert client.put("/api/layout", json={**base, "settings": {"keep_on": bad}}).status_code == 400
    assert "settings" not in client.put("/api/layout", json=base).json()


def test_keep_on_validate():
    assert validate_layout({"settings": {}}, {"switch.a"}, {"switch.a"})["settings"] == {"keep_on": []}
    assert validate_layout({"settings": {"keep_on": ["switch.a", "switch.a"]}}, set(), {"switch.a"})["settings"] == \
        {"keep_on": ["switch.a"]}
    with pytest.raises(LayoutError):
        validate_layout({"settings": []}, set(), set())
