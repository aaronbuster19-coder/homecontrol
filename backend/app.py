import math
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import COOKIE, SESSION_TTL, AuthMiddleware, RateLimiter, Sessions, check_basic_auth, check_credentials, client_key, is_https, load_secret  # noqa: F401
from .config import Settings, load_settings
from .discovery import Device, parse_template_output
from .ha import DISCOVERY_TEMPLATE, HAClient, HAError
from .live import Live, ws_url
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


def check_temp(t: float) -> None:
    if not math.isfinite(t) or not 5 <= t <= 35:
        raise HTTPException(400, "temperature must be between 5 and 35")


def create_app(settings: Settings | None = None, ha: HAClient | None = None, live: Live | None = None) -> FastAPI:
    settings = settings or load_settings()
    ha = ha or HAClient(settings.ha_url, settings.ha_token)
    store = LayoutStore(settings.db_path)
    cache: dict = {"at": 0.0, "devices": None}
    live = live or Live(ha, ws_url(settings.ha_url), settings.ha_token)

    @asynccontextmanager
    async def lifespan(app):
        live.start()
        yield
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
        dev = await require(entity_id, ("light", "plug"))
        domain = "light" if dev.kind == "light" else "switch"
        await ha.call_service(domain, "toggle", {"entity_id": entity_id})
        return {"ok": True}

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
        devs = await devices()
        groups: dict[str, list[str]] = {"light": [], "switch": []}
        for eid in dict.fromkeys(body.entity_ids):
            d = devs.get(eid)
            if d is None or d.kind not in ("light", "plug"):
                raise HTTPException(400, f"not a light or plug: {eid}")
            groups["light" if d.kind == "light" else "switch"].append(eid)
        for domain, ids in groups.items():
            if ids:
                await ha.call_service(domain, body.action, {"entity_id": ids})
        return {"ok": True, "count": sum(map(len, groups.values()))}

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
        try:
            devs = await devices()
            known |= set(devs)
            plugs |= {e for e, d in devs.items() if d.kind == "plug"}
        except HAError:
            pass
        try:
            layout = validate_layout(data, known, plugs)
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

