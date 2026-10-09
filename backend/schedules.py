"""Schedules run by the app (not written into Home Assistant): "Weekdays 06:30 bedroom lights 40 %", "sunset −15 min
lounge lamps on", "Mon–Fri 06:00 radiators 20°".

Safety rules, because these switch real devices:
- every occurrence fires at most once: (schedule, local date, revision) is written to SQLite *before* the call;
- nothing missed is replayed: an occurrence fires only up to GRACE seconds late (e.g. after a restart), and never
  if it lies before the moment the schedule (or the master switch) was last switched on or edited;
- one service call per action: everything due in one tick is merged per domain/service/value, one call each, and a
  device named by two due schedules gets only the last one's action; a failed call is not retried;
- radiators: skipped while Away; a radiator held low by an open window is not touched, window heating restores it
  to the schedule's temperature when the window closes.
"""
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, time as dtime, timedelta

from .geometry import placed_in
from .sun import sun_event

log = logging.getLogger("homecontrol.schedules")
GRACE = 120              # fire up to 2 min late, never later
MAX_SCHEDULES = 50
MAX_ENTITIES = 50
MAX_OFFSET = 180         # sunrise/sunset ± up to 3 h
TEMP = (5, 35)
DEFAULT_SETTINGS = {"enabled": True, "lat": 51.5074, "lon": -0.1278}   # London
ACTIONS = {"on": ("light", "plug"), "off": ("light", "plug"), "brightness": ("light",), "temperature": ("valve",)}
HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class ScheduleError(ValueError):
    pass


