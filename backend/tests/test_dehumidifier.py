import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.alerts import DEFAULT_SETTINGS, validate_settings
from backend.app import create_app
from backend.automations import AutoStore
from backend.config import Settings
from backend.dehumidifier import TankAlert
from backend.discovery import Device, classify, parse_template_output
from backend.ha import HAClient
from backend.history import History, build, ms
from backend.live import Live, build_device
from backend.tests.conftest import STATES, TEMPLATE_OUTPUT, FakeHA

# Shape 1: official Tuya integration — humidifier entity (device_class dehumidifier) plus feature switches/sensors.
HUMIDIFIER_SHAPE = """humidifier|humidifier.dehumidifier|Dehumidifier|Tuya|CS-20L
dc|humidifier.dehumidifier|dehumidifier
rel|humidifier.dehumidifier|sensor.dehumidifier_humidity|humidity|%|measurement|Dehumidifier Humidity
rel|humidifier.dehumidifier|sensor.dehumidifier_temperature|temperature|°C|measurement|Dehumidifier Temperature
rel|humidifier.dehumidifier|binary_sensor.dehumidifier_defrost|problem|||Dehumidifier Defrost
rel|humidifier.dehumidifier|binary_sensor.dehumidifier_tank_full|problem|||Dehumidifier Tank full
switch|switch.dehumidifier_child_lock|Dehumidifier|Tuya|CS-20L
switch|switch.dehumidifier_ionizer|Dehumidifier|Tuya|CS-20L
switch|switch.dehumidifier_power|Dehumidifier|Tuya|CS-20L
"""
# Shape 2: switch only (e.g. Local Tuya), the device model says what it is.
SWITCH_SHAPE = """switch|switch.bedroom_dh|Bedroom|Tuya|Dehumidifier DH-12
rel|switch.bedroom_dh|sensor.bedroom_dh_humidity|humidity|%|measurement|Bedroom Humidity
rel|switch.bedroom_dh|binary_sensor.bedroom_dh_water|moisture|||Bedroom Water
switch|switch.bedroom_dh_child_lock|Bedroom|Tuya|Dehumidifier DH-12
switch|switch.bedroom_dh_sleep|Bedroom|Tuya|Dehumidifier DH-12
"""
TEMPLATE = TEMPLATE_OUTPUT + HUMIDIFIER_SHAPE + SWITCH_SHAPE + "switch|switch.radarr|Radarr||\nswitch|switch.tile_tracker|Tile|Tile|Mate\n"

DH, DS = "humidifier.dehumidifier", "switch.bedroom_dh"


def dh_states(**over):
    attrs = {"humidity": 50, "current_humidity": 62, "min_humidity": 30, "max_humidity": 80, "mode": "auto",
             "available_modes": ["auto", "continuous", "sleep"], "action": "drying", "device_class": "dehumidifier"}
    attrs.update(over)
    return [
        {"entity_id": DH, "state": "on", "attributes": attrs},
        {"entity_id": "sensor.dehumidifier_humidity", "state": "61", "attributes": {"unit_of_measurement": "%"}},
        {"entity_id": "sensor.dehumidifier_temperature", "state": "21.46", "attributes": {"unit_of_measurement": "°C"}},
        {"entity_id": "binary_sensor.dehumidifier_tank_full", "state": "off", "attributes": {}},
        {"entity_id": DS, "state": "off", "attributes": {}},
        {"entity_id": "sensor.bedroom_dh_humidity", "state": "57.3", "attributes": {"unit_of_measurement": "%"}},
        {"entity_id": "binary_sensor.bedroom_dh_water", "state": "on", "attributes": {}},
    ]


class DehumHA(FakeHA):
    def __init__(self):
        super().__init__()
        self.states = STATES + dh_states()

    def handler(self, request):
        if request.url.path == "/api/template":
            self.calls.append((request.method, request.url.path, None))
            return httpx.Response(200, text=TEMPLATE)
        if request.url.path == "/api/states":
            self.calls.append((request.method, request.url.path, None))
            return httpx.Response(200, json=self.states)
        return super().handler(request)


@pytest.fixture
def dha():
    return DehumHA()


