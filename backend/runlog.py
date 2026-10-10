"""Appliance run log: every run of a linked appliance with its start and end, the energy it used and what that cost,
plus an optional note added from the appliance sheet. Read-only: nothing here ever switches a device.

What counts as a run (keep the README's *Run log* in step):
  washer / dryer / dishwasher — a cycle exactly as the live cycle tracker (backend/appliances.py) sees it: from its
      "Running" start to "Finished" (outcome "finished"), or to the plug being switched off mid-cycle ("stopped").
  hoover — a charge, from "Charging" to "Charged" ("charged"), or unplugged mid-charge ("stopped").
  everything else (kettle, heater, TV, fan, …) — the plug drawing more than the appliance's busy level (3 W for fans,
      lamps and other "on" appliances) until it has stayed below it for END_GRACE (1 min for kettles, microwaves,
      coffee machines and toasters; 10 min for the rest, so a heater's thermostat pauses don't split a run), or the plug
      goes off. Runs under MIN_RUN are dropped (a power spike isn't a use).
  fridges, freezers and home servers are always on and have no runs.

Energy comes from the plug's live power readings, integrated by Meter (power holds until the next reading); cost
splits it by half-hour, at the Octopus rate of each half-hour when the tariff is used for costs (backend/tariff.py),
else at the flat unit rate (Energy → Set tariff). Runs that began before a restart are marked "partial" (energy only
since the restart). Timing uses an injected clock (epoch seconds), so it is unit-tested without waiting.
"""
import logging
import sqlite3
import threading

from .appliances import APPLIANCES, LEFT_W, PROTECTED, TYPE_NAME, linked, thresholds

log = logging.getLogger("homecontrol.runlog")

SHORT = ("kettle", "microwave", "coffee_machine", "toaster")
END_GRACE = {t: 60 for t in SHORT}
DEFAULT_GRACE = 600
MIN_RUN = 20             # seconds
KEEP_SAMPLES = 26 * 3600  # the meter remembers this much (longer than any cycle or charge)
SAMPLE_EVERY = 30         # a tick adds a meter sample at most this often (readings always add one)
KEEP_DAYS = 400
NOTE_MAX = 200
PAGE = 20
STATE_KEY = "runlog_state"


def tracked(f: dict) -> bool:
    return f.get("type") in APPLIANCES and f["type"] not in PROTECTED


def rule_of(f: dict) -> str:
    return APPLIANCES[f["type"]][0]


def busy_level(f: dict) -> float:
    return thresholds(f).get("on_w", LEFT_W) if rule_of(f) == "busy" else LEFT_W


def display_name(f: dict) -> str:
    return f.get("label") or TYPE_NAME.get(f["type"]) or f["type"].replace("_", " ").capitalize()


# ---------------- energy meter ----------------
class Meter:
    """Cumulative kWh and pence per plug from live power readings. price(p_w, t0_s, t1_s) -> (kWh, pence | None)."""

    def __init__(self, price):
        self.price = price
        self.samples: dict[str, list[list]] = {}  # plug -> [[t, kWh, pence, unpriced kWh, W]]; W holds until the next

    def feed(self, plug: str, t: float, p: float | None, force: bool = True) -> None:
        s = self.samples.setdefault(plug, [])
        if s:
            last = s[-1]
            if t <= last[0]:
                if t == last[0]:
                    last[4] = p
                return
            if not force and t - last[0] < SAMPLE_EVERY and p == last[4]:
                return
            s.append([t, *self._grow(last, t), p])
        else:
            s.append([t, 0.0, 0.0, 0.0, p])
        cut = t - KEEP_SAMPLES
        while len(s) > 2 and s[1][0] <= cut:
            s.pop(0)

    def _grow(self, row: list, t: float) -> tuple[float, float, float]:
        """The counters of sample row carried on to time t at its power."""
        kwh, pence, unpriced = row[1], row[2], row[3]
        if row[4] is not None and row[4] > 0 and t > row[0]:
            k, c = self.price(row[4], row[0], t)
            kwh += k
            if c is None:
                unpriced += k
            else:
                pence += c
        return kwh, pence, unpriced

    def first(self, plug: str) -> float | None:
        s = self.samples.get(plug)
        return s[0][0] if s else None

    def at(self, plug: str, t: float):
        """(kWh, pence, unpriced kWh) counters at time t, or None when the meter has nothing up to t."""
        prev = None
        for row in self.samples.get(plug) or []:
            if row[0] > t:
                break
            prev = row
        return self._grow(prev, t) if prev is not None else None

    def between(self, plug: str, a: float, b: float) -> tuple[float | None, float | None, bool]:
        """(kWh, pence | None, partial) over [a, b]; partial when the meter started after a."""
        first = self.first(plug)
        if first is None or b <= first:
            return None, None, True
        partial = a < first
        x, y = self.at(plug, max(a, first)), self.at(plug, b)
        if x is None or y is None:
            return None, None, True
        kwh = max(0.0, y[0] - x[0])
        pence = round(max(0.0, y[1] - x[1]), 2) if y[2] - x[2] < 1e-9 else None
        return round(kwh, 4), pence, partial

    def forget(self, keep: set[str]) -> None:
        for plug in [p for p in self.samples if p not in keep]:
            del self.samples[plug]


