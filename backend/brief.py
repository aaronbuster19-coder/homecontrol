"""Morning brief: one card with last night's door / window log, today's weather, yesterday's energy cost and anything
left on — plus the routes of the monthly energy report (backend/energy_report.py).

Everything here is read-only: the brief lists what's on, it never switches anything (rows open the device sheet).
Plugs that are meant to stay on (keep-on plugs, linked fridges / freezers / home servers) aren't "left on".
"""
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, time as dtime, timedelta, timezone

from .appliances import protected_plugs
from .energy import cost_p, midnight, tariff
from .energy_report import DailyStore, ReportError, Rollup, appliance_names, monthly, totals
from .history import by_entity, ms, parse_ts, samples, state_of
from .live import build_device

log = logging.getLogger("homecontrol.brief")

NIGHT_FROM, NIGHT_TO = dtime(22, 0), dtime(7, 0)
MAX_EVENTS = 40
DOORS_TTL = 60
RAIN_PCT = 50       # "Rain likely from 15:00": the first hour today at or above this chance
MEDIA_OFF = ("off", "standby", "unavailable", "unknown")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def night_window(now: float, tz) -> tuple[float, float]:
    """Last night: 22:00 yesterday to 07:00 today (local), cut at now before 07:00. Opened in the evening, it's still
    the night that ended this morning."""
    loc = datetime.fromtimestamp(now, tz)
    d = loc.date()
    a = datetime.combine(d - timedelta(days=1), NIGHT_FROM, tzinfo=tz).timestamp()
    b = datetime.combine(d, NIGHT_TO, tzinfo=tz).timestamp()
    return a, min(b, now)


def door_events(rows: list[dict], a_ms: int, b_ms: int) -> list[tuple[int, str]]:
    """Opened / closed changes in (a, b]: the first row is the state at `a`, not a change."""
    out, prev = [], None
    for t, v in samples(rows, state_of("open", "closed")):
        if v is None:
            prev = None  # unavailable: a change straight out of it (HA restart) isn't an event
            continue
        if prev is not None and v != prev and a_ms < t <= b_ms:
            out.append((t, v))
        prev = v
    return out


def left_on(devs: dict, states: dict, layout: dict) -> list[dict]:
    """Lights, plugs, TVs and dehumidifiers that are on now, except plugs that are meant to stay on."""
    keep = set((layout.get("settings") or {}).get("keep_on") or []) | protected_plugs(layout)
    appl = appliance_names(layout)
    out = []
    for d in devs.values():
        if d.hidden or d.kind not in ("light", "plug", "media", "dehumidifier") or d.entity_id in keep:
            continue
        item = build_device(d, states)
        s = item["state"]
        if (s in MEDIA_OFF) if d.kind == "media" else s != "on":
            continue
        row = {"entity_id": d.entity_id, "name": appl[d.entity_id]["name"] if d.entity_id in appl else d.name,
               "kind": d.kind, "power": item.get("power"), "since": parse_ts((states.get(d.entity_id) or {}).get("last_changed"))}
        out.append(row)
    out.sort(key=lambda r: (r["since"] or 0, r["name"].lower()))
    return out


def weather_part(w: dict, now: float, tz) -> dict:
    if not w.get("available") or not w.get("current"):
        return {"available": False, "entity_id": w.get("entity_id")}
    c, today = w["current"], w.get("today") or {}
    out = {"available": True, "condition": c["condition"], "text": c["text"], "temperature": c.get("temperature"),
           "high": today.get("high"), "low": today.get("low"), "tonight": (w.get("tonight") or {}).get("text")}
    d = datetime.fromtimestamp(now, tz).date()
    rain = next((h for h in w.get("hourly") or [] if (h.get("precipitation_probability") or 0) >= RAIN_PCT
                 and datetime.fromtimestamp(h["t"] / 1000, tz).date() == d and h["t"] >= (now - 3600) * 1000), None)
    if rain:
        out["rain_from"] = datetime.fromtimestamp(rain["t"] / 1000, tz).strftime("%H:%M")
        out["rain_pct"] = round(rain["precipitation_probability"])
    return out


