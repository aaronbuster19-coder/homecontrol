import asyncio
import base64

import httpx
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.discovery import Related, number, parse_template_output, pick_related
from backend.ha import HAClient
from backend.live import Live, build_device, sse, ws_url
from backend.tests.conftest import STATES, TEMPLATE_OUTPUT


def devices():
    return {d.entity_id: d for d in parse_template_output(TEMPLATE_OUTPUT)}


def test_template_related_parsed():
    devs = devices()
    assert len(devs) == 6
    assert devs["switch.fan"].related == {"power": "sensor.fan_current_consumption",
                                          "energy_today": "sensor.fan_today_s_consumption"}
    assert devs["switch.kettle"].related == {"power": "sensor.kettle_current_consumption"}
    assert devs["climate.lounge_valve"].related == {"battery": "sensor.lounge_valve_battery",
                                                    "battery_low": "binary_sensor.lounge_valve_battery_low"}
    assert devs["binary_sensor.contact_sensor_door"].related == {
        "battery_low": "binary_sensor.contact_sensor_door_battery_low"}
    assert devs["light.kitchen_1"].related == {}
    assert "related" not in devs["switch.fan"].to_dict()


def test_pick_related_rules():
    rows = [Related("sensor.a_total", "energy", "kWh", "total_increasing", "A Total consumption"),
            Related("sensor.a_x", "energy", "kWh", "total_increasing", "A Today's consumption"),
            Related("binary_sensor.a_problem", "problem", "", "", "A Overheated"),
            Related("sensor.a_volts", "voltage", "V", "measurement", "A Voltage")]
    assert pick_related(rows) == {"energy_today": "sensor.a_x"}  # found via friendly name; lifetime total ignored
    assert pick_related(rows[:1] + rows[2:]) == {}
    assert pick_related([Related("sensor.b", "battery", "%", "", "B")]) == {"battery": "sensor.b"}
    assert pick_related([Related("binary_sensor.b", "battery", "", "", "B")]) == {"battery_low": "binary_sensor.b"}


def test_number_parsing():
    assert number({"state": "12.5"}) == 12.5
    assert number({"state": "1.5", "attributes": {"unit_of_measurement": "kW"}}) == 1500
    assert number({"state": "250", "attributes": {"unit_of_measurement": "Wh"}}) == 0.25
    for bad in ("unavailable", "unknown", "abc", "nan", None):
        assert number({"state": bad}) is None
    assert number(None) is None


def test_build_device_values():
    states = {s["entity_id"]: s for s in STATES}
    devs = devices()
    fan = build_device(devs["switch.fan"], states)
    assert fan["power"] == 12.4 and fan["energy_today"] == 0.153
    kettle = build_device(devs["switch.kettle"], states)
    assert "power" not in kettle and "energy_today" not in kettle
    valve = build_device(devs["climate.lounge_valve"], states)
    assert valve["battery"] == 15 and valve["battery_low"] is False
    door = build_device(devs["binary_sensor.contact_sensor_door"], states)
    assert door["battery_low"] is True and "battery" not in door


def test_ws_url():
    assert ws_url("http://ha.local:8123") == "ws://ha.local:8123/api/websocket"
    assert ws_url("https://ha.example.com/") == "wss://ha.example.com/api/websocket"


def make_live():
    live = Live(None, "ws://x/api/websocket", "t", use_ws=False)
    live.set_devices(devices())
    live.load_states(STATES)
    return live


def event(eid, state, attrs=None):
    return {"id": 1, "type": "event", "event": {"event_type": "state_changed", "data": {
        "entity_id": eid, "new_state": {"entity_id": eid, "state": state, "attributes": attrs or {}}}}}


def drain(c):
    out = []
    while not c.queue.empty():
        out.append(c.queue.get_nowait())
    return out


def test_state_changed_fans_out():
    async def go():
        live = make_live()
        a, b = live.subscribe(), live.subscribe()
        live.handle(event("sensor.fan_current_consumption", "40", {"unit_of_measurement": "W"}))
        live.handle(event("sensor.unrelated", "1"))
        live.handle({"id": 1, "type": "result", "success": True})
        msgs = drain(a)
        assert len(msgs) == 1 and drain(b) == msgs
        assert msgs[0].startswith("event: device\ndata: ") and '"power":40.0' in msgs[0]
        live.handle([event("switch.fan", "on")])  # coalesced list form
        assert '"state":"on"' in drain(a)[0]
        drain(b)
        live.unsubscribe(b)
        live.handle(event("switch.fan", "off"))
        assert drain(b) == []
    asyncio.run(go())


def test_slow_client_dropped():
    async def go():
        live = make_live()
        c = live.subscribe()
        for i in range(200):
            live.handle(event("switch.fan", "on" if i % 2 else "off"))
        assert c not in live.clients and drain(c) == [None]
    asyncio.run(go())


def test_stream_snapshot_update_ping():
    async def go():
        live = make_live()
        c = live.subscribe()
        gen = live.stream(c, live.device_list(), ping_every=0.01)
        assert (await gen.__anext__()).startswith("event: snapshot\n")
        assert await gen.__anext__() == sse("status", {"ws": False})
        assert await gen.__anext__() == ": ping\n\n"
        live.handle(event("switch.fan", "on"))
        assert (await gen.__anext__()).startswith("event: device\n")
        await gen.aclose()
        assert c not in live.clients
    asyncio.run(go())


class FakeLive(Live):
    def start(self):
        pass

    def subscribe(self):
        c = super().subscribe()
        c.queue.put_nowait(sse("device", {"entity_id": "switch.fan", "state": "on"}))
        c.queue.put_nowait(None)  # end the stream so the test client returns
        return c


def test_events_endpoint(tmp_path, fake_ha):
    settings = Settings("http://ha.test", "test-token", "aaron", "s3cret", str(tmp_path / "l.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(fake_ha.handler))
    live = FakeLive(ha, "ws://ha.test/api/websocket", "test-token", use_ws=False)
    with TestClient(create_app(settings, ha, live)) as client:
        client.headers["Authorization"] = "Basic " + base64.b64encode(b"aaron:s3cret").decode()
        r = client.get("/api/events")
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
        blocks = r.text.strip().split("\n\n")
        assert blocks[0].startswith("event: snapshot\n") and '"power":12.4' in blocks[0]
        assert blocks[1] == 'event: status\ndata: {"ws":false}'
        assert blocks[2] == 'event: device\ndata: {"entity_id":"switch.fan","state":"on"}'
        assert live.clients == set()
        # /api/devices is now served from the fresh state map without another HA round trip
        n = sum(1 for c in fake_ha.calls if c[1] == "/api/states")
        devs = {d["entity_id"]: d for d in client.get("/api/devices").json()}
        assert sum(1 for c in fake_ha.calls if c[1] == "/api/states") == n
        assert devs["switch.fan"]["power"] == 12.4 and devs["binary_sensor.contact_sensor_door"]["battery_low"] is True
