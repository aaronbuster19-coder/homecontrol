import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.ha import HAClient
from backend.live import Live
from backend.tests import conftest

LIGHTS = "/api/services/light/turn_off"
SWITCH = "/api/services/switch/turn_off"
TEMP = "/api/services/climate/set_temperature"


def svc(fake_ha):
    return fake_ha.service_calls()


def away(client):
    r = client.post("/api/mode", json={"mode": "away"})
    assert r.status_code == 200, r.text
    return r.json()


def home(client):
    r = client.post("/api/mode", json={"mode": "home"})
    assert r.status_code == 200, r.text
    return r.json()


def test_default_mode(client):
    assert client.get("/api/mode").json() == {"mode": "home", "since": None, "away_temp": 16.0, "tv_off": True}


def test_away_calls(client, fake_ha):
    r = away(client)
    assert svc(fake_ha) == [
        (LIGHTS, {"entity_id": ["light.kitchen_1", "light.strip"]}),
        (SWITCH, {"entity_id": ["switch.fan", "switch.kettle"]}),
        (TEMP, {"entity_id": ["climate.lounge_valve"], "temperature": 16.0}),
    ]
    assert r["mode"] == "away" and r["since"] and r["valves"] == {"climate.lounge_valve": 16.0}
    assert r["turned_off"] == ["light.kitchen_1", "light.strip", "switch.fan", "switch.kettle"] and r["kept_on"] == []
    assert client.get("/api/mode").json()["mode"] == "away"


def test_away_respects_keep_on(client, fake_ha):
    assert client.put("/api/layout", json={"rooms": [], "placements": [], "settings": {"keep_on": ["switch.fan"]}}).status_code == 200
    r = away(client)
    assert (SWITCH, {"entity_id": ["switch.kettle"]}) in svc(fake_ha)
    assert "switch.fan" not in str(svc(fake_ha)) and r["kept_on"] == ["switch.fan"]


def test_home_restores_targets(client, fake_ha):
    away(client)
    fake_ha.calls.clear()
    r = home(client)
    assert svc(fake_ha) == [(TEMP, {"entity_id": ["climate.lounge_valve"], "temperature": 21.0})]
    assert r["mode"] == "home" and r["valves"] == {"climate.lounge_valve": 21.0}
    fake_ha.calls.clear()
    assert home(client)["valves"] == {} and svc(fake_ha) == []  # home twice: nothing to do


def test_away_twice_keeps_targets(client, fake_ha):
    since = away(client)["since"]
    # Valve now reports the away temperature; a second Away must not remember 16°.
    conftest.STATES[4]["attributes"]["temperature"] = 16
    try:
        client.post("/api/devices/refresh")
        r = away(client)
        assert r["since"] == since
        fake_ha.calls.clear()
        home(client)
        assert svc(fake_ha) == [(TEMP, {"entity_id": ["climate.lounge_valve"], "temperature": 21.0})]
    finally:
        conftest.STATES[4]["attributes"]["temperature"] = 21


def test_home_skips_vanished_and_clamps(client, fake_ha, tmp_path):
    from backend.modes import ModeStore, restore_groups
    assert restore_groups({"a": 40, "b": 2, "c": 20, "gone": 20}, {"a", "b", "c"}) == {35: ["a"], 5: ["b"], 20: ["c"]}
    away(client)
    st = ModeStore(str(tmp_path / "layout.db"))
    s = st.get()
    s["targets"] = {"climate.lounge_valve": 99, "climate.gone": 20}
    st.put(s)
    fake_ha.calls.clear()
    r = home(client)
    assert svc(fake_ha) == [(TEMP, {"entity_id": ["climate.lounge_valve"], "temperature": 35.0})]
    assert r["skipped"] == ["climate.gone"]


@pytest.mark.parametrize("prev", [True, False])
def test_alerts_flag_remembered(client, prev):
    client.put("/api/alerts/settings", json={"enabled": prev, "door_open_minutes": 7})
    assert away(client)["alerts_enabled"] is True
    assert client.get("/api/alerts/settings").json()["enabled"] is True
    assert home(client)["alerts_enabled"] is prev
    s = client.get("/api/alerts/settings").json()
    assert s["enabled"] is prev and s["door_open_minutes"] == 7


@pytest.mark.parametrize("body", [{}, {"away_temp": 4}, {"away_temp": 26}, {"away_temp": "16"}, {"away_temp": True}, []])
def test_settings_invalid(client, body):
    assert client.put("/api/mode/settings", json=body).status_code == 400


def test_settings_and_bad_mode(client, fake_ha):
    assert client.put("/api/mode/settings", json={"away_temp": 12.5}).json()["away_temp"] == 12.5
    assert client.post("/api/mode", json={"mode": "party"}).status_code == 400
    away(client)
    assert svc(fake_ha)[-1] == (TEMP, {"entity_id": ["climate.lounge_valve"], "temperature": 12.5})


def make(tmp_path, fake_ha):
    settings = Settings("http://ha.test", "test-token", "aaron", "s3cret", str(tmp_path / "layout.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(fake_ha.handler))
    c = TestClient(create_app(settings, ha, Live(ha, "ws://x/api/websocket", "t", use_ws=False)))
    c.headers["Authorization"] = "Basic " + base64.b64encode(b"aaron:s3cret").decode()
    return c


def test_mode_survives_restart(tmp_path, fake_ha):
    with make(tmp_path, fake_ha) as c:
        c.put("/api/alerts/settings", json={"enabled": False})
        away(c)
    fake_ha.calls.clear()
    with make(tmp_path, fake_ha) as c:
        assert c.get("/api/mode").json()["mode"] == "away"
        r = home(c)
        assert r["valves"] == {"climate.lounge_valve": 21.0} and r["alerts_enabled"] is False
    assert svc(fake_ha) == [(TEMP, {"entity_id": ["climate.lounge_valve"], "temperature": 21.0})]
