"""Automations that act on their own: window open -> radiators down, device health pushes, weekly summary, schedules.

Everything runs in one background loop (every CHECK_EVERY s, sooner after a contact sensor changes). Each
feature has its own switch in the alert settings, state that must survive a restart lives in SQLite, and every
service call is de-duplicated and backed off after failures so nothing ever loops against real devices.
"""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time

from .alerts import parse_time
from .dehumidifier import TankAlert
from .geometry import opening_rooms, placed_in
from .live import build_device
from .presence import Presence
from .quiet import HeldStore, Quiet
from .schedules import ScheduleEngine, ScheduleStore
from .standby import StandbySaver
from .summary import WeeklySummary, local_tz

log = logging.getLogger("homecontrol.automations")
CHECK_EVERY = 15
STARTUP_DELAY = 10      # let Live load HA's states before the first decision
DEBOUNCE = 1.0          # coalesce bursts of sensor events before acting
RETRY_AFTER = 300       # after a failed service call, wait this long before trying that valve again
RESEND_GUARD = 120      # never send the same temperature to the same valve twice within this window
WEEK = 7 * 24 * 3600
BATTERY_LOW, BATTERY_OK = 15, 20  # alert below 15 %, consider it replaced at 20 % (no flapping around 15)
RECOVER_SECS = 120      # "back online" only once a device has stayed available this long
UNAVAILABLE_GRACE = 3600  # a window sensor that drops out keeps the hold this long, then the radiator is released


