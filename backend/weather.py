"""Outdoor weather from Home Assistant's weather.* entities (the Met.no integration HA sets up by default creates
weather.forecast_home).

Current conditions come from the entity's state (live, no extra calls). Forecasts come from the service
weather.get_forecasts (HA 2023.9+), called over REST with ?return_response, hourly and daily, cached 15 min per entity.
Older HA / integrations without it: the legacy `forecast` attribute if there is one, else current conditions only.
Also: the cold-night hint (tonight's low under COLD_C) and the optional "Frost tonight" push (off by default; a
quiet-hours category, so a push due at night waits for the morning digest).
"""
import asyncio
import logging
import math
import time
from datetime import datetime, time as dtime, timedelta, timezone

from fastapi import HTTPException, Request

log = logging.getLogger("homecontrol.weather")

DEFAULT_ENTITY = "weather.forecast_home"
FORECAST_TTL = 15 * 60
ERROR_TTL = 5 * 60
COLD_C = 3.0
FROST_FROM_HOUR = 17      # the frost push goes out once a day, from 17:00 local
CHECK_EVERY = 600
HOURLY_N, DAILY_N = 12, 5
DEFAULT_SETTINGS = {"entity_id": None, "frost_push": False}
CONDITIONS = {"clear-night": "Clear", "cloudy": "Cloudy", "exceptional": "Exceptional", "fog": "Fog", "hail": "Hail",
              "lightning": "Thunder", "lightning-rainy": "Thunder and rain", "partlycloudy": "Partly cloudy",
              "pouring": "Heavy rain", "rainy": "Rain", "snowy": "Snow", "snowy-rainy": "Sleet", "sunny": "Sunny",
              "windy": "Windy", "windy-variant": "Windy and cloudy"}
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


class WeatherError(ValueError):
    pass


def num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def to_c(v, unit: str | None) -> float | None:
    f = num(v)
    if f is None:
        return None
    if unit in ("°F", "F"):
        f = (f - 32) * 5 / 9
    elif unit in ("K",):
        f -= 273.15
    return round(f, 1)


def to_mph(v, unit: str | None) -> float | None:
    f = num(v)
    if f is None:
        return None
    factor = {"km/h": 0.621371, "m/s": 2.23694, "kn": 1.15078, "ft/s": 0.681818, "mph": 1.0}.get(unit or "km/h", 0.621371)
    return round(f * factor, 1)


def compass(b) -> str | None:
    f = num(b)
    return None if f is None else COMPASS[int((f % 360) / 45 + 0.5) % 8]


def current(state: dict | None) -> dict | None:
    """The weather entity's state -> {condition, text, temperature, apparent_temperature, humidity, wind_mph, wind_dir}."""
    if not state or state.get("state") in ("unavailable", "unknown", None):
        return None
    a = state.get("attributes") or {}
    tu = a.get("temperature_unit")
    return {"condition": state["state"], "text": CONDITIONS.get(state["state"], str(state["state"]).replace("-", " ").capitalize()),
            "temperature": to_c(a.get("temperature"), tu), "apparent_temperature": to_c(a.get("apparent_temperature"), tu),
            "humidity": num(a.get("humidity")), "wind_mph": to_mph(a.get("wind_speed"), a.get("wind_speed_unit")),
            "wind_dir": compass(a.get("wind_bearing")), "name": a.get("friendly_name") or state.get("entity_id")}


def parse_item(f: dict, units: dict) -> dict | None:
    t = f.get("datetime")
    try:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tu = units.get("temperature_unit")
    out = {"t": int(dt.timestamp() * 1000), "condition": f.get("condition"), "temperature": to_c(f.get("temperature"), tu),
           "templow": to_c(f.get("templow"), tu), "precipitation": num(f.get("precipitation")),
           "precipitation_probability": num(f.get("precipitation_probability")),
           "wind_mph": to_mph(f.get("wind_speed"), units.get("wind_speed_unit"))}
    if "is_daytime" in f:
        out["is_daytime"] = bool(f["is_daytime"])
    return out


def parse_forecasts(data, entity_id: str) -> list[dict] | None:
    """The forecast list from a get_forecasts response: REST {"service_response": {id: {"forecast": [...]}}}, or the
    websocket/script shape {id: {"forecast": [...]}}. None when the response doesn't carry one (an HA without
    return_response answers with the list of changed states)."""
    if not isinstance(data, dict):
        return None
    resp = data.get("service_response", data)
    ent = resp.get(entity_id) if isinstance(resp, dict) else None
    fc = ent.get("forecast") if isinstance(ent, dict) else None
    return [f for f in fc if isinstance(f, dict)] if isinstance(fc, list) else None