def make_client(tmp_path, dha):
    settings = Settings("http://ha.test", "test-token", "aaron", "s3cret", str(tmp_path / "layout.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(dha.handler))
    live = Live(ha, "ws://ha.test/api/websocket", settings.ha_token, use_ws=False)
    c = TestClient(create_app(settings, ha, live, push_sender=lambda sub, p: 201))
    c.headers["Authorization"] = "Basic " + base64.b64encode(b"aaron:s3cret").decode()
    return c


@pytest.fixture
def dclient(tmp_path, dha):
    with make_client(tmp_path, dha) as c:
        yield c


def devices(c):
    return {d["entity_id"]: d for d in c.get("/api/devices").json()}


# ---------------- discovery ----------------
def test_classify_humidifier_entities():
    assert classify("humidifier", "humidifier.x", "Generic", "Y", "Box", "dehumidifier") == "dehumidifier"
    assert classify("humidifier", "humidifier.x", "Tuya", "CS", "Box", "") == "dehumidifier"           # Tuya / Local Tuya
    assert classify("humidifier", "humidifier.x", "Acme", "Y", "Bathroom dehumidifier", "") == "dehumidifier"
    assert classify("humidifier", "humidifier.x", "Acme", "Y", "Nursery", "humidifier") is None        # a real humidifier
    assert classify("humidifier", "humidifier.x", "Acme", "Y", "Nursery", "") is None


def test_classify_switch_only_dehumidifier():
    assert classify("switch", "switch.dry", "Tuya", "Dehumidifier DH-12", "Bedroom") == "dehumidifier"   # by model
    assert classify("switch", "switch.dry", "Tuya", "TS011", "Hall dehumidifier") == "dehumidifier"       # by name
    assert classify("switch", "switch.hall_dehumidifier", "", "", "") == "dehumidifier"                   # by entity id
    for feature in ("child_lock", "ionizer", "anion", "sleep", "led", "sound", "filter_reset", "defrost", "timer_2"):
        assert classify("switch", f"switch.dry_{feature}", "Tuya", "Dehumidifier", "Bedroom") is None, feature
    assert classify("switch", "switch.radarr", "", "", "Radarr") is None
    assert classify("switch", "switch.tuya_socket", "Tuya", "TS011", "Socket") is None
    # A TP-Link plug that happens to power a dehumidifier stays a plug.
    assert classify("switch", "switch.dehumidifier_plug", "TP-Link", "P110", "Dehumidifier plug") == "plug"
    assert classify("switch", "switch.dehumidifier_plug_led", "TP-Link", "P110", "Dehumidifier plug") is None
    # Tuya bits that aren't a device we control
    assert classify("binary", "binary_sensor.dehumidifier_tank", "Tuya", "X", "Dehum") is None
    assert classify("climate", "climate.dehumidifier", "Tuya", "CS", "Dehum") is None


def test_parse_one_device_per_dehumidifier_and_related():
    devs = {d.entity_id: d for d in parse_template_output(TEMPLATE)}
    kinds = {e: d.kind for e, d in devs.items()}
    # lights/plugs/valves/sensors exactly as before, plus one dehumidifier per HA device
    assert kinds == {"light.kitchen_1": "light", "light.strip": "light", "switch.fan": "plug", "switch.kettle": "plug",
                     "climate.lounge_valve": "valve", "binary_sensor.contact_sensor_door": "sensor",
                     DH: "dehumidifier", DS: "dehumidifier"}
    assert devs[DH].related == {"humidity": "sensor.dehumidifier_humidity", "temperature": "sensor.dehumidifier_temperature",
                                "tank": "binary_sensor.dehumidifier_tank_full"}  # defrost is not the tank
    assert devs[DS].related == {"humidity": "sensor.bedroom_dh_humidity", "tank": "binary_sensor.bedroom_dh_water"}
    assert devs[DH].name == "Dehumidifier" and devs[DH].model == "CS-20L"
    # unchanged for the others
    assert devs["switch.fan"].related == {"power": "sensor.fan_current_consumption", "energy_today": "sensor.fan_today_s_consumption"}


def test_parse_switch_only_picks_power_switch_and_tank_fallback():
    text = """switch|switch.dh_power|Dry|Tuya|Dehumidifier
rel|switch.dh_power|binary_sensor.dh_fault|problem|||Dry Fault
rel|switch.dh_power|binary_sensor.dh_battery|battery|||Dry Battery
switch|switch.dh_anion|Dry|Tuya|Dehumidifier
switch|switch.dh|Dry|Tuya|Dehumidifier
"""
    devs = parse_template_output(text)
    assert [d.entity_id for d in devs] == ["switch.dh_power"]
    assert devs[0].related["tank"] == "binary_sensor.dh_fault"     # device_class problem when nothing says "tank"
    assert devs[0].related["battery_low"] == "binary_sensor.dh_battery"


def test_two_dehumidifiers_stay_separate():
    text = "switch|switch.a_dehumidifier|A|Tuya|X\nswitch|switch.b_dehumidifier|B|Tuya|X\n"
    assert {d.entity_id for d in parse_template_output(text)} == {"switch.a_dehumidifier", "switch.b_dehumidifier"}


# ---------------- device dict ----------------
def test_device_fields(dclient):
    d = devices(dclient)
    h, s = d[DH], d[DS]
    assert {k: h[k] for k in ("kind", "state", "control", "current_humidity", "current_temperature", "target_humidity",
                              "min_humidity", "max_humidity", "mode", "available_modes", "action", "tank_full")} == {
        "kind": "dehumidifier", "state": "on", "control": "humidifier", "current_humidity": 62, "current_temperature": 21.5,
        "target_humidity": 50, "min_humidity": 30, "max_humidity": 80, "mode": "auto",
        "available_modes": ["auto", "continuous", "sleep"], "action": "drying", "tank_full": False}
    assert s["control"] == "switch" and s["current_humidity"] == 57.3 and s["tank_full"] is True
    assert s["target_humidity"] is None and s["available_modes"] == [] and s["current_temperature"] is None


def test_device_fields_fallbacks():
    d = Device(DH, "dehumidifier", "D", "X", {"humidity": "sensor.h"})
    states = {DH: {"state": "off", "attributes": {"humidity": 45}}, "sensor.h": {"state": "70", "attributes": {}}}
    item = build_device(d, states)
    assert item["current_humidity"] == 70  # older HA: no current_humidity attribute -> humidity sensor
    assert (item["min_humidity"], item["max_humidity"]) == (0, 100)  # HA's defaults
    assert "tank_full" not in item
    states["sensor.h"]["state"] = "unavailable"
    assert build_device(d, states)["current_humidity"] is None


# ---------------- endpoints ----------------
def svc(dha):
    return dha.service_calls()


def test_toggle(dclient, dha):
    assert dclient.post(f"/api/devices/{DH}/toggle").status_code == 200
    assert dclient.post(f"/api/devices/{DS}/toggle").status_code == 200
    assert dclient.post("/api/devices/switch.fan/toggle").status_code == 200
    assert svc(dha) == [("/api/services/humidifier/toggle", {"entity_id": DH}),
                        ("/api/services/switch/toggle", {"entity_id": DS}),
                        ("/api/services/switch/toggle", {"entity_id": "switch.fan"})]
    assert dclient.post("/api/devices/switch.dehumidifier_child_lock/toggle").status_code == 404


@pytest.mark.parametrize("h", [30, 55, 80, 45.0])
def test_set_humidity(dclient, dha, h):
    r = dclient.post(f"/api/devices/{DH}/humidity", json={"humidity": h})
    assert r.status_code == 200, r.text
    assert svc(dha) == [("/api/services/humidifier/set_humidity", {"entity_id": DH, "humidity": int(h)})]


@pytest.mark.parametrize("body", [{"humidity": 25}, {"humidity": 85}, {"humidity": 52.5}, {"humidity": True},
                                  {"humidity": "50"}, {"humidity": None}, {}, [50]])
def test_set_humidity_invalid(dclient, dha, body):
    r = dclient.post(f"/api/devices/{DH}/humidity", json=body)
    assert r.status_code == 400, r.text
    assert svc(dha) == []


def test_set_humidity_wrong_device(dclient, dha):
    assert dclient.post(f"/api/devices/{DS}/humidity", json={"humidity": 50}).json()["detail"] == \
        "this dehumidifier can only be switched on and off"
    assert dclient.post("/api/devices/switch.fan/humidity", json={"humidity": 50}).status_code == 400
    assert dclient.post("/api/devices/humidifier.ghost/humidity", json={"humidity": 50}).status_code == 404
    assert dclient.post(f"/api/devices/{DH}/humidity", content=b"{nope").status_code == 400
    assert svc(dha) == []


def test_set_mode(dclient, dha):
    assert dclient.post(f"/api/devices/{DH}/mode", json={"mode": "sleep"}).status_code == 200
    assert svc(dha) == [("/api/services/humidifier/set_mode", {"entity_id": DH, "mode": "sleep"})]
    dha.calls.clear()
    for body in ({"mode": "turbo"}, {"mode": 1}, {}, {"mode": "Sleep"}):
        assert dclient.post(f"/api/devices/{DH}/mode", json=body).status_code == 400
    assert dclient.post(f"/api/devices/{DS}/mode", json={"mode": "auto"}).json()["detail"] == "this dehumidifier has no modes"
    assert svc(dha) == []


def test_humidity_range_follows_entity(tmp_path, dha):
    dha.states = STATES + dh_states(min_humidity=35, max_humidity=70, available_modes=None)
    with make_client(tmp_path, dha) as c:
        assert c.post(f"/api/devices/{DH}/humidity", json={"humidity": 30}).json()["detail"] == "humidity must be 35–70 %"
        assert c.post(f"/api/devices/{DH}/humidity", json={"humidity": 75}).status_code == 400
        assert c.post(f"/api/devices/{DH}/humidity", json={"humidity": 70}).status_code == 200
        assert c.post(f"/api/devices/{DH}/mode", json={"mode": "auto"}).status_code == 400  # no available_modes


def test_all_off_bulk_includes_dehumidifiers(dclient, dha):
    r = dclient.post("/api/bulk", json={"action": "turn_off", "entity_ids": ["light.kitchen_1", DH, DS]})
    assert r.status_code == 200 and r.json()["count"] == 3
    assert svc(dha) == [("/api/services/light/turn_off", {"entity_id": ["light.kitchen_1"]}),
                        ("/api/services/switch/turn_off", {"entity_id": [DS]}),
                        ("/api/services/humidifier/turn_off", {"entity_id": [DH]})]


def test_away_leaves_dehumidifier_running(dclient, dha):
    r = dclient.post("/api/mode", json={"mode": "away"}).json()
    assert DH not in r["turned_off"] and DS not in r["turned_off"]
    assert not [b for p, b in svc(dha) if DH in json.dumps(b) or DS in json.dumps(b)]


def test_layout_all_off_include(dclient):
    base = {"rooms": [], "placements": []}
    r = dclient.put("/api/layout", json={**base, "settings": {"keep_on": [], "all_off_include": [DH, DH]}})
    assert r.status_code == 200 and r.json()["settings"] == {"keep_on": [], "all_off_include": [DH]}
    assert dclient.get("/api/layout").json()["settings"]["all_off_include"] == [DH]
    for bad in (["switch.fan"], ["light.ghost"], DH, [1]):
        assert dclient.put("/api/layout", json={**base, "settings": {"all_off_include": bad}}).status_code == 400
    # without the key nothing is added (old layouts unchanged)
    assert dclient.put("/api/layout", json={**base, "settings": {"keep_on": []}}).json()["settings"] == {"keep_on": []}


# ---------------- tank-full push ----------------
def test_alert_setting():
    assert DEFAULT_SETTINGS["dehumidifier_tank"] is True
    assert validate_settings({"dehumidifier_tank": False}, DEFAULT_SETTINGS)["dehumidifier_tank"] is False
    with pytest.raises(ValueError):
        validate_settings({"dehumidifier_tank": "no"}, DEFAULT_SETTINGS)


def test_tank_push_once_and_reset(tmp_path):
    store, pushes, settings = AutoStore(str(tmp_path / "a.db")), [], dict(DEFAULT_SETTINGS)

    async def notify(p):
        pushes.append(p)
    t = TankAlert(store, lambda: settings, notify)
    item = {"entity_id": DH, "name": "Dehumidifier", "kind": "dehumidifier", "tank_full": True}
    run = lambda **kw: asyncio.run(t.tick([{**item, **kw}]))
    assert t.observe(item)
    run(); run(); run(tank_full=None)  # unavailable tank sensor: no reset
    assert [p["title"] for p in pushes] == ["Dehumidifier: tank full"] and pushes[0]["tag"] == f"tank-{DH}"
    assert not t.observe(item)
    # a restart doesn't repeat it
    t = TankAlert(store, lambda: settings, notify); run()
    assert len(pushes) == 1
    run(tank_full=False); run()   # emptied, full again: one more
    assert len(pushes) == 2
    run(tank_full=False)
    settings["dehumidifier_tank"] = False
    run()
    assert len(pushes) == 2


def test_tank_push_through_automations(dclient, dha):
    sent = []
    automations = dclient.app.state.automations
    automations.pusher.send = lambda sub, p: sent.append(p) or 201
    automations.pusher.store.add({"endpoint": "https://push.test/1", "keys": {"p256dh": "k", "auth": "a"}})
    dclient.get("/api/devices")
    asyncio.run(automations.tick())
    asyncio.run(automations.tick())
    assert [p["title"] for p in sent if p["tag"].startswith("tank-")] == ["Bedroom: tank full"]  # the switch-only one has its water sensor on


# ---------------- history ----------------
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def row(dt, state, **attrs):
    return {"state": state, "last_changed": dt.isoformat(), "last_updated": dt.isoformat(), "attributes": attrs}


def test_history_series_and_timeline():
    t0 = NOW - timedelta(hours=24)
    dev = Device(DH, "dehumidifier", "D", "X", {"humidity": "sensor.h"})
    rows = [{**row(t0, "on", humidity=50, current_humidity=65), "entity_id": DH},
            row(t0 + timedelta(hours=6), "on", humidity=50, current_humidity=55),
            row(t0 + timedelta(hours=12), "off", humidity=45, current_humidity=58)]
    out = build(dev, "24h", ms(t0), ms(NOW), {DH: rows})
    assert [s["name"] for s in out["series"]] == ["Current", "Target"]
    assert out["series"][0]["unit"] == "%" and out["series"][1]["step"] is True
    assert [p[1] for p in out["series"][0]["points"]] == [65.0, 55.0, 58.0, 58.0]
    assert [p[1] for p in out["series"][1]["points"]] == [50.0, 50.0, 45.0, 45.0]
    assert [(r["state"], r["start"], r["end"]) for r in out["timeline"]] == [
        ("on", ms(t0), ms(t0) + 12 * 3_600_000), ("off", ms(t0) + 12 * 3_600_000, ms(NOW))]


def test_history_switch_only_uses_humidity_sensor():
    t0 = NOW - timedelta(hours=24)
    dev = Device(DS, "dehumidifier", "D", "X", {"humidity": "sensor.h"})
    out = build(dev, "24h", ms(t0), ms(NOW), {DS: [{**row(t0, "on"), "entity_id": DS}],
                                               "sensor.h": [{**row(t0, "60"), "entity_id": "sensor.h"}]})
    assert [s["name"] for s in out["series"]] == ["Current"] and out["series"][0]["points"][0][1] == 60.0
    assert out["timeline"][0]["state"] == "on"


def test_history_endpoint_fetches_attributes_and_sensor(dclient, dha):
    dha.history = [[{**row(NOW, "on", humidity=50, current_humidity=60), "entity_id": DH}]]
    r = dclient.get(f"/api/history/{DH}?range=24h")
    assert r.status_code == 200 and r.json()["kind"] == "dehumidifier"
    req = [q for q in dha.requests if q.url.path.startswith("/api/history/period/")][-1]
    assert req.url.params["filter_entity_id"] == f"{DH},sensor.dehumidifier_humidity"
    assert "no_attributes" not in req.url.params


def test_history_class_dehumidifier_ids():
    calls = []

    class HA:
        async def history(self, start, end, ids, attributes=False):
            calls.append((ids, attributes))
            return []
    dev = Device(DS, "dehumidifier", "D", "X", {})
    asyncio.run(History(HA()).device(dev, "24h", now=NOW))
    assert calls == [([DS], True)]
