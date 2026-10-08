import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.ha import HAClient
from backend.live import Live

TEMPLATE_OUTPUT = """light|light.kitchen_1|Kitchen 1|TP-Link|L530
light|light.strip|Strip|TP-Link|L430C
switch|switch.fan|Fan|TP-Link|P110
rel|switch.fan|sensor.fan_current_consumption|power|W|measurement|Fan Current consumption
rel|switch.fan|sensor.fan_today_s_consumption|energy|kWh|total_increasing|Fan Today's consumption
rel|switch.fan|sensor.fan_total_consumption|energy|kWh|total_increasing|Fan Total consumption
rel|switch.fan|sensor.fan_signal_level|signal_strength|dBm|measurement|Fan Signal level
rel|switch.fan|binary_sensor.fan_overheated|problem|||Fan Overheated
switch|switch.fan_auto_off_enabled_2|Fan|TP-Link|P110
switch|switch.fan_led|Fan|TP-Link|P110
switch|switch.kettle|Kettle|TP-Link|TP11
rel|switch.kettle|sensor.kettle_current_consumption|power|W|measurement|Kettle Current consumption
switch|switch.hub_led|Hub|TP-Link|KH100
switch|switch.valve_child_lock|Valve|TP-Link|KE100
switch|switch.radarr|||
climate|climate.lounge_valve|Lounge valve|TP-Link|KE100
rel|climate.lounge_valve|sensor.lounge_valve_battery|battery|%|measurement|Lounge valve Battery
rel|climate.lounge_valve|binary_sensor.lounge_valve_battery_low|battery|||Lounge valve Battery low
binary|binary_sensor.contact_sensor_door|Front door|TP-Link|T110
rel|binary_sensor.contact_sensor_door|binary_sensor.contact_sensor_door_battery_low|battery|||Front door Battery low
rel|binary_sensor.contact_sensor_door|binary_sensor.contact_sensor_door_cloud_connection|connectivity|||Front door Cloud
binary|binary_sensor.contact_sensor_door_cloud_connection|Front door|TP-Link|T110
binary|binary_sensor.dehumidifier_tank|Dehum|Tuya|X
"""

STATES = [
    {"entity_id": "light.kitchen_1", "state": "on", "attributes": {"brightness": 200}},
    {"entity_id": "light.strip", "state": "off", "attributes": {}},
    {"entity_id": "switch.fan", "state": "off", "attributes": {}},
    {"entity_id": "switch.kettle", "state": "on", "attributes": {}},
    {"entity_id": "climate.lounge_valve", "state": "heat",
     "attributes": {"current_temperature": 19.5, "temperature": 21, "min_temp": 5, "max_temp": 30}},
    {"entity_id": "binary_sensor.contact_sensor_door", "state": "on", "attributes": {}},
    {"entity_id": "sensor.other", "state": "1", "attributes": {}},
    {"entity_id": "sensor.fan_current_consumption", "state": "12.4", "attributes": {"unit_of_measurement": "W"}},
    {"entity_id": "sensor.fan_today_s_consumption", "state": "0.153", "attributes": {"unit_of_measurement": "kWh"}},
    {"entity_id": "sensor.fan_total_consumption", "state": "88.2", "attributes": {"unit_of_measurement": "kWh"}},
    {"entity_id": "sensor.kettle_current_consumption", "state": "unavailable", "attributes": {"unit_of_measurement": "W"}},
    {"entity_id": "sensor.lounge_valve_battery", "state": "15", "attributes": {"unit_of_measurement": "%"}},
    {"entity_id": "binary_sensor.lounge_valve_battery_low", "state": "off", "attributes": {}},
    {"entity_id": "binary_sensor.contact_sensor_door_battery_low", "state": "on", "attributes": {}},
]


class FakeHA:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.template_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.url.path == "/api/template":
            self.template_calls += 1
            return httpx.Response(200, text=TEMPLATE_OUTPUT)
        if request.url.path == "/api/states":
            return httpx.Response(200, json=STATES)
        if request.url.path.startswith("/api/services/"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    def service_calls(self):
        return [(p, b) for m, p, b in self.calls if p.startswith("/api/services/")]


@pytest.fixture
def fake_ha():
    return FakeHA()


@pytest.fixture
def client(tmp_path, fake_ha):
    settings = Settings("http://ha.test", "test-token", "aaron", "s3cret", str(tmp_path / "layout.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(fake_ha.handler))
    live = Live(ha, "ws://ha.test/api/websocket", settings.ha_token, use_ws=False)
    with TestClient(create_app(settings, ha, live)) as c:
        c.headers["Authorization"] = "Basic " + base64.b64encode(b"aaron:s3cret").decode()
        yield c
