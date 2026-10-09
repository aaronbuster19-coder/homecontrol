"""Disco mode: colour lights cycle through party colours, run by the server (it keeps going while the phone sleeps).

One disco at a time, for the lights it was started with (a room's or the whole home's colour lights). Safety, since
these are real bulbs on a real network:
- at most one colour change per light per MIN_GAP seconds, and one light.turn_on per distinct colour per step;
- a hard stop after the chosen time (default 30 min, at most MAX_MINUTES);
- stops on All off, Away (by hand or Auto Away), or when anyone changes a light taking part (this app, the HA app, a
  wall switch); the light changed by hand keeps its new state;
- lights that were off take part only when picked by name (entity_ids); a room / whole home start takes the ones on;
- on stop every light goes back to what it was (on/off, brightness, colour or white temperature), in grouped calls;
- HA errors back off (doubling) and MAX_FAILS in a row stop the disco (and restore);
- the snapshot is kept in SQLite, so lights are restored after a restart mid-disco too;
- "Party flash" needs the photosensitivity warning acknowledged (warning_ok) by the caller.

Only the first step (which may switch lights on) and the restore are written to the activity log; the colour steps
are not, or they would flood it.
"""
import asyncio
import json
import logging
import sqlite3
import time

from .activity import acting
from .geometry import in_room
from .live import light_caps
from .roles import LIGHTS, allowed

log = logging.getLogger("homecontrol.disco")

MIN_GAP = 1.0            # seconds between two colour changes of one light, whatever the preset
DEFAULT_MINUTES = 30
MAX_MINUTES = 120
MAX_FAILS = 3            # HA errors in a row before the disco stops
BACKOFF_MAX = 30.0
ON_BRIGHTNESS = 80       # % for picked lights that were off
GRACE = 2.0              # state echoes this soon after the start are not taken as a change by hand
HUE_TOL, SAT_TOL, BRI_TOL = 12.0, 15.0, 10  # a reported colour this close to one sent recently is ours
RECENT = 6               # colours per light remembered for that comparison (HA reports and polls lag a few steps)
RESTORE_STALE = 600      # after a restart, a snapshot older than its end + this is dropped (lights changed since)
BAD = ("unavailable", "unknown")
SPEEDS = ("slow", "normal", "fast")
PARTY = [(0, 100), (220, 100), (120, 100), (300, 100), (50, 100), (180, 100)]
PRESETS = {
    "rainbow": {"name": "Rainbow fade", "interval": {"slow": 4.0, "normal": 2.0, "fast": 1.0}, "warning": False,
                "about": "All lights glide through the rainbow together."},
    "flash": {"name": "Party flash", "interval": {"slow": 2.0, "normal": 1.5, "fast": 1.0}, "warning": True,
              "about": "Sudden jumps between bright party colours."},
    "chill": {"name": "Slow chill", "interval": {"slow": 12.0, "normal": 8.0, "fast": 5.0}, "warning": False,
              "about": "Soft colours drifting slowly, each light its own."},
}
REASONS = {"user": "Stopped", "timeout": "Time's up", "manual": "A light was changed by hand", "all_off": "All off",
           "away": "Away mode", "errors": "Home Assistant kept failing", "restart": "The app restarted"}


class DiscoError(ValueError):
    pass


class Forbidden(DiscoError):
    pass


def colours(preset: str, step: int, n: int) -> list[tuple[float, float]]:
    """The colour (hue, saturation) of each of n lights at a step."""
    if preset == "flash":
        return [PARTY[step % len(PARTY)]] * n
    if preset == "chill":
        return [(round((step * 15 + i * 360 / n) % 360, 1), 70) for i in range(n)]
    return [((step * 24) % 360, 100)] * n


def snapshot(state: dict | None) -> dict:
    """What a light is now, to put back afterwards."""
    s = state or {}
    a = s.get("attributes") or {}
    return {"state": s.get("state", "unknown"), **{k: a.get(k) for k in
            ("brightness", "color_mode", "hs_color", "rgb_color", "xy_color", "color_temp_kelvin")}}


