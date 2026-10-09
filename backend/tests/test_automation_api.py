from backend.alerts import DEFAULT_SETTINGS

TEMP = "/api/services/climate/set_temperature"
VALVE = "climate.lounge_valve"
LAYOUT = {"rooms": [{"id": "l", "name": "Lounge", "x": 0, "y": 0, "w": 4, "h": 3}],
          "placements": [{"entity_id": VALVE, "x": 1, "y": 1}],
          "openings": [{"id": "w1", "type": "window", "x": 1, "y": 0, "len": 1.2, "orient": "h",
                        "entity_id": "binary_sensor.contact_sensor_door"}]}


def auto(client):
    return client.app.state.automations


def test_settings_roundtrip(client):
    s = client.get("/api/alerts/settings").json()
    assert s == DEFAULT_SETTINGS and s["window_open_minutes"] == 2 and s["window_off_temp"] == 7.0
    r = client.put("/api/alerts/settings", json={"window_open_minutes": 5, "window_off_temp": 8, "health_battery": False})
    assert r.status_code == 200 and r.json()["window_off_temp"] == 8.0 and r.json()["door_open_minutes"] == 5
    assert client.put("/api/alerts/settings", json={"window_off_temp": 20}).status_code == 400
    assert client.get("/api/alerts/settings").json()["health_battery"] is False


def test_status_and_summary_endpoints(client, fake_ha):
    assert client.get("/api/automations/status").json() == {"windows_linked": 0, "held": []}
    assert client.put("/api/layout", json=LAYOUT).status_code == 200
    assert client.get("/api/automations/status").json()["windows_linked"] == 1
    assert client.get("/api/summary/latest").status_code == 404
    p = client.post("/api/summary/preview").json()
    assert p["preview"] and p["energy"]["this_kwh"] == 0 and p["plugs"][0]["name"] == "Fan"
    auto(client).store.put_summary("2026-W41", {"week": "2026-W41", "text": "x"})
    assert client.get("/api/summary/latest").json()["week"] == "2026-W41"
    assert not [c for c in fake_ha.service_calls()]  # previews never touch devices


def test_away_home_respect_window_hold(client, fake_ha):
    client.put("/api/layout", json=LAYOUT)
    a = auto(client)
    a.window.holds[VALVE] = {"restore": 21.0, "temp": 7.0, "since": 0, "windows": ["w1"], "pending": False}
    r = client.post("/api/mode", json={"mode": "away"}).json()
    assert r["held_by_window"] == [VALVE] and r["valves"] == {}
    assert not [c for c in fake_ha.service_calls() if c[0] == TEMP]  # the open window keeps it low
    fake_ha.calls.clear()
    r = client.post("/api/mode", json={"mode": "home"}).json()
    assert r["held_by_window"] == [VALVE] and r["valves"] == {}
    assert not fake_ha.service_calls()
    assert a.window.held() == {VALVE: 21.0}  # pre-window target, restored when it closes
    # without a hold, Away/Home behave as before
    a.window.holds.clear()
    client.post("/api/mode", json={"mode": "away"})
    assert (TEMP, {"entity_id": [VALVE], "temperature": 16.0}) in fake_ha.service_calls()


def test_home_adopts_target_for_held_valve(client, fake_ha):
    client.put("/api/layout", json=LAYOUT)
    client.post("/api/mode", json={"mode": "away"})  # remembers 21
    a = auto(client)
    a.window.holds[VALVE] = {"restore": 16.0, "temp": 7.0, "since": 0, "windows": ["w1"], "pending": False}
    fake_ha.calls.clear()
    r = client.post("/api/mode", json={"mode": "home"}).json()
    assert r["valves"] == {} and a.window.held() == {VALVE: 21.0} and not fake_ha.service_calls()
