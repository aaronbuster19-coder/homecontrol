"""A fake Home Assistant for the browser tests. Token: "test-token". Never talks to a real HA.

    python e2e/fake_ha.py PORT [HOST]

HA API: GET /api/states, POST /api/template (discovery lines), POST /api/services/<domain>/<service> (applied to the
states and broadcast), GET /api/history/period/<start> (generated, deterministic history), websocket /api/websocket
(token auth, subscribe_events -> state_changed events).
Test controls: GET /fake/calls (service call log), POST /fake/reset (calls + states) — also as /_calls, /_reset —
and POST /fake/set {"entity_id", "state"?, "attributes"?} (change a state and push it like a wall switch would;
also as /_state).
"""
import json
import math
import random
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

TOKEN = "test-token"
# domain|entity_id|device name|manufacturer|model, then rel|primary|entity|device_class|unit|state_class|name
TEMPLATE = """light|light.lounge|Lounge lamp|TP-Link|L530
light|light.kitchen|Kitchen|TP-Link|L630
light|light.bedroom|Bedroom|TP-Link|L530
light|light.strip|TV strip|TP-Link|L430C
switch|switch.kettle|Kettle|TP-Link|P110
rel|switch.kettle|sensor.kettle_power|power|W|measurement|Kettle Current consumption
rel|switch.kettle|sensor.kettle_today|energy|kWh|total_increasing|Kettle Today's consumption
switch|switch.kettle_led|Kettle|TP-Link|P110
switch|switch.tv|TV|TP-Link|P110
rel|switch.tv|sensor.tv_power|power|W|measurement|TV Current consumption
rel|switch.tv|sensor.tv_today|energy|kWh|total_increasing|TV Today's consumption
climate|climate.lounge_valve|Lounge radiator|TP-Link|KE100
rel|climate.lounge_valve|sensor.lounge_valve_battery|battery|%|measurement|Lounge radiator Battery
climate|climate.bedroom_valve|Bedroom radiator|TP-Link|KE100
binary|binary_sensor.contact_sensor_door|Front door|TP-Link|T110
rel|binary_sensor.contact_sensor_door|sensor.front_door_battery|battery|%|measurement|Front door Battery
binary|binary_sensor.contact_sensor_door_2|Bedroom window|TP-Link|T110
binary|binary_sensor.contact_sensor_door_cloud_connection|Front door|TP-Link|T110
light|light.other_brand|Other lamp|Philips|LCT015
humidifier|humidifier.dehumidifier|Dehumidifier|Tuya|CS-20L
dc|humidifier.dehumidifier|dehumidifier
rel|humidifier.dehumidifier|sensor.dehumidifier_humidity|humidity|%|measurement|Dehumidifier Humidity
rel|humidifier.dehumidifier|sensor.dehumidifier_temperature|temperature|°C|measurement|Dehumidifier Temperature
rel|humidifier.dehumidifier|binary_sensor.dehumidifier_tank_full|problem|||Dehumidifier Tank full
switch|switch.dehumidifier_child_lock|Dehumidifier|Tuya|CS-20L
person|person.alex|Alex||
person|person.sam|Sam||
"""


def initial_states():
    def s(eid, state, **attrs):
        return {"entity_id": eid, "state": state, "attributes": attrs,
                "last_changed": "2026-10-09T10:00:00+00:00", "last_updated": "2026-10-09T10:00:00+00:00"}
    return {x["entity_id"]: x for x in [
        s("light.lounge", "on", brightness=200, supported_color_modes=["hs", "color_temp"], color_mode="color_temp",
          color_temp_kelvin=2700, min_color_temp_kelvin=2500, max_color_temp_kelvin=6500),
        s("light.kitchen", "off", supported_color_modes=["brightness"]),
        s("light.bedroom", "off", supported_color_modes=["brightness"]),
        s("light.strip", "on", brightness=120, supported_color_modes=["hs"], color_mode="hs", hs_color=[275, 90]),
        s("light.other_brand", "on", supported_color_modes=["onoff"]),
        s("switch.kettle", "off"), s("sensor.kettle_power", "0", unit_of_measurement="W", device_class="power"),
        s("sensor.kettle_today", "0.42", unit_of_measurement="kWh", device_class="energy"),
        s("switch.kettle_led", "on"),
        s("switch.tv", "on"), s("sensor.tv_power", "86.4", unit_of_measurement="W", device_class="power"),
        s("sensor.tv_today", "0.42", unit_of_measurement="kWh", device_class="energy"),
        s("climate.lounge_valve", "heat", current_temperature=20.5, temperature=21, min_temp=5, max_temp=30),
        s("sensor.lounge_valve_battery", "80", unit_of_measurement="%", device_class="battery"),
        s("climate.bedroom_valve", "heat", current_temperature=18.0, temperature=19, min_temp=5, max_temp=30),
        s("binary_sensor.contact_sensor_door", "off", device_class="door"),
        s("sensor.front_door_battery", "15", unit_of_measurement="%", device_class="battery"),
        s("binary_sensor.contact_sensor_door_2", "off", device_class="window"),
        s("binary_sensor.contact_sensor_door_cloud_connection", "on"),
        s("humidifier.dehumidifier", "on", device_class="dehumidifier", humidity=50, current_humidity=62, min_humidity=30,
          max_humidity=80, mode="auto", available_modes=["auto", "continuous", "sleep"], action="drying"),
        s("sensor.dehumidifier_humidity", "62", unit_of_measurement="%"),
        s("sensor.dehumidifier_temperature", "21.5", unit_of_measurement="°C"),
        s("binary_sensor.dehumidifier_tank_full", "off"),
        s("switch.dehumidifier_child_lock", "off"),
        # presence for Auto Away (HA person entities; the companion app's zone: home / not_home / a zone name)
        s("person.alex", "home", friendly_name="Alex", source="device_tracker.alex_phone"),
        s("person.sam", "home", friendly_name="Sam", source="device_tracker.sam_phone"),
    ]}


