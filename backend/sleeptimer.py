"""Sleep timers: "off in 15 / 30 / 60 / n minutes" for a light, a plug, a TV or a whole room, run by the server.

- One timer per target (a device, or a room): setting it again replaces it; cancel any time.
- The end time is absolute and kept in SQLite, so a restart doesn't lose or restart the countdown. A timer that came
  due while the app was down still fires if it is at most LATE_OK late; an older one is dropped (logged as missed):
  hours later somebody may well be using the light again.
- A room timer turns off what is in the room when it is set (lights, plugs, TVs that can be turned off); guests' room
  timers take the room's lights only. Protected plugs (fridge / freezer / home server, keep-on) are never switched off:
  they can't get a timer, aren't part of a room's, and are skipped if they became protected since.
- When it fires: one turn_off per HA domain, only for what is still on (nothing is ever switched on). A failed call is
  tried again after RETRY seconds, at most TRIES times in all, then given up — never a loop of calls.
- The loop wakes at least every TICK seconds; the clock is injectable (tests drive tick() through hours at once).
"""
import asyncio
import json
import logging
import secrets
import sqlite3
import time

from .activity import acting
from .geometry import in_room
from .media import decode, features, is_on
from .roles import LIGHTS, allowed
from .scenes import protected

log = logging.getLogger("homecontrol.sleeptimer")

PRESETS = (15, 30, 60)
MAX_MINUTES = 12 * 60
LATE_OK = 15 * 60     # after a restart, a timer at most this overdue still fires
RETRY = 30            # seconds before a failed switch-off is tried again…
TRIES = 3             # …this many attempts in all
TICK = 1.0
MAX_TIMERS = 50
KINDS = ("light", "plug", "media")
DOMAIN = {"light": "light", "plug": "switch", "media": "media_player"}
BAD = ("unavailable", "unknown")


class TimerError(ValueError):
    pass


class Forbidden(TimerError):
    pass


