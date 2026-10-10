"""A fake Home Assistant for the browser tests. Token: "test-token". Never talks to a real HA.

    python e2e/fake_ha.py PORT [HOST]

HA API: GET /api/states, POST /api/template (discovery lines), POST /api/services/<domain>/<service> (applied to the
states and broadcast), POST /api/services/weather/get_forecasts?return_response (hourly / daily for weather.forecast_home),
GET /api/history/period/<start> (generated, deterministic history, plus the real changes made since the last reset),
websocket /api/websocket (token auth, subscribe_events -> state_changed events).
Test controls: GET /fake/calls (service call log), POST /fake/reset (calls + states + forecast) — also as /_calls,
/_reset — POST /fake/set {"entity_id", "state"?, "attributes"?} (change a state and push it like a wall switch would;
also as /_state), POST /fake/forecast {"cold": true} (tonight drops to 1°) or {"error": 500} (get_forecasts fails)
and GET /fake/forecast_calls (get_forecasts requests, kept out of /fake/calls). POST /fake/fail {"service": "turn_on",
"count": n, "status": 500} makes the next n calls of that service fail (logged in /fake/calls as "failed": true).
Fake Octopus Energy API (the app's OCTOPUS_API points here in the browser tests): GET /octopus/v1/products/<product>/
electricity-tariffs/<code>/standard-unit-rates/ — deterministic half-hourly prices for any period (7.5p 01:00–04:00,
35p 16:00–19:00 London time, 18–24p otherwise); products other than AGILE-*/GO-* answer 404. POST /fake/octopus
{"fail": true} makes it answer 503, {"tomorrow": false} withholds the last day of the requested period; GET
/fake/octopus_calls lists the requests (kept out of /fake/calls).
With FAKE_HA_TV=1 a Samsung TV (media_player, with a SmartThings duplicate on the same device), a speaker, and
GET /api/media_player_proxy/<entity> (the TV's artwork, a PNG) exist (e2e/test_tv.py).
"""
import struct
import json
import math
import os
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
        # HA's sun (presence lighting: below_horizon = dark); the tests set the state they need
        s("sun.sun", "above_horizon", next_setting="2026-10-09T17:24:00+00:00", next_rising="2026-10-10T06:13:00+00:00",
          friendly_name="Sun"),
        # outdoor weather: the Met.no entity HA creates by default (forecasts via weather.get_forecasts)
        s("weather.forecast_home", "partlycloudy", temperature=12.4, apparent_temperature=10.2, humidity=71, wind_speed=16.1,
          wind_bearing=225, temperature_unit="°C", wind_speed_unit="km/h", pressure=1012, friendly_name="Forecast Home"),
    ]}


# FAKE_HA_APPLIANCES=1 (the appliance tests' own stack): six more plugs with power sensors, for linking appliances.
APPLIANCE_PLUGS = os.environ.get("FAKE_HA_APPLIANCES") == "1"
if APPLIANCE_PLUGS:
    TEMPLATE += """switch|switch.washer|Washing machine|TP-Link|P110
rel|switch.washer|sensor.washer_power|power|W|measurement|Washing machine Current consumption
rel|switch.washer|sensor.washer_today|energy|kWh|total_increasing|Washing machine Today's consumption
switch|switch.fridge|Fridge plug|TP-Link|P110
rel|switch.fridge|sensor.fridge_power|power|W|measurement|Fridge plug Current consumption
switch|switch.plug_3|Plug 3|TP-Link|TP11
rel|switch.plug_3|sensor.plug_3_power|power|W|measurement|Plug 3 Current consumption
switch|switch.hoover|Hoover plug|TP-Link|P110
rel|switch.hoover|sensor.hoover_power|power|W|measurement|Hoover plug Current consumption
switch|switch.pc|PC plug|TP-Link|P110
rel|switch.pc|sensor.pc_power|power|W|measurement|PC plug Current consumption
switch|switch.server|Server plug|TP-Link|P110
rel|switch.server|sensor.server_power|power|W|measurement|Server plug Current consumption
"""
    _base_states = initial_states

    def initial_states():
        out = _base_states()
        for eid, state, w in (("washer", "on", "0.8"), ("fridge", "on", "2.1"), ("plug_3", "off", "0"),
                              ("hoover", "on", "0.5"), ("pc", "off", "0"), ("server", "on", "35")):
            out[f"switch.{eid}"] = {"entity_id": f"switch.{eid}", "state": state, "attributes": {},
                                    "last_changed": "2026-10-09T10:00:00+00:00", "last_updated": "2026-10-09T10:00:00+00:00"}
            out[f"sensor.{eid}_power"] = {"entity_id": f"sensor.{eid}_power", "state": w,
                                          "attributes": {"unit_of_measurement": "W", "device_class": "power"},
                                          "last_changed": "2026-10-09T10:00:00+00:00", "last_updated": "2026-10-09T10:00:00+00:00"}
        return out


