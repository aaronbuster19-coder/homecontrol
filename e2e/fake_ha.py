"""A tiny fake Home Assistant for browser tests: REST states/template/services + websocket state_changed events.

Run: python e2e/fake_ha.py PORT   (token: test-token). GET /_calls lists service calls, POST /_reset clears them,
POST /_state {"entity_id", "state", "attributes"?} changes one entity (merging attributes) and pushes it.
"""
import asyncio
import json
import sys
import time
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

TOKEN = "test-token"
TEMPLATE = """light|light.lounge|Lounge lamp|TP-Link|L530
light|light.kitchen|Kitchen|TP-Link|L630
light|light.bedroom|Bedroom|TP-Link|L530
light|light.strip|TV strip|TP-Link|L430C
switch|switch.kettle|Kettle|TP-Link|P110
rel|switch.kettle|sensor.kettle_power|power|W|measurement|Kettle Current consumption
switch|switch.tv|TV|TP-Link|P110
rel|switch.tv|sensor.tv_power|power|W|measurement|TV Current consumption
climate|climate.lounge_valve|Lounge radiator|TP-Link|KE100
climate|climate.bedroom_valve|Bedroom radiator|TP-Link|KE100
binary|binary_sensor.contact_sensor_door|Front door|TP-Link|T110
humidifier|humidifier.dehumidifier|Dehumidifier|Tuya|CS-20L
dc|humidifier.dehumidifier|dehumidifier
rel|humidifier.dehumidifier|sensor.dehumidifier_humidity|humidity|%|measurement|Dehumidifier Humidity
rel|humidifier.dehumidifier|sensor.dehumidifier_temperature|temperature|°C|measurement|Dehumidifier Temperature
rel|humidifier.dehumidifier|binary_sensor.dehumidifier_tank_full|problem|||Dehumidifier Tank full
switch|switch.dehumidifier_child_lock|Dehumidifier|Tuya|CS-20L
"""


def initial_states():
    def s(eid, state, **attrs):
        return {"entity_id": eid, "state": state, "attributes": attrs, "last_changed": "2026-10-09T10:00:00+00:00"}
    return {x["entity_id"]: x for x in [
        s("light.lounge", "on", brightness=200, supported_color_modes=["hs", "color_temp"], color_mode="color_temp",
          color_temp_kelvin=2700, min_color_temp_kelvin=2500, max_color_temp_kelvin=6500),
        s("light.kitchen", "off", supported_color_modes=["brightness"]),
        s("light.bedroom", "off", supported_color_modes=["brightness"]),
        s("light.strip", "on", brightness=120, supported_color_modes=["hs"], color_mode="hs", hs_color=[275, 90]),
        s("switch.kettle", "off"), s("sensor.kettle_power", "0", unit_of_measurement="W"),
        s("switch.tv", "on"), s("sensor.tv_power", "86.4", unit_of_measurement="W"),
        s("climate.lounge_valve", "heat", current_temperature=20.5, temperature=21, min_temp=5, max_temp=30),
        s("climate.bedroom_valve", "heat", current_temperature=18.0, temperature=19, min_temp=5, max_temp=30),
        s("binary_sensor.contact_sensor_door", "off"),
        s("humidifier.dehumidifier", "on", device_class="dehumidifier", humidity=50, current_humidity=62, min_humidity=30,
          max_humidity=80, mode="auto", available_modes=["auto", "continuous", "sleep"], action="drying"),
        s("sensor.dehumidifier_humidity", "62", unit_of_measurement="%"),
        s("sensor.dehumidifier_temperature", "21.5", unit_of_measurement="°C"),
        s("binary_sensor.dehumidifier_tank_full", "off"),
        s("switch.dehumidifier_child_lock", "off"),
    ]}


app = FastAPI()
app.state.states = initial_states()
app.state.calls = []
app.state.sockets = set()


def auth(request: Request):
    return request.headers.get("authorization") == f"Bearer {TOKEN}"


async def broadcast(eid):
    msg = json.dumps({"id": 1, "type": "event", "event": {"event_type": "state_changed",
                      "data": {"entity_id": eid, "new_state": app.state.states[eid]}}})
    for ws in list(app.state.sockets):
        try:
            await ws.send_text(msg)
        except Exception:
            app.state.sockets.discard(ws)


@app.get("/api/states")
async def states(request: Request):
    if not auth(request):
        return JSONResponse({"message": "unauthorized"}, 401)
    return list(app.state.states.values())


@app.post("/api/template")
async def template(request: Request):
    return PlainTextResponse(TEMPLATE)


@app.get("/api/history/period/{start}")
async def history(start: str, request: Request):
    if "humidifier.dehumidifier" not in request.query_params.get("filter_entity_id", ""):
        return []
    now = time.time()  # dehumidifier: drying for 6 h, idle-off for 2 h, back on for the last 4 h

    def at(h_ago, state, cur, target):
        t = datetime.fromtimestamp(now - h_ago * 3600, timezone.utc).isoformat()
        return {"entity_id": "humidifier.dehumidifier", "state": state, "last_changed": t, "last_updated": t,
                "attributes": {"humidity": target, "current_humidity": cur}}
    return [[at(24, "on", 68, 50), at(18, "on", 58, 50), at(12, "off", 52, 50), at(10, "on", 60, 45), at(4, "on", 62, 50)]]


@app.post("/api/services/{domain}/{service}")
async def service(domain: str, service: str, request: Request):
    body = await request.json()
    app.state.calls.append({"domain": domain, "service": service, "data": body, "t": time.time()})
    ids = body.get("entity_id", [])
    for eid in [ids] if isinstance(ids, str) else ids:
        s = app.state.states.get(eid)
        if not s:
            continue
        if service in ("turn_on", "turn_off", "toggle"):
            on = service == "turn_on" or (service == "toggle" and s["state"] != "on")
            s["state"] = "on" if on else "off"
        elif service == "set_temperature":
            s["attributes"]["temperature"] = body.get("temperature")
        elif service == "set_humidity":
            s["attributes"]["humidity"] = body.get("humidity")
        elif service == "set_mode":
            s["attributes"]["mode"] = body.get("mode")
        await broadcast(eid)
    return []


@app.get("/_calls")
async def calls():
    return app.state.calls


@app.post("/_state")
async def set_state(request: Request):
    body = await request.json()
    s = app.state.states[body["entity_id"]]
    s["state"] = body.get("state", s["state"])
    s["attributes"].update(body.get("attributes") or {})
    await broadcast(body["entity_id"])
    return s


@app.post("/_reset")
async def reset():
    app.state.calls.clear()
    app.state.states = initial_states()
    for eid in app.state.states:
        await broadcast(eid)
    return {}


@app.websocket("/api/websocket")
async def websocket(ws: WebSocket):
    await ws.accept()
    await ws.send_text(json.dumps({"type": "auth_required"}))
    msg = json.loads(await ws.receive_text())
    if msg.get("access_token") != TOKEN:
        await ws.send_text(json.dumps({"type": "auth_invalid"}))
        await ws.close()
        return
    await ws.send_text(json.dumps({"type": "auth_ok"}))
    app.state.sockets.add(ws)
    try:
        while True:
            m = json.loads(await ws.receive_text())
            await ws.send_text(json.dumps({"id": m.get("id"), "type": "result", "success": True, "result": None}))
    except WebSocketDisconnect:
        pass
    finally:
        app.state.sockets.discard(ws)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
