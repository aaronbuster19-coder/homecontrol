import math
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles


class RevalidatingStaticFiles(StaticFiles):
    """The app's files always revalidate (ETag -> cheap 304s): without this, browsers guessed a freshness lifetime from
    Last-Modified and could keep an old furniture.js/style.css next to a new appliances.js after a deploy."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp
from pydantic import BaseModel

from . import activity as activity_api, weather as weather_api
from . import brief as brief_api
from .activity import Activity, ActivityStore, ActorMiddleware, acting
from .appliances import linked, protected_plugs
from .appliance_stats import ApplianceStats
from .alerts import Alerts, AlertStore, Pusher, SettingsError, load_vapid, validate_settings, validate_subscription, webpush_sender
from .automations import Automations, AutoStore
from .auth import COOKIE, SESSION_TTL, AuthMiddleware, RateLimiter, Sessions, check_basic_auth, check_credentials, client_key, is_https, load_secret  # noqa: F401
from .config import Settings, load_settings
from .dehumidifier import DehumError, check_humidity, check_mode
from .discovery import Device, apply_names, parse_template_output
from .energy import Energy, EnergyError
from .ha import DISCOVERY_TEMPLATE, HAClient, HAError
from .history import DOOR_RANGES, History, RangeError, check_range
from .live import Live, build_device, ws_url
from .schedules import ScheduleError, validate_schedule
from .schedules import validate_settings as validate_schedule_settings
from . import media, presence as presence_api, standby as standby_api
from .modes import ModeError, ModeStore, current_targets, now_iso, public, restore_groups, validate_mode_settings
from .summary import local_tz
from .store import NAME_MAX, LayoutError, LayoutStore, carry_settings, stored_media, stored_plugs, stored_refs, validate_energy, validate_layout

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
    source: str | None = None  # "all_off": the header's All off button (named in the activity timeline)


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
    """Split lights/plugs (and dehumidifiers and TVs, for "All off") by HA domain; raises ValueError naming the first id that
    isn't one."""
    groups: dict[str, list[str]] = {"light": [], "switch": []}
    for eid in dict.fromkeys(entity_ids):
        d = devs.get(eid)
        if d is None or d.kind not in ("light", "plug", "dehumidifier", "media"):
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
               push_sender=None, clock=time.time) -> FastAPI:
    settings = settings or load_settings()
    ha = ha or HAClient(settings.ha_url, settings.ha_token)
    store = LayoutStore(settings.db_path)
    cache: dict = {"at": 0.0, "devices": None}
    live = live or Live(ha, ws_url(settings.ha_url), settings.ha_token)
    vapid = load_vapid(settings.db_path)
    alert_store = AlertStore(settings.db_path)
    mode_store = ModeStore(settings.db_path)
    pusher = Pusher(alert_store, push_sender or webpush_sender(vapid))
    # Activity timeline (backend/activity.py): every service call, push and sign-in is logged with who/what did it.
    activity = Activity(ActivityStore(settings.db_path), ha, lambda: devices(), lambda: store.get(), clock, local_tz())
    ha.call_service = activity.wrap_call(getattr(ha.call_service, "__wrapped__", ha.call_service))
    send_push = pusher.notify

    async def logged_push(payload: dict):
        r = await send_push(payload)
        activity.record_push(payload, r)
        return r
    pusher.notify = logged_push

    @asynccontextmanager
    async def lifespan(app):
        live.start()
        alerts.start()
        automations.start()
        weather.start()
        yield
        await weather.stop()
        await automations.stop()
        await alerts.stop()
        await live.stop()
        await ha.close()

    app = FastAPI(title="homecontrol", lifespan=lifespan)

    sessions = Sessions(load_secret(settings.db_path), settings.app_password)
    limiter = RateLimiter()
    count_fail = limiter.fail

    def logged_fail(key: str):  # form and Basic sign-in failures alike, for the activity timeline
        count_fail(key)
        activity.record("login_failed", ip=key)
    limiter.fail = logged_fail
    app.add_middleware(ActorMiddleware)  # inside the auth guard: sees the signed-in user
    app.add_middleware(AuthMiddleware, sessions=sessions, user=settings.app_user, password=settings.app_password, limiter=limiter)

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request):
        key = client_key(request.headers, request.client.host if request.client else None)
        if limiter.blocked(key):
            raise HTTPException(429, "too many attempts, try again later")
        if not check_credentials(body.username, body.password, settings.app_user, settings.app_password):
            limiter.fail(key)
            raise HTTPException(401, "wrong username or password")
        activity.record("login", user=settings.app_user, ip=key)
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
            apply_names(cache["devices"], store.get().get("settings"))
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
    automations = Automations(AutoStore(settings.db_path), alert_store.settings, pusher, live, ha, store, mode_store.get, devices,
                              clock=clock)
    app.state.automations = automations
    activity.auto_store = automations.store
    activity.schedule_name = lambda sid: (automations.schedules.store.get(sid) or {}).get("name")
    automations.appliances.on_event = activity.record_appliance
    live.add_observer(activity.observe)
    hold_push = automations.quiet.store.add

    def logged_hold(key: str, category: str, payload: dict, at: float):
        hold_push(key, category, payload, at)
        activity.record("push_held", title=str(payload.get("title") or ""), body=str(payload.get("body") or "")[:200],
                        category=category)
    automations.quiet.store.add = logged_hold

    def heating_start() -> float | None:
        nxt = [s["next"] for s in automations.schedules.listing()["schedules"]
               if s.get("next") and s["enabled"] and s["action"]["type"] == "temperature"]
        return min(nxt) / 1000 if nxt else None
    weather = weather_api.Weather(ha, live, automations.store, automations._notify, heating_start, clock, local_tz())
    history = History(ha)
    appliance_stats = ApplianceStats(ha)

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

    # ---- quiet hours / mute for automation pushes ----
    @app.get("/api/alerts/quiet")
    async def quiet_state():
        return automations.quiet.state()

    @app.post("/api/alerts/mute")
    async def mute(request: Request):
        data = await json_body(request)
        kind = data.get("for") if isinstance(data, dict) else None
        if kind not in ("1h", "morning", "off"):
            raise HTTPException(400, "for must be 1h, morning or off")
        alert_store.put_settings({**alert_store.settings(), "mute_until": automations.quiet.mute_until(kind)})
        automations.wake.set()  # unmuted: deliver the digest now
        return automations.quiet.state()

    # ---- schedules ----
    @app.get("/api/schedules")
    async def list_schedules():
        return automations.schedules.listing()

    @app.put("/api/schedules/settings")
    async def put_schedule_settings(request: Request):
        try:
            s = validate_schedule_settings(await json_body(request), automations.schedules.store.settings())
        except ScheduleError as e:
            raise HTTPException(400, str(e))
        automations.schedules.put_settings(s)
        automations.wake.set()
        return automations.schedules.listing()

    async def checked_schedule(request: Request, old: dict | None = None) -> dict:
        data = await json_body(request)
        try:
            return validate_schedule(data, await devices(), store.get(), old)
        except ScheduleError as e:
            raise HTTPException(400, str(e))

    def public_schedule(sid: str) -> dict:
        return next(s for s in automations.schedules.listing()["schedules"] if s["id"] == sid)

    @app.post("/api/schedules")
    async def create_schedule(request: Request):
        data = await checked_schedule(request)
        try:
            s = automations.schedules.create(data)
        except ScheduleError as e:
            raise HTTPException(400, str(e))
        automations.wake.set()
        return public_schedule(s["id"])

    @app.put("/api/schedules/{sid}")
    async def update_schedule(sid: str, request: Request):
        old = automations.schedules.store.get(sid)
        if old is None:
            raise HTTPException(404, "unknown schedule")
        automations.schedules.update(old, await checked_schedule(request, old))
        automations.wake.set()
        return public_schedule(sid)

    @app.delete("/api/schedules/{sid}")
    async def delete_schedule(sid: str):
        if not automations.schedules.store.delete(sid):
            raise HTTPException(404, "unknown schedule")
        return {"ok": True}

    # ---- automations: window heating status, weekly summary ----
    @app.get("/api/automations/status")
    async def automations_status():
        return automations.status()

    @app.get("/api/appliances")
    async def appliances():
        """Linked appliances: cycle state (washer / dryer / dishwasher) and status text, with the server's clock."""
        await devices()
        if not live.fresh():
            live.load_states(await ha.states())
        items = {e: build_device(d, live.states) for e, d in live.devices.items() if d.kind == "plug"}
        return automations.appliances.public(items)

    @app.get("/api/appliances/{fid}/stats")
    async def appliance_stats_api(fid: str):
        """Usage from HA history for one linked appliance: cycles, boils, hours on, kWh and cost."""
        layout = store.get()
        f = next((x for x in linked(layout) if x["id"] == fid), None)
        if f is None:
            raise HTTPException(404, "no linked appliance with that id")
        dev = (await devices()).get(f["plug"])
        if dev is None:
            raise HTTPException(404, "its plug isn't in Home Assistant right now")
        return await appliance_stats.stats(f, dev, layout)

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

    @app.post("/api/devices/{entity_id}/turn_off")
    async def turn_off(entity_id: str):
        """Only ever switches OFF (the "Turn off" button on a left-on reminder push; session cookie as everywhere)."""
        await require(entity_id, ("light", "plug"))
        await ha.call_service(entity_id.split(".", 1)[0], "turn_off", {"entity_id": entity_id})
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

    # ---- TVs / media players (backend/media.py) ----
    @app.post("/api/devices/{entity_id}/media")
    async def media_control(entity_id: str, request: Request):
        dev = await require(entity_id, ("media",))
        if not live.fresh():
            live.load_states(await ha.states())
        item = build_device(dev, live.states)
        try:
            service, data = media.command(await json_body(request), item)
        except media.MediaError as e:
            raise HTTPException(400, str(e))
        await ha.call_service("media_player", service, {"entity_id": entity_id, **data})
        return {"ok": True, "service": service}

    @app.get("/api/media/{entity_id}/artwork")
    async def media_artwork(entity_id: str, v: str = ""):
        """Now-playing artwork, fetched by the server (HA's token never reaches the browser). ?v= busts the cache."""
        await require(entity_id, ("media",))
        if not live.fresh():
            live.load_states(await ha.states())
        url = ((live.states.get(entity_id) or {}).get("attributes") or {}).get("entity_picture")
        if not url:
            raise HTTPException(404, "no artwork right now")
        try:
            content, ctype = await media.artwork(ha, url)
        except media.MediaError as e:
            raise HTTPException(404, str(e))
        except HAError as e:
            raise HTTPException(502, "artwork unavailable") from e
        cache = "private, max-age=86400" if v and v == media.picture_version(url) else "private, no-cache"
        return Response(content, media_type=ctype, headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff"})

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
        if body.action == "turn_on" and groups.get("media_player"):  # "All off" only: a TV is never switched on in bulk
            raise HTTPException(400, f"not a light or plug: {groups['media_player'][0]}")
        with acting("All off" if body.source == "all_off" else None):
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
        layout = store.get()  # keep-on plugs and fridges / freezers linked to a plug stay on
        keep = set(layout.get("settings", {}).get("keep_on", [])) | protected_plugs(layout)
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
        tvs = media.away_off(devs, live.states) if s.get("tv_off", True) else []
        if tvs:  # TVs are often left on; Away turns them off unless switched off in the Away settings
            await ha.call_service("media_player", "turn_off", {"entity_id": tvs})
        held_now = sorted(e for e in valves if e in held)
        valves = [e for e in valves if e not in held]
        if valves:
            await ha.call_service("climate", "set_temperature", {"entity_id": valves, "temperature": s["away_temp"]})
        alert_store.put_settings({**alert_store.settings(), "enabled": True})
        alerts.wake.set()
        s["mode"] = "away"
        mode_store.put(s)
        return {**public(s), "turned_off": sorted(off + tvs), "kept_on": kept,
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

    automations.presence.actions = (go_away, go_home)  # Auto Away runs exactly these

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
            with acting("Away mode" if mode == "away" else "Home mode"):
                r = await (go_away() if mode == "away" else go_home())
            automations.presence.manual(mode)  # set by hand: Auto Away holds off
            activity.record("mode", mode=mode, user=request.state.user)
            return r

    @app.put("/api/mode/settings")
    async def put_mode_settings(request: Request):
        try:
            changes = validate_mode_settings(await json_body(request))
        except ModeError as e:
            raise HTTPException(400, str(e))
        s = mode_store.get()
        s.update(changes)
        mode_store.put(s)
        return public(s)

    @app.get("/api/layout")
    async def get_layout():
        return store.get()

    async def save_layout(data) -> dict:
        # Entities already referenced stay valid even if HA is briefly missing them.
        old = store.get()
        known = stored_refs(old)
        plugs = stored_plugs(old)
        players = stored_media(old)
        dehums = set(old.get("settings", {}).get("all_off_include", []))  # dehumidifiers and TVs
        try:
            devs = await devices()
            known |= set(devs)
            plugs |= {e for e, d in devs.items() if d.kind == "plug"}
            players |= {e for e, d in devs.items() if d.kind == "media"}
            dehums |= {e for e, d in devs.items() if d.kind in ("dehumidifier", "media")}
        except HAError:
            pass
        try:
            layout = carry_settings(validate_layout(data, known, plugs, dehums, players), old, data)
        except LayoutError as e:
            raise HTTPException(400, str(e))
        store.put(layout)
        if cache["devices"]:  # names / hidden flags show everywhere at once, SSE included
            live.republish(apply_names(cache["devices"], layout.get("settings")))
        return layout

    @app.put("/api/layout")
    async def put_layout(request: Request):
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON")
        return await save_layout(data)

    def with_settings(**changes) -> dict:
        layout = store.get()
        return {**layout, "settings": {"keep_on": [], **(layout.get("settings") or {}), **changes}}

    @app.put("/api/devices/{entity_id}/meta")
    async def put_device_meta(entity_id: str, request: Request):
        """Display name (empty or null = HA's name) and hidden flag, kept in the layout settings."""
        data = await json_body(request)
        if not isinstance(data, dict) or not set(data) <= {"name", "hidden"}:
            raise HTTPException(400, "send {name, hidden}")
        dev = (await devices()).get(entity_id)
        if dev is None:
            raise HTTPException(404, "unknown device")
        s = store.get().get("settings") or {}
        names, hidden = dict(s.get("names") or {}), set(s.get("hidden") or [])
        if "name" in data:
            name = data["name"]
            if name is not None and not isinstance(name, str):
                raise HTTPException(400, "name must be text")
            name = " ".join((name or "").split())
            if len(name) > NAME_MAX:
                raise HTTPException(400, f"name must be at most {NAME_MAX} characters")
            if name and name != dev.ha_name:
                names[entity_id] = name
            else:
                names.pop(entity_id, None)
        if "hidden" in data:
            if not isinstance(data["hidden"], bool):
                raise HTTPException(400, "hidden must be true or false")
            (hidden.add if data["hidden"] else hidden.discard)(entity_id)
        return await save_layout(with_settings(names=names, hidden=sorted(hidden)))

    # ---- energy costs ----
    energy = Energy(ha)

    @app.get("/api/energy/settings")
    async def get_energy_settings():
        return validate_energy((store.get().get("settings") or {}).get("energy") or {})

    @app.put("/api/energy/settings")
    async def put_energy_settings(request: Request):
        try:
            e = validate_energy(await json_body(request))
        except LayoutError as err:
            raise HTTPException(400, str(err))
        await save_layout(with_settings(energy=e))
        return e

    async def plug_devices():
        devs = await devices()
        if not live.fresh():
            live.load_states(await ha.states())
        plugs = [d for d in devs.values() if d.kind == "plug"]
        return plugs, {d.entity_id: build_device(d, live.states) for d in plugs}

    @app.get("/api/energy")
    async def get_energy(range: str = "today"):
        plugs, items = await plug_devices()
        try:
            return await energy.totals(plugs, items, store.get(), range)
        except EnergyError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/energy/standby")
    async def get_standby():
        plugs, _ = await plug_devices()
        return await energy.standby(plugs, store.get())

    # ---- activity timeline (backend/activity.py), outdoor weather (backend/weather.py) ----
    async def ensure_states():
        await devices()
        if not live.fresh():
            live.load_states(await ha.states())

    activity_api.add_routes(app, activity)
    weather_api.add_routes(app, weather, ensure_states, json_body)
    brief_api.add_routes(app, settings.db_path, ha, live, store.get, devices, plug_devices, weather, local_tz(), clock)  # morning brief, monthly report

    # ---- Auto Away (backend/presence.py), standby saver (backend/standby.py) ----
    presence_api.add_routes(app, automations.presence, devices, live, ha, json_body, automations.wake.set)
    standby_api.add_routes(app, automations.standby, devices, plug_devices, energy, store.get, live, json_body, automations.wake.set)

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(FRONTEND_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(FRONTEND_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    if FRONTEND_DIR.is_dir():
        app.mount("/", RevalidatingStaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app

