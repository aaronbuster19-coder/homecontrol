import base64

from backend.app import check_basic_auth


def hdr(s: str) -> str:
    return "Basic " + base64.b64encode(s.encode()).decode()


def test_basic_auth_check():
    assert check_basic_auth(hdr("u:p"), "u", "p")
    assert check_basic_auth(hdr("u:p:x"), "u", "p:x")
    assert not check_basic_auth(hdr("u:wrong"), "u", "p")
    assert not check_basic_auth(hdr("x:p"), "u", "p")
    assert not check_basic_auth(hdr("u"), "u", "p")
    assert not check_basic_auth("Bearer abc", "u", "p")
    assert not check_basic_auth("Basic !!!", "u", "p")
    assert not check_basic_auth(None, "u", "p")
    assert not check_basic_auth(hdr(":"), "", "")  # unset creds never authenticate


def test_auth_required(client):
    assert client.get("/healthz", headers={"Authorization": ""}).status_code == 200
    r = client.get("/api/devices", headers={"Authorization": hdr("aaron:bad")})
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert client.get("/", headers={"Authorization": ""}).status_code == 401


def test_devices(client):
    devs = {d["entity_id"]: d for d in client.get("/api/devices").json()}
    assert len(devs) == 6
    assert devs["switch.fan"]["kind"] == "plug" and devs["switch.fan"]["state"] == "off"
    v = devs["climate.lounge_valve"]
    assert v["current_temperature"] == 19.5 and v["temperature"] == 21
    assert devs["binary_sensor.contact_sensor_door"]["state"] == "on"


def test_discovery_cached_and_refresh(client, fake_ha):
    client.get("/api/devices")
    client.get("/api/devices")
    assert fake_ha.template_calls == 1
    assert client.post("/api/devices/refresh").json() == {"count": 6}
    assert fake_ha.template_calls == 2


def test_toggle(client, fake_ha):
    assert client.post("/api/devices/light.kitchen_1/toggle").status_code == 200
    assert client.post("/api/devices/switch.fan/toggle").status_code == 200
    assert fake_ha.service_calls() == [
        ("/api/services/light/toggle", {"entity_id": "light.kitchen_1"}),
        ("/api/services/switch/toggle", {"entity_id": "switch.fan"}),
    ]
    assert client.post("/api/devices/climate.lounge_valve/toggle").status_code == 400
    assert client.post("/api/devices/switch.fan_led/toggle").status_code == 404


def test_temperature(client, fake_ha):
    r = client.post("/api/devices/climate.lounge_valve/temperature", json={"temperature": 21.5})
    assert r.status_code == 200
    assert fake_ha.service_calls() == [("/api/services/climate/set_temperature",
                                        {"entity_id": "climate.lounge_valve", "temperature": 21.5})]
    assert client.post("/api/devices/climate.lounge_valve/temperature", json={"temperature": 99}).status_code == 400
    assert client.post("/api/devices/switch.fan/temperature", json={"temperature": 20}).status_code == 400


def test_layout_roundtrip(client):
    assert client.get("/api/layout").json() == {"unit": "m", "rooms": [], "placements": []}
    layout = {"unit": "m", "rooms": [{"id": "r1", "name": "Kitchen", "x": 0, "y": 0, "w": 4, "h": 3}],
              "placements": [{"entity_id": "light.kitchen_1", "x": 1.2, "y": 0.8}]}
    assert client.put("/api/layout", json=layout).status_code == 200
    assert client.get("/api/layout").json()["placements"][0]["entity_id"] == "light.kitchen_1"
    layout["placements"][0]["entity_id"] = "light.ghost"
    assert client.put("/api/layout", json=layout).status_code == 400


def test_layout_openings_and_cut(client):
    door = {"id": "o1", "type": "door", "x": 0, "y": 1, "len": 0.9, "orient": "v",
            "entity_id": "binary_sensor.contact_sensor_door"}
    layout = {"unit": "m", "rooms": [{"id": "r1", "name": "Hall", "x": 0, "y": 0, "w": 4, "h": 3,
                                      "cut": {"corner": "se", "w": 1, "h": 1}}],
              "placements": [], "openings": [door]}
    r = client.put("/api/layout", json=layout)
    assert r.status_code == 200, r.text
    got = client.get("/api/layout").json()
    assert got["openings"][0]["entity_id"] == door["entity_id"] and got["rooms"][0]["cut"]["corner"] == "se"
    layout["openings"][0]["entity_id"] = "binary_sensor.ghost"
    assert client.put("/api/layout", json=layout).status_code == 400


def test_frontend_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "homecontrol" in r.text.lower()
