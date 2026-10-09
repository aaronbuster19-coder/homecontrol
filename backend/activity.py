"""Activity timeline: one feed of what happened at home, newest first, a local day per page.

Sources, merged and de-duplicated here (server side):
- HA's recorder (/api/history/period, ONE call per day page, cached): doors/windows, lights, plugs, radiator targets,
  the dehumidifier, media players and people coming and going;
- the app's own log (SQLite table activity_log): every service call the app makes, with WHO/WHAT made it (the signed-in
  user of the request, or the automation acting: schedule, Away mode, Auto Away, window heating, standby saver, All off),
  sign-ins and failed attempts, pushes sent or held for the digest, appliance cycles and kettle boils;
- logs kept elsewhere: Auto Away's log, standby saver failures, schedule runs that were skipped or failed.

Attribution: an HA state change follows an app call for the same entity within ATTRIBUTE_WINDOW seconds -> "by you"
(or the automation's name); a change of a controllable device without one -> "manually / other".
"""
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time as dtime, timedelta, timezone

from .appliances import APPLIANCES, DONE_TITLE, TYPE_NAME, fmt_dur, linked, thresholds
from .geometry import in_room, opening_rooms
from .history import BAD, by_entity, parse_ts

log = logging.getLogger("homecontrol.activity")

ATTRIBUTE_WINDOW = 5.0     # an app call this many seconds before an HA change caused it
EARLY_SLACK = 2.0          # HA's clock may run a little behind ours
CALL_LAG = 60.0            # an app call this recent whose change isn't in HA's history yet still shows on its own
FAIL_BURST = 600           # failed sign-ins from one address within 10 min are one entry
MAX_DAYS = 7
KEEP_DAYS = 9
TODAY_TTL, PAST_TTL = 20.0, 3600.0
TYPES = ("doors", "lights", "heating", "appliances", "people", "alerts", "security")
CONTROLLED = ("light", "plug", "valve", "dehumidifier", "media")
MEDIA_ON = ("on", "playing", "paused", "idle", "buffering")
MEDIA_OFF = ("off", "standby")
KIND_TYPE = {"sensor": "doors", "light": "lights", "plug": "lights", "valve": "heating", "dehumidifier": "appliances",
             "media": "appliances", "person": "people"}

# What is acting right now: set by request handlers and the automations loop, read when a service call is recorded.
ACTOR: ContextVar[str | None] = ContextVar("hc_actor", default=None)
USER: ContextVar[str | None] = ContextVar("hc_user", default=None)


@contextmanager
def acting(label: str | None):
    token = ACTOR.set(label)
    try:
        yield
    finally:
        ACTOR.reset(token)


