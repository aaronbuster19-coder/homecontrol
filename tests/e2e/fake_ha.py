"""Tiny fake Home Assistant for the browser tests (REST, template, services, websocket). Token: "tok"."""
import json
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse, JSONResponse

TEMPLATE = """light|light.kitchen|Kitchen light|TP-Link|L530
light|light.lounge|Lounge lamp|TP-Link|L530
switch|switch.tv|TV|TP-Link|P110
climate|climate.lounge_valve|Lounge valve|TP-Link|KE100
binary|binary_sensor.contact_sensor_door|Front door|TP-Link|T110
"""
STATES = [
    {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
    {"entity_id": "light.lounge", "state": "off", "attributes": {}},
    {"entity_id": "switch.tv", "state": "off", "attributes": {}},
    {"entity_id": "climate.lounge_valve", "state": "heat", "attributes": {"current_temperature": 20, "temperature": 21}},
    {"entity_id": "binary_sensor.contact_sensor_door", "state": "off", "attributes": {}},
]
app = FastAPI()

@app.get("/api/states")
def states():
    return STATES

@app.post("/api/template")
def template():
    return PlainTextResponse(TEMPLATE)

@app.post("/api/services/{d}/{s}")
def service(d: str, s: str):
    return []

@app.websocket("/api/websocket")
async def ws(w: WebSocket):
    await w.accept()
    await w.send_text(json.dumps({"type": "auth_required"}))
    m = json.loads(await w.receive_text())
    await w.send_text(json.dumps({"type": "auth_ok" if m.get("access_token") == "tok" else "auth_invalid"}))
    m = json.loads(await w.receive_text())
    await w.send_text(json.dumps({"id": m["id"], "type": "result", "success": True}))
    try:
        while True:
            await w.receive_text()
    except Exception:
        pass
