"""Presence lighting: a door opening after dark turns a room's lights on; they go off again after a quiet period.

Opt-in twice, both off by default: a master switch, then per room. Each room links door contacts (default: the sensors
of the doors on its walls) and lights (default: the lights placed in it), plus a quiet period (default 10 min).

- Dark is Home Assistant's sun.sun (below_horizon); without it, sunset / sunrise computed for the schedules' location
  (London unless changed) in the local zone.
- A door contact opening (off -> on) in an enabled room after dark switches on the room's lights that are off — one
  light.turn_on per room, never more often than every GAP s. The lights it switched on are "owned".
- Any door activity (open or close) restarts the quiet timer. After quiet_minutes with none, the owned lights that are
  still on go off. Lights that were already on are never owned, so never switched off.
- Never fights a manual change: a room light switched on or off by anyone else (a wall switch, the app, a schedule)
  hands the room back — owned lights are forgotten and the room is paused until the next quiet period (quiet_minutes
  with no door activity). A state change arriving within SETTLE s of our own call, to the state we asked for, is ours.
- Never runs while Away: no switch-on, and owned lights are forgotten (Away has switched them off).
- Only light.* entities can be linked, so plugs (fridge, home server, keep-on) are never switched. State that must survive
  a restart (owned lights, pause, timers) is in SQLite; door events seen before a restart are not replayed. A failed
  switch-on is not retried (the next door opening may try after RETRY s); a failed switch-off is retried with back-off,
  OFF_TRIES times in all.
"""
import logging
import time
from datetime import datetime, timedelta

from .geometry import opening_rooms, placed_in
from .sun import sun_event

log = logging.getLogger("homecontrol.presence_lighting")
ROOM_DEFAULTS = {"enabled": False, "sensors": None, "lights": None, "quiet_minutes": 10}
QUIET = (1, 120)
SETTLE = 60          # our own call's state change arriving this late (and to the state we asked) is still ours
GAP = 10             # never two switch-ons for one room within this many seconds (debounce)
RETRY, OFF_TRIES = 60, 3
MAX_SENSORS, MAX_LIGHTS = 10, 30
LOG_MAX = 40
ACTOR = "presence lighting"


class LightingError(ValueError):
    pass


def validate_settings(data) -> dict:
    if not isinstance(data, dict) or set(data) != {"enabled"} or not isinstance(data["enabled"], bool):
        raise LightingError("send {\"enabled\": true|false}")
    return {"enabled": data["enabled"]}


def validate_room(data, current: dict, devices: dict) -> dict:
    keys = set(ROOM_DEFAULTS)
    if not isinstance(data, dict) or not data or not set(data) <= keys:
        raise LightingError("send any of {enabled, sensors, lights, quiet_minutes}")
    out = {**ROOM_DEFAULTS, **current}
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise LightingError("enabled must be true or false")
        out["enabled"] = data["enabled"]
    if "quiet_minutes" in data:
        m, (lo, hi) = data["quiet_minutes"], QUIET
        if isinstance(m, bool) or not isinstance(m, int) or not lo <= m <= hi:
            raise LightingError(f"quiet_minutes must be a whole number from {lo} to {hi}")
        out["quiet_minutes"] = m
    for key, kind, most, what in (("sensors", "sensor", MAX_SENSORS, "door sensor"), ("lights", "light", MAX_LIGHTS, "light")):
        if key not in data:
            continue
        v = data[key]
        if v is None:  # back to the default from the plan
            out[key] = None
            continue
        if not isinstance(v, list) or len(v) > most or not all(isinstance(e, str) for e in v):
            raise LightingError(f"{key} must be null or a list of up to {most} entity ids")
        for e in v:
            d = devices.get(e)
            if d is None or d.kind != kind or (kind == "light" and not e.startswith("light.")):
                raise LightingError(f"{e} is not a {what}")
        out[key] = sorted(set(v))
    return out


def room_sensors(layout: dict, room: dict) -> list[str]:
    """Default door contacts: sensors linked to the doors on the room's walls."""
    return sorted({o["entity_id"] for o in layout.get("openings", []) or []
                   if o.get("type") == "door" and o.get("entity_id") and any(r.get("id") == room.get("id")
                                                                             for r in opening_rooms(layout, o))})


def _fresh() -> dict:
    return {"owned": [], "on_at": None, "last": None, "paused_at": None, "noted": False, "fails": 0, "retry_at": None}


