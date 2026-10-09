"""Appliances: furniture pieces linked to a smart plug. Status is read-only inference from the plug's power; nothing here
ever switches a device.

Rules per furniture type (keep in step with APPLIANCE in frontend/furniture.js):
  "busy"  — over on_w the appliance is busy ("Boiling…"), else idle ("Idle"); kettle, fridge, microwave, …
  "on"    — "On · 35 W" while the plug is on; fan, floor lamp
  "cycle" — washer / dryer / dishwasher: Running once power stays over run_w for run_min minutes, Finished once it then
            stays under idle_w for idle_min minutes (short pauses mid-cycle, e.g. soaking, don't count), Idle after that.

The cycle tracker runs in the automations loop with an injected clock and keeps its state in SQLite. One push per cycle
("Washing finished"), marked as sent before it goes out so it can never repeat; after a restart the quiet/busy timers
start again from scratch, so a running cycle is never declared finished on the strength of a gap we didn't see.
"""
import logging
import math

log = logging.getLogger("homecontrol.appliances")

# type -> (rule, busy text, idle text, default thresholds)
CYCLE_DEFAULTS = {"run_w": 10.0, "run_min": 2.0, "idle_w": 5.0, "idle_min": 3.0}
APPLIANCES = {
    "fan": ("on", None, None, {}),
    "floor_lamp": ("on", None, None, {}),
    "tv": ("busy", "On", "Standby", {"on_w": 15.0}),
    "heater": ("busy", "Heating", "Idle", {"on_w": 100.0}),
    "kettle": ("busy", "Boiling…", "Idle", {"on_w": 1000.0}),
    "microwave": ("busy", "Heating…", "Idle", {"on_w": 300.0}),
    "coffee_machine": ("busy", "Brewing…", "Idle", {"on_w": 300.0}),
    "toaster": ("busy", "Toasting…", "Idle", {"on_w": 300.0}),
    "iron": ("busy", "Heating", "Ready", {"on_w": 100.0}),
    "hair_straightener": ("busy", "Heating", "Ready", {"on_w": 15.0}),
    "fridge": ("busy", "Cooling", "Idle", {"on_w": 30.0}),
    "freezer": ("busy", "Cooling", "Idle", {"on_w": 30.0}),
    "washer": ("cycle", None, None, CYCLE_DEFAULTS),
    "dryer": ("cycle", None, None, CYCLE_DEFAULTS),
    "dishwasher": ("cycle", None, None, CYCLE_DEFAULTS),
}
APPLIANCE_TYPES = tuple(APPLIANCES)
PROTECTED = ("fridge", "freezer")  # a tap opens the sheet; switching off needs a confirm (frontend)
DONE_TITLE = {"washer": "Washing finished", "dryer": "Dryer finished", "dishwasher": "Dishwasher finished"}
TYPE_NAME = {"washer": "Washing machine", "dryer": "Tumble dryer", "dishwasher": "Dishwasher"}
THRESHOLD_RANGE = {"on_w": (0.5, 5000.0), "run_w": (0.5, 5000.0), "idle_w": (0.1, 5000.0),
                   "run_min": (0.5, 60.0), "idle_min": (0.5, 120.0)}
FINISHED_FOR = 2 * 3600  # "Finished 12 min ago" this long, then "Idle"

# Left-on reminders: types that remind by default and after how long (minutes); every other non-cycle, non-fridge
# appliance can be switched on per link (layout "remind": minutes, or false for off).
REMIND_DEFAULT = {"heater": 180, "fan": 180, "iron": 60, "hair_straightener": 60}
REMIND_RANGE = (15, 24 * 60)
SAFETY = ("heater", "iron", "hair_straightener")  # their reminders bypass quiet hours, like door alerts
LEFT_W = 3.0       # "drawing": above this; a heater or iron thermostat dropping below it for a while doesn't count…
LEFT_GRACE = 600   # …unless it stays below for 10 min, which ends the run (re-armed only once the plug is off)


class ThresholdError(ValueError):
    pass