class AutoStore:
    """Small key/value table for automation state, plus stored weekly summaries."""

    def __init__(self, path: str):
        self.path, self._lock = path, threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS automation_state (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS weekly_summaries (week TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def get(self, key: str, default=None):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT data FROM automation_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO automation_state (key, data) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET data = excluded.data", (key, json.dumps(value)))

    def put_summary(self, week: str, data: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO weekly_summaries (week, data, created) VALUES (?, ?, ?) "
                      "ON CONFLICT(week) DO UPDATE SET data = excluded.data", (week, json.dumps(data), time.time()))

    def latest_summary(self) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT data FROM weekly_summaries ORDER BY created DESC, week DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None


def target_of(states: dict, eid: str) -> float | None:
    s = states.get(eid) or {}
    if s.get("state") in ("unavailable", "unknown", None):
        return None
    t = (s.get("attributes") or {}).get("temperature")
    return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None


def _since(memo: dict, key, raw: dict, now: float) -> float:
    """When the entity entered its current state: HA's last_changed, else when we first saw it like this."""
    t = parse_time(raw.get("last_changed"))
    if t is None:
        t = memo.setdefault(key, now)
    return min(t, now)


# ---------------- window open -> radiators down ----------------
class WindowHeating:
    """Holds radiators at a low target while a window in their room is open, then puts them back.

    holds: valve -> {"restore": target to go back to, "temp": what we sent (None if it was already that low),
    "since": ts, "windows": [opening ids], "pending": send still owed}. Persisted in SQLite.
    """

    def __init__(self, store: AutoStore, settings, call, notify, mode, clock=time.time):
        self.store, self.settings, self.call, self.notify, self.mode, self.clock = store, settings, call, notify, mode, clock
        self.holds: dict[str, dict] = store.get("window_holds", {}) or {}
        self.notified: set[str] = set(store.get("window_notified", []) or [])
        self.first_on: dict = {}
        self.first_gone: dict = {}                        # window sensor -> when it went unavailable
        self.sent: dict[str, tuple[float, float]] = {}    # valve -> (temp, when) of our last command
        self.failed: dict[str, float] = {}                # valve -> when a call last failed

    # ---- used by Away/Home (backend/app.py) ----
    def held(self) -> dict[str, float | None]:
        return {v: h.get("restore") for v, h in self.holds.items()}

    def adopt(self, valve: str, target: float) -> None:
        """Home pressed while the window is open: restore to the home target once it closes."""
        if valve in self.holds:
            self.holds[valve]["restore"] = float(target)
            self._save()

    def _save(self) -> None:
        self.store.put("window_holds", self.holds)
        self.store.put("window_notified", sorted(self.notified))

    async def _send(self, valve: str, temp: float, states: dict) -> bool:
        now = self.clock()
        last = self.sent.get(valve)
        if target_of(states, valve) == temp or (last and last[0] == temp and now - last[1] < RESEND_GUARD):
            return True  # already there / just sent: never the same call twice in a row
        if now - self.failed.get(valve, -1e18) < RETRY_AFTER:
            return False
        try:
            await self.call(valve, temp)
        except Exception as e:
            log.warning("window heating: setting %s to %s failed: %s", valve, temp, e)
            self.failed[valve] = now
            return False
        self.failed.pop(valve, None)
        self.sent[valve] = (temp, now)
        return True

    def _open_since(self, eid: str, states: dict, now: float) -> float | None:
        raw = states.get(eid) or {}
        if raw.get("state") != "on":
            self.first_on.pop(eid, None)
            return None
        return _since(self.first_on, eid, raw, now)

    def _keeps(self, o: dict, states: dict, now: float) -> bool:
        """Does this window still hold its radiators down? Open: yes. Sensor unavailable/unknown/missing: only for
        UNAVAILABLE_GRACE, so a sensor with a flat battery can't leave a room at the window temperature for days."""
        eid = o["entity_id"]
        raw = states.get(eid) or {}
        st = raw.get("state")
        if st == "on":
            self.first_gone.pop(eid, None)
            return True
        if st in ("unavailable", "unknown", None):
            return now - _since(self.first_gone, eid, raw, now) < UNAVAILABLE_GRACE
        self.first_gone.pop(eid, None)
        return False

    async def tick(self, layout: dict, devices: dict, states: dict) -> None:
        s, now = self.settings(), self.clock()
        valves = {e for e, d in devices.items() if d.kind == "valve"}
        windows = [o for o in layout.get("openings", []) if o.get("type") == "window" and o.get("entity_id")]
        info, by_valve = {}, {}
        for o in windows:
            rooms = opening_rooms(layout, o)
            vs = sorted({v for r in rooms for v in placed_in(layout, r, valves)})
            info[o["id"]] = (o, [r["name"] for r in rooms], vs)
            for v in vs:
                by_valve.setdefault(v, []).append(o)
        enabled, changed = s["window_heating_enabled"], False

        for oid in list(self.notified):  # a closed (or removed) window may notify again next time
            o = info.get(oid, (None,))[0]
            if o is None or (states.get(o["entity_id"]) or {}).get("state") == "off":
                self.notified.discard(oid)
                changed = True

        if enabled:
            limit, off = s["window_open_minutes"] * 60, float(s["window_off_temp"])
            for oid, (o, names, vs) in info.items():
                since = self._open_since(o["entity_id"], states, now)
                if since is None or now - since < limit:
                    continue
                for v in vs:
                    if v in self.holds:
                        continue
                    t = target_of(states, v)
                    if t is None:
                        continue  # valve offline / target unknown: try again next tick
                    self.holds[v] = {"restore": t, "temp": off if t > off else None, "since": now,
                                     "windows": [oid], "pending": t > off}
                    changed = True
                    log.info("window %s open: holding %s at %s (was %s)", oid, v, off, t)
                if oid not in self.notified and any(v in self.holds for v in vs):
                    self.notified.add(oid)
                    changed = True
                    if s["window_notify"]:
                        room = " & ".join(names) or "Room"
                        await self.notify({"title": f"{room} window open — radiator{'s' if len(vs) > 1 else ''} off",
                                           "body": f"Heating set to {off:g}° until the window closes.",
                                           "tag": f"window-{oid}", "url": "/"})

        for v, h in list(self.holds.items()):
            if v not in valves and devices:
                del self.holds[v]  # valve gone from HA
                changed = True
                continue
            ws = by_valve.get(v, [])
            for o in ws:
                if o["id"] not in h["windows"] and (states.get(o["entity_id"]) or {}).get("state") == "on":
                    h["windows"].append(o["id"])
                    changed = True
            if enabled and any(self._keeps(o, states, now) for o in ws):
                # still open: owe the low target, or follow the user's own change
                if h.get("pending"):
                    if await self._send(v, h["temp"], states):
                        h["pending"] = False
                        changed = True
                else:
                    t = target_of(states, v)
                    if t is not None and t != h.get("temp") and t != h["restore"]:
                        log.info("window heating: %s changed by hand to %s while held", v, t)
                        h["restore"], h["temp"] = t, None  # user wins; it becomes the restore target
                        changed = True
                continue
            # every window affecting it closed (or the automation was switched off): put it back
            if target_of(states, v) is None:
                continue  # valve offline: restore when it's back
            m = self.mode()
            value = float(m["away_temp"]) if m.get("mode") == "away" else h["restore"]
            if value is not None and not await self._send(v, value, states):
                continue
            log.info("window heating: released %s to %s", v, value)
            del self.holds[v]
            changed = True
        if changed:
            self._save()


# ---------------- device health ----------------
class Health:
    """Low battery and offline pushes, one per problem; sent-markers persisted so restarts don't repeat them."""

    def __init__(self, store: AutoStore, settings, notify, clock=time.time):
        self.store, self.settings, self.notify, self.clock = store, settings, notify, clock
        self.marks: dict[str, dict] = store.get("health", {}) or {}
        self.memo: dict = {}

    async def tick(self, devices: dict, states: dict) -> None:
        s, now, changed = self.settings(), self.clock(), False
        for eid, d in sorted(devices.items()):
            if d.kind == "person":
                continue  # presence, not a device that can go flat or offline
            item, m = build_device(d, states), self.marks.setdefault(eid, {})
            bat, low_flag = item.get("battery"), item.get("battery_low")
            low = low_flag is True or (bat is not None and bat < BATTERY_LOW)
            ok = (bat is not None or low_flag is not None) and low_flag is not True and (bat is None or bat >= BATTERY_OK)
            if low and s["health_battery"] and now - m.get("battery_at", -1e18) >= WEEK:
                m["battery_at"], changed = now, True
                await self.notify({"title": f"Low battery: {d.name}",
                                   "body": f"Battery at {bat} % — replace it soon." if bat is not None else "Battery low — replace it soon.",
                                   "tag": f"battery-{eid}", "url": "/"})
            elif ok and "battery_at" in m:
                del m["battery_at"]
                changed = True

            raw = states.get(eid)
            if raw is None:
                continue
            down = raw.get("state") == "unavailable"
            since = _since(self.memo, (eid, down), raw, now)
            self.memo.pop((eid, not down), None)
            if down and s["health_unavailable"] and "down_at" not in m and now - since >= s["health_unavailable_minutes"] * 60:
                m["down_at"], changed = now, True
                await self.notify({"title": f"{d.name} is offline",
                                   "body": f"Unavailable in Home Assistant for {int((now - since) // 60)} min.",
                                   "tag": f"health-{eid}", "url": "/"})
            elif not down and "down_at" in m and now - since >= RECOVER_SECS:
                del m["down_at"]
                changed = True
                if s["health_unavailable"]:
                    await self.notify({"title": f"{d.name} is back online", "body": "Available again.",
                                       "tag": f"health-{eid}", "url": "/"})
        for eid in [e for e, m in self.marks.items() if not m]:
            del self.marks[eid]
        if changed:
            self.store.put("health", self.marks)


# ---------------- glue ----------------
class Automations:
    def __init__(self, store: AutoStore, settings, pusher, live, ha, layout_store, mode, ensure_devices=None,
                 clock=time.time, tz=None):
        self.store, self.settings, self.pusher, self.live, self.ha = store, settings, pusher, live, ha
        self.layout_store, self.ensure_devices = layout_store, ensure_devices
        self.lock = asyncio.Lock()  # Away/Home take it too, so they never interleave with a window tick
        tz = tz or local_tz()
        self.clock = clock
        # automation pushes go through quiet hours; door alerts (alerts.py) and the test push don't
        self.quiet = Quiet(HeldStore(store.path), settings, pusher, clock, tz)
        self.window = WindowHeating(store, settings, self._set_temp, lambda p: self._notify(p, "window"), mode, clock)
        self.health = Health(store, settings, lambda p: self._notify(p, "health"), clock)
        self.tank = TankAlert(store, settings, lambda p: self._notify(p, "health"))
        self.summary = WeeklySummary(store, settings, ha, lambda: self.live.devices, layout_store.get,
                                     lambda p: self._notify(p, "summary"), clock, tz)
        self.schedules = ScheduleEngine(ScheduleStore(store.path), ha.call_service, layout_store.get, mode, self.window, clock, tz)
        self.presence = Presence(store, lambda p: self._notify(p, "presence"), mode, clock, tz)  # Auto Away
        self.standby = StandbySaver(store, ha.call_service, ha, layout_store.get, mode, clock, tz)
        self.wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        live.add_observer(self.on_device)

    async def _set_temp(self, valve: str, temp: float) -> None:
        await self.ha.call_service("climate", "set_temperature", {"entity_id": valve, "temperature": temp})

    async def _notify(self, payload: dict, category: str) -> None:
        await self.quiet.notify(payload, category)

    def on_device(self, item: dict, raw: dict | None) -> None:
        if item.get("kind") == "plug":
            self.standby.observe(item)
        if item.get("kind") in ("sensor", "person") or self.tank.observe(item):
            self.wake.set()

    async def tick(self) -> None:
        if not self.live.devices and self.ensure_devices:
            try:
                await self.ensure_devices()
            except Exception as e:
                log.warning("automations: device discovery failed: %s", e)
        devices, states = self.live.devices, self.live.states
        if devices and states:  # nothing is decided before HA's states are known
            for name, step in (("schedules", lambda: self._schedules(devices, states)),
                               ("window heating", lambda: self._window(devices, states)),
                               ("auto away", lambda: self._locked(self.presence.tick(devices, states))),
                               ("standby saver", lambda: self._locked(self.standby.tick(devices, states))),
                               ("device health", lambda: self.health.tick(devices, states)),
                               ("dehumidifier tank", lambda: self.tank.tick(
                                   [build_device(d, states) for d in devices.values() if d.kind == "dehumidifier"]))):
                try:
                    await step()
                except Exception as e:
                    log.warning("%s failed: %s", name, e)
        try:
            await self.summary.tick()
        except Exception as e:
            log.warning("weekly summary failed: %s", e)
        try:
            await self.quiet.tick()
        except Exception as e:
            log.warning("quiet hours digest failed: %s", e)

    async def _schedules(self, devices, states) -> None:
        async with self.lock:
            await self.schedules.tick(devices, states)
    async def _locked(self, coro):
        async with self.lock:
            return await coro

    async def _window(self, devices, states) -> None:
        async with self.lock:
            await self.window.tick(self.layout_store.get(), devices, states)

    def status(self) -> dict:
        layout = self.layout_store.get()
        windows = [o for o in layout.get("openings", []) if o.get("type") == "window" and o.get("entity_id")]
        names = {e: d.name for e, d in self.live.devices.items()}
        return {"windows_linked": len(windows),
                "held": [{"entity_id": v, "name": names.get(v, v), "restore": h.get("restore"), "since": h.get("since")}
                         for v, h in sorted(self.window.holds.items())]}

    async def _run(self, every: float) -> None:
        await asyncio.sleep(STARTUP_DELAY)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("automations check failed: %s", e)
            wait = every
            try:  # wake up right when the next schedule / Auto Away / standby saver step is due
                due = min((t for t in (self.schedules.next_due(), self.presence.next_due(), self.standby.next_due())
                           if t is not None), default=None)
                if due is not None:
                    wait = min(every, max(0.5, due - self.clock() + 0.1))
            except Exception as e:
                log.warning("schedules: next run unknown: %s", e)
            try:
                await asyncio.wait_for(self.wake.wait(), wait)
                await asyncio.sleep(DEBOUNCE)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    def start(self, every: float = CHECK_EVERY) -> None:
        self._task = asyncio.create_task(self._run(every))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