def restore_calls(saved: dict[str, dict]) -> list[tuple[str, dict]]:
    """light.turn_on / turn_off calls that put lights back, one per identical payload."""
    groups: dict[tuple[str, str], list[str]] = {}
    for eid in sorted(saved):
        s = saved[eid]
        if s["state"] in BAD:
            continue  # nothing known to go back to
        if s["state"] != "on":
            groups.setdefault(("turn_off", "{}"), []).append(eid)
            continue
        data: dict = {}
        if isinstance(s.get("brightness"), (int, float)):
            data["brightness"] = int(s["brightness"])
        mode = s.get("color_mode")
        if mode == "color_temp" and s.get("color_temp_kelvin"):
            data["color_temp_kelvin"] = int(s["color_temp_kelvin"])
        elif mode == "xy" and s.get("xy_color"):
            data["xy_color"] = list(s["xy_color"])
        elif mode in ("rgb", "rgbw", "rgbww") and s.get("rgb_color"):
            data["rgb_color"] = list(s["rgb_color"])
        elif s.get("hs_color"):
            data["hs_color"] = list(s["hs_color"])
        elif s.get("color_temp_kelvin"):
            data["color_temp_kelvin"] = int(s["color_temp_kelvin"])
        groups.setdefault(("turn_on", json.dumps(data, sort_keys=True)), []).append(eid)
    return [(svc, {"entity_id": ids, **json.loads(key)}) for (svc, key), ids in groups.items()]


def hue_close(a: float, b: float) -> bool:
    d = abs(a - b) % 360
    return min(d, 360 - d) <= HUE_TOL