class ActorMiddleware:
    """Pure ASGI, added inside the auth guard: the signed-in user of each request becomes USER for its service calls."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        user = (scope.get("state") or {}).get("user") if scope["type"] == "http" else None
        if not user:
            return await self.app(scope, receive, send)
        token = USER.set(user)
        try:
            return await self.app(scope, receive, send)
        finally:
            USER.reset(token)


class ActivityStore:
    def __init__(self, path: str):
        self.path, self._lock = path, threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS activity_log (id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, "
                      "kind TEXT NOT NULL, data TEXT NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS activity_log_t ON activity_log (t)")
        self._pruned = 0.0

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def add(self, kind: str, data: dict, t: float) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO activity_log (t, kind, data) VALUES (?, ?, ?)", (t, kind, json.dumps(data)))
            if t - self._pruned > 3600:
                c.execute("DELETE FROM activity_log WHERE t < ?", (t - KEEP_DAYS * 86400,))
                self._pruned = t

    def between(self, t0: float, t1: float) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT id, t, kind, data FROM activity_log WHERE t >= ? AND t < ? ORDER BY t, id", (t0, t1)).fetchall()
        return [{"id": i, "t": t, "kind": k, **json.loads(d)} for i, t, k, d in rows]

    def schedule_runs(self, t0: float, t1: float) -> list[tuple[str, float, str]]:
        """Schedule runs (backend/schedules.py's table) in [t0, t1); none if schedules never ran here."""
        try:
            with self._lock, self._conn() as c:
                return c.execute("SELECT sid, at, status FROM schedule_runs WHERE at >= ? AND at < ?", (t0, t1)).fetchall()
        except sqlite3.OperationalError:
            return []


# ---------------- HA history -> events ----------------
def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _deg(v: float) -> str:
    return f"{v:g}°"


def ha_events(dev, rows: list[dict], start_ms: int) -> list[dict]:
    """State changes of one device: [{t, entity_id, change, value?}]. The first row is the state carried in at the
    start of the page (not a change); a change out of unavailable/unknown isn't one either (HA restarted / offline)."""
    pts = []
    for r in rows:
        t = parse_ts(r.get("last_updated") or r.get("last_changed"))
        if t is not None:
            pts.append((t, r.get("state"), r.get("attributes") or {}))
    pts.sort(key=lambda p: p[0])
    out, prev = [], None  # prev: (state, target) last known; None = unknown
    for i, (t, s, a) in enumerate(pts):
        bad = s in BAD
        if dev.kind == "valve":
            cur = None if bad else (s, _num(a.get("temperature")) if "temperature" in a else (prev[1] if prev else None))
        elif dev.kind == "dehumidifier":
            cur = None if bad else (s, _num(a.get("humidity")) if "humidity" in a else (prev[1] if prev else None))
        elif dev.kind == "media":
            cur = None if bad else ("on" if s in MEDIA_ON else "off" if s in MEDIA_OFF else s, None)
        else:
            cur = None if bad else (s, None)
        first = i == 0 and t <= start_ms + 1000
        if cur is not None and prev is not None and not first:
            ev = {"t": t, "entity_id": dev.entity_id}
            k, (ps, pt), (cs, ct) = dev.kind, prev, cur
            if k == "sensor" and cs != ps and cs in ("on", "off"):
                out.append({**ev, "change": "open" if cs == "on" else "closed"})
            elif k in ("light", "plug", "media", "dehumidifier") and cs != ps and cs in ("on", "off") and ps in ("on", "off"):
                out.append({**ev, "change": cs})
            elif k == "person" and cs != ps:
                out.append({**ev, "change": "home" if cs == "home" else "away" if cs == "not_home" else "zone", "value": cs})
            elif k == "valve" and cs != ps and "off" in (cs, ps):
                out.append({**ev, "change": "off" if cs == "off" else "on"})
            elif k == "valve" and ct is not None and pt is not None and ct != pt and cs != "off":
                out.append({**ev, "change": "target", "value": ct})
            if k == "dehumidifier" and ct is not None and pt is not None and ct != pt and cs == ps:
                out.append({**ev, "change": "target", "value": ct})
        if cur is not None or bad:
            prev = cur
    return out


def change_text(kind: str, name: str, ev: dict) -> str:
    c, v = ev["change"], ev.get("value")
    if kind == "sensor":
        return f"{name} {'opened' if c == 'open' else 'closed'}"
    if kind == "person":
        return f"{name} came home" if c == "home" else f"{name} left" if c == "away" else f"{name} is at {v}"
    if c == "target":
        return f"{name} set to {_deg(v)}" if kind == "valve" else f"{name} target {v:g} %"
    return f"{name} turned {c}"


def call_text(name: str, service: str, data: dict) -> str:
    if service == "set_temperature" and _num(data.get("temperature")) is not None:
        return f"{name} set to {_deg(data['temperature'])}"
    if service == "set_humidity" and _num(data.get("humidity")) is not None:
        return f"{name} target {data['humidity']:g} %"
    if service == "set_mode":
        return f"{name} mode {data.get('mode')}"
    if service == "turn_on" and _num(data.get("brightness_pct")) is not None:
        return f"{name} set to {data['brightness_pct']:g} %"
    if service == "turn_on" and any(k in data for k in ("hs_color", "rgb_color", "color_temp_kelvin")):
        return f"{name} colour changed"
    return f"{name} {'toggled' if service == 'toggle' else 'turned ' + service.removeprefix('turn_')}"


def fits(service: str, ev: dict) -> bool:
    """Could this service call have caused this change?"""
    c = ev["change"]
    if service == "toggle":
        return c in ("on", "off")
    if service in ("turn_on", "turn_off"):
        return c == service.removeprefix("turn_")
    if service in ("set_temperature", "set_humidity"):
        return c == "target"
    return service == "set_mode"


def by_text(call: dict) -> str:
    if call.get("label"):
        return call["label"]
    return "by you" if call.get("user") else "by the app"


def attribute(events: list[dict], calls: list[dict]) -> set[int]:
    """Sets ev["by"] for each HA change an app call explains: per call and entity, the fitting change within the
    window, preferring one after the call (then the closest). Returns the indexes of the calls that explained something."""
    hit: set[int] = set()
    by_eid: dict[str, list[dict]] = {}
    for ev in events:
        by_eid.setdefault(ev["entity_id"], []).append(ev)
    for i, c in sorted(enumerate(calls), key=lambda x: x[1]["t"]):
        for e in c["entity_ids"]:
            best = None
            for ev in by_eid.get(e, []):
                dt = ev["t"] / 1000 - c["t"]
                if "by" in ev or not -EARLY_SLACK <= dt <= ATTRIBUTE_WINDOW or not fits(c["service"], ev):
                    continue
                if best is None or (dt < 0, abs(dt)) < best[0]:
                    best = ((dt < 0, abs(dt)), ev)
            if best:
                best[1]["by"] = by_text(c)
                hit.add(i)
    return hit


# ---------------- rooms ----------------
def room_index(layout: dict) -> dict[str, dict]:
    """entity id -> room (a linked door/window by its wall, everything else by where it's placed)."""
    rooms = layout.get("rooms", [])
    out: dict[str, dict] = {}
    for p in layout.get("placements", []):
        r = next((r for r in rooms if in_room(r, p["x"], p["y"])), None)
        if r:
            out[p["entity_id"]] = r
    for o in layout.get("openings", []):
        if o.get("entity_id"):
            rs = opening_rooms(layout, o)
            if rs:
                out[o["entity_id"]] = rs[0]
    return out


def opening_types(layout: dict) -> dict[str, str]:
    return {o["entity_id"]: o.get("type", "door") for o in layout.get("openings", []) if o.get("entity_id")}


def fail_bursts(fails: list[dict]) -> list[dict]:
    """Failed sign-ins -> one entry per address and burst (gaps under FAIL_BURST)."""
    out: list[dict] = []
    open_: dict[str, dict] = {}
    for f in sorted(fails, key=lambda f: f["t"]):
        ip = f.get("ip") or "?"
        b = open_.get(ip)
        if b and f["t"] - b["last"] < FAIL_BURST:
            b["n"] += 1
            b["last"] = f["t"]
        else:
            b = open_[ip] = {"ip": ip, "n": 1, "first": f["t"], "last": f["t"]}
            out.append(b)
    return out


# ---------------- the feed ----------------
class Activity:
    def __init__(self, store: ActivityStore, ha, devices, layout, clock=time.time, tz=None, auto_store=None, schedule_name=None):
        """devices: async () -> {entity id: Device}; layout: () -> layout; auto_store: AutoStore (Auto Away and
        standby saver logs); schedule_name: (sid) -> name or None."""
        self.store, self.ha, self.devices, self.layout, self.clock = store, ha, devices, layout, clock
        self.tz = tz or timezone.utc
        self.auto_store, self.schedule_name = auto_store, schedule_name
        self.cache: dict = {}
        self.boiling: dict[str, float] = {}  # kettle plug -> when it went over its threshold

    # ---- recording ----
    def wrap_call(self, call):
        """Wraps HAClient.call_service: every successful call is logged with whoever is acting."""
        async def recorded(domain: str, service: str, data: dict):
            t = self.clock()
            r = await call(domain, service, data)
            try:
                self.record_call(domain, service, data, t)
            except Exception as e:  # the log never breaks a call
                log.warning("activity: recording %s.%s failed: %s", domain, service, e)
            return r
        recorded.__wrapped__ = call
        return recorded

    def record_call(self, domain: str, service: str, data: dict, t: float | None = None) -> None:
        ids = data.get("entity_id") or []
        ids = [ids] if isinstance(ids, str) else [e for e in ids if isinstance(e, str)]
        if not ids:
            return
        extra = {k: data[k] for k in ("temperature", "humidity", "mode", "brightness_pct", "hs_color", "rgb_color",
                                       "color_temp_kelvin") if k in data}
        self.store.add("call", {"domain": domain, "service": service, "entity_ids": ids, "data": extra,
                                "label": ACTOR.get(), "user": USER.get()}, self.clock() if t is None else t)
        for k in [k for k in self.cache if len(k) == 2]:  # today's page: fetch HA's history afresh to show its result
            del self.cache[k]

    def record(self, kind: str, **data) -> None:
        try:
            self.store.add(kind, data, self.clock())
        except Exception as e:
            log.warning("activity: recording %s failed: %s", kind, e)

    def record_push(self, payload: dict, result) -> None:
        sent = result.get("sent") if isinstance(result, dict) else None
        self.record("push", title=str(payload.get("title") or ""), body=str(payload.get("body") or "")[:200],
                    tag=str(payload.get("tag") or ""), sent=sent)

    def record_appliance(self, f: dict, ev: str, c: dict) -> None:
        name = f.get("label") or TYPE_NAME.get(f["type"]) or f["type"].replace("_", " ").capitalize()
        secs = (c.get("finished_at") or 0) - (c.get("run_start") or 0) if ev == "finished" else None
        self.record("appliance", plug=f["plug"], type=f["type"], name=name, event=ev,
                    secs=secs if secs and secs > 0 else None)

    def observe(self, item: dict, raw: dict | None = None) -> None:
        """Live observer: kettle boils (power over the kettle's threshold, then back under it)."""
        if item.get("kind") != "plug":
            return
        kettles = [f for f in linked(self.layout()) if f["type"] == "kettle" and f["plug"] == item["entity_id"]]
        if not kettles:
            return
        p, now, eid = item.get("power"), self.clock(), item["entity_id"]
        if p is None or item.get("state") != "on":
            if item.get("state") == "off":
                self.boiling.pop(eid, None)
            return
        on_w = thresholds(kettles[0]).get("on_w", APPLIANCES["kettle"][3]["on_w"])
        if p >= on_w:
            self.boiling.setdefault(eid, now)
        elif eid in self.boiling:
            began = self.boiling.pop(eid)
            if now - began >= 20:  # a real boil, not a blip
                self.record("appliance", plug=eid, type="kettle", name=kettles[0].get("label") or "Kettle",
                            event="boiled", secs=now - began)

    # ---- day pages ----
    def midnight(self, d: date) -> float:
        return datetime.combine(d, dtime(0), tzinfo=self.tz).timestamp()

    def day_of(self, t: float) -> date:
        return datetime.fromtimestamp(t, self.tz).date()

    def label(self, d: date, today: date) -> str:
        if d == today:
            return "Today"
        if d == today - timedelta(days=1):
            return "Yesterday"
        return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"

    async def _history(self, devs: list, t0: float, t1: float, today: bool) -> tuple[dict, str | None]:
        ids = tuple(sorted(d.entity_id for d in devs))
        if not ids:
            return {}, None
        key = (t0, ids) if today else (t0, t1, ids)
        hit = self.cache.get(key)
        mono = time.monotonic()
        if hit and mono - hit[0] < (TODAY_TTL if today else PAST_TTL) and (today or hit[2] >= t1):
            return hit[1], None
        try:
            data = await self.ha.history(datetime.fromtimestamp(t0, timezone.utc), datetime.fromtimestamp(t1, timezone.utc),
                                         list(ids), attributes=True)
        except Exception as e:
            log.warning("activity: history failed: %s", e)
            return ({}, str(e)) if not hit else (hit[1], None)
        rows = by_entity(data)
        if len(self.cache) > 16:
            self.cache.clear()
        self.cache[key] = (mono, rows, t1)
        return rows, None

    async def page(self, before: float | None = None, days: int = 1, room: str | None = None, type_: str | None = None) -> dict:
        now = self.clock()
        today = self.day_of(now)
        oldest = self.midnight(today - timedelta(days=MAX_DAYS - 1))
        end = min(now, before) if before else now
        days = max(1, min(MAX_DAYS, days))
        if end <= oldest:
            return {"start": int(oldest * 1000), "end": int(oldest * 1000), "today": today.isoformat(), "days": [],
                    "entries": [], "next_before": None}
        last_day = self.day_of(end - 0.001)
        start = max(oldest, self.midnight(last_day - timedelta(days=days - 1)))
        is_today = last_day == today
        devs_all = await self.devices()
        devs = [d for d in devs_all.values() if not d.hidden and d.kind in KIND_TYPE]
        # today: a little past now (HA's end_time is whole seconds, and its clock may be ahead of ours)
        rows, err = await self._history(devs, start, end if not is_today else now + 5, is_today)
        hist_end = now if is_today else end
        entries = self._entries(devs_all, devs, rows, err, start, end, hist_end)
        if room:
            entries = [e for e in entries if e.get("room") == room]
        if type_:
            entries = [e for e in entries if e["type"] == type_]
        ds = [last_day - timedelta(days=i) for i in range((last_day - self.day_of(start)).days + 1)]
        return {"start": int(start * 1000), "end": int(end * 1000), "today": today.isoformat(),
                "days": [{"date": d.isoformat(), "label": self.label(d, today)} for d in ds],
                "entries": entries, "next_before": int(start * 1000) if start > oldest else None,
                **({"error": f"Home Assistant history unavailable: {err}"} if err else {})}

    def _entries(self, devs_all: dict, devs: list, rows: dict, err, start: float, end: float, hist_end: float) -> list[dict]:
        layout = self.layout()
        rooms, otypes = room_index(layout), opening_types(layout)
        names = {e: d.name for e, d in devs_all.items()}
        kinds = {e: d.kind for e, d in devs_all.items()}
        logs = self.store.between(start, end)
        calls = [x for x in logs if x["kind"] == "call"]
        events = []
        for d in devs:
            events += ha_events(d, rows.get(d.entity_id, []), int(start * 1000))
        upto = end + 300 if end >= self.clock() - 1 else end  # up to now: HA's clock may be a little ahead of ours
        events = [e for e in events if start * 1000 <= e["t"] < upto * 1000]
        hit = attribute(events, calls)
        out: list[dict] = []

        def add(t_ms: float, type_: str, icon: str, text: str, eid: str | None = None, by: str | None = None,
                detail: str | None = None, src: str = ""):
            r = rooms.get(eid) if eid else None
            out.append({"t": int(t_ms), "day": self.day_of(t_ms / 1000).isoformat(), "type": type_, "icon": icon, "text": text,
                        "by": by, "detail": detail, "entity_id": eid if eid in devs_all else None,
                        "room": r["id"] if r else None, "room_name": r["name"] if r else None, "src": src})

        for ev in events:
            k, eid = kinds[ev["entity_id"]], ev["entity_id"]
            icon = otypes.get(eid) or ("window" if "window" in names[eid].lower() else "door") if k == "sensor" else k
            by = ev.get("by") or ("manually / other" if k in CONTROLLED else None)
            add(ev["t"], KIND_TYPE[k], icon, change_text(k, names[eid], ev), eid, by, src="ha")
        for i, c in enumerate(calls):
            # an app call nothing in HA's history explains yet (the recorder lags a few seconds), or HA's history is down
            if i in hit or (not err and c["t"] < hist_end - CALL_LAG):
                continue
            for eid in c["entity_ids"]:
                k = kinds.get(eid)
                if k is None or (devs_all.get(eid) and devs_all[eid].hidden):
                    continue
                add(c["t"] * 1000, KIND_TYPE.get(k, "lights"), k, call_text(names[eid], c["service"], c["data"]), eid,
                    by_text(c), src="call")
        for x in logs:
            k, t = x["kind"], x["t"] * 1000
            if k == "login":
                add(t, "security", "login", f"{x.get('user') or 'Someone'} signed in", detail=x.get("ip") and f"from {x['ip']}")
            elif k == "users":  # backend/users.py: accounts added / changed / removed, passwords reset
                what = {"user_added": "added", "user_changed": "changed", "user_removed": "removed",
                        "user_password_reset": "reset the password of", "password_changed": "changed the password of"}
                add(t, "security", "login", f"{x.get('by') or 'Someone'} {what.get(x.get('action'), 'changed')} "
                    f"user {x.get('username') or '?'}")
            elif k == "push":
                sent = x.get("sent")
                add(t, "alerts", "push", f"Notification: {x['title']}", detail=x.get("body") or None,
                    by="no devices subscribed" if sent == 0 else None)
            elif k == "push_held":
                add(t, "alerts", "push", f"Held for the digest: {x['title']}", detail=x.get("body") or None)
            elif k == "mode":
                add(t, "people", "away", "Away mode on" if x.get("mode") == "away" else "Home mode on",
                    by="by you" if x.get("user") else None)
            elif k == "appliance":
                ev, name, eid = x.get("event"), x.get("name") or "Appliance", x.get("plug")
                dur = f" · {fmt_dur(x['secs'])}" if x.get("secs") else ""
                text = {"started": f"{name} started", "aborted": f"{name} switched off mid-cycle",
                        "boiled": f"{name} boiled{dur}",
                        "finished": f"{DONE_TITLE.get(x.get('type'), name + ' finished')}{dur}"}.get(ev)
                if text:
                    add(t, "appliances", "kettle" if x.get("type") == "kettle" else "washer", text, eid)
        for b in fail_bursts([x for x in logs if x["kind"] == "login_failed"]):
            text = "Failed sign-in attempt" if b["n"] == 1 else f"{b['n']} failed sign-in attempts"
            add(b["last"] * 1000, "security", "alert", text, detail=f"from {b['ip']}")
        if self.auto_store is not None:
            for x in self.auto_store.get("presence_log", []) or []:
                if start <= x.get("at", 0) < end:
                    add(x["at"] * 1000, "people", "away", f"Auto Away: {x['text']}")
            for x in self.auto_store.get("standby_log", []) or []:
                if start <= x.get("at", 0) < end and x.get("action") == "failed":
                    add(x["at"] * 1000, "lights", "plug", f"Standby saver: {x.get('name')} — {x.get('note')}", x.get("entity_id"))
        for sid, at, status in self.store.schedule_runs(start, end):
            if status and ("skipped" in status or "failed" in status or "held" in status):
                name = (self.schedule_name(sid) if self.schedule_name else None) or "schedule"
                add(at * 1000, "heating" if "°" in status or "radiator" in status else "lights", "schedule",
                    f"Schedule “{name}”: {status}")
        seen, uniq = set(), []
        for e in sorted(out, key=lambda e: (-e["t"], e["text"])):
            key = (e["text"], e["by"], e["entity_id"], e["t"] // 1000)
            if key not in seen:
                seen.add(key)
                uniq.append(e)
        return uniq


def check_type(t: str | None) -> str | None:
    if t in (None, "", "all"):
        return None
    if t not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    return t


def add_routes(app, activity: Activity) -> None:
    from fastapi import HTTPException

    @app.get("/api/activity")
    async def get_activity(before: int | None = None, days: int = 1, room: str = "", type: str = ""):
        try:
            t = check_type(type)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not 1 <= days <= MAX_DAYS:
            raise HTTPException(400, f"days must be 1–{MAX_DAYS}")
        return await activity.page(before / 1000 if before else None, days, room or None, t)