class Brief:
    def __init__(self, ha, live, layout, devices, weather, rollup: Rollup, tz, clock=time.time):
        self.ha, self.live, self.layout, self.devices, self.weather = ha, live, layout, devices, weather
        self.rollup, self.tz, self.clock = rollup, tz, clock
        self._doors: tuple[float, tuple, dict] | None = None

    async def night(self, devs: dict, now: float) -> dict:
        a, b = night_window(now, self.tz)
        sensors = sorted((d for d in devs.values() if d.kind == "sensor" and not d.hidden), key=lambda d: d.name.lower())
        key = (int(a), int(b // 60), tuple(d.entity_id for d in sensors))
        if self._doors and self._doors[1] == key and self.clock() - self._doors[0] < DOORS_TTL:
            return self._doors[2]
        a_ms, b_ms = int(a * 1000), int(b * 1000)
        rows = by_entity(await self.ha.history(datetime.fromtimestamp(a, timezone.utc), datetime.fromtimestamp(b, timezone.utc),
                                               [d.entity_id for d in sensors])) if sensors else {}
        events, doors = [], []
        for d in sensors:
            ev = door_events(rows.get(d.entity_id, []), a_ms, b_ms)
            events += [{"t": t, "entity_id": d.entity_id, "name": d.name, "state": v} for t, v in ev]
            doors.append({"entity_id": d.entity_id, "name": d.name, "opens": sum(1 for _, v in ev if v == "open")})
        events.sort(key=lambda e: e["t"])
        out = {"from": a_ms, "to": b_ms, "label": f"{NIGHT_FROM:%H:%M}–{datetime.fromtimestamp(b, self.tz):%H:%M}",
               "sensors": len(sensors), "events": events[-MAX_EVENTS:], "more": max(0, len(events) - MAX_EVENTS),
               "doors": [x for x in doors if x["opens"]]}
        self._doors = (self.clock(), key, out)
        return out

    async def yesterday(self, plugs, devs: dict, layout: dict) -> dict:
        today = self.rollup.today()
        y, before = today - timedelta(days=1), today - timedelta(days=2)
        await self.rollup.run(plugs, 2, oldest=before)
        kwh_y, days_y = totals(self.rollup.store.between(y, y))
        kwh_b, days_b = totals(self.rollup.store.between(before, before))
        t = tariff(layout)
        appl = appliance_names(layout)
        total, prev = sum(kwh_y.values()), sum(kwh_b.values())
        top = sorted(((k, e) for e, k in kwh_y.items() if k >= 0.005), reverse=True)[:3]
        name = lambda e: appl[e]["name"] if e in appl else devs[e].name if e in devs else e
        return {"date": y.isoformat(), "available": bool(days_y), "plugs": len([p for p in plugs if p.related.get("power")]),
                "kwh": round(total, 3), "cost_p": cost_p(total, t["rate_p"]), **t,
                "prev_kwh": round(prev, 3) if days_b else None, "prev_cost_p": cost_p(prev, t["rate_p"]) if days_b else None,
                "top": [{"entity_id": e, "name": name(e), "kwh": round(k, 3), "cost_p": cost_p(k, t["rate_p"])} for k, e in top]}

    async def build(self, plugs) -> dict:
        now = self.clock()
        devs, layout = await self.devices(), self.layout()
        loc = datetime.fromtimestamp(now, self.tz)
        out = {"date": loc.date().isoformat(), "label": f"{WEEKDAYS[loc.weekday()]} {loc.day} {loc:%B}",
               "generated_at": int(now * 1000), "errors": {}}
        for part, make in (("night", lambda: self.night(devs, now)), ("weather", self._weather),
                           ("energy", lambda: self.yesterday(plugs, devs, layout))):
            try:
                out[part] = await make()
            except Exception as e:  # one part failing (HA history down) still shows the rest
                log.warning("brief %s failed: %s", part, e)
                out[part], out["errors"][part] = None, str(e) or type(e).__name__
        out["left_on"] = left_on(devs, self.live.states, layout)
        out["open_now"] = [{"entity_id": d.entity_id, "name": d.name} for d in sorted(devs.values(), key=lambda d: d.name.lower())
                           if d.kind == "sensor" and not d.hidden and (self.live.states.get(d.entity_id) or {}).get("state") == "on"]
        return out

    async def _weather(self) -> dict:
        return weather_part(await self.weather.get(), self.clock(), self.tz)


def add_routes(app, db_path: str, ha, live, layout, devices, plug_devices, weather, tz, clock=time.time):
    """GET /api/brief, GET /api/energy/report?month=YYYY-MM. Starts the hourly energy rollup with the app."""
    from fastapi import HTTPException

    rollup = Rollup(DailyStore(db_path), ha, tz, clock)
    brief = Brief(ha, live, layout, devices, weather, rollup, tz, clock)
    app.state.energy_rollup = rollup

    @app.get("/api/brief")
    async def get_brief():
        plugs, _ = await plug_devices()  # also loads live states
        return await brief.build(plugs)

    @app.get("/api/energy/report")
    async def get_report(month: str = ""):
        plugs, _ = await plug_devices()
        try:
            return await monthly(rollup, plugs, await devices(), layout(), month or None)
        except ReportError as e:
            raise HTTPException(400, str(e))

    # The rollup runs alongside the app's own lifespan (wrapped here so app.py's lifespan stays as it is).
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        async with inner(a) as state:
            rollup.start(plug_devices)
            try:
                yield state
            finally:
                await rollup.stop()
    app.router.lifespan_context = lifespan
    return brief
