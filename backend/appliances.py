"""Appliances: furniture pieces linked to a smart plug. Status is read-only inference from the plug's power; nothing here
ever switches a device.

Rules per furniture type (keep in step with APPLIANCE in frontend/furniture.js):
  "busy"  — over on_w the appliance is busy ("Boiling…"), else idle ("Idle"); kettle, fridge, microwave, …
  "on"    — "On · 35 W" while the plug is on; fan, floor lamp ("Running · 35 W": home server)
  "cycle" — washer / dryer / dishwasher: Running once power stays over run_w for run_min minutes, Finished once it then
            stays under idle_w for idle_min minutes (short pauses mid-cycle, e.g. soaking, don't count), Idle after that.
  "charge" — hoover on its dock: Charging while over charge_w, Charged once a charge has then stayed under trickle_w for
            charged_min minutes (shorter dips don't count), Not charging otherwise.

The cycle tracker runs in the automations loop with an injected clock and keeps its state in SQLite. One push per cycle
("Washing finished"), marked as sent before it goes out so it can never repeat; after a restart the quiet/busy timers
start again from scratch, so a running cycle is never declared finished on the strength of a gap we didn't see.
The hoover's charge tracker works the same way ("Hoover charged", one push per charge). The one exception to "never
switches": a hoover whose link has "auto_off": true (opt-in per appliance, default off) gets its plug switched off once
when a charge is detected as finished — marked done before the call, logged, never retried or repeated.
A linked home server also has a watchdog: its plug at 0 W / off / unavailable for 5 min = one safety push.
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
    "hoover": ("charge", None, None, {"charge_w": 10.0, "trickle_w": 3.0, "charged_min": 10.0}),
    "desktop_pc": ("busy", "On", "Sleep", {"on_w": 10.0}),
    "home_server": ("on", "Running", None, {}),
}
APPLIANCE_TYPES = tuple(APPLIANCES)
# A tap opens the sheet; switching off needs a confirm (frontend); "All off", Away and the standby saver leave them on.
PROTECTED = ("fridge", "freezer", "home_server")
DONE_TITLE = {"washer": "Washing finished", "dryer": "Dryer finished", "dishwasher": "Dishwasher finished"}
TYPE_NAME = {"washer": "Washing machine", "dryer": "Tumble dryer", "dishwasher": "Dishwasher", "hoover": "Hoover",
             "desktop_pc": "Desktop PC", "home_server": "Home server"}
THRESHOLD_RANGE = {"on_w": (0.5, 5000.0), "run_w": (0.5, 5000.0), "idle_w": (0.1, 5000.0),
                   "run_min": (0.5, 60.0), "idle_min": (0.5, 120.0),
                   "charge_w": (0.5, 5000.0), "trickle_w": (0.1, 5000.0), "charged_min": (0.5, 120.0)}
CHARGERS = tuple(t for t, a in APPLIANCES.items() if a[0] == "charge")  # these may opt in to "auto_off"
SERVERS = ("home_server",)
SERVER_DOWN_W = 1.0    # the server's plug under this (or off / unavailable)…
SERVER_DOWN_FOR = 300  # …for 5 min: "Home server lost power"
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
    if "trickle_w" in th and th["trickle_w"] > th["charge_w"]:
        raise ThresholdError("the charged level must not be above the charging level")
    return out


def validate_auto_off(v, ftype: str):
    """Layout "auto_off": true = switch the plug off once a charge is finished (chargers only); absent/false = never."""
    if v is None or v is False:
        return None
    if ftype not in CHARGERS:
        raise ThresholdError(f"a {ftype} can't switch itself off")
    if v is not True:
        raise ThresholdError("auto_off must be true or false")
    return True


def remind_ok(ftype: str) -> bool:
    return ftype in APPLIANCES and APPLIANCES[ftype][0] not in ("cycle", "charge") and ftype not in PROTECTED


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
    """Plugs of linked fridges / freezers / home servers: "All off" and Away leave them on, like keep-on plugs."""
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
    rule, busy, idle, _ = APPLIANCES[ftype]
    if state != "on":
        return "Not charging" if rule == "charge" else "Off"
    p = item.get("power")
    if rule == "on":
        return f"{busy or 'On'} · {fmt_w(p)}" if p is not None else busy or "On"
    if rule == "busy":
        if p is None:
            return "On"
        return busy if p > th["on_w"] else idle
    c = cycle or {}
    if rule == "charge":
        if c.get("phase") == "charging" or (p is not None and p > th["charge_w"]):
            return "Charging"
        return "Charged" if c.get("phase") == "charged" else "Not charging"
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


# ---------------- charge detection (hoover) ----------------
def charge_step(c: dict, item: dict | None, th: dict, now: float) -> str | None:
    """Advance one charger. Returns "charging", "charged", "aborted" or None.
    c: {"phase": idle|charging|charged, "charge_start", "charged_at", "notified", "off_done", "low_since"}."""
    state = (item or {}).get("state")
    if state == "off":  # unplugged at the plug: a charge in progress ends without a push; "charged" stays charged
        c["low_since"] = None
        if c["phase"] == "charging":
            c["phase"] = "idle"
            return "aborted"
        return None
    p = (item or {}).get("power")
    if state != "on" or p is None:
        return None  # offline / no reading: hold
    if p > th["charge_w"]:
        c["low_since"] = None
        if c["phase"] != "charging":  # docked again (after use): a new charge
            c.update(phase="charging", charge_start=now, charged_at=None, notified=False, off_done=False)
            return "charging"
        return None
    if c["phase"] == "charging":
        if p < th["trickle_w"]:
            c["low_since"] = c.get("low_since") or now
            if now - c["low_since"] >= th["charged_min"] * 60:
                c.update(phase="charged", charged_at=c["low_since"], low_since=None)
                return "charged"
        else:
            c["low_since"] = None  # tapering off between the two levels: still charging
    return None


CHARGE_PERSISTED = ("phase", "charge_start", "charged_at", "notified", "off_done")


def server_step(c: dict, item: dict | None, now: float) -> bool:
    """Home server watchdog. c: {"since": when its plug first read down, "sent"}. True = push now."""
    if item is None:
        return False  # not in HA's device list at the moment: hold
    state, p = item.get("state"), item.get("power")
    if state == "on" and (p is None or p >= SERVER_DOWN_W):
        c.update(since=None, sent=False)  # running (again): re-armed
        return False
    c["since"] = c.get("since") or now
    if not c.get("sent") and now - c["since"] >= SERVER_DOWN_FOR:
        c["sent"] = True
        return True
    return False


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
    """Cycle trackers for linked washers / dryers / dishwashers, charge trackers for hoovers and the home server
    watchdog, keyed "<furniture id>|<plug>".

    store: AutoStore; layout: callable -> layout; items: callable -> {plug entity id: device item};
    notify: async fn(payload, category) — "appliance" goes through quiet hours, "safety" (heater / iron left on) doesn't;
    publish: fn(public state) for the SSE stream. Also runs the left-on reminders."""

    def __init__(self, store, settings, layout, notify, publish=None, clock=None, turn_off=None):
        self.store, self.settings, self.layout, self.notify, self.publish = store, settings, layout, notify, publish
        self.clock, self.turn_off = clock, turn_off  # turn_off: async fn(entity id), only for a hoover's opt-in auto-off
        saved = store.get("appliance_cycles", {}) or {}
        # Busy / quiet timers start afresh: what happened while we were down is unknown.
        self.cycles: dict[str, dict] = {k: {**{p: v.get(p) for p in PERSISTED}, "high_since": None, "low_since": None}
                                        for k, v in saved.items() if isinstance(v, dict) and v.get("phase") in ("idle", "running", "finished")}
        # Left-on timers survive a restart: a heater on for 2 h before it is still on for 2 h after.
        self.left: dict[str, dict] = {k: {"since": v.get("since"), "sent": bool(v.get("sent")), "low_since": None}
                                      for k, v in (store.get("appliance_left_on", {}) or {}).items() if isinstance(v, dict)}
        # Charges: the same rule — the trickle timer starts afresh, so no false "charged" from a gap we didn't see.
        self.charges: dict[str, dict] = {k: {**{p: v.get(p) for p in CHARGE_PERSISTED}, "low_since": None}
                                         for k, v in (store.get("appliance_charges", {}) or {}).items()
                                         if isinstance(v, dict) and v.get("phase") in ("idle", "charging", "charged")}
        # Server watchdog: only "sent" survives; the down timer starts again from what we see after a restart.
        self.servers: dict[str, dict] = {k: {"since": None, "sent": bool(v.get("sent"))}
                                         for k, v in (store.get("appliance_servers", {}) or {}).items() if isinstance(v, dict)}
        self.pending: list[tuple[dict, str]] = []
        self.pending_off: list[tuple[dict, str]] = []  # (piece, key): auto-off calls owed, each made once in tick()
        self.on_event = None  # fn(furniture, "started"|"finished"|"aborted", cycle): the activity log

    @staticmethod
    def key(f: dict) -> str:
        return f"{f['id']}|{f['plug']}"

    def _save(self) -> None:
        self.store.put("appliance_cycles", {k: {p: c.get(p) for p in PERSISTED} for k, c in self.cycles.items()})

    def _save_left(self) -> None:
        self.store.put("appliance_left_on", {k: {"since": c.get("since"), "sent": c.get("sent", False)} for k, c in self.left.items()})

    def _save_charges(self) -> None:
        self.store.put("appliance_charges", {k: {p: c.get(p) for p in CHARGE_PERSISTED} for k, c in self.charges.items()})

    def _save_servers(self) -> None:
        self.store.put("appliance_servers", {k: {"sent": c.get("sent", False)} for k, c in self.servers.items()})

    def _charge_pieces(self, layout=None) -> list[dict]:
        return [f for f in linked(layout or self.layout()) if APPLIANCES[f["type"]][0] == "charge"]

    def _server_pieces(self, layout=None) -> list[dict]:
        return [f for f in linked(layout or self.layout()) if f["type"] in SERVERS]

    def _advance_charge(self, f: dict, item: dict | None, now: float) -> bool:
        k = self.key(f)
        c = self.charges.setdefault(k, {"phase": "idle", "charge_start": None, "charged_at": None, "notified": False,
                                        "off_done": False, "low_since": None})
        ev = charge_step(c, item, thresholds(f), now)
        if ev is None:
            return False
        log.info("appliance %s (%s): %s", f["id"], f["plug"], ev)
        if ev == "charged":
            auto = f.get("auto_off") is True and not c.get("off_done") and self.turn_off is not None
            if auto:
                c["off_done"] = True  # saved before the call is made: one call per charge, never repeated
                self.pending_off.append((f, k))
            if not c.get("notified"):
                c["notified"] = True
                if self.settings().get("appliance_done", True):
                    name = f.get("label") or TYPE_NAME[f["type"]]
                    took = fmt_dur(c["charged_at"] - c["charge_start"])
                    p = {"title": f"{name} charged", "tag": f"charged-{f['id']}", "url": f"/?dev={f['plug']}",
                         "entity_id": f["plug"]}
                    if auto:
                        p["body"] = f"Charged in {took}. Switching its plug off now."
                    else:
                        p.update(body=f"Charged in {took}. Turn its plug off so it doesn't sit on the charger?",
                                 actions=[{"action": "off", "title": "Turn off"}])
                    self.pending.append((p, "appliance"))
        return True

    def _advance_server(self, f: dict, item: dict | None, now: float) -> bool:
        c = self.servers.setdefault(self.key(f), {"since": None, "sent": False})
        before = c.get("sent")
        if server_step(c, item, now):
            name = f.get("label") or TYPE_NAME[f["type"]]
            log.warning("appliance %s (%s): server plug down for %d min", f["id"], f["plug"], SERVER_DOWN_FOR // 60)
            self.pending.append(({"title": f"{name} lost power" if f.get("label") else "Home server lost power",
                                  "body": f"Its plug ({f['plug']}) has read 0 W, off or unavailable for "
                                          f"{SERVER_DOWN_FOR // 60} min.",
                                  "tag": f"server-{f['id']}", "url": f"/?dev={f['plug']}"}, "safety"))
        return before != c.get("sent")

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
        servers = [f for f in self._server_pieces(layout) if f["plug"] == item["entity_id"]]
        if [f for f in servers if self._advance_server(f, item, now)]:
            self._save_servers()
        pieces = [f for f in self._cycle_pieces(layout) if f["plug"] == item["entity_id"]]
        charges = [f for f in self._charge_pieces(layout) if f["plug"] == item["entity_id"]]
        if pieces:
            if [f for f in pieces if self._advance(f, item, now)]:
                self._save()
        if charges and [f for f in charges if self._advance_charge(f, item, now)]:
            self._save_charges()
        if pieces or charges:
            self._publish()  # with every new reading: browsers keep the server's clock for "Running 47 min"
        return bool(self.pending or self.pending_off)

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
        charges, ch_changed = self._charge_pieces(layout), False
        for f in charges:
            ch_changed |= self._advance_charge(f, items.get(f["plug"]), now)
        keep = {self.key(f) for f in charges}
        gone = [k for k in self.charges if k not in keep]
        for k in gone:
            del self.charges[k]
        if ch_changed or gone:
            self._save_charges()
        if ch_changed:
            self._publish()
        servers, sv_changed = self._server_pieces(layout), False
        for f in servers:
            sv_changed |= self._advance_server(f, items.get(f["plug"]), now)
        keep = {self.key(f) for f in servers}
        for k in [k for k in self.servers if k not in keep]:
            del self.servers[k]
            sv_changed = True
        if sv_changed:
            self._save_servers()
        offs, self.pending_off = self.pending_off, []
        for f, k in offs:
            if k not in self.charges:
                continue  # unlinked in the meantime
            log.info("appliance %s: charged, switching its plug %s off (auto-off is on)", f["id"], f["plug"])
            try:
                await self.turn_off(f["plug"])
            except Exception as e:  # not retried: the plug just stays on
                log.warning("appliance %s: auto-off of %s failed: %s", f["id"], f["plug"], e)
        pending, self.pending = self.pending, []
        for p, category in pending:
            await self.notify(p, category)

    def public(self, items: dict | None = None) -> dict:
        """GET /api/appliances and the SSE "appliances" event: per furniture id, the cycle and (with items) the status."""
        now, out = self.clock(), {}
        for f in linked(self.layout()):
            rule = APPLIANCES[f["type"]][0]
            c = self.cycles.get(self.key(f)) if rule == "cycle" else self.charges.get(self.key(f)) if rule == "charge" else None
            entry = {"plug": f["plug"], "type": f["type"]}
            if c is not None and rule == "cycle":
                entry.update(phase=c["phase"], run_start=c.get("run_start"), finished_at=c.get("finished_at"))
            elif c is not None:
                entry.update(phase=c["phase"], charge_start=c.get("charge_start"), charged_at=c.get("charged_at"))
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
