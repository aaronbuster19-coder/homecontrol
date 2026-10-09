import math
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .alerts import Alerts, AlertStore, Pusher, SettingsError, load_vapid, validate_settings, validate_subscription, webpush_sender
from .automations import Automations, AutoStore
from .auth import COOKIE, SESSION_TTL, AuthMiddleware, RateLimiter, Sessions, check_basic_auth, check_credentials, client_key, is_https, load_secret  # noqa: F401
from .config import Settings, load_settings
from .dehumidifier import DehumError, check_humidity, check_mode
from .discovery import Device, parse_template_output
from .ha import DISCOVERY_TEMPLATE, HAClient, HAError
from .history import DOOR_RANGES, History, RangeError, check_range
from .live import Live, build_device, ws_url
from .modes import ModeError, ModeStore, current_targets, now_iso, public, restore_groups, validate_mode_settings
from .store import LayoutError, LayoutStore, validate_layout

CACHE_TTL = 300
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


class TemperatureBody(BaseModel):
    temperature: float


class LightBody(BaseModel):
    model_config = {"extra": "forbid"}
    brightness_pct: float | None = None
    hs_color: list[float] | None = None
    rgb_color: list[int] | None = None
    color_temp_kelvin: int | None = None


class BulkBody(BaseModel):
    action: str
    entity_ids: list[str]


class ValvesBody(BaseModel):
    temperature: float
    entity_ids: list[str] | None = None


def light_data(b: LightBody) -> dict:
    data = {}
    if b.brightness_pct is not None:
        if not math.isfinite(b.brightness_pct) or not 1 <= b.brightness_pct <= 100:
            raise HTTPException(400, "brightness_pct must be 1–100")
        data["brightness_pct"] = round(b.brightness_pct)
    if b.hs_color is not None:
        if len(b.hs_color) != 2 or not all(math.isfinite(v) for v in b.hs_color) \
                or not (0 <= b.hs_color[0] <= 360 and 0 <= b.hs_color[1] <= 100):
            raise HTTPException(400, "hs_color must be [hue 0–360, saturation 0–100]")
        data["hs_color"] = [round(v, 1) for v in b.hs_color]
    if b.rgb_color is not None:
        if len(b.rgb_color) != 3 or not all(0 <= v <= 255 for v in b.rgb_color):
            raise HTTPException(400, "rgb_color must be three values 0–255")
        data["rgb_color"] = b.rgb_color
    if b.color_temp_kelvin is not None:
        if not 1000 <= b.color_temp_kelvin <= 12000:
            raise HTTPException(400, "color_temp_kelvin must be 1000–12000")
        data["color_temp_kelvin"] = b.color_temp_kelvin
    if sum(k in data for k in ("hs_color", "rgb_color", "color_temp_kelvin")) > 1:
        raise HTTPException(400, "give only one of hs_color, rgb_color, color_temp_kelvin")
    if not data:
        raise HTTPException(400, "nothing to set")
    return data


def on_off_groups(devs: dict[str, Device], entity_ids) -> dict[str, list[str]]:
    """Split lights/plugs (and dehumidifiers, for "All off") by HA domain; raises ValueError naming the first id that isn't one."""
    groups: dict[str, list[str]] = {"light": [], "switch": []}
    for eid in dict.fromkeys(entity_ids):
        d = devs.get(eid)
        if d is None or d.kind not in ("light", "plug", "dehumidifier"):
            raise ValueError(eid)
        groups.setdefault(eid.split(".", 1)[0], []).append(eid)
    return groups


async def call_groups(ha: HAClient, action: str, groups: dict[str, list[str]]) -> int:
    for domain, ids in groups.items():
        if ids:
            await ha.call_service(domain, action, {"entity_id": ids})
    return sum(map(len, groups.values()))


def check_temp(t: float) -> None:
    if not math.isfinite(t) or not 5 <= t <= 35:
        raise HTTPException(400, "temperature must be between 5 and 35")