# ---------------- busy runs (kettle, heater, TV, …) ----------------
def busy_step(c: dict, item: dict | None, level: float, grace: float, now: float, rule: str) -> tuple[float, float] | None:
    """Advance a busy-run state c {"since", "low_since"}. Returns (start, end) when a run ends, else None."""
    state = (item or {}).get("state")
    p = (item or {}).get("power")
    if state == "off":
        if c.get("since") is not None:
            run = (c["since"], c.get("low_since") or now)
            c.update(since=None, low_since=None)
            return run
        c["low_since"] = None
        return None
    if state != "on":
        return None  # unavailable / unknown: hold
    drawing = p > level if p is not None else rule == "on"
    if drawing:
        c["low_since"] = None
        if c.get("since") is None:
            c["since"] = now
        return None
    if c.get("since") is None:
        return None
    c["low_since"] = c.get("low_since") or now
    if now - c["low_since"] >= grace:
        run = (c["since"], c["low_since"])
        c.update(since=None, low_since=None)
        return run
    return None


# ---------------- storage ----------------
class RunStore:
    def __init__(self, path: str):
        self.path, self._lock = path, threading.Lock()
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS appliance_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, fid TEXT NOT NULL, "
                      "plug TEXT, type TEXT, start REAL NOT NULL, end REAL NOT NULL, kwh REAL, cost_p REAL, "
                      "partial INTEGER NOT NULL DEFAULT 0, outcome TEXT, note TEXT)")
            c.execute("CREATE INDEX IF NOT EXISTS appliance_runs_fid ON appliance_runs (fid, start)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    @staticmethod
    def _row(r) -> dict:
        return {"id": r[0], "fid": r[1], "plug": r[2], "type": r[3], "start": int(r[4] * 1000), "end": int(r[5] * 1000),
                "minutes": round((r[5] - r[4]) / 60), "kwh": r[6], "cost_p": r[7], "partial": bool(r[8]),
                "outcome": r[9], "note": r[10] or ""}

    def add(self, run: dict) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute("INSERT INTO appliance_runs (fid, plug, type, start, end, kwh, cost_p, partial, outcome, note) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')",
                            (run["fid"], run["plug"], run["type"], run["start"], run["end"], run.get("kwh"),
                             run.get("cost_p"), int(bool(run.get("partial"))), run.get("outcome")))
            return cur.lastrowid

    def page(self, fid: str, before: float | None = None, limit: int = PAGE) -> list[dict]:
        q, args = "SELECT * FROM appliance_runs WHERE fid = ?", [fid]
        if before is not None:
            q += " AND start < ?"
            args.append(before)
        q += " ORDER BY start DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._lock, self._conn() as c:
            return [self._row(r) for r in c.execute(q, args).fetchall()]

    def get(self, fid: str, rid: int) -> dict | None:
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM appliance_runs WHERE fid = ? AND id = ?", (fid, rid)).fetchone()
        return self._row(r) if r else None

    def set_note(self, fid: str, rid: int, note: str) -> dict | None:
        with self._lock, self._conn() as c:
            n = c.execute("UPDATE appliance_runs SET note = ? WHERE fid = ? AND id = ?", (note, fid, rid)).rowcount
        return self.get(fid, rid) if n else None

    def recent_minutes(self, fid: str, n: int = 5) -> list[float]:
        """Lengths (min) of the last n complete runs."""
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT start, end FROM appliance_runs WHERE fid = ? AND outcome IN ('finished', 'charged') "
                             "ORDER BY start DESC LIMIT ?", (fid, n)).fetchall()
        return [(b - a) / 60 for a, b in rows]

    def prune(self, before: float) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM appliance_runs WHERE end < ?", (before,))