# FAKE_HA_TV=1 (the TV tests' own stack): a Samsung TV as both HA integrations show it, and a speaker.
TV_FEATURES = 1 | 4 | 8 | 16 | 32 | 128 | 256 | 1024 | 2048 | 16384 | 65536  # samsungtv + Wake-on-LAN + UPnP volume
MEDIA = os.environ.get("FAKE_HA_TV") == "1"
if MEDIA:
    TEMPLATE += f"""media|media_player.samsung_tv|Samsung TV|Samsung|QE55Q80A
dc|media_player.samsung_tv|tv
sf|media_player.samsung_tv|{TV_FEATURES}
media|media_player.samsung_tv_2|Samsung TV|Samsung|QE55Q80A
dc|media_player.samsung_tv_2|tv
sf|media_player.samsung_tv_2|{8 | 256 | 1024 | 2048}
switch|switch.samsung_tv|Samsung TV|Samsung|QE55Q80A
media|media_player.kitchen_speaker|Kitchen speaker|Sonos|One
dc|media_player.kitchen_speaker|speaker
sf|media_player.kitchen_speaker|{1 | 4 | 8 | 16 | 32 | 16384}
"""
    _pre_media_states = initial_states

    def initial_states():
        out = _pre_media_states()
        for eid, state, attrs in (
            ("media_player.samsung_tv", "on", {
                "device_class": "tv", "supported_features": TV_FEATURES, "friendly_name": "Samsung TV", "volume_level": 0.24,
                "is_volume_muted": False, "source": "TV", "source_list": ["TV", "HDMI1", "HDMI2", "Netflix", "YouTube", "Disney+"],
                "app_name": "Netflix", "media_title": "The Crown", "media_series_title": "Season 2", "media_content_type": "tvshow",
                "entity_picture": "/api/media_player_proxy/media_player.samsung_tv?token=fakeproxytoken&cache=1",
                "sound_mode": "Standard", "sound_mode_list": ["Standard", "Movie", "Music", "Amplify"]}),
            ("media_player.samsung_tv_2", "on", {"supported_features": 8 | 256 | 1024 | 2048}),
            ("switch.samsung_tv", "on", {}),
            ("media_player.kitchen_speaker", "idle", {"device_class": "speaker", "supported_features": 1 | 4 | 8 | 16 | 32 | 16384,
                                                      "volume_level": 0.3, "is_volume_muted": False}),
        ):
            out[eid] = {"entity_id": eid, "state": state, "attributes": attrs,
                        "last_changed": "2026-10-09T10:00:00+00:00", "last_updated": "2026-10-09T10:00:00+00:00"}
        return out