def _num(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


# ---------------- validation ----------------
def validate_schedule(data, devices: dict, layout: dict, old: dict | None = None) -> dict:
    """devices: entity_id -> Device (kind). Entities already in `old` stay valid while HA is briefly missing them."""
    if not isinstance(data, dict):
        raise ScheduleError("schedule must be an object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ScheduleError("name required")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ScheduleError("enabled must be true or false")

    days = data.get("days")
    if not isinstance(days, list) or not days or not all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days):
        raise ScheduleError("days must be a non-empty list of 0 (Mon) – 6 (Sun)")

    t = data.get("time")
    if not isinstance(t, dict) or t.get("type") not in ("fixed", "sunrise", "sunset"):
        raise ScheduleError("time.type must be fixed, sunrise or sunset")
    if t["type"] == "fixed":
        if not isinstance(t.get("at"), str) or not HHMM.match(t["at"]):
            raise ScheduleError("time.at must be HH:MM")
        tm = {"type": "fixed", "at": t["at"]}
    else:
        off = t.get("offset", 0)
        if isinstance(off, bool) or not isinstance(off, int) or not -MAX_OFFSET <= off <= MAX_OFFSET:
            raise ScheduleError(f"time.offset must be whole minutes from -{MAX_OFFSET} to {MAX_OFFSET}")
        tm = {"type": t["type"], "offset": off}

    a = data.get("action")
    if not isinstance(a, dict) or a.get("type") not in ACTIONS:
        raise ScheduleError("action.type must be on, off, brightness or temperature")
    act = {"type": a["type"]}
    if a["type"] == "brightness":
        v = a.get("value")
        if not _num(v) or not 1 <= v <= 100:
            raise ScheduleError("brightness must be 1–100 %")
        act["value"] = round(v)
    elif a["type"] == "temperature":
        v = a.get("value")
        if not _num(v) or not TEMP[0] <= v <= TEMP[1]:
            raise ScheduleError(f"temperature must be {TEMP[0]}–{TEMP[1]}")
        act["value"] = round(float(v) * 2) / 2
    kinds = ACTIONS[a["type"]]

    tg = data.get("target")
    if not isinstance(tg, dict):
        raise ScheduleError("target required")
    if "room" in tg:
        if "light" not in kinds:
            raise ScheduleError("a room target switches the room's lights: use on, off or brightness")
        if not any(r.get("id") == tg["room"] for r in layout.get("rooms", [])):
            raise ScheduleError("unknown room")
        target = {"room": tg["room"]}
    else:
        ids = tg.get("entity_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(e, str) for e in ids):
            raise ScheduleError("target.entity_ids must list at least one device")
        ids = list(dict.fromkeys(ids))
        if len(ids) > MAX_ENTITIES:
            raise ScheduleError(f"at most {MAX_ENTITIES} devices per schedule")
        before = set((old or {}).get("target", {}).get("entity_ids", []))
        for e in ids:
            d = devices.get(e)
            if d is None and e not in before:
                raise ScheduleError(f"unknown device {e}")
            if d is not None and d.kind not in kinds:
                raise ScheduleError(f"{e} is a {d.kind}: can't {a['type']}")
        target = {"entity_ids": ids}
    return {"name": name.strip()[:60], "enabled": enabled, "days": sorted(set(days)), "time": tm,
            "action": act, "target": target}


def validate_settings(data, current: dict) -> dict:
    if not isinstance(data, dict):
        raise ScheduleError("settings must be an object")
    out = dict(current)
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise ScheduleError("enabled must be true or false")
        out["enabled"] = data["enabled"]
    for k, lim in (("lat", 90), ("lon", 180)):
        if k in data:
            if not _num(data[k]) or not -lim <= data[k] <= lim:
                raise ScheduleError(f"{k} must be between -{lim} and {lim}")
            out[k] = round(float(data[k]), 4)
    return out


# ---------------- timing ----------------
def occurrence(s: dict, d: date, tz, lat: float, lon: float) -> float | None:
    """When the schedule runs on local date d (epoch seconds), or None (not one of its days / no sunrise).
    Fixed times are wall-clock in tz: 06:30 is 06:30 in GMT and BST. A time skipped by the spring-forward gap
    (01:30 on the last Sunday of March in London) runs an hour later on the wall clock, 02:30 BST; a time that
    happens twice in autumn runs at the first one, once."""
    if d.weekday() not in s["days"]:
        return None
    t = s["time"]
    if t["type"] == "fixed":
        h, m = map(int, t["at"].split(":"))
        return datetime.combine(d, dtime(h, m), tzinfo=tz).timestamp()
    ev = sun_event(d, t["type"], lat, lon)
    return None if ev is None else ev + t["offset"] * 60


def next_run(s: dict, now: float, tz, lat: float, lon: float) -> float | None:
    today = datetime.fromtimestamp(now, tz).date()
    for i in range(-1, 9):
        ts = occurrence(s, today + timedelta(days=i), tz, lat, lon)
        if ts is not None and ts > now:
            return ts
    return None


def describe_time(t: dict) -> str:
    if t["type"] == "fixed":
        return t["at"]
    o = t["offset"]
    return t["type"] + (f" {'+' if o > 0 else '−'}{abs(o)} min" if o else "")


# ---------------- storage ----------------
class ScheduleStore:
    def __init__(self, path: str):
        self.path, self._lock = path, threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS schedules (id TEXT PRIMARY KEY, pos INTEGER NOT NULL, data TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS schedule_runs (sid TEXT NOT NULL, day TEXT NOT NULL, rev INTEGER NOT NULL, "
                      "at REAL NOT NULL, status TEXT NOT NULL, PRIMARY KEY (sid, day))")
            c.execute("CREATE TABLE IF NOT EXISTS schedule_settings (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def settings(self) -> dict:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT data FROM schedule_settings WHERE id = 1").fetchone()
        return {**DEFAULT_SETTINGS, **(json.loads(row[0]) if row else {})}

    def put_settings(self, s: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO schedule_settings (id, data) VALUES (1, ?) "
                      "ON CONFLICT(id) DO UPDATE SET data = excluded.data", (json.dumps(s),))

    def all(self) -> list[dict]:
        with self._lock, self._conn() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT data FROM schedules ORDER BY pos, id")]

    def get(self, sid: str) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT data FROM schedules WHERE id = ?", (sid,)).fetchone()
        return json.loads(row[0]) if row else None

    def count(self) -> int:
        with self._lock, self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]

    def put(self, s: dict) -> None:
        with self._lock, self._conn() as c:
            pos = c.execute("SELECT pos FROM schedules WHERE id = ?", (s["id"],)).fetchone()
            if pos is None:
                pos = c.execute("SELECT COALESCE(MAX(pos), 0) + 1 FROM schedules").fetchone()
            c.execute("INSERT INTO schedules (id, pos, data) VALUES (?, ?, ?) "
                      "ON CONFLICT(id) DO UPDATE SET data = excluded.data", (s["id"], pos[0], json.dumps(s)))

    def delete(self, sid: str) -> bool:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM schedule_runs WHERE sid = ?", (sid,))
            return c.execute("DELETE FROM schedules WHERE id = ?", (sid,)).rowcount > 0

    # runs: one row per schedule and local date; rev says which version of the schedule fired
    def run(self, sid: str, day: str) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT rev, at, status FROM schedule_runs WHERE sid = ? AND day = ?", (sid, day)).fetchone()
        return {"rev": row[0], "at": row[1], "status": row[2]} if row else None

    def put_run(self, sid: str, day: str, rev: int, at: float, status: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO schedule_runs (sid, day, rev, at, status) VALUES (?, ?, ?, ?, ?) "
                      "ON CONFLICT(sid, day) DO UPDATE SET rev = excluded.rev, at = excluded.at, status = excluded.status",
                      (sid, day, rev, at, status))

    def last_runs(self) -> dict[str, dict]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT sid, at, status FROM schedule_runs r WHERE at = "
                             "(SELECT MAX(at) FROM schedule_runs WHERE sid = r.sid)").fetchall()
        return {sid: {"at": at, "status": st} for sid, at, st in rows}

    def prune_runs(self, before_day: str) -> None:
        """Keep the newest row per schedule (for "Last ran") and anything recent; drop the rest."""
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM schedule_runs WHERE day < ? AND at < (SELECT MAX(at) FROM schedule_runs x "
                      "WHERE x.sid = schedule_runs.sid)", (before_day,))


def new_id() -> str:
    return secrets.token_hex(6)


# ---------------- engine ----------------
def _plural(n: int, w: str) -> str:
    return f"{n} {w}{'' if n == 1 else 's'}"


class ScheduleEngine:
    """Called from the automations loop: tick() fires what is due now."""

    def __init__(self, store: ScheduleStore, call, layout, mode, window, clock=time.time, tz=None):
        self.store, self.call, self.layout, self.mode, self.window = store, call, layout, mode, window
        self.clock, self.tz = clock, tz
        self._pruned = None

    # ---- API helpers (backend/app.py) ----
    def listing(self) -> dict:
        st, now = self.store.settings(), self.clock()
        last = self.store.last_runs()
        items = []
        for s in self.store.all():
            nxt = next_run(s, now, self.tz, st["lat"], st["lon"]) if s["enabled"] and st["enabled"] else None
            lr = last.get(s["id"])
            items.append({**{k: v for k, v in s.items() if k not in ("rev", "armed_at")},
                          "next": int(nxt * 1000) if nxt else None,
                          "last": {"at": int(lr["at"] * 1000), "status": lr["status"]} if lr else None})
        return {"enabled": st["enabled"], "lat": st["lat"], "lon": st["lon"], "tz": getattr(self.tz, "key", str(self.tz)),
                "now": int(now * 1000), "schedules": items}

    def create(self, data: dict) -> dict:
        if self.store.count() >= MAX_SCHEDULES:
            raise ScheduleError(f"at most {MAX_SCHEDULES} schedules")
        s = {"id": new_id(), **data, "rev": 1, "armed_at": self.clock()}
        self.store.put(s)
        return s

    def update(self, old: dict, data: dict) -> dict:
        timing = {k: data[k] for k in ("days", "time", "action", "target")} != {k: old[k] for k in ("days", "time", "action", "target")}
        s = {**old, **data, "rev": old["rev"] + (1 if timing else 0)}
        if timing or (data["enabled"] and not old["enabled"]):
            s["armed_at"] = self.clock()  # nothing before this moment is caught up
        self.store.put(s)
        return s

    def put_settings(self, new: dict) -> dict:
        old = self.store.settings()
        if new["enabled"] and not old["enabled"] or (new["lat"], new["lon"]) != (old["lat"], old["lon"]):
            new = {**new, "armed_at": self.clock()}
        self.store.put_settings(new)
        return new

    def next_due(self) -> float | None:
        """Earliest upcoming occurrence of any enabled schedule (lets the loop wake up on time)."""
        st = self.store.settings()
        if not st["enabled"]:
            return None
        now = self.clock()
        ts = [n for s in self.store.all() if s["enabled"] and (n := next_run(s, now, self.tz, st["lat"], st["lon"]))]
        return min(ts) if ts else None

    # ---- firing ----
    def _due(self, now: float, st: dict) -> list[tuple[dict, str]]:
        today = datetime.fromtimestamp(now, self.tz).date()
        out = []
        for s in self.store.all():
            if not s["enabled"]:
                continue
            armed = max(s.get("armed_at", 0), st.get("armed_at", 0))
            for d in (today - timedelta(days=1), today):  # 23:59 + grace runs past midnight
                ts = occurrence(s, d, self.tz, st["lat"], st["lon"])
                if ts is None or not (ts <= now < ts + GRACE) or ts < armed:
                    continue
                run = self.store.run(s["id"], d.isoformat())
                if run and run["rev"] == s["rev"]:
                    continue
                out.append((s, d.isoformat()))
        return out

    def _targets(self, s: dict, devices: dict, layout: dict) -> list[str]:
        kinds = ACTIONS[s["action"]["type"]]
        if "room" in s["target"]:
            room = next((r for r in layout.get("rooms", []) if r.get("id") == s["target"]["room"]), None)
            if room is None:
                return []
            return placed_in(layout, room, [e for e, d in devices.items() if d.kind == "light"])
        return [e for e in s["target"]["entity_ids"] if e in devices and devices[e].kind in kinds]

    async def tick(self, devices: dict, states: dict) -> list[dict]:
        """Fire everything due. Returns the service calls made (for tests / logs)."""
        st = self.store.settings()
        if not st["enabled"]:
            return []
        now = self.clock()
        due = self._due(now, st)
        if not due:
            return []
        layout, away = self.layout(), self.mode().get("mode") == "away"
        held = self.window.held() if self.window else {}
        plan: dict[str, tuple] = {}            # entity -> (domain, service, extra data); later schedules win
        owner: dict[str, str] = {}             # entity -> schedule id that set it
        notes: dict[str, list[str]] = {}
        for s, day in due:
            a, ids = s["action"], self._targets(s, devices, layout)
            n = notes.setdefault(s["id"], [])
            # recorded before any call: a crash or a failure never fires this occurrence again
            if not ids:
                n.append("skipped: no devices")
            elif a["type"] == "temperature" and away:
                n.append("skipped: away")
                ids = []
            elif a["type"] == "temperature":
                hold = [e for e in ids if e in held]
                for e in hold:
                    self.window.adopt(e, a["value"])
                if hold:
                    n.append(f"{_plural(len(hold), 'radiator')} held by an open window → {a['value']:g}° when it closes")
                ids = [e for e in ids if e not in hold]
            for e in ids:
                d = devices[e]
                if a["type"] == "temperature":
                    plan[e] = ("climate", "set_temperature", (("temperature", a["value"]),))
                elif a["type"] == "brightness":
                    plan[e] = ("light", "turn_on", (("brightness_pct", a["value"]),))
                else:
                    plan[e] = ("light" if d.kind == "light" else "switch", f"turn_{a['type']}", ())
                owner[e] = s["id"]
            self.store.put_run(s["id"], day, s["rev"], now, "running")
        groups: dict[tuple, list[str]] = {}
        for e, key in plan.items():
            groups.setdefault(key, []).append(e)
        failed: set[str] = set()
        calls = []
        for (domain, service, extra), ids in groups.items():
            data = {"entity_id": sorted(ids), **dict(extra)}
            calls.append({"domain": domain, "service": service, "data": data})
            try:
                await self.call(domain, service, data)
            except Exception as e:
                log.warning("schedule call %s.%s %s failed: %s", domain, service, ids, e)
                failed |= {owner[x] for x in ids}
        for s, day in due:
            mine = [e for e, o in owner.items() if o == s["id"]]
            parts = notes[s["id"]]
            if mine:
                a = s["action"]
                kinds = {devices[e].kind for e in mine}
                noun = {"light": "light", "plug": "plug", "valve": "radiator"}[kinds.pop()] if len(kinds) == 1 else "device"
                what = {"on": "on", "off": "off", "brightness": f"at {a.get('value')} %", "temperature": f"to {a.get('value', 0):g}°"}[a["type"]]
                parts.insert(0, f"{_plural(len(mine), noun)} {what}")
            status = ("failed: Home Assistant error · " if s["id"] in failed else "") + (" · ".join(parts) or "done")
            self.store.put_run(s["id"], day, s["rev"], now, status)
            log.info("schedule %r (%s): %s", s["name"], day, status)
        cutoff = (datetime.fromtimestamp(now, self.tz).date() - timedelta(days=14)).isoformat()
        if self._pruned != cutoff:
            self.store.prune_runs(cutoff)
            self._pruned = cutoff
        return calls