def clean_note(v) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError("note must be text")
    v = " ".join(v.split())
    if len(v) > NOTE_MAX:
        raise ValueError(f"note must be at most {NOTE_MAX} characters")
    return v


# ---------------- the tracker ----------------
class RunLog:
    """Watches linked appliances and logs their runs.

    kv: AutoStore (state across restarts); layout: callable -> layout; appliances: the live cycle / charge trackers
    (backend/appliances.py Appliances: .cycles, .charges, .key); price: fn(p_w, t0, t1) -> (kWh, pence | None);
    clock: epoch seconds."""

    def __init__(self, store: RunStore, kv, layout, appliances, price, clock):
        self.store, self.kv, self.layout, self.appliances, self.clock = store, kv, layout, appliances, clock
        self.meter = Meter(price)
        saved = kv.get(STATE_KEY, {}) or {}
        self.seen: dict[str, dict] = {k: v for k, v in (saved.get("seen") or {}).items() if isinstance(v, dict)}
        # A busy run open when we stopped keeps its start; the quiet timer starts afresh.
        self.busy: dict[str, dict] = {k: {"since": v.get("since"), "low_since": None}
                                      for k, v in (saved.get("busy") or {}).items() if isinstance(v, dict)}
        self.on_run = None  # fn(run) after a run is stored

    def _save(self) -> None:
        self.kv.put(STATE_KEY, {"seen": self.seen, "busy": {k: {"since": c.get("since")} for k, c in self.busy.items()}})

    def pieces(self, layout=None) -> list[dict]:
        return [f for f in linked(layout or self.layout()) if tracked(f)]

    def _log(self, f: dict, start: float, end: float, outcome: str) -> dict | None:
        if end - start < MIN_RUN:
            return None
        kwh, pence, partial = self.meter.between(f["plug"], start, end)
        run = {"fid": f["id"], "plug": f["plug"], "type": f["type"], "start": start, "end": end, "kwh": kwh,
               "cost_p": pence, "partial": partial, "outcome": outcome}
        run["id"] = self.store.add(run)
        log.info("run log: %s %s %.0f min, %s kWh", f["id"], outcome, (end - start) / 60, kwh)
        if self.on_run:
            try:
                self.on_run(run)
            except Exception as e:
                log.warning("run log: on_run failed: %s", e)
        return run

    def _poll_tracker(self, f: dict, now: float) -> bool:
        """Cycles and charges: follow the live tracker's phases; a finished / aborted one becomes a run."""
        k = self.appliances.key(f)
        charge = rule_of(f) == "charge"
        c = (self.appliances.charges if charge else self.appliances.cycles).get(k)
        if c is None:
            return False
        busy, done = ("charging", "charged") if charge else ("running", "finished")
        start_key, end_key = ("charge_start", "charged_at") if charge else ("run_start", "finished_at")
        seen = self.seen.get(k) or {}
        phase, start = c.get("phase"), c.get(start_key)
        if seen.get("phase") == busy and seen.get("start") is not None:
            if phase == done and start == seen["start"] and c.get(end_key):
                self._log(f, seen["start"], c[end_key], done)
            elif phase == "idle" or (phase == busy and start != seen["start"]):
                self._log(f, seen["start"], now, "stopped")
        new = {"phase": phase, "start": start}
        if new != seen:
            self.seen[k] = new
            return True
        return False

    def _advance_busy(self, f: dict, item: dict | None, now: float) -> bool:
        k = self.appliances.key(f)
        c = self.busy.setdefault(k, {"since": None, "low_since": None})
        before = c.get("since")
        run = busy_step(c, item, busy_level(f), END_GRACE.get(f["type"], DEFAULT_GRACE), now, rule_of(f))
        if run:
            self._log(f, *run, "finished")
        return before != c.get("since")

    def _step(self, f: dict, item: dict | None, now: float) -> bool:
        if rule_of(f) in ("cycle", "charge"):
            return self._poll_tracker(f, now)
        return self._advance_busy(f, item, now)

    def observe(self, item: dict, raw=None) -> None:
        """Live observer: every reading of a plug feeds its meter and moves its runs on at once."""
        if item.get("kind") != "plug":
            return
        pieces = [f for f in self.pieces() if f["plug"] == item["entity_id"]]
        if not pieces:
            return
        now = self.clock()
        self.meter.feed(item["entity_id"], now, item.get("power") if item.get("state") == "on" else 0.0)
        if [f for f in pieces if self._step(f, item, now)]:
            self._save()

    def tick(self, items: dict) -> None:
        """Time passes: the meter carries on with the last reading, cycles finish, quiet timers run out."""
        now, pieces, changed = self.clock(), self.pieces(), False
        for plug in {f["plug"] for f in pieces}:
            item = items.get(plug)
            if item is not None:
                self.meter.feed(plug, now, item.get("power") if item.get("state") == "on" else 0.0, force=False)
        for f in pieces:
            changed |= self._step(f, items.get(f["plug"]), now)
        keep = {self.appliances.key(f) for f in pieces}
        for d in (self.seen, self.busy):
            for k in [k for k in d if k not in keep]:  # unlinked, relinked or deleted
                del d[k]
                changed = True
        self.meter.forget({f["plug"] for f in pieces})
        if changed:
            self._save()

    def prune(self) -> None:
        self.store.prune(self.clock() - KEEP_DAYS * 86400)

    def open_run(self, f: dict) -> dict | None:
        """The run going on now, if any: {"start" ms, "kwh"}."""
        k, now = self.appliances.key(f), self.clock()
        if rule_of(f) in ("cycle", "charge"):
            s = self.seen.get(k) or {}
            start = s.get("start") if s.get("phase") in ("running", "charging") else None
        else:
            start = (self.busy.get(k) or {}).get("since")
        if start is None:
            return None
        kwh, pence, partial = self.meter.between(f["plug"], start, now)
        return {"start": int(start * 1000), "kwh": kwh, "cost_p": pence, "partial": partial}