def validate_thresholds(t, ftype: str) -> dict:
    """Per-link overrides: only the keys this type's rule uses; returns {} when nothing is overridden."""
    if t is None:
        return {}
    if not isinstance(t, dict):
        raise ThresholdError("thresholds must be an object")
    allowed = APPLIANCES[ftype][3]
    out = {}
    for k, v in t.items():
        if k not in allowed:
            raise ThresholdError(f"threshold {k!r} doesn't apply to {ftype}")
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ThresholdError(f"threshold {k} must be a number")
        lo, hi = THRESHOLD_RANGE[k]
        if not lo <= v <= hi:
            raise ThresholdError(f"threshold {k} must be {lo:g}–{hi:g}")
        out[k] = round(float(v), 1)
    th = {**allowed, **out}
    if "idle_w" in th and th["idle_w"] > th["run_w"]:
        raise ThresholdError("the quiet level must not be above the running level")
    return out


def remind_ok(ftype: str) -> bool:
    return ftype in APPLIANCES and APPLIANCES[ftype][0] != "cycle" and ftype not in PROTECTED


def validate_remind(v, ftype: str):
    """Layout "remind": absent/None = the type's default, false = off, minutes (15–1440) = on."""
    if v is None:
        return None
    if not remind_ok(ftype):
        raise ThresholdError(f"a {ftype} has no left-on reminder")
    if v is False:
        return False
    if isinstance(v, bool) or not isinstance(v, int) or not REMIND_RANGE[0] <= v <= REMIND_RANGE[1]:
        raise ThresholdError(f"remind must be false or {REMIND_RANGE[0]}–{REMIND_RANGE[1]} minutes")
    return v


def remind_minutes(f: dict) -> int | None:
    """Effective left-on reminder for a linked piece, None when off."""
    if not remind_ok(f["type"]):
        return None
    v = f.get("remind")
    if v is False:
        return None
    return v if isinstance(v, int) and not isinstance(v, bool) else REMIND_DEFAULT.get(f["type"])


def thresholds(f: dict) -> dict:
    return {**APPLIANCES[f["type"]][3], **(f.get("thresholds") or {})}


def linked(layout: dict) -> list[dict]:
    """Furniture pieces with a plug, of an appliance type."""
    return [f for f in layout.get("furniture") or [] if f.get("plug") and f.get("type") in APPLIANCES]


def protected_plugs(layout: dict) -> set[str]:
    """Plugs of linked fridges / freezers: "All off" and Away leave them on, like keep-on plugs."""
    return {f["plug"] for f in linked(layout) if f["type"] in PROTECTED}


def fmt_w(w: float) -> str:
    return f"{round(w)} W" if w >= 100 else f"{round(w * 10) / 10:g} W"


def fmt_hm(mins: int) -> str:
    """180 -> "3 h", 90 -> "1 h 30 min", 45 -> "45 min"."""
    if mins < 60:
        return f"{mins} min"
    return f"{mins // 60} h {mins % 60} min" if mins % 60 else f"{mins // 60} h"