def items(raw: list[dict] | None, units: dict) -> list[dict]:
    out = [p for p in (parse_item(f, units) for f in raw or []) if p and p["temperature"] is not None]
    out.sort(key=lambda p: p["t"])
    return out


def twice_daily_to_daily(fc: list[dict], tz) -> list[dict]:
    """twice_daily (day + night entries) -> one entry per local day: the day's condition and high, the night's low."""
    days: dict = {}
    for f in fc:
        d = datetime.fromtimestamp(f["t"] / 1000, tz).date()
        e = days.setdefault(d, {**f, "templow": None})
        if f.get("is_daytime", True):
            e.update({k: f[k] for k in ("t", "condition", "temperature", "precipitation", "precipitation_probability", "wind_mph")})
        else:
            e["templow"] = f["temperature"] if e["templow"] is None else min(e["templow"], f["temperature"])
    return [days[d] for d in sorted(days)]


def summarize(cur: dict | None, hourly: list[dict], daily: list[dict], now: float, tz) -> dict:
    """Today high/low, the next 12 h, the next 5 days and tonight's low (from the hourly forecast, else the daily)."""
    loc = datetime.fromtimestamp(now, tz)
    today = loc.date()
    day_of = lambda p: datetime.fromtimestamp(p["t"] / 1000, tz).date()
    hour_ago = (now - 3600) * 1000
    next12 = [h for h in hourly if h["t"] > hour_ago][:HOURLY_N]
    days = [d for d in daily if day_of(d) >= today][:DAILY_N]
    td = next((d for d in days if day_of(d) == today), None)
    hours = [h["temperature"] for h in hourly if day_of(h) == today] + ([cur["temperature"]] if cur and cur.get("temperature") is not None else [])
    hi = max([x for x in [td["temperature"] if td else None, *hours] if x is not None], default=None)
    lo = td["templow"] if td and td.get("templow") is not None else min(hours, default=None)
    # tonight: until 08:00 tomorrow (or this morning, before 08:00), from 18:00 or now
    at = lambda d, h: datetime.combine(d, dtime(h), tzinfo=tz).timestamp()
    if loc.hour < 8:
        w0, w1 = now, at(today, 8)
    else:
        w0, w1 = max(now, at(today, 18)), at(today + timedelta(days=1), 8)
    night = [h["temperature"] for h in hourly if w0 * 1000 - 3600e3 < h["t"] <= w1 * 1000]
    low = min(night) if night else None
    if low is None:
        d = today if loc.hour < 8 else today + timedelta(days=1)
        nd = next((x for x in daily if day_of(x) == d), None)
        low = nd["templow"] if nd else None
    return {"today": {"high": hi, "low": lo}, "hourly": next12, "daily": days,
            "tonight": {"low": low, "cold": low is not None and low < COLD_C}}


