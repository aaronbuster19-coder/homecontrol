import base64
import math
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings, load_settings
from .discovery import Device, parse_template_output
from .ha import DISCOVERY_TEMPLATE, HAClient, HAError
from .store import LayoutError, LayoutStore, validate_layout

CACHE_TTL = 300
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def check_basic_auth(header: str | None, user: str, password: str) -> bool:
    if not header or not user or not password:
        return False
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        given_user, sep, given_pw = base64.b64decode(encoded, validate=True).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    if not sep:
        return False
    # Evaluate both comparisons so timing doesn't reveal which one failed.
    ok_user = secrets.compare_digest(given_user.encode(), user.encode())
    ok_pw = secrets.compare_digest(given_pw.encode(), password.encode())
    return ok_user and ok_pw


class TemperatureBody(BaseModel):
    temperature: float


def create_app(settings: Settings | None = None, ha: HAClient | None = None) -> FastAPI:
    settings = settings or load_settings()
    ha = ha or HAClient(settings.ha_url, settings.ha_token)
    store = LayoutStore(settings.db_path)
    cache: dict = {"at": 0.0, "devices": None}

    @asynccontextmanager
    async def lifespan(app):
        yield
        await ha.close()

    app = FastAPI(title="homecontrol", lifespan=lifespan)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if not check_basic_auth(request.headers.get("authorization"), settings.app_user, settings.app_password):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="homecontrol"'})
        return await call_next(request)

    @app.exception_handler(HAError)
    async def ha_error(request, exc: HAError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    async def devices() -> dict[str, Device]:
        if cache["devices"] is None or time.monotonic() - cache["at"] > CACHE_TTL:
            text = await ha.render_template(DISCOVERY_TEMPLATE)
            cache["devices"] = {d.entity_id: d for d in parse_template_output(text)}
            cache["at"] = time.monotonic()
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
        devs = await devices()
        states = {s["entity_id"]: s for s in await ha.states() if s.get("entity_id") in devs}
        out = []
        for d in devs.values():
            s = states.get(d.entity_id, {})
            attrs = s.get("attributes", {})
            item = d.to_dict()
            item["state"] = s.get("state", "unavailable")
            if d.kind == "valve":
                for k in ("current_temperature", "temperature", "min_temp", "max_temp", "target_temp_step"):
                    item[k] = attrs.get(k)
            if d.kind == "light":
                item["brightness"] = attrs.get("brightness")
            out.append(item)
        out.sort(key=lambda x: (x["kind"], x["name"].lower()))
        return out

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
        t = body.temperature
        if not math.isfinite(t) or not 5 <= t <= 35:
            raise HTTPException(400, "temperature must be between 5 and 35")
        await ha.call_service("climate", "set_temperature", {"entity_id": entity_id, "temperature": t})
        return {"ok": True}

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
        known = {p["entity_id"] for p in store.get().get("placements", [])}
        try:
            known |= set(await devices())
        except HAError:
            pass
        try:
            layout = validate_layout(data, known)
        except LayoutError as e:
            raise HTTPException(400, str(e))
        store.put(layout)
        return layout

    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app