class DiscoStore:
    """The snapshot of a running disco (id = 1), so a restart can still restore the lights."""

    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS disco (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def get(self) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT data FROM disco WHERE id = 1").fetchone()
        try:
            return json.loads(row[0]) if row else None
        except ValueError:
            return None

    def put(self, data: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO disco (id, data) VALUES (1, ?)", (json.dumps(data),))

    def clear(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM disco WHERE id = 1")


class Run:
    def __init__(self, preset: str, speed: str, ids: list[str], saved: dict, turn_on: list[str], user: str | None,
                 started: float, minutes: int):
        self.preset, self.speed, self.ids, self.saved, self.turn_on = preset, speed, ids, saved, turn_on
        self.user, self.started, self.ends = user, started, started + minutes * 60
        self.interval = PRESETS[preset]["interval"][speed]
        self.step = 0
        self.fails = 0
        self.halted = False                     # set at once by a hand change, before the stop task runs
        self.last_sent: dict[str, float] = {}   # entity id -> clock of its last colour change
        self.recent: dict[str, list] = {e: [] for e in ids}  # colours sent lately, newest last
        self.brightness: dict[str, int | None] = {e: (saved[e].get("brightness") if e not in turn_on else
                                                      round(ON_BRIGHTNESS * 2.55)) for e in ids}


class Disco:
    """call(domain, service, data): the logged service call (activity log); raw_call: the same without logging.
    states(): live raw HA states by entity id; devices(): async discovered devices; layout(): the plan.
    clock/sleep are injectable (tests run whole discos in no time)."""

    def __init__(self, store: DiscoStore, call, raw_call, states, devices, layout, broadcast=None, record=None,
                 clock=time.time, sleep=asyncio.sleep):
        self.store, self.call, self.raw_call, self.states, self.devices, self.layout = store, call, raw_call, states, devices, layout
        self.broadcast, self.record = broadcast or (lambda *_: None), record or (lambda *_, **__: None)
        self.clock, self.sleep = clock, sleep
        self.run: Run | None = None
        self.last: dict | None = None  # {"reason", "text", "at"} of the last stop
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()

    # ---------- status ----------
    def status(self) -> dict:
        r = self.run
        out = {"running": r is not None, "now": int(self.clock() * 1000), "last": self.last,
               "presets": [{"id": k, "name": p["name"], "about": p["about"], "warning": p["warning"]} for k, p in PRESETS.items()],
               "speeds": list(SPEEDS), "default_minutes": DEFAULT_MINUTES, "max_minutes": MAX_MINUTES}
        if r:
            out.update(preset=r.preset, preset_name=PRESETS[r.preset]["name"], speed=r.speed, entity_ids=r.ids,
                       turned_on=r.turn_on, started_by=r.user, started_at=int(r.started * 1000), ends_at=int(r.ends * 1000))
        return out

    def _publish(self) -> None:
        try:
            self.broadcast("disco", self.status())
        except Exception as e:
            log.warning("disco: broadcast failed: %s", e)

    # ---------- choosing lights ----------
    async def participants(self, body: dict, role: str | None) -> tuple[list[str], list[str]]:
        """(entity ids taking part, those of them to switch on). Raises DiscoError / Forbidden."""
        devs = await self.devices()
        states = self.states()

        def colour(eid: str) -> bool:
            d = devs.get(eid)
            s = states.get(eid) or {}
            return bool(d and d.kind == "light" and light_caps(s.get("attributes") or {})["supports_color"])
        picked = body.get("entity_ids")
        if picked is not None:
            if not isinstance(picked, list) or not all(isinstance(e, str) for e in picked):
                raise DiscoError("entity_ids must be a list of light ids")
            ids = list(dict.fromkeys(picked))
            for e in ids:
                if not colour(e):
                    raise DiscoError(f"not a colour light: {e}")
                if (states.get(e) or {}).get("state") in BAD:
                    raise DiscoError(f"{devs[e].name} is unavailable")
            on = [e for e in ids if (states.get(e) or {}).get("state") != "on"]  # picked by name: switched on
        else:
            room = body.get("room")
            pool = [e for e, d in devs.items() if d.kind == "light" and not d.hidden and colour(e)]
            if room is not None:
                r = next((x for x in self.layout().get("rooms", []) if x.get("id") == room), None)
                if r is None:
                    raise DiscoError("unknown room")
                placed = {p["entity_id"] for p in self.layout().get("placements", []) if in_room(r, p["x"], p["y"])}
                pool = [e for e in pool if e in placed]
            ids, on = sorted(e for e in pool if (states.get(e) or {}).get("state") == "on"), []
            if not ids:
                where = f" in {r['name']}" if room is not None else ""
                raise DiscoError(f"No colour lights are on{where} — pick the lights to use")
        if not ids:
            raise DiscoError("pick at least one light")
        if any(not allowed(role, LIGHTS, {"entity_id": e}) for e in ids):
            raise Forbidden("You can't control all of those lights.")
        return ids, on

    def may_stop(self, role: str | None) -> bool:
        return self.run is None or all(allowed(role, LIGHTS, {"entity_id": e}) for e in self.run.ids)

    # ---------- start / stop ----------
    async def start(self, body, role: str | None = "admin", user: str | None = None) -> dict:
        if not isinstance(body, dict):
            raise DiscoError("send {preset, speed, entity_ids or room, minutes}")
        unknown = set(body) - {"preset", "speed", "entity_ids", "room", "minutes", "warning_ok"}
        if unknown:
            raise DiscoError(f"unknown field {sorted(unknown)[0]!r}")
        preset, speed = body.get("preset", "rainbow"), body.get("speed", "normal")
        if preset not in PRESETS:
            raise DiscoError(f"preset must be one of {', '.join(PRESETS)}")
        if speed not in SPEEDS:
            raise DiscoError(f"speed must be one of {', '.join(SPEEDS)}")
        minutes = body.get("minutes", DEFAULT_MINUTES)
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= MAX_MINUTES:
            raise DiscoError(f"minutes must be a whole number 1–{MAX_MINUTES}")
        if PRESETS[preset]["warning"] and body.get("warning_ok") is not True:
            raise DiscoError("Party flash has fast flashing lights: confirm the photosensitivity warning first")
        ids, turn_on = await self.participants(body, role)
        if self.run and not self.may_stop(role):
            raise Forbidden("A disco you can't stop is running.")
        # A new disco replaces the old one: its other lights go back now; shared ones keep their real snapshot (the
        # live state shows disco colours), and one the old disco switched on still goes back to off at the end.
        before = dict(self.run.saved) if self.run else {}
        await self.stop("user", keep=set(ids) & set(before))
        states = self.states()
        async with self._lock:
            saved = {e: before.get(e) or snapshot(states.get(e)) for e in ids}
            if body.get("entity_ids") is not None:
                turn_on = [e for e in ids if saved[e]["state"] != "on"]
            r = Run(preset, speed, ids, saved, turn_on, user, self.clock(), minutes)
            self.store.put({"saved": saved, "ends": r.ends, "started": r.started})
            self.run, self.last = r, None
            try:  # the first step right away: it may switch lights on, and an HA error shows to whoever started it
                await self.step(r, r.started)
            except Exception:
                self.run = None
                self.last = {"reason": "errors", "text": REASONS["errors"], "at": int(self.clock() * 1000)}
                if await self._restore(saved):
                    self.store.clear()
                raise
            self._task = asyncio.create_task(self._loop(r))
        self.record("disco", event="start", preset=preset, n=len(ids), user=user)
        self._publish()
        return self.status()

    async def stop(self, reason: str = "user", keep: set[str] | frozenset = frozenset()) -> bool:
        """Stop and put the lights back, except those in keep (changed by hand / being switched off anyway)."""
        async with self._lock:
            r = self.run
            if r is None:
                return False
            r.halted = True
            self.run = None
            task, self._task = self._task, None
            if task and task is not asyncio.current_task():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            self.last = {"reason": reason, "text": REASONS.get(reason, reason), "at": int(self.clock() * 1000)}
            if await self._restore({e: s for e, s in r.saved.items() if e not in keep}):
                self.store.clear()  # else kept: the next start of the app tries again (recover)
        self.record("disco", event="stop", reason=reason, user=r.user if reason == "user" else None)
        self._publish()
        return True

    async def interrupt(self, reason: str, entity_ids) -> None:
        """Before the app changes lights itself (All off, Away, a tap on a light): stop if one of them takes part.
        Lights in entity_ids are left to that change; the others go back."""
        r = self.run
        ids = set(entity_ids)
        if r is None or (reason == "manual" and not ids & set(r.ids)):
            return
        r.halted = True
        await self.stop(reason, keep=ids)

    async def _restore(self, saved: dict) -> bool:
        ok = True
        with acting("Disco"):
            for service, data in restore_calls(saved):
                try:
                    await self.call("light", service, data)
                except Exception as e:  # one failed group doesn't keep the others in disco colours
                    ok = False
                    log.warning("disco: restoring %s failed: %s", data.get("entity_id"), e)
        return ok

    # ---------- the loop ----------
    async def _loop(self, r: Run) -> None:
        try:
            t0, wait = r.started, r.interval  # start() made the first step
            while not r.halted:
                await self.sleep(max(MIN_GAP / 10, min(t0 + wait - self.clock(), r.ends - self.clock())))
                t0 = self.clock()
                if r.halted or t0 >= r.ends:
                    break
                try:
                    await self.step(r, t0)
                    r.fails, wait = 0, r.interval
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    r.fails += 1
                    log.warning("disco: step failed (%s/%s): %s", r.fails, MAX_FAILS, e)
                    if r.fails >= MAX_FAILS:
                        self._spawn(self.stop("errors"))
                        return
                    wait = min(BACKOFF_MAX, r.interval * 2 ** r.fails)
            if not r.halted:
                self._spawn(self.stop("timeout"))
        except asyncio.CancelledError:
            pass

    async def step(self, r: Run, now: float) -> int:
        """One step: one light.turn_on per colour. Lights changed under MIN_GAP ago sit this step out. -> calls made."""
        groups: dict[str, list[str]] = {}
        payload: dict[str, dict] = {}
        first = r.step == 0
        for eid, (h, s) in zip(r.ids, colours(r.preset, r.step, len(r.ids))):
            if now - r.last_sent.get(eid, -1e9) < MIN_GAP - 1e-6:
                continue
            data = {"hs_color": [h, s]}
            if first and eid in r.turn_on:
                data["brightness_pct"] = ON_BRIGHTNESS
            key = json.dumps(data, sort_keys=True)
            groups.setdefault(key, []).append(eid)
            payload[key] = data
        r.step += 1
        calls = 0
        for key, ids in groups.items():
            if r.halted:
                break
            for e in ids:
                r.last_sent[e] = now
                r.recent[e] = (r.recent[e] + [tuple(payload[key]["hs_color"])])[-RECENT:]
            data = {"entity_id": ids, **payload[key]}
            if first:  # logged: it may switch lights on, and the timeline should say it was the disco
                with acting("Disco"):
                    await self.call("light", "turn_on", data)
            else:
                await self.raw_call("light", "turn_on", data)
            calls += 1
        return calls

    # ---------- changes by hand ----------
    def by_hand(self, item: dict) -> bool:
        """Is this live update of a light taking part a change somebody else made?"""
        r = self.run
        eid = item.get("entity_id")
        if r is None or r.halted or eid not in r.recent:
            return False
        state = item.get("state")
        if state in BAD:
            return False
        if state != "on":
            return True  # the disco never switches a light off
        if not r.recent[eid] or self.clock() - r.started < GRACE:  # colour / brightness echoes of the start
            return False  # nothing sent to it yet
        if item.get("color_mode") == "color_temp":
            return True
        b, want = item.get("brightness"), r.brightness.get(eid)
        if isinstance(b, (int, float)) and isinstance(want, (int, float)) and abs(b - want) > BRI_TOL:
            return True
        hs = item.get("hs_color")
        if isinstance(hs, (list, tuple)) and len(hs) == 2 and all(isinstance(v, (int, float)) for v in hs):
            return not any(hue_close(hs[0], h) and abs(hs[1] - s) <= SAT_TOL for h, s in r.recent[eid])
        return False

    def observe(self, item: dict, raw=None) -> None:
        """Live observer (backend/live.py): a participating light changed by someone else stops the disco."""
        if item.get("kind") == "light" and self.by_hand(item):
            self.run.halted = True
            log.info("disco: %s changed by hand, stopping", item.get("entity_id"))
            self._spawn(self.stop("manual", keep={item["entity_id"]}))

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ---------- app lifecycle ----------
    async def recover(self) -> None:
        """At startup: a disco that was running when the app stopped -> put its lights back (if still recent)."""
        data = self.store.get()
        if not data:
            return
        if not isinstance(data.get("saved"), dict) or self.clock() > float(data.get("ends") or 0) + RESTORE_STALE:
            self.store.clear()
            return
        for attempt in range(3):
            try:
                with acting("Disco"):
                    for service, payload in restore_calls(data["saved"]):
                        await self.call("light", service, payload)
                break
            except Exception as e:
                log.warning("disco: restoring after restart failed (%s): %s", attempt + 1, e)
                await self.sleep(10 * (attempt + 1))
        self.store.clear()
        self.last = {"reason": "restart", "text": REASONS["restart"], "at": int(self.clock() * 1000)}

    def start_background(self) -> None:
        self._spawn(self.recover())

    async def shutdown(self) -> None:
        try:
            await asyncio.wait_for(self.stop("restart"), 5)
        except Exception as e:  # the snapshot stays in SQLite; recover() restores at the next start
            log.warning("disco: stop at shutdown failed: %s", e)
        for t in list(self._bg):
            t.cancel()


def add_routes(app, disco: Disco, ensure_states, json_body) -> None:
    """GET /api/disco, POST /api/disco/start, POST /api/disco/stop (guests too, for the lights they may control)."""
    from fastapi import HTTPException, Request

    @app.get("/api/disco")
    async def disco_status():
        return disco.status()

    @app.post("/api/disco/start")
    async def disco_start(request: Request):
        body = await json_body(request)
        await ensure_states()
        try:
            return await disco.start(body, request.state.role, request.state.user)
        except Forbidden as e:
            raise HTTPException(403, str(e))
        except DiscoError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/disco/stop")
    async def disco_stop(request: Request):
        if not disco.may_stop(request.state.role):
            raise HTTPException(403, "You can't control all of the disco's lights.")
        stopped = await disco.stop("user")
        return {**disco.status(), "stopped": stopped}