def add_routes(app, runlog: RunLog, json_body) -> None:
    """GET /api/appliances/{fid}/runs?before=<ms>&limit=n, PUT /api/appliances/{fid}/runs/{rid} {"note"}."""
    from fastapi import HTTPException, Request

    def piece(fid: str) -> dict | None:
        return next((f for f in linked(runlog.layout()) if f["id"] == fid), None)

    @app.get("/api/appliances/{fid}/runs")
    async def list_runs(fid: str, before: int | None = None, limit: int = PAGE):
        limit = max(1, min(100, limit))
        runs = runlog.store.page(fid, before / 1000 if before is not None else None, limit + 1)
        more = len(runs) > limit
        runs = runs[:limit]
        f = piece(fid)
        return {"fid": fid, "tracked": bool(f and tracked(f)), "now": int(runlog.clock() * 1000),
                "current": runlog.open_run(f) if f and tracked(f) else None, "runs": runs,
                "next_before": runs[-1]["start"] if more and runs else None}

    @app.put("/api/appliances/{fid}/runs/{rid}")
    async def put_run_note(fid: str, rid: int, request: Request):
        body = await json_body(request)
        if not isinstance(body, dict) or set(body) != {"note"}:
            raise HTTPException(400, 'send {"note": text}')
        try:
            note = clean_note(body["note"])
        except ValueError as e:
            raise HTTPException(400, str(e))
        run = runlog.store.set_note(fid, rid, note)
        if run is None:
            raise HTTPException(404, "no such run")
        return run