class Weather:
    def __init__(self, ha, live, store, notify=None, heating_start=None, clock=time.time, tz=None):
        """store: AutoStore (settings + frost marker); notify: async (payload, category); heating_start: () -> epoch
        seconds of the next heating schedule, or None."""
        self.ha, self.live, self.store, self.notify, self.heating_start = ha, live, store, notify, heating_start
        self.clock, self.tz = clock, tz or timezone.utc
        self.cache: dict[str, tuple[float, dict]] = {}
        self._task: asyncio.Task | None = None

    # ---- settings ----
    def settings(self) -> dict:
        return {**DEFAULT_SETTINGS, **(self.store.get("weather_settings", {}) or {})}

    def put_settings(self, data) -> dict:
        if not isinstance(data, dict) or not set(data) <= set(DEFAULT_SETTINGS):
            raise WeatherError("send {entity_id, frost_push}")
        s = self.settings()
        if "entity_id" in data:
            e = data["entity_id"]
            if e is not None and (not isinstance(e, str) or not e.startswith("weather.")):
                raise WeatherError("entity_id must be a weather.* entity or null")
            if e is not None and self.live.states and e not in self.live.states:
                raise WeatherError(f"{e} isn't in Home Assistant")
            s["entity_id"] = e
        if "frost_push" in data:
            if not isinstance(data["frost_push"], bool):
                raise WeatherError("frost_push must be true or false")
            s["frost_push"] = data["frost_push"]
        self.store.put("weather_settings", s)
        return s

    def entities(self) -> list[dict]:
        return [{"entity_id": e, "name": (s.get("attributes") or {}).get("friendly_name") or e}
                for e, s in sorted(self.live.states.items()) if e.startswith("weather.")]

    def entity(self) -> str | None:
        ids = [e["entity_id"] for e in self.entities()]
        want = self.settings()["entity_id"]
        if want in ids:
            return want
        return DEFAULT_ENTITY if DEFAULT_ENTITY in ids else (ids[0] if ids else None)

    # ---- forecasts ----
    async def _get_forecasts(self, eid: str, kind: str) -> list[dict] | None:
        data = await self.ha.call_service_response("weather", "get_forecasts", {"entity_id": eid, "type": kind})
        return parse_forecasts(data, eid)

    async def forecasts(self, eid: str) -> dict:
        hit = self.cache.get(eid)
        now = time.monotonic()
        if hit and now - hit[0] < (ERROR_TTL if hit[1]["error"] else FORECAST_TTL):
            return hit[1]
        attrs = (self.live.states.get(eid) or {}).get("attributes") or {}
        out, errors = {"hourly": [], "daily": [], "error": None}, []
        for kind in ("hourly", "daily"):
            try:
                raw = await self._get_forecasts(eid, kind)
                if raw is None:
                    errors.append(f"{kind}: no forecast in the response (Home Assistant 2023.9 or newer needed)")
                    continue
                out[kind] = items(raw, attrs)
            except Exception as e:
                if kind == "daily":
                    try:  # some integrations only offer twice_daily
                        raw = await self._get_forecasts(eid, "twice_daily")
                        if raw:
                            out["daily"] = twice_daily_to_daily(items(raw, attrs), self.tz)
                            continue
                    except Exception:
                        pass
                errors.append(f"{kind}: {e}")
        if not out["hourly"] and not out["daily"] and isinstance(attrs.get("forecast"), list):
            legacy = items(attrs["forecast"], attrs)  # HA before 2024.3 kept it as an attribute
            hourly = len(legacy) > 1 and legacy[1]["t"] - legacy[0]["t"] < 3 * 3600e3
            out["hourly" if hourly else "daily"] = legacy
            errors = []
        if errors:
            out["error"] = "; ".join(errors)
            log.warning("weather forecast for %s: %s", eid, out["error"])
        self.cache[eid] = (now, out)
        return out

    def clear(self) -> None:
        self.cache.clear()

    async def get(self) -> dict:
        ents, eid = self.entities(), self.entity()
        base = {"entities": ents, "settings": self.settings(), "entity_id": eid}
        if eid is None:
            return {**base, "available": False}
        cur = current(self.live.states.get(eid))
        fc = await self.forecasts(eid)
        now = self.clock()
        s = summarize(cur, fc["hourly"], fc["daily"], now, self.tz)
        night = s["tonight"]
        if night["cold"]:
            night["text"] = f"Cold night ahead ({round(night['low'])}°)"
            hs = self.heating_start() if self.heating_start else None
            if hs and hs - now < 36 * 3600:
                night["heating"] = datetime.fromtimestamp(hs, self.tz).strftime("%H:%M")
                night["text"] += f" — heating comes on at {night['heating']}"
        return {**base, "available": cur is not None, "current": cur, **s, "forecast_error": fc["error"], "now": int(now * 1000)}

    # ---- frost push ----
    async def tick(self) -> dict | None:
        s = self.settings()
        if not s["frost_push"] or not self.notify:
            return None
        now = self.clock()
        loc = datetime.fromtimestamp(now, self.tz)
        if loc.hour < FROST_FROM_HOUR or self.store.get("weather_frost_sent") == loc.date().isoformat():
            return None
        eid = self.entity()
        if eid is None:
            return None
        w = await self.get()
        if not w["tonight"]["cold"]:
            return None
        self.store.put("weather_frost_sent", loc.date().isoformat())  # marked first: never twice a day
        body = f"Low of {round(w['tonight']['low'])}° tonight."
        if w["tonight"].get("heating"):
            body += f" Heating comes on at {w['tonight']['heating']}."
        payload = {"title": "Frost tonight", "body": body, "tag": "frost", "url": "/?weather"}
        await self.notify(payload, "weather")
        return payload

    async def _run(self, every: float, delay: float) -> None:
        await asyncio.sleep(delay)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("frost check failed: %s", e)
            await asyncio.sleep(every)

    def start(self, every: float = CHECK_EVERY, delay: float = 30) -> None:
        self._task = asyncio.create_task(self._run(every, delay))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


def add_routes(app, weather: Weather, ensure_states, json_body) -> None:
    """GET /api/weather, PUT /api/weather/settings, POST /api/weather/refresh (drops the forecast cache)."""

    @app.get("/api/weather")
    async def get_weather():
        await ensure_states()
        return await weather.get()

    @app.put("/api/weather/settings")
    async def put_weather_settings(request: Request):
        await ensure_states()
        try:
            weather.put_settings(await json_body(request))
        except WeatherError as e:
            raise HTTPException(400, str(e))
        return await weather.get()

    @app.post("/api/weather/refresh")
    async def refresh_weather():
        await ensure_states()
        weather.clear()
        return await weather.get()