def create_app(settings: Settings | None = None, ha: HAClient | None = None, live: Live | None = None,
               push_sender=None) -> FastAPI:
    settings = settings or load_settings()
    ha = ha or HAClient(settings.ha_url, settings.ha_token)
    store = LayoutStore(settings.db_path)
    cache: dict = {"at": 0.0, "devices": None}
    live = live or Live(ha, ws_url(settings.ha_url), settings.ha_token)
    vapid = load_vapid(settings.db_path)
    alert_store = AlertStore(settings.db_path)
    mode_store = ModeStore(settings.db_path)
    pusher = Pusher(alert_store, push_sender or webpush_sender(vapid))

    @asynccontextmanager
    async def lifespan(app):
        live.start()
        alerts.start()
        automations.start()
        yield
        await automations.stop()
        await alerts.stop()
        await live.stop()
        await ha.close()

    app = FastAPI(title="homecontrol", lifespan=lifespan)

    sessions = Sessions(load_secret(settings.db_path), settings.app_password)
    limiter = RateLimiter()
    app.add_middleware(AuthMiddleware, sessions=sessions, user=settings.app_user, password=settings.app_password, limiter=limiter)

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request):
        key = client_key(request.headers, request.client.host if request.client else None)
        if limiter.blocked(key):
            raise HTTPException(429, "too many attempts, try again later")
        if not check_credentials(body.username, body.password, settings.app_user, settings.app_password):
            limiter.fail(key)
            raise HTTPException(401, "wrong username or password")
        resp = JSONResponse({"user": settings.app_user})
        resp.set_cookie(COOKIE, sessions.issue(settings.app_user), max_age=SESSION_TTL, path="/",
                        httponly=True, samesite="lax", secure=is_https(request))
        return resp

    @app.post("/api/logout")
    async def logout(request: Request):
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=is_https(request))
        return resp

    @app.get("/api/me")
    async def me(request: Request):
        return {"user": request.state.user}

    @app.exception_handler(HAError)
    async def ha_error(request, exc: HAError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    async def devices() -> dict[str, Device]:
        if cache["devices"] is None or time.monotonic() - cache["at"] > CACHE_TTL:
            text = await ha.render_template(DISCOVERY_TEMPLATE)
            cache["devices"] = {d.entity_id: d for d in parse_template_output(text)}
            cache["at"] = time.monotonic()
            live.set_devices(cache["devices"])
        return cache["devices"]

    async def require(entity_id: str, kinds: tuple[str, ...]) -> Device:
        dev = (await devices()).get(entity_id)
        if dev is None:
            raise HTTPException(404, "unknown device")
        if dev.kind not in kinds:
            raise HTTPException(400, f"not supported for {dev.kind}")
        return dev

    alerts = Alerts(alert_store, pusher, live, devices)
    automations = Automations(AutoStore(settings.db_path), alert_store.settings, pusher, live, ha, store, mode_store.get, devices)
    app.state.automations = automations
    history = History(ha)

    @app.get("/api/history/{entity_id}")
    async def get_history(entity_id: str, range: str = "24h"):
        try:
            rng = check_range(range)
        except RangeError as e:
            raise HTTPException(400, str(e))
        dev = await require(entity_id, ("light", "plug", "valve", "sensor", "dehumidifier"))
        return await history.device(dev, rng, build_device(dev, live.states))

    @app.get("/api/doors/log")
    async def doors_log(range: str = "24h", tz: str | None = None):
        try:
            rng = check_range(range, DOOR_RANGES)
        except RangeError as e:
            raise HTTPException(400, str(e))
        return await history.doors([d for d in (await devices()).values() if d.kind == "sensor"], rng, tz)

    async def json_body(request: Request):
        try:
            return await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON")

    @app.get("/api/push/key")
    async def push_key():
        return {"publicKey": vapid.public_key}

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request):
        try:
            sub = validate_subscription(await json_body(request))
        except SettingsError as e:
            raise HTTPException(400, str(e))
        alert_store.add(sub)
        return {"ok": True}

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(request: Request):
        data = await json_body(request)
        ep = data.get("endpoint") if isinstance(data, dict) else None
        if not isinstance(ep, str):
            raise HTTPException(400, "endpoint required")
        return {"removed": alert_store.remove(ep)}

    @app.post("/api/push/test")
    async def push_test():
        return await pusher.notify({"title": "homecontrol", "body": "Test notification — alerts work on this device.",
                                    "tag": "test", "url": "/"})

    @app.get("/api/alerts/settings")
    async def get_alert_settings():
        return alert_store.settings()

    @app.put("/api/alerts/settings")
    async def put_alert_settings(request: Request):
        try:
            s = validate_settings(await json_body(request), alert_store.settings())
        except SettingsError as e:
            raise HTTPException(400, str(e))
        alert_store.put_settings(s)
        alerts.wake.set()
        automations.wake.set()
        return s

    # ---- automations: window heating status, weekly summary ----
    @app.get("/api/automations/status")
    async def automations_status():
        return automations.status()

    @app.get("/api/summary/latest")
    async def summary_latest():
        s = automations.store.latest_summary()
        if s is None:
            raise HTTPException(404, "no weekly summary yet")
        return s

    @app.post("/api/summary/preview")
    async def summary_preview():
        await devices()
        return await automations.summary.preview()

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/api/devices")
    async def list_devices():
        await devices()
        if not live.fresh():
            live.load_states(await ha.states())
        return live.device_list()

    @app.get("/api/events")
    async def events():
        await devices()
        if not live.fresh():
            live.load_states(await ha.states())
        client = live.subscribe()
        return StreamingResponse(live.stream(client, live.device_list()), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/devices/refresh")
    async def refresh():
        cache["devices"] = None
        return {"count": len(await devices())}

    @app.post("/api/devices/{entity_id}/toggle")
    async def toggle(entity_id: str):
        await require(entity_id, ("light", "plug", "dehumidifier"))
        # light.* / switch.* (plugs, switch-only dehumidifiers) / humidifier.*
        await ha.call_service(entity_id.split(".", 1)[0], "toggle", {"entity_id": entity_id})
        return {"ok": True}

    async def dehum_item(entity_id: str) -> dict:
        dev = await require(entity_id, ("dehumidifier",))
        if not live.fresh():
            live.load_states(await ha.states())
        return build_device(dev, live.states)

    @app.post("/api/devices/{entity_id}/humidity")
    async def set_humidity(entity_id: str, request: Request):
        item = await dehum_item(entity_id)
        try:
            h = check_humidity(await json_body(request), item)
        except DehumError as e:
            raise HTTPException(400, str(e))
        await ha.call_service("humidifier", "set_humidity", {"entity_id": entity_id, "humidity": h})
        return {"ok": True, "humidity": h}

    @app.post("/api/devices/{entity_id}/mode")
    async def set_dehum_mode(entity_id: str, request: Request):
        item = await dehum_item(entity_id)
        try:
            m = check_mode(await json_body(request), item)
        except DehumError as e:
            raise HTTPException(400, str(e))
        await ha.call_service("humidifier", "set_mode", {"entity_id": entity_id, "mode": m})
        return {"ok": True, "mode": m}

    @app.post("/api/devices/{entity_id}/temperature")
    async def set_temperature(entity_id: str, body: TemperatureBody):
        await require(entity_id, ("valve",))
        check_temp(body.temperature)
        await ha.call_service("climate", "set_temperature", {"entity_id": entity_id, "temperature": body.temperature})
        return {"ok": True}

    @app.post("/api/devices/{entity_id}/light")
    async def set_light(entity_id: str, body: LightBody):
        await require(entity_id, ("light",))
        data = light_data(body)
        await ha.call_service("light", "turn_on", {"entity_id": entity_id, **data})
        return {"ok": True}

    @app.post("/api/bulk")
    async def bulk(body: BulkBody):
        if body.action not in ("turn_on", "turn_off"):
            raise HTTPException(400, "action must be turn_on or turn_off")
        try:
            groups = on_off_groups(await devices(), body.entity_ids)
        except ValueError as e:
            raise HTTPException(400, f"not a light or plug: {e}")
        return {"ok": True, "count": await call_groups(ha, body.action, groups)}

    @app.post("/api/valves/temperature")
    async def set_valves(body: ValvesBody):
        check_temp(body.temperature)
        valves = [e for e, d in (await devices()).items() if d.kind == "valve"]
        ids = valves if body.entity_ids is None else list(dict.fromkeys(body.entity_ids))
        bad = [e for e in ids if e not in valves]
        if bad:
            raise HTTPException(400, f"not a valve: {bad[0]}")
        if ids:
            await ha.call_service("climate", "set_temperature", {"entity_id": ids, "temperature": body.temperature})
        return {"ok": True, "count": len(ids)}

    # ---- away / home ----
    async def go_away() -> dict:
        s = mode_store.get()
        devs = await devices()
        if not live.fresh():
            live.load_states(await ha.states())
        keep = set(store.get().get("settings", {}).get("keep_on", []))
        off = sorted(e for e, d in devs.items() if d.kind in ("light", "plug") and e not in keep)
        kept = sorted(e for e in keep if e in devs)
        valves = sorted(e for e, d in devs.items() if d.kind == "valve")
        held = automations.window.held()  # radiators an open window keeps low stay low; remember their real target
        if s["mode"] != "away":  # pressing Away twice keeps the first remembered targets
            s["targets"] = {**current_targets(valves, live.states),
                            **{e: t for e, t in held.items() if e in valves and t is not None}}
            s["prev_alerts_enabled"] = alert_store.settings()["enabled"]
            s["since"] = now_iso()
        await call_groups(ha, "turn_off", on_off_groups(devs, off))
        held_now = sorted(e for e in valves if e in held)
        valves = [e for e in valves if e not in held]
        if valves:
            await ha.call_service("climate", "set_temperature", {"entity_id": valves, "temperature": s["away_temp"]})
        alert_store.put_settings({**alert_store.settings(), "enabled": True})
        alerts.wake.set()
        s["mode"] = "away"
        mode_store.put(s)
        return {**public(s), "turned_off": sorted(off), "kept_on": kept,
                "valves": {e: s["away_temp"] for e in valves}, "held_by_window": held_now, "alerts_enabled": True}

    async def go_home() -> dict:
        s = mode_store.get()
        if s["mode"] != "away":
            return {**public(s), "valves": {}, "alerts_enabled": alert_store.settings()["enabled"]}
        valves = {e for e, d in (await devices()).items() if d.kind == "valve"}
        restored, skipped = {}, sorted(set(s["targets"]) - valves)
        held = automations.window.held()  # window still open: keep it low, restore this target when it closes
        for e in sorted(set(held) & set(s["targets"])):
            automations.window.adopt(e, s["targets"][e])
        targets = {e: t for e, t in s["targets"].items() if e not in held}
        for t, ids in restore_groups(targets, valves).items():
            await ha.call_service("climate", "set_temperature", {"entity_id": ids, "temperature": t})
            restored.update({e: t for e in ids})
        a = alert_store.settings()
        if isinstance(s["prev_alerts_enabled"], bool):
            a = {**a, "enabled": s["prev_alerts_enabled"]}
            alert_store.put_settings(a)
            alerts.wake.set()
        s.update(mode="home", since=now_iso(), targets={}, prev_alerts_enabled=None)
        mode_store.put(s)
        return {**public(s), "valves": restored, "skipped": skipped, "held_by_window": sorted(set(held) & valves),
                "alerts_enabled": a["enabled"]}

    @app.get("/api/mode")
    async def get_mode():
        return public(mode_store.get())

    @app.post("/api/mode")
    async def set_mode(request: Request):
        data = await json_body(request)
        mode = data.get("mode") if isinstance(data, dict) else None
        if mode not in ("away", "home"):
            raise HTTPException(400, "mode must be away or home")
        async with automations.lock:
            return await (go_away() if mode == "away" else go_home())

    @app.put("/api/mode/settings")
    async def put_mode_settings(request: Request):
        try:
            t = validate_mode_settings(await json_body(request))
        except ModeError as e:
            raise HTTPException(400, str(e))
        s = mode_store.get()
        s["away_temp"] = t
        mode_store.put(s)
        return public(s)

    @app.get("/api/layout")
    async def get_layout():
        return store.get()

    @app.put("/api/layout")
    async def put_layout(request: Request):
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON")
        # Entities already placed stay valid even if HA is briefly missing them.
        old = store.get()
        known = {p["entity_id"] for p in old.get("placements", [])}
        known |= {o["entity_id"] for o in old.get("openings", []) if o.get("entity_id")}
        plugs = set(old.get("settings", {}).get("keep_on", []))
        dehums = set(old.get("settings", {}).get("all_off_include", []))
        try:
            devs = await devices()
            known |= set(devs)
            plugs |= {e for e, d in devs.items() if d.kind == "plug"}
            dehums |= {e for e, d in devs.items() if d.kind == "dehumidifier"}
        except HAError:
            pass
        try:
            layout = validate_layout(data, known, plugs, dehums)
        except LayoutError as e:
            raise HTTPException(400, str(e))
        store.put(layout)
        return layout

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(FRONTEND_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(FRONTEND_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app