class PresenceLighting:
    """settings: AutoStore "plighting_settings" {"enabled", "rooms": {room id: ROOM_DEFAULTS}};
    state: "plighting_state" {room id: _fresh()}; log: "plighting_log". tick() runs in the automations loop."""

    def __init__(self, store, call, layout, mode, latlon, clock=time.time, tz=None):
        self.store, self.call, self.layout, self.mode, self.latlon = store, call, layout, mode, latlon
        self.clock, self.tz = clock, tz
        self.state: dict = store.get("plighting_state", {}) or {}
        self.seen: dict[str, str] = {}                     # entity -> last on/off seen
        self.events: list[tuple[str, bool, float]] = []    # door contact, opened?, when
        self.manual: list[tuple[str, float]] = []          # light switched by someone else, when
        self.expect: dict[str, tuple[str, float]] = {}     # light -> (state we asked for, when)
        self.last_on: dict[str, float] = {}                # room -> our last switch-on call

    # ---- settings ----
    def settings(self) -> dict:
        s = self.store.get("plighting_settings", {}) or {}
        return {"enabled": bool(s.get("enabled", False)), "rooms": s.get("rooms") or {}}

    def room(self, rid: str) -> dict:
        return {**ROOM_DEFAULTS, **self.settings()["rooms"].get(rid, {})}

    def put_settings(self, new: dict) -> None:
        s = self.settings()
        if s["enabled"] != new["enabled"]:
            self._log("", "switched on" if new["enabled"] else "switched off")
            self._drop_all()
        self.store.put("plighting_settings", {**s, "enabled": new["enabled"]})

    def put_room(self, rid: str, name: str, new: dict) -> None:
        s = self.settings()
        old = self.room(rid)
        if old["enabled"] != new["enabled"]:
            self._log(name, "enabled" if new["enabled"] else "disabled")
            self.state.pop(rid, None)  # either way, start from nothing: never switches off what it didn't just switch on
            self._save()
        s["rooms"] = {**s["rooms"], rid: new}
        self.store.put("plighting_settings", s)

    def _drop_all(self) -> None:
        self.state, self.events, self.manual = {}, [], []
        self._save()

    def _save(self) -> None:
        self.store.put("plighting_state", self.state)

    def _log(self, room: str, action: str, note: str = "") -> None:
        logs = self.store.get("plighting_log", []) or []
        logs.insert(0, {"at": self.clock(), "room": room, "action": action, "note": note})
        self.store.put("plighting_log", logs[:LOG_MAX])
        log.info("presence lighting: %s %s %s", room, action, note)

    # ---- dark ----
    def dark(self, states: dict, now: float) -> dict:
        """{"dark", "source": "ha" | "computed", "until": when it changes (epoch s) or None}."""
        sun = states.get("sun.sun") or {}
        st, attrs = sun.get("state"), sun.get("attributes") or {}
        if st in ("below_horizon", "above_horizon"):
            dark = st == "below_horizon"
            nxt = attrs.get("next_rising" if dark else "next_setting")
            until = None
            if isinstance(nxt, str):
                try:
                    until = datetime.fromisoformat(nxt.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
            return {"dark": dark, "source": "ha", "until": until}
        ll = self.latlon() or {}
        lat, lon = ll.get("lat", 51.5074), ll.get("lon", -0.1278)
        today = datetime.fromtimestamp(now, self.tz).date()
        rise, set_ = sun_event(today, "sunrise", lat, lon), sun_event(today, "sunset", lat, lon)
        if rise is None or set_ is None:  # polar day / night: no dusk to wait for
            return {"dark": False, "source": "computed", "until": None}
        if now < rise:
            return {"dark": True, "source": "computed", "until": rise}
        if now >= set_:
            return {"dark": True, "source": "computed", "until": sun_event(today + timedelta(days=1), "sunrise", lat, lon)}
        return {"dark": False, "source": "computed", "until": set_}

    # ---- what a room uses ----
    def resolved(self, layout: dict, room: dict, cfg: dict, devices: dict) -> tuple[list[str], list[str]]:
        sensors = cfg["sensors"] if cfg["sensors"] is not None else room_sensors(layout, room)
        lights = cfg["lights"] if cfg["lights"] is not None else placed_in(
            layout, room, [e for e, d in devices.items() if d.kind == "light"])
        ok = lambda e, k: e in devices and devices[e].kind == k
        return [e for e in sensors if ok(e, "sensor")], sorted(e for e in lights if ok(e, "light") and e.startswith("light."))

    # ---- live changes (Live observer, via backend/automations.py) ----
    def observe(self, item: dict) -> bool:
        """Remember door activity and lights switched by someone else; True = wake the loop."""
        kind, eid, st = item.get("kind"), item.get("entity_id"), item.get("state")
        if kind not in ("sensor", "light") or st not in ("on", "off"):
            return False
        prev, self.seen[eid] = self.seen.get(eid), st
        if prev is None or prev == st:
            return False
        now = self.clock()
        if kind == "sensor":
            self.events.append((eid, st == "on", now))
            return True
        exp = self.expect.pop(eid, None)
        if exp and exp[0] == st and now - exp[1] <= SETTLE:
            return False  # our own call landing
        self.manual.append((eid, now))
        return True

    # ---- loop ----
    def _quiet_from(self, rs: dict) -> float:
        return max(rs.get("paused_at") or 0, rs.get("last") or 0)

    def next_due(self) -> float | None:
        s = self.settings()
        if not s["enabled"]:
            return None
        now, ts = self.clock(), []
        for rid, rs in self.state.items():
            cfg = self.room(rid)
            if not cfg["enabled"]:
                continue
            q = cfg["quiet_minutes"] * 60
            if rs.get("owned") and rs.get("last") is not None:
                ts.append(rs["last"] + q)
            if rs.get("paused_at"):
                ts.append(self._quiet_from(rs) + q)
            if rs.get("retry_at"):
                ts.append(rs["retry_at"])
        ts = [t for t in ts if t > now]
        return min(ts) if ts else None

    async def tick(self, devices: dict, states: dict) -> list[dict]:
        """Returns the service calls made (for tests / logs)."""
        s, now = self.settings(), self.clock()
        events, manual = self.events, self.manual
        self.events, self.manual = [], []
        if not s["enabled"]:
            return []
        layout = self.layout()
        rooms = {r.get("id"): r for r in layout.get("rooms", []) or []}
        away = self.mode().get("mode") == "away"
        dark = None
        cur = lambda e: (states.get(e) or {}).get("state")
        ons, offs, changed = [], [], False
        for rid, cfg in sorted(s["rooms"].items()):
            cfg, room = {**ROOM_DEFAULTS, **cfg}, rooms.get(rid)
            if not cfg["enabled"] or room is None:
                continue
            name, q = room.get("name") or rid, cfg["quiet_minutes"] * 60
            sensors, lights = self.resolved(layout, room, cfg, devices)
            rs = self.state.setdefault(rid, _fresh())
            door = [(opened, t) for e, opened, t in events if e in sensors]
            if away:  # Away switched everything off: nothing is ours, and nothing switches on
                if rs["owned"] or door:
                    rs.update(owned=[], on_at=None, fails=0, retry_at=None)
                    if door:
                        rs["last"] = max(t for _, t in door)
                    changed = True
                continue
            touched = [t for e, t in manual if e in lights]
            if touched:
                note = bool(rs["owned"]) or rs["noted"] or (dark := dark or self.dark(states, now))["dark"]
                if note and not rs["paused_at"]:
                    self._log(name, "paused", "a light was switched by hand — back to automatic after "
                                              f"{cfg['quiet_minutes']} min without door activity")
                rs.update(owned=[], on_at=None, paused_at=max(touched), noted=note, retry_at=None, fails=0)
                changed = True
            if rs["paused_at"] and now - self._quiet_from(rs) >= q:  # before this tick's door activity counts
                if rs["noted"]:
                    self._log(name, "resumed", f"{cfg['quiet_minutes']} min without door activity")
                rs.update(paused_at=None, noted=False)
                changed = True
            if door:
                rs["last"], changed = max(t for _, t in door), True
            opened = any(o for o, _ in door)
            retry_ok = rs["retry_at"] is None or now >= rs["retry_at"]
            if opened and not rs["paused_at"]:
                dark = dark or self.dark(states, now)
                if dark["dark"] and retry_ok and now - self.last_on.get(rid, -1e18) >= GAP:
                    want = [e for e in lights if cur(e) == "off"]
                    if want:
                        ons.append((rid, name, want))
                        self.last_on[rid] = now
            elif rs["owned"] and rs["last"] is not None and now - rs["last"] >= q and retry_ok:
                still = [e for e in rs["owned"] if cur(e) == "on"]
                if still:
                    offs.append((rid, name, still, cfg["quiet_minutes"]))
                else:
                    rs.update(owned=[], on_at=None, fails=0, retry_at=None)
                    changed = True
        if changed:
            self._save()
        calls = []
        for rid, name, ids in ons:
            calls.append(await self._switch(rid, name, "turn_on", ids, now))
        for rid, name, ids, mins in offs:
            calls.append(await self._switch(rid, name, "turn_off", ids, now, mins))
        return calls

    async def _switch(self, rid: str, name: str, service: str, ids: list[str], now: float, mins: int = 0) -> dict:
        rs = self.state.setdefault(rid, _fresh())
        want = "on" if service == "turn_on" else "off"
        for e in ids:
            self.expect[e] = (want, now)  # before the call: its state change may arrive while we wait
        data = {"entity_id": sorted(ids)}
        try:
            await self.call("light", service, data)
            ok = True
        except Exception as e:
            log.warning("presence lighting: light.%s %s failed: %s", service, ids, e)
            ok = False
            for x in ids:
                self.expect.pop(x, None)
        n = f"{len(ids)} light{'s' if len(ids) != 1 else ''}"
        if service == "turn_on":
            if ok:
                rs["owned"] = sorted(set(rs["owned"]) | set(ids))
                rs["on_at"] = rs["on_at"] or now
                rs.update(fails=0, retry_at=None)
                self._log(name, "on", f"{n} — door opened after dark")
            else:  # not retried: the next door opening may try again after RETRY
                rs["retry_at"] = now + RETRY
                self._log(name, "failed", "Home Assistant error switching the lights on")
        elif ok:
            rs.update(owned=[], on_at=None, fails=0, retry_at=None)
            self._log(name, "off", f"{n} — {mins} min without door activity")
        else:
            rs["fails"] = rs.get("fails", 0) + 1
            if rs["fails"] >= OFF_TRIES:
                rs.update(owned=[], on_at=None, fails=0, retry_at=None)
                self._log(name, "failed", f"Home Assistant error switching the lights off — gave up after {OFF_TRIES} tries")
            else:
                wait = RETRY * 2 ** (rs["fails"] - 1)
                rs["retry_at"] = now + wait
                self._log(name, "failed", f"Home Assistant error switching the lights off — trying again in {wait // 60} min")
        self._save()
        return {"domain": "light", "service": service, "data": data, "ok": ok}

    # ---- API ----
    def status(self, devices: dict, states: dict) -> dict:
        s, now, layout = self.settings(), self.clock(), self.layout()
        ms = lambda t: int(t * 1000) if t else None
        names = {e: d.name for e, d in devices.items()}
        dk = self.dark(states, now)
        away = self.mode().get("mode") == "away"
        cur = lambda e: (states.get(e) or {}).get("state", "unavailable")
        out = []
        for room in layout.get("rooms", []) or []:
            rid = room.get("id")
            cfg = self.room(rid)
            rs = {**_fresh(), **self.state.get(rid, {})}
            sensors, lights = self.resolved(layout, room, cfg, devices)
            q = cfg["quiet_minutes"] * 60
            if not s["enabled"] or not cfg["enabled"]:
                phase = "off"
            elif not sensors or not lights:
                phase = "setup"
            elif away:
                phase = "away"
            elif rs["paused_at"]:
                phase = "paused"
            elif rs["owned"]:
                phase = "on"
            else:
                phase = "ready" if dk["dark"] else "daylight"
            placed = set(placed_in(layout, room, [e for e, d in devices.items() if d.kind == "light"]))
            out.append({"id": rid, "name": room.get("name") or rid, **{k: cfg[k] for k in ("enabled", "quiet_minutes")},
                        "auto_sensors": cfg["sensors"] is None, "auto_lights": cfg["lights"] is None,
                        "sensors": sensors, "lights": lights, "suggested_sensors": room_sensors(layout, room),
                        "room_lights": sorted(placed), "phase": phase, "owned": rs["owned"],
                        "off_at": ms(rs["last"] + q) if phase == "on" and rs["last"] else None,
                        "paused_until": ms(self._quiet_from(rs) + q) if phase == "paused" else None})
        dev = lambda k: [{"entity_id": e, "name": names[e], "state": cur(e)}
                         for e, d in sorted(devices.items(), key=lambda x: x[1].name.lower()) if d.kind == k
                         and (k != "light" or e.startswith("light."))]
        return {"enabled": s["enabled"], "now": ms(now), "mode": "away" if away else "home",
                "dark": {**dk, "until": ms(dk["until"])}, "rooms": out, "all_sensors": dev("sensor"),
                "all_lights": dev("light"), "log": [{**x, "at": ms(x["at"])} for x in self.store.get("plighting_log", []) or []]}


def add_routes(app, lighting: PresenceLighting, devices, ensure_states, live, layout, json_body, wake) -> None:
    """GET /api/presence-lighting, PUT /api/presence-lighting/settings, PUT /api/presence-lighting/rooms/{room_id}."""
    from fastapi import HTTPException, Request

    async def status() -> dict:
        await ensure_states()
        return lighting.status(await devices(), live.states)

    @app.get("/api/presence-lighting")
    async def get_presence_lighting():
        return await status()

    @app.put("/api/presence-lighting/settings")
    async def put_presence_lighting_settings(request: Request):
        try:
            new = validate_settings(await json_body(request))
        except LightingError as e:
            raise HTTPException(400, str(e))
        lighting.put_settings(new)
        wake()
        return await status()

    @app.put("/api/presence-lighting/rooms/{room_id}")
    async def put_presence_lighting_room(room_id: str, request: Request):
        room = next((r for r in layout().get("rooms", []) or [] if r.get("id") == room_id), None)
        if room is None:
            raise HTTPException(404, "unknown room")
        try:
            new = validate_room(await json_body(request), lighting.room(room_id), await devices())
        except LightingError as e:
            raise HTTPException(400, str(e))
        lighting.put_room(room_id, room.get("name") or room_id, new)
        wake()
        return await status()