def _png(w: int = 96, h: int = 96) -> bytes:
    """Artwork for the fake TV: a small purple-to-red gradient PNG."""
    rows = b"".join(b"\0" + bytes(c for x in range(w) for c in (90 + x, 40 + y // 2, 200 - x)) for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


app = FastAPI()
app.state.states = initial_states()
app.state.calls = []
app.state.sockets = set()
app.state.changes = {}   # entity id -> [(before, after)] state dicts: real changes, merged into the history
app.state.forecast = {}  # {"cold": bool, "error": status}
app.state.forecast_calls = []
app.state.octopus, app.state.octopus_calls = {}, []  # fake Octopus settings, requests
app.state.fail = {}  # service -> {"count": n, "status": code}: the next n calls of it fail (disco error tests)


def authorized(request: Request) -> bool:
    return request.headers.get("authorization") == f"Bearer {TOKEN}"


@app.middleware("http")
async def check_token(request: Request, call_next):
    if request.url.path.startswith("/api/") and not authorized(request):
        return JSONResponse({"message": "Unauthorized"}, 401)
    return await call_next(request)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def changed(eid: str, before: dict) -> None:
    """Remember a real state change for /api/history/period (the recorder)."""
    s = app.state.states[eid]
    if (before["state"], before["attributes"]) != (s["state"], s["attributes"]):
        app.state.changes.setdefault(eid, []).append((before, json.loads(json.dumps(s))))


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


def forecast(kind: str) -> list[dict]:
    """Hourly: 36 h from this hour; daily: 6 days from today. {"cold": true} brings tonight down to 1°."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    cold = app.state.forecast.get("cold")
    if kind == "hourly":
        out = []
        for i in range(36):
            t = now + timedelta(hours=i)
            h = t.astimezone(LONDON).hour
            night = h >= 20 or h < 7
            temp = (1.0 if cold else 7.0) + (0 if night else 6 + 4 * math.sin((h - 9) / 10 * math.pi))
            out.append({"datetime": t.isoformat(), "condition": "clear-night" if night else ["sunny", "partlycloudy", "rainy"][i % 3],
                        "temperature": round(temp, 1), "precipitation": 0.4 if i % 3 == 2 else 0.0,
                        "precipitation_probability": 60 if i % 3 == 2 else 10, "wind_speed": 14.0, "wind_bearing": 220})
        return out
    day0 = datetime.now(LONDON).replace(hour=12, minute=0, second=0, microsecond=0)
    conds = ["partlycloudy", "rainy", "sunny", "cloudy", "snowy", "fog"]
    return [{"datetime": (day0 + timedelta(days=i)).astimezone(timezone.utc).isoformat(), "condition": conds[i],
             "temperature": 13.0 - i, "templow": (1.0 if cold and i == 1 else 6.0 - i / 2), "precipitation": 0.0}
            for i in range(6)]


@app.post("/api/services/weather/get_forecasts")
async def get_forecasts(request: Request):
    body = await request.json()
    # logged apart from /fake/calls: the browser fetches the weather on its own schedule
    app.state.forecast_calls.append({"data": body, "query": str(request.query_params), "t": time.time()})
    if "return_response" not in request.query_params:
        return JSONResponse({"message": "Service call requires responses but caller did not ask for responses"}, 400)
    if app.state.forecast.get("error"):
        return JSONResponse({"message": "error"}, app.state.forecast["error"])
    eid = body.get("entity_id")
    if eid not in app.state.states or body.get("type") not in ("hourly", "daily"):
        return JSONResponse({"message": "not supported"}, 400)
    return {"changed_states": [], "service_response": {eid: {"forecast": forecast(body["type"])}}}


@app.post("/api/services/{domain}/{service}")
async def service(domain: str, service: str, request: Request):
    body = await request.json()
    f = app.state.fail.get(service)
    if f and f["count"] > 0:
        f["count"] -= 1
        app.state.calls.append({"domain": domain, "service": service, "data": body, "t": time.time(), "failed": True})
        return JSONResponse({"message": "fake failure"}, f.get("status", 500))
    app.state.calls.append({"domain": domain, "service": service, "data": body, "t": time.time()})
    ids = body.get("entity_id", [])
    for eid in [ids] if isinstance(ids, str) else ids:
        s = app.state.states.get(eid)
        if not s:
            continue
        before = json.loads(json.dumps(s))
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
        elif service == "volume_set":
            a["volume_level"] = body.get("volume_level")
        elif service in ("volume_up", "volume_down"):
            a["volume_level"] = round(min(1, max(0, (a.get("volume_level") or 0) + (0.01 if service == "volume_up" else -0.01))), 2)
        elif service == "volume_mute":
            a["is_volume_muted"] = body.get("is_volume_muted")
        elif service == "select_source":
            a["source"] = body.get("source")
            a["app_name"] = body["source"] if body.get("source") in ("Netflix", "YouTube", "Disney+") else None
            for k in ("media_title", "media_series_title", "entity_picture"):
                a.pop(k, None)
        elif service == "select_sound_mode":
            a["sound_mode"] = body.get("sound_mode")
        elif service == "media_play_pause":
            s["state"] = "paused" if s["state"] == "playing" else "playing"
        s["last_changed"] = s["last_updated"] = now_iso()
        changed(eid, before)
        await broadcast(eid)
    return []


# ---------- generated history ----------
# Plug power for the energy tests: TV 86.4 W 18:00–23:00 local, 4 W standby otherwise; kettle 2000 W 08:00–08:15.
LONDON = ZoneInfo("Europe/London")
POWER = {"sensor.tv_power": lambda h: 86.4 if 18 <= h < 23 else 4.0,
         "sensor.kettle_power": lambda h: 2000.0 if h == 8 else 0.0}
if APPLIANCE_PLUGS:  # a 2 h wash every day (09:00 2 kW, 10:00 500 W), a fridge compressor every third hour, a fan 13–16
    POWER.update({"sensor.washer_power": lambda h: 2000.0 if h == 9 else 500.0 if h == 10 else 1.0,
                  "sensor.fridge_power": lambda h: 80.0 if h % 3 == 0 else 2.0,
                  "sensor.plug_3_power": lambda h: 35.0 if 13 <= h < 16 else 0.0,
                  "sensor.hoover_power": lambda h: 45.0 if h == 19 else 0.5,  # one 1 h charge a day
                  "sensor.pc_power": lambda h: 120.0 if 9 <= h < 17 else 4.0,
                  "sensor.server_power": lambda h: 35.0})


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
    quiet = end - timedelta(minutes=5)  # no generated switching in the last minutes: a test's own change is the newest
    if eid.startswith(("light.", "switch.")):
        on = False
        while t < quiet:
            row(t, "on" if on else "off")
            t += timedelta(minutes=rnd.randint(20, 40) if on else rnd.randint(60, 200))
            on = not on
    elif eid.startswith("binary_sensor."):
        while t < quiet:
            row(t, "off")
            t += timedelta(minutes=rnd.randint(50, 240))
            if t >= quiet:
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


def with_changes(eid: str, rows: list[dict], end: datetime, with_attrs: bool) -> list[dict]:
    """The generated rows up to the first real change (and the real state a minute before it), then the real changes."""
    real = [(b, a) for b, a in app.state.changes.get(eid, []) if datetime.fromisoformat(a["last_changed"]) <= end]
    if not real:
        return rows
    first = datetime.fromisoformat(real[0][1]["last_changed"])
    keep = [r for r in rows if datetime.fromisoformat(r["last_changed"]) < first - timedelta(seconds=61)]
    before = {**real[0][0], "last_changed": (first - timedelta(seconds=60)).isoformat()}
    before["last_updated"] = before["last_changed"]
    out = keep + [before] + [a for _, a in real]
    return [{**r, "entity_id": eid, "attributes": r["attributes"] if with_attrs else {}} for r in out]


@app.get("/api/history/period/{start}")
async def history(start: str, request: Request):
    q = request.query_params
    t0 = datetime.fromisoformat(start)
    t1 = datetime.fromisoformat(q["end_time"]) if q.get("end_time") else datetime.now(timezone.utc)
    minimal = "minimal_response" in q
    out = []
    for eid in filter(None, q.get("filter_entity_id", "").split(",")):
        rows = with_changes(eid, _rows(eid, t0, t1, "no_attributes" not in q), t1, "no_attributes" not in q)
        rows = [r for r in rows if datetime.fromisoformat(r["last_changed"]) >= t0 - timedelta(seconds=1)] or rows[-1:]
        if minimal:  # like HA: only the first row carries entity_id and attributes
            rows = rows[:1] + [{"state": r["state"], "last_changed": r["last_changed"]} for r in rows[1:]]
        if rows:
            out.append(rows)
    return out


# ---------- fake Octopus Energy API ----------
def octo_price(t: datetime) -> float:
    loc = t.astimezone(LONDON)
    if 1 <= loc.hour < 4:
        return 7.5
    if 16 <= loc.hour < 19:
        return 35.0
    return round(21 + 3 * math.sin(loc.hour / 24 * 2 * math.pi), 2)


@app.get("/octopus/v1/products/{product}/electricity-tariffs/{code}/standard-unit-rates/")
async def octopus_rates(product: str, code: str, request: Request):
    q = request.query_params
    app.state.octopus_calls.append({"product": product, "code": code, **dict(q)})
    if app.state.octopus.get("fail"):
        return JSONResponse({"detail": "Service unavailable"}, 503)
    if not product.startswith(("AGILE-", "GO-")) or not code.startswith(f"E-1R-{product}-"):
        return JSONResponse({"detail": "No EnergyTariff matches the given query."}, 404)
    t0 = datetime.fromisoformat(q["period_from"].replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(q["period_to"].replace("Z", "+00:00"))
    if app.state.octopus.get("tomorrow") is False:  # the last local day isn't published yet
        last = (t1 - timedelta(seconds=1)).astimezone(LONDON).date()
        t1 = datetime.combine(last, datetime.min.time(), tzinfo=LONDON).astimezone(timezone.utc)
    out, t = [], t0
    while t < t1:
        out.append({"value_exc_vat": round(octo_price(t) / 1.05, 4), "value_inc_vat": octo_price(t),
                    "valid_from": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "valid_to": (t + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), "payment_method": None})
        t += timedelta(minutes=30)
    return {"count": len(out), "next": None, "previous": None, "results": out[::-1]}


@app.get("/api/media_player_proxy/{entity_id}")
async def media_proxy(entity_id: str):
    from fastapi.responses import Response
    return Response(_png(), media_type="image/png")


# ---------- test controls ----------
@app.get("/_calls")  # old name, kept for copies of the earlier wall-test fixtures
@app.get("/fake/calls")
async def calls():
    return app.state.calls


@app.post("/_reset")
@app.post("/fake/reset")
async def reset():
    app.state.calls.clear()
    app.state.changes.clear()
    app.state.forecast = {}
    app.state.fail = {}
    app.state.octopus = {}
    app.state.octopus_calls.clear()
    app.state.states = initial_states()
    for eid in app.state.states:
        await broadcast(eid)
    return {}


@app.post("/_state")  # old name used by the dehumidifier tests
@app.post("/fake/set")
async def set_state(request: Request):
    body = await request.json()
    s = app.state.states[body["entity_id"]]
    before = json.loads(json.dumps(s))
    if "state" in body:
        s["state"] = str(body["state"])
    s["attributes"].update(body.get("attributes") or {})
    s["last_changed"] = s["last_updated"] = now_iso()
    changed(body["entity_id"], before)
    await broadcast(body["entity_id"])
    return s


@app.post("/fake/fail")
async def set_fail(request: Request):
    body = await request.json()
    app.state.fail[body["service"]] = {"count": int(body.get("count", 1)), "status": int(body.get("status", 500))}
    return app.state.fail


@app.post("/fake/octopus")
async def set_octopus(request: Request):
    app.state.octopus = await request.json()
    return app.state.octopus


@app.get("/fake/octopus_calls")
async def octopus_calls():
    return app.state.octopus_calls


@app.get("/fake/forecast_calls")
async def forecast_calls():
    return app.state.forecast_calls


@app.post("/fake/forecast")
async def set_forecast(request: Request):
    app.state.forecast = await request.json()
    return app.state.forecast


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
    # Keep-alive far longer than any test, so it never closes an idle connection the app is about to reuse (conftest.py).
    uvicorn.run(app, host=sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1", port=int(sys.argv[1]), log_level="warning",
                timeout_keep_alive=600)