def fmt_dur(secs: float) -> str:
    m = int(max(0, secs) // 60)
    if m < 1:
        return "<1 min"
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h"


def status(ftype: str, item: dict | None, th: dict, cycle: dict | None = None, now: float = 0.0) -> str:
    """What the plan shows for the appliance (mirrors applianceStatus in frontend/furniture.js)."""
    state = (item or {}).get("state")
    if item is None or state in ("unavailable", "unknown", None):
        return "Offline"
    if state != "on":
        return "Off"
    p = item.get("power")
    rule, busy, idle, _ = APPLIANCES[ftype]
    if rule == "on":
        return f"On · {fmt_w(p)}" if p is not None else "On"
    if rule == "busy":
        if p is None:
            return "On"
        return busy if p > th["on_w"] else idle
    c = cycle or {}
    if c.get("phase") == "running":
        return f"Running {fmt_dur(now - c['run_start'])}"
    if p is not None and p > th["run_w"]:
        return "Starting…"
    if c.get("phase") == "finished" and now - c["finished_at"] < FINISHED_FOR:
        return f"Finished {fmt_dur(now - c['finished_at'])} ago"
    return "Idle"


# ---------------- cycle detection ----------------
def step(c: dict, item: dict | None, th: dict, now: float) -> str | None:
    """Advance one cycle state with the plug's current reading. Returns "started", "finished", "aborted" or None.
    c: {"phase": idle|running|finished, "run_start", "finished_at", "notified", "high_since", "low_since"}."""
    state = (item or {}).get("state")
    if state == "off":  # switched off at the plug: no cycle, and certainly no "finished" push
        c["high_since"] = c["low_since"] = None
        if c["phase"] == "running":
            c["phase"] = "idle"
            return "aborted"
        return None
    p = (item or {}).get("power")
    if state != "on" or p is None:
        return None  # offline / no reading: hold everything as it is
    if c["phase"] == "running":
        if p < th["idle_w"]:
            c["low_since"] = c.get("low_since") or now
            if now - c["low_since"] >= th["idle_min"] * 60:
                c.update(phase="finished", finished_at=c["low_since"], low_since=None, high_since=None)
                return "finished"
        else:
            c["low_since"] = None  # a pause that didn't last: still the same cycle
        return None
    if p > th["run_w"]:
        c["high_since"] = c.get("high_since") or now
        if now - c["high_since"] >= th["run_min"] * 60:
            c.update(phase="running", run_start=c["high_since"], finished_at=None, notified=False,
                     high_since=None, low_since=None)
            return "started"
    else:
        c["high_since"] = None
    return None


PERSISTED = ("phase", "run_start", "finished_at", "notified")


def left_step(c: dict, item: dict | None, now: float, minutes: int) -> bool:
    """Advance a left-on timer. c: {"since": start of the drawing run, "low_since", "sent"}. True = remind now."""
    state = (item or {}).get("state")
    if state == "off":  # switched off: re-armed
        c.update(since=None, low_since=None, sent=False)
        return False
    p = (item or {}).get("power")
    if state != "on" or p is None:
        return False  # offline / no reading: hold
    if p > LEFT_W:
        c["low_since"] = None
        c["since"] = c.get("since") or now
    else:
        c["low_since"] = c.get("low_since") or now
        if now - c["low_since"] >= LEFT_GRACE:
            c["since"] = None  # it stopped drawing; still needs an off before it can remind again
    if c.get("since") is not None and not c.get("sent") and now - c["since"] >= minutes * 60:
        c["sent"] = True
        return True
    return False


class Appliances:
    """Cycle trackers for linked washers / dryers / dishwashers, keyed "<furniture id>|<plug>".

    store: AutoStore; layout: callable -> layout; items: callable -> {plug entity id: device item};
    notify: async fn(payload, category) — "appliance" goes through quiet hours, "safety" (heater / iron left on) doesn't;
    publish: fn(public state) for the SSE stream. Also runs the left-on reminders."""

    def __init__(self, store, settings, layout, notify, publish=None, clock=None):
        self.store, self.settings, self.layout, self.notify, self.publish = store, settings, layout, notify, publish
        self.clock = clock
        saved = store.get("appliance_cycles", {}) or {}
        # Busy / quiet timers start afresh: what happened while we were down is unknown.
        self.cycles: dict[str, dict] = {k: {**{p: v.get(p) for p in PERSISTED}, "high_since": None, "low_since": None}
                                        for k, v in saved.items() if isinstance(v, dict) and v.get("phase") in ("idle", "running", "finished")}
        # Left-on timers survive a restart: a heater on for 2 h before it is still on for 2 h after.
        self.left: dict[str, dict] = {k: {"since": v.get("since"), "sent": bool(v.get("sent")), "low_since": None}
                                      for k, v in (store.get("appliance_left_on", {}) or {}).items() if isinstance(v, dict)}
        self.pending: list[tuple[dict, str]] = []
        self.on_event = None  # fn(furniture, "started"|"finished"|"aborted", cycle): the activity log

    @staticmethod
    def key(f: dict) -> str:
        return f"{f['id']}|{f['plug']}"

    def _save(self) -> None:
        self.store.put("appliance_cycles", {k: {p: c.get(p) for p in PERSISTED} for k, c in self.cycles.items()})

    def _save_left(self) -> None:
        self.store.put("appliance_left_on", {k: {"since": c.get("since"), "sent": c.get("sent", False)} for k, c in self.left.items()})

    def _left_pieces(self, layout=None) -> list[dict]:
        return [f for f in linked(layout or self.layout()) if remind_minutes(f)]

    def _advance_left(self, f: dict, item: dict | None, now: float) -> bool:
        c = self.left.setdefault(self.key(f), {"since": None, "low_since": None, "sent": False})
        before = (c.get("since"), c.get("sent"))
        mins = remind_minutes(f)
        if left_step(c, item, now, mins):
            name = f.get("label") or TYPE_NAME.get(f["type"]) or f["type"].replace("_", " ").capitalize()
            log.info("appliance %s (%s) left on for %s", f["id"], f["plug"], fmt_hm(mins))
            self.pending.append(({"title": f"{name} has been on for {fmt_hm(mins)}",
                                  "body": "Still on and drawing power. Turn it off?",
                                  "tag": f"lefton-{f['id']}", "url": f"/?dev={f['plug']}", "entity_id": f["plug"],
                                  "actions": [{"action": "off", "title": "Turn off"}]},
                                 "safety" if f["type"] in SAFETY else "appliance"))
        return before != (c.get("since"), c.get("sent"))

    def _cycle_pieces(self, layout=None) -> list[dict]:
        return [f for f in linked(layout or self.layout()) if APPLIANCES[f["type"]][0] == "cycle"]

    def _advance(self, f: dict, item: dict | None, now: float) -> bool:
        c = self.cycles.setdefault(self.key(f), {"phase": "idle", "run_start": None, "finished_at": None, "notified": False,
                                                  "high_since": None, "low_since": None})
        ev = step(c, item, thresholds(f), now)
        if ev is None:
            return False
        log.info("appliance %s (%s): %s", f["id"], f["plug"], ev)
        if self.on_event:
            self.on_event(f, ev, c)
        if ev == "finished" and not c.get("notified"):
            c["notified"] = True  # marked (and saved below) before the push goes out: never twice
            if self.settings().get("appliance_done", True):
                mins = fmt_dur(c["finished_at"] - c["run_start"])
                name = f.get("label") or TYPE_NAME[f["type"]]
                self.pending.append(({"title": DONE_TITLE[f["type"]], "body": f"{name} ran {mins}.",
                                      "tag": f"appliance-{f['id']}", "url": "/"}, "appliance"))
        return True

    def observe(self, item: dict) -> bool:
        """A plug changed (Live observer): advance its cycles now so a short dip or spike is never missed.
        Returns True when the automations loop should run (a push is waiting)."""
        if item.get("kind") != "plug":
            return False
        layout, now = self.layout(), self.clock()
        left = [f for f in self._left_pieces(layout) if f["plug"] == item["entity_id"]]
        if [f for f in left if self._advance_left(f, item, now)]:
            self._save_left()
        pieces = [f for f in self._cycle_pieces(layout) if f["plug"] == item["entity_id"]]
        if pieces:
            if [f for f in pieces if self._advance(f, item, now)]:
                self._save()
            self._publish()  # with every new reading: browsers keep the server's clock for "Running 47 min"
        return bool(self.pending)

    async def tick(self, items: dict) -> None:
        """Every automations check: time passes even when the power reading doesn't change."""
        layout, now = self.layout(), self.clock()
        pieces = self._cycle_pieces(layout)
        changed = False
        for f in pieces:
            changed |= self._advance(f, items.get(f["plug"]), now)
        keep = {self.key(f) for f in pieces}
        gone = [k for k in self.cycles if k not in keep]
        for k in gone:  # unlinked, relinked or deleted
            del self.cycles[k]
        if changed or gone:
            self._save()
        if changed:
            self._publish()
        left = self._left_pieces(layout)
        left_changed = False
        for f in left:
            left_changed |= self._advance_left(f, items.get(f["plug"]), now)
        keep = {self.key(f) for f in left}
        for k in [k for k in self.left if k not in keep]:  # unlinked, or the reminder switched off: forget the timer
            del self.left[k]
            left_changed = True
        if left_changed:
            self._save_left()
        pending, self.pending = self.pending, []
        for p, category in pending:
            await self.notify(p, category)

    def public(self, items: dict | None = None) -> dict:
        """GET /api/appliances and the SSE "appliances" event: per furniture id, the cycle and (with items) the status."""
        now, out = self.clock(), {}
        for f in linked(self.layout()):
            c = self.cycles.get(self.key(f)) if APPLIANCES[f["type"]][0] == "cycle" else None
            entry = {"plug": f["plug"], "type": f["type"]}
            if c is not None:
                entry.update(phase=c["phase"], run_start=c.get("run_start"), finished_at=c.get("finished_at"))
            if items is not None:
                entry["status"] = status(f["type"], items.get(f["plug"]), thresholds(f), c, now)
            out[f["id"]] = entry
        return {"now": now, "appliances": out}

    def _publish(self) -> None:
        if self.publish:
            try:
                self.publish(self.public())
            except Exception as e:
                log.warning("appliances: publish failed: %s", e)