class TimerStore:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS sleep_timers (id TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def all(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT data FROM sleep_timers").fetchall()
        out = []
        for (raw,) in rows:
            try:
                t = json.loads(raw)
            except ValueError:
                continue
            if isinstance(t, dict) and isinstance(t.get("id"), str) and isinstance(t.get("ends_at"), (int, float)):
                out.append(t)
        return sorted(out, key=lambda t: t["ends_at"])

    def put(self, t: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO sleep_timers (id, data) VALUES (?, ?)", (t["id"], json.dumps(t)))

    def delete(self, tid: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM sleep_timers WHERE id = ?", (tid,)).rowcount > 0


def device_on(kind: str, state: dict | None) -> bool:
    s = (state or {}).get("state")
    return is_on(s) if kind == "media" else s == "on"


class SleepTimers:
    """call(domain, service, data): the logged HA call; devices(): async discovered devices; states(): live raw states;
    layout(): the plan; broadcast(event, data): SSE to browsers; record(kind, **data): the activity log;
    interrupt(reason, ids): disco.interrupt, so a disco lets go of lights being switched off."""

    def __init__(self, store: TimerStore, call, devices, states, layout, broadcast=None, record=None, interrupt=None,
                 clock=time.time):
        self.store, self.call, self.devices, self.states, self.layout = store, call, devices, states, layout
        self.broadcast = broadcast or (lambda *_: None)
        self.record = record or (lambda *_, **__: None)
        self.interrupt = interrupt
        self.clock = clock
        self.last: dict | None = None  # the last timer that fired: {"name", "at", "n", "ok"}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.wake = asyncio.Event()

    # ---------- status ----------
    def public(self, t: dict) -> dict:
        return {k: t.get(k) for k in ("id", "target", "entity_id", "room", "name", "entity_ids", "minutes", "user")} | {
            "ends_at": int(t["ends_at"] * 1000), "created_at": int(t["created_at"] * 1000), "retrying": t.get("tries", 0) > 0}

    def status(self, role: str | None = None) -> dict:
        timers = [self.public(t) for t in self.store.all()]
        if role is not None:
            for t in timers:
                t["may_cancel"] = self.may_control(t["entity_ids"], role)
        return {"now": int(self.clock() * 1000), "timers": timers, "presets": list(PRESETS), "max_minutes": MAX_MINUTES,
                "last": self.last}

    def _publish(self) -> None:
        try:
            self.broadcast("timers", self.status())
        except Exception as e:
            log.warning("sleep timer: broadcast failed: %s", e)

    @staticmethod
    def may_control(ids, role) -> bool:
        return all(allowed(role, LIGHTS, {"entity_id": e}) for e in ids)

    # ---------- targets ----------
    def _switchable(self, d, keep: set[str]) -> str | None:
        """Why this device can't have a sleep timer, or None."""
        if d.kind not in KINDS:
            return f"{d.name} can't have a sleep timer"
        if d.entity_id in keep:
            return f"{d.name} is protected — it is never switched off"
        if d.kind == "media":
            attrs = (self.states().get(d.entity_id) or {}).get("attributes") or {}
            if not decode(features(attrs))["turn_off"]:
                return f"{d.name} can't be turned off through Home Assistant"
        return None

    async def targets(self, body: dict, role: str | None) -> tuple[str, str | None, str, list[str]]:
        """-> (target "entity" / "room", room id, display name, entity ids)."""
        devs, layout = await self.devices(), self.layout()
        keep = protected(layout)
        eid, room = body.get("entity_id"), body.get("room")
        if (eid is None) == (room is None):
            raise TimerError("send entity_id or room")
        if eid is not None:
            d = devs.get(eid) if isinstance(eid, str) else None
            if d is None:
                raise TimerError("unknown device")
            why = self._switchable(d, keep)
            if why:
                raise TimerError(why)
            if not self.may_control([eid], role):
                raise Forbidden("Guests can only set sleep timers for lights.")
            return "entity", None, d.name, [eid]
        r = next((x for x in layout.get("rooms", []) if x.get("id") == room), None) if isinstance(room, str) else None
        if r is None:
            raise TimerError("unknown room")
        placed = [p["entity_id"] for p in layout.get("placements", []) if in_room(r, p["x"], p["y"])]
        placed += [f["media"] for f in layout.get("furniture") or [] if f.get("media") and in_room(r, f["x"], f["y"])]
        ids = []
        for e in dict.fromkeys(placed):
            d = devs.get(e)
            if d is None or d.hidden or self._switchable(d, keep) or not self.may_control([e], role):
                continue
            ids.append(e)
        if not ids:
            raise TimerError(f"Nothing in {r['name']} a sleep timer can switch off")
        return "room", r["id"], r["name"], sorted(ids)

    # ---------- set / cancel ----------
    async def set(self, body, role: str | None = "admin", user: str | None = None) -> dict:
        if not isinstance(body, dict) or set(body) - {"entity_id", "room", "minutes"}:
            raise TimerError("send {entity_id or room, minutes}")
        minutes = body.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= MAX_MINUTES:
            raise TimerError(f"minutes must be a whole number 1–{MAX_MINUTES}")
        target, room, name, ids = await self.targets(body, role)
        key = f"room:{room}" if target == "room" else f"entity:{ids[0]}"
        async with self._lock:
            timers = self.store.all()
            old = next((t for t in timers if t.get("key") == key), None)
            if old is None and len(timers) >= MAX_TIMERS:
                raise TimerError(f"at most {MAX_TIMERS} sleep timers")
            now = self.clock()
            t = {"id": old["id"] if old else "t" + secrets.token_hex(4), "key": key, "target": target,
                 "entity_id": ids[0] if target == "entity" else None, "room": room, "name": name, "entity_ids": ids,
                 "minutes": minutes, "created_at": now, "ends_at": now + minutes * 60, "user": user, "tries": 0}
            self.store.put(t)
        self.record("sleep_timer", event="set", name=name, minutes=minutes, user=user)
        self.wake.set()
        self._publish()
        return self.public(t)

    async def cancel(self, tid: str, role: str | None = "admin", user: str | None = None) -> bool:
        async with self._lock:
            t = next((x for x in self.store.all() if x["id"] == tid), None)
            if t is None:
                return False
            if not self.may_control(t["entity_ids"], role):
                raise Forbidden("You can't control everything this timer switches off.")
            self.store.delete(tid)
        self.record("sleep_timer", event="cancel", name=t["name"], user=user)
        self._publish()
        return True

    # ---------- firing ----------
    async def tick(self) -> int:
        """Fire every timer that is due -> how many fired (or were given up)."""
        now = self.clock()
        due = [t for t in self.store.all() if t["ends_at"] <= now]
        n = 0
        for t in due:
            async with self._lock:
                cur = next((x for x in self.store.all() if x["id"] == t["id"]), None)
                if cur is None or cur["ends_at"] > self.clock():  # cancelled or replaced meanwhile
                    continue
                if await self._fire(cur):
                    n += 1
        if due:
            self._publish()
        return n

    async def _fire(self, t: dict) -> bool:
        """True when the timer is finished with (done, nothing to do, or given up); False when it will try again."""
        devs, layout, states = await self.devices(), self.layout(), self.states()
        keep = protected(layout)
        groups: dict[str, list[str]] = {}
        for e in t["entity_ids"]:
            d = devs.get(e)
            if d is None or d.kind not in KINDS or e in keep or (states.get(e) or {}).get("state") in BAD:
                continue
            if device_on(d.kind, states.get(e)):
                groups.setdefault(DOMAIN[d.kind], []).append(e)
        tries = t.get("tries", 0) + 1
        # Counted before the calls: even a restart mid-call can't make it try more than TRIES times.
        self.store.put({**t, "tries": tries, "ends_at": self.clock() + RETRY})
        failed = []
        if groups and self.interrupt is not None:
            try:
                await self.interrupt("manual", [e for ids in groups.values() for e in ids])
            except Exception as e:
                log.warning("sleep timer: stopping the disco failed: %s", e)
        with acting("Sleep timer"):
            for domain, ids in groups.items():
                try:
                    await self.call(domain, "turn_off", {"entity_id": ids})
                except Exception as e:
                    failed += ids
                    log.warning("sleep timer %s: turning off %s failed (%s/%s): %s", t["name"], ids, tries, TRIES, e)
        n = sum(map(len, groups.values()))
        if failed and tries < TRIES:
            self.store.put({**t, "tries": tries, "ends_at": self.clock() + RETRY, "entity_ids": failed})
            return False
        self.store.delete(t["id"])
        ok = not failed
        self.last = {"id": t["id"], "name": t["name"], "at": int(self.clock() * 1000), "n": n - len(failed), "ok": ok}
        self.record("sleep_timer", event="fired" if ok else "failed", name=t["name"], n=n - len(failed))
        return True

    # ---------- lifecycle ----------
    def recover(self) -> list[dict]:
        """At startup: drop timers that came due too long ago (the app was down); the rest carry on."""
        now, dropped = self.clock(), []
        for t in self.store.all():
            if t["ends_at"] + LATE_OK < now:
                self.store.delete(t["id"])
                dropped.append(t)
                log.info("sleep timer %s missed while the app was down", t["name"])
                self.record("sleep_timer", event="missed", name=t["name"])
        return dropped

    def next_wait(self) -> float:
        timers = self.store.all()
        if not timers:
            return TICK
        return max(0.05, min(TICK, timers[0]["ends_at"] - self.clock()))

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # the loop never dies; the timer stays stored and is retried next tick
                log.warning("sleep timer tick failed: %s", e)
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), self.next_wait())
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        self.recover()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


def add_routes(app, timers: SleepTimers, ensure_states, json_body) -> None:
    """GET /api/timers, POST /api/timers {entity_id | room, minutes}, DELETE /api/timers/{tid}. Guests for lights."""
    from fastapi import HTTPException, Request

    @app.get("/api/timers")
    async def list_timers(request: Request):
        return timers.status(request.state.role)

    @app.post("/api/timers")
    async def set_timer(request: Request):
        body = await json_body(request)
        await ensure_states()
        try:
            return await timers.set(body, request.state.role, request.state.user)
        except Forbidden as e:
            raise HTTPException(403, str(e))
        except TimerError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/timers/{tid}")
    async def cancel_timer(tid: str, request: Request):
        try:
            if not await timers.cancel(tid, request.state.role, request.state.user):
                raise HTTPException(404, "no such timer")
        except Forbidden as e:
            raise HTTPException(403, str(e))
        return {"ok": True}