app = FastAPI()
app.state.states = initial_states()
app.state.calls = []
app.state.sockets = set()


def authorized(request: Request) -> bool:
    return request.headers.get("authorization") == f"Bearer {TOKEN}"


@app.middleware("http")
async def check_token(request: Request, call_next):
    if request.url.path.startswith("/api/") and not authorized(request):
        return JSONResponse({"message": "Unauthorized"}, 401)
    return await call_next(request)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def broadcast(eid):
    msg = json.dumps({"id": 1, "type": "event", "event": {"event_type": "state_changed",
                      "data": {"entity_id": eid, "new_state": app.state.states[eid]}}})
    for ws in list(app.state.sockets):
        try:
            await ws.send_text(msg)
        except Exception:
            app.state.sockets.discard(ws)


@app.get("/api/states")
async def states():
    return list(app.state.states.values())


@app.post("/api/template")
async def template():
    return PlainTextResponse(TEMPLATE)


@app.post("/api/services/{domain}/{service}")
async def service(domain: str, service: str, request: Request):
    body = await request.json()
    app.state.calls.append({"domain": domain, "service": service, "data": body, "t": time.time()})
    ids = body.get("entity_id", [])
    for eid in [ids] if isinstance(ids, str) else ids:
        s = app.state.states.get(eid)
        if not s:
            continue
        a = s["attributes"]
        if service in ("turn_on", "turn_off", "toggle"):
            on = service == "turn_on" or (service == "toggle" and s["state"] != "on")
            s["state"] = "on" if on else "off"
            if "brightness_pct" in body:
                a["brightness"] = round(body["brightness_pct"] * 2.55)
            for k in ("hs_color", "rgb_color", "color_temp_kelvin"):
                if k in body:
                    a[k] = body[k]
                    a["color_mode"] = {"hs_color": "hs", "rgb_color": "rgb", "color_temp_kelvin": "color_temp"}[k]
        elif service == "set_temperature":
            a["temperature"] = body.get("temperature")
        elif service == "set_humidity":
            a["humidity"] = body.get("humidity")
        elif service == "set_mode":
            a["mode"] = body.get("mode")
        s["last_changed"] = s["last_updated"] = now_iso()
        await broadcast(eid)
    return []


# ---------- generated history ----------
# Plug power for the energy tests: TV 86.4 W 18:00–23:00 local, 4 W standby otherwise; kettle 2000 W 08:00–08:15.
LONDON = ZoneInfo("Europe/London")
POWER = {"sensor.tv_power": lambda h: 86.4 if 18 <= h < 23 else 4.0,
         "sensor.kettle_power": lambda h: 2000.0 if h == 8 else 0.0}


def _rows(eid: str, start: datetime, end: datetime, with_attrs: bool) -> list[dict]:
    """Deterministic per entity: on/off runs for lights/plugs, open/close for doors, W for power sensors,
    current/target temperatures for valves. The last row is the current state."""
    rnd = random.Random(zlib.crc32(eid.encode()))
    cur = app.state.states.get(eid)
    out = []

    def row(t, state, attrs=None):
        out.append({"entity_id": eid, "state": str(state), "attributes": attrs or {},
                    "last_changed": t.isoformat(), "last_updated": t.isoformat()})

    t = start
    if eid in POWER:  # energy tests: a fixed daily profile in 15-minute steps
        t = start - timedelta(seconds=start.timestamp() % 900)
        while t < end:
            loc = t.astimezone(LONDON)
            v = POWER[eid](loc.hour) if eid != "sensor.kettle_power" or loc.minute < 15 else 0.0
            row(max(t, start), v)
            t += timedelta(minutes=15)
        return out if with_attrs else [{**r, "attributes": {}} for r in out]
    if eid == "humidifier.dehumidifier":  # drying 6 h, off 2 h, back on for the last 4 h
        for h_ago, state, cur_h, target in ((24, "on", 68, 50), (18, "on", 58, 50), (12, "off", 52, 50), (10, "on", 60, 45), (4, "on", 62, 50)):
            tt = end - timedelta(hours=h_ago)
            if tt >= start:
                row(tt, state, {"humidity": target, "current_humidity": cur_h})
        return out if with_attrs else [{**r, "attributes": {}} for r in out]
    if eid.startswith(("light.", "switch.")):
        on = False
        while t < end:
            row(t, "on" if on else "off")
            t += timedelta(minutes=rnd.randint(20, 40) if on else rnd.randint(60, 200))
            on = not on
    elif eid.startswith("binary_sensor."):
        while t < end:
            row(t, "off")
            t += timedelta(minutes=rnd.randint(50, 240))
            if t >= end:
                break
            row(t, "on")
            t += timedelta(minutes=rnd.randint(1, 25))
    elif eid.endswith("_power"):
        base = 80 if "tv" in eid else 0
        busy = False
        while t < end:
            if rnd.random() < 0.15:
                busy = not busy
            v = (base + rnd.uniform(-8, 8) if busy else 0) if base else (2000 + rnd.uniform(-50, 50) if busy and rnd.random() < 0.5 else 0)
            row(t, round(v, 1))
            t += timedelta(minutes=10)
    elif eid.startswith("climate."):
        target = (cur or {}).get("attributes", {}).get("temperature", 20)
        while t < end:
            night = t.hour < 7 or t.hour >= 23
            tg = target - 4 if night else target
            ct = round(tg - 1 + math.sin(t.timestamp() / 5400) + rnd.uniform(-0.3, 0.3), 1)
            row(t, "heat", {"current_temperature": ct, "temperature": tg})
            t += timedelta(minutes=30)
    elif cur:
        row(t, cur["state"])
    if cur and out:
        row(end - timedelta(seconds=30), cur["state"], cur["attributes"] if eid.startswith("climate.") else None)
    if not with_attrs:
        for r in out:
            r["attributes"] = {}
    return out


@app.get("/api/history/period/{start}")
async def history(start: str, request: Request):
    q = request.query_params
    t0 = datetime.fromisoformat(start)
    t1 = datetime.fromisoformat(q["end_time"]) if q.get("end_time") else datetime.now(timezone.utc)
    minimal = "minimal_response" in q
    out = []
    for eid in filter(None, q.get("filter_entity_id", "").split(",")):
        rows = _rows(eid, t0, t1, "no_attributes" not in q)
        if minimal:  # like HA: only the first row carries entity_id and attributes
            rows = rows[:1] + [{"state": r["state"], "last_changed": r["last_changed"]} for r in rows[1:]]
        if rows:
            out.append(rows)
    return out


# ---------- test controls ----------
@app.get("/_calls")  # old name, kept for copies of the earlier wall-test fixtures
@app.get("/fake/calls")
async def calls():
    return app.state.calls


@app.post("/_reset")
@app.post("/fake/reset")
async def reset():
    app.state.calls.clear()
    app.state.states = initial_states()
    for eid in app.state.states:
        await broadcast(eid)
    return {}


@app.post("/_state")  # old name used by the dehumidifier tests
@app.post("/fake/set")
async def set_state(request: Request):
    body = await request.json()
    s = app.state.states[body["entity_id"]]
    if "state" in body:
        s["state"] = str(body["state"])
    s["attributes"].update(body.get("attributes") or {})
    s["last_changed"] = s["last_updated"] = now_iso()
    await broadcast(body["entity_id"])
    return s


@app.websocket("/api/websocket")
async def websocket(ws: WebSocket):
    await ws.accept()
    await ws.send_text(json.dumps({"type": "auth_required", "ha_version": "2026.10.0"}))
    msg = json.loads(await ws.receive_text())
    if msg.get("type") != "auth" or msg.get("access_token") != TOKEN:
        await ws.send_text(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
        await ws.close()
        return
    await ws.send_text(json.dumps({"type": "auth_ok", "ha_version": "2026.10.0"}))
    try:
        while True:
            m = json.loads(await ws.receive_text())
            await ws.send_text(json.dumps({"id": m.get("id"), "type": "result", "success": True, "result": None}))
            if m.get("type") == "subscribe_events":
                app.state.sockets.add(ws)
    except WebSocketDisconnect:
        pass
    finally:
        app.state.sockets.discard(ws)


if __name__ == "__main__":
    uvicorn.run(app, host=sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1", port=int(sys.argv[1]), log_level="warning")
