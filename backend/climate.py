"""Smart preheat and damp / mould warnings, per room.

Preheat (opt-in per room, plus a master switch; both off by default): each room's warm-up rate (°C per hour) is learnt
from its radiator valves — live, and from HA's recorder history when preheat is switched on for a room. A warm-up is a
target raised at least MIN_GAP above the room's temperature; it ends when the room gets within REACHED of the target
(or after MAX_EPISODE, or when the target changes). Before a heating schedule (backend/schedules.py) that sets this
room's radiators, preheat sets them to the schedule's temperature early enough that the room is warm on time:
lead = (setpoint − now) / rate × MARGIN, capped at max_lead_min.

Safety: one service call per (schedule occurrence, room), recorded in SQLite before the call and never repeated (a
failed call is logged, not retried); nothing while Away, while a window in the room is open (or window heating holds
one of its radiators), or when the room is already warm / already set to the setpoint. The schedule itself still runs
on time as usual. The loop runs every CHECK_EVERY s and takes the automations' lock, so it never interleaves with
Away/Home, schedules or window heating.

Damp: a room whose humidity has stayed at or above damp_humidity while its temperature is at or below damp_temp for
damp_minutes is at risk of damp and mould. Shown in the Climate sheet; the push (off by default, a quiet-hours
category) goes out once per room per damp_cooldown_h, and suggests the dehumidifier.
"""
import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from .activity import acting
from .alerts import parse_time
from .geometry import opening_rooms, placed_in
from .live import build_device
from .schedules import next_run

log = logging.getLogger("homecontrol.climate")

CHECK_EVERY = 60
STARTUP_DELAY = 20
DEBOUNCE = 1.0          # coalesce a burst of settings changes into one check
DEFAULTS = {"preheat_enabled": False, "max_lead_min": 120,
            "damp_push": False, "damp_humidity": 70, "damp_temp": 16.0, "damp_minutes": 120, "damp_cooldown_h": 12}
ROOM_DEFAULTS = {"preheat": False, "humidity_entity": None}
INTS = {"max_lead_min": (15, 240), "damp_humidity": (50, 95), "damp_minutes": (15, 720), "damp_cooldown_h": (1, 72)}
DAMP_TEMP = (10, 22)
# warm-up learning
MIN_GAP = 1.0           # a target this far above the room starts a warm-up
REACHED = 0.3           # within this of the target: warm
MIN_RISE = 0.5          # ignore warm-ups that moved less than this …
MIN_DURATION = 10 * 60  # … or took less than this
MAX_EPISODE = 6 * 3600  # a warm-up still going after this long ends with the rise so far
RATE = (0.2, 8.0)       # °C per hour, clamped
ALPHA = 0.3             # weight of a new warm-up in the running average
DEFAULT_RATE = 1.0      # until a room has learnt its own
MARGIN = 1.15
MIN_LEAD = 5 * 60       # less than this early: leave it to the schedule
LEARN_DAYS = 7
LOOK_AHEAD = 24 * 3600  # status shows the next heating schedule up to this far ahead
# damp
HYST_H, HYST_T = 3, 0.5  # once at risk, it stays at risk until humidity drops 3 % or it warms 0.5° past the limits
LOG_MAX = 30
BAD = ("unavailable", "unknown", None, "")


class ClimateError(ValueError):
    pass


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ---------------- settings ----------------
def validate_settings(data, current: dict) -> dict:
    if not isinstance(data, dict) or not set(data) <= set(DEFAULTS):
        raise ClimateError(f"send any of {{{', '.join(DEFAULTS)}}}")
    out = {**DEFAULTS, **current}
    for k in ("preheat_enabled", "damp_push"):
        if k in data:
            if not isinstance(data[k], bool):
                raise ClimateError(f"{k} must be true or false")
            out[k] = data[k]
    for k, (lo, hi) in INTS.items():
        if k in data:
            v = data[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ClimateError(f"{k} must be a whole number from {lo} to {hi}")
            out[k] = v
    if "damp_temp" in data:
        v, (lo, hi) = _num(data["damp_temp"]), DAMP_TEMP
        if v is None or not lo <= v <= hi:
            raise ClimateError(f"damp_temp must be {lo}–{hi}")
        out["damp_temp"] = round(v * 2) / 2
    return out


def validate_room(data, current: dict, states: dict) -> dict:
    if not isinstance(data, dict) or not data or not set(data) <= set(ROOM_DEFAULTS):
        raise ClimateError("send {preheat, humidity_entity}")
    out = {**ROOM_DEFAULTS, **current}
    if "preheat" in data:
        if not isinstance(data["preheat"], bool):
            raise ClimateError("preheat must be true or false")
        out["preheat"] = data["preheat"]
    if "humidity_entity" in data:
        e = data["humidity_entity"]
        if e is not None and (not isinstance(e, str) or not e.startswith("sensor.")):
            raise ClimateError("humidity_entity must be a sensor.* entity or null")
        if e is not None and states and e not in states:
            raise ClimateError(f"{e} isn't in Home Assistant")
        out["humidity_entity"] = e
    return out


# ---------------- warm-up learning ----------------
class WarmUp:
    """Feed one valve's readings in time order; feed() returns a learnt rate (°C/h) when a warm-up ends."""

    def __init__(self):
        self.prev_target: float | None = None
        self.ep: dict | None = None

    def feed(self, t: float, target: float | None, cur: float | None) -> float | None:
        """Readings come only when something changes (HA history, live state events), so a long quiet spell is not a
        gap; the valve going unavailable is, and drops the warm-up."""
        if target is None or cur is None:
            self.ep = None
            return None
        ep, out = self.ep, None
        if ep and t < ep["last"]:
            ep = None
        elif ep and target != ep["target"]:
            out, ep = self._done(ep, ep["last"], ep["max"]), None  # as far as it got under the old target
        if ep:
            ep["last"], ep["max"] = t, max(ep["max"], cur)
            if cur >= ep["target"] - REACHED or t - ep["t0"] >= MAX_EPISODE:
                out, ep = self._done(ep, t, cur), None
        elif self.prev_target is not None and target > self.prev_target and target - cur >= MIN_GAP:
            ep = {"t0": t, "c0": cur, "target": target, "last": t, "max": cur}
        self.ep, self.prev_target = ep, target
        return out

    @staticmethod
    def _done(ep: dict, t: float, cur: float) -> float | None:
        rise, secs = cur - ep["c0"], t - ep["t0"]
        if rise < MIN_RISE or secs < MIN_DURATION:
            return None
        return min(RATE[1], max(RATE[0], rise / (secs / 3600)))


def blend(old: dict | None, rate: float, at: float) -> dict:
    """Running average of a valve's warm-up rates: {"rate", "n", "at"}."""
    if not old or not old.get("n"):
        return {"rate": round(rate, 3), "n": 1, "at": at}
    return {"rate": round(old["rate"] * (1 - ALPHA) + rate * ALPHA, 3), "n": old["n"] + 1, "at": at}


def history_series(rows: list[dict]) -> list[tuple[float, float | None, float | None]]:
    """HA history rows of a climate entity (with attributes) -> [(t, target, current)] in time order."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        t = parse_time(r.get("last_updated") or r.get("last_changed"))
        if t is None:
            continue
        a = r.get("attributes") or {}
        ok = r.get("state") not in BAD
        out.append((t, _num(a.get("temperature")) if ok else None, _num(a.get("current_temperature")) if ok else None))
    out.sort(key=lambda x: x[0])
    return out


def learn_from(series) -> list[float]:
    w, rates = WarmUp(), []
    for t, tg, cur in series:
        r = w.feed(t, tg, cur)
        if r is not None:
            rates.append(r)
    return rates


def lead_seconds(setpoint: float, cur: float, rate: float, max_lead_min: int) -> float:
    return min(max_lead_min * 60, max(0.0, (setpoint - cur) / rate * 3600 * MARGIN))


# ---------------- engine ----------------
class Climate:
    """store: AutoStore. Keys: climate_settings, climate_rooms {room id: {preheat, humidity_entity}}, climate_rates
    {valve: {rate, n, at}}, climate_preheat_done {"sid|occurrence|room": ts}, climate_damp {room: {since, pushed}},
    climate_log."""

    def __init__(self, store, live, call, layout, mode, schedules, window, notify, lock, ensure_devices=None,
                 clock=time.time, tz=None):
        self.store, self.live, self.call, self.layout, self.mode = store, live, call, layout, mode
        self.schedules, self.window, self.notify, self.lock, self.ensure_devices = schedules, window, notify, lock, ensure_devices
        self.clock, self.tz = clock, tz or timezone.utc
        self.learners: dict[str, WarmUp] = {}
        self.seen: dict[str, tuple] = {}   # valve -> last (target, current) fed, so a quiet valve isn't fed every tick
        self.damp: dict = store.get("climate_damp", {}) or {}
        self.wake = asyncio.Event()  # settings changed: check now rather than at the next minute
        self._task: asyncio.Task | None = None

    # ---- settings ----
    def settings(self) -> dict:
        return {**DEFAULTS, **(self.store.get("climate_settings", {}) or {})}

    def put_settings(self, data) -> dict:
        old = self.settings()
        new = validate_settings(data, old)
        self.store.put("climate_settings", new)
        if new["preheat_enabled"] != old["preheat_enabled"]:
            self._log(None, "Smart preheat on" if new["preheat_enabled"] else "Smart preheat off")
        return new

    def rooms_cfg(self) -> dict:
        return self.store.get("climate_rooms", {}) or {}

    def room_cfg(self, rid: str) -> dict:
        return {**ROOM_DEFAULTS, **self.rooms_cfg().get(rid, {})}

    def put_room(self, room: dict, data) -> dict:
        old = self.room_cfg(room["id"])
        new = validate_room(data, old, self.live.states)
        if new["preheat"] and not old["preheat"] and not self.valves_in(room):
            raise ClimateError("there's no radiator valve in this room")
        all_ = self.rooms_cfg()
        all_[room["id"]] = new
        self.store.put("climate_rooms", all_)
        if new["preheat"] != old["preheat"]:
            self._log(room["name"], "Preheat on" if new["preheat"] else "Preheat off")
        return new

    def rates(self) -> dict:
        return self.store.get("climate_rates", {}) or {}

    def _log(self, room: str | None, action: str, note: str = "") -> None:
        logs = self.store.get("climate_log", []) or []
        logs.insert(0, {"at": self.clock(), "room": room, "action": action, "note": note})
        self.store.put("climate_log", logs[:LOG_MAX])
        log.info("climate: %s %s %s", room or "", action, note)

    # ---- helpers ----
    def valves_in(self, room: dict) -> list[str]:
        valves = [e for e, d in self.live.devices.items() if d.kind == "valve"]
        return sorted(placed_in(self.layout(), room, valves))

    def room_rate(self, valves: list[str]) -> dict:
        rs = self.rates()
        got = [rs[v] for v in valves if v in rs]
        if not got:
            return {"rate": None, "n": 0, "at": None}
        return {"rate": round(_mean(r["rate"] for r in got), 2), "n": sum(r["n"] for r in got), "at": max(r["at"] for r in got)}

    def window_open(self, layout: dict, room: dict, states: dict) -> bool:
        for o in layout.get("openings", []):
            if o.get("type") == "window" and o.get("entity_id") and (states.get(o["entity_id"]) or {}).get("state") == "on":
                if any(r.get("id") == room.get("id") for r in opening_rooms(layout, o)):
                    return True
        return False

    def _valve(self, states: dict, v: str) -> tuple[float | None, float | None]:
        s = states.get(v) or {}
        if s.get("state") in BAD:
            return None, None
        a = s.get("attributes") or {}
        return _num(a.get("temperature")), _num(a.get("current_temperature"))

    # ---- learning ----
    def observe_valves(self, states: dict, now: float) -> None:
        changed, rates = False, self.rates()
        for e, d in self.live.devices.items():
            if d.kind != "valve":
                continue
            tc = self._valve(states, e)
            if self.seen.get(e) == tc:
                continue
            self.seen[e] = tc
            r = self.learners.setdefault(e, WarmUp()).feed(now, *tc)
            if r is not None:
                rates[e], changed = blend(rates.get(e), r, now), True
                log.info("climate: %s warmed at %.2f °C/h", e, r)
        if changed:
            self.store.put("climate_rates", rates)

    async def learn_history(self, ha, valves: list[str], days: int = LEARN_DAYS) -> dict:
        """Re-learn these valves from HA's history (replaces what they had learnt: history includes it)."""
        now = self.clock()
        end = datetime.fromtimestamp(now, timezone.utc)
        data = await ha.history(end - timedelta(days=days), end, valves, attributes=True)
        rows: dict[str, list] = {}
        for group in data if isinstance(data, list) else []:
            if isinstance(group, list) and group:
                eid = next((r.get("entity_id") for r in group if isinstance(r, dict) and r.get("entity_id")), None)
                if eid in valves:
                    rows.setdefault(eid, []).extend(group)
        rates, found = self.rates(), 0
        for v in valves:
            got = learn_from(history_series(rows.get(v, [])))
            found += len(got)
            if got:
                acc = None
                for r in got:
                    acc = blend(acc, r, now)
                rates[v] = acc
        self.store.put("climate_rates", rates)
        return {"warmups": found}

    # ---- preheat ----
    def _heating_schedules(self, valves: list[str]) -> list[dict]:
        st = self.schedules.store.settings()
        if not st["enabled"]:
            return []
        return [s for s in self.schedules.store.all() if s["enabled"] and s["action"]["type"] == "temperature"
                and set(s["target"].get("entity_ids", [])) & set(valves)]

    def plan(self, room: dict, states: dict, now: float, horizon: float) -> dict | None:
        """The next heating schedule for this room within horizon: {sid, name, at, setpoint, valves, cur, lead, start}."""
        valves = self.valves_in(room)
        if not valves:
            return None
        st, best = self.schedules.store.settings(), None
        for s in self._heating_schedules(valves):
            nxt = next_run(s, now, self.tz, st["lat"], st["lon"])
            if nxt is None or nxt - now > horizon or (best and nxt >= best["at"]):
                continue
            mine = [v for v in valves if v in s["target"]["entity_ids"]]
            best = {"sid": s["id"], "name": s["name"], "at": nxt, "setpoint": s["action"]["value"], "valves": mine}
        if not best:
            return None
        cur = _mean(self._valve(states, v)[1] for v in best["valves"])
        rate = self.room_rate(valves)["rate"] or DEFAULT_RATE
        lead = None if cur is None else lead_seconds(best["setpoint"], cur, rate, self.settings()["max_lead_min"])
        return {**best, "cur": cur, "lead": lead, "start": None if lead is None else best["at"] - lead}

    def blocked(self, room: dict, layout: dict, states: dict) -> str | None:
        if self.mode().get("mode") == "away":
            return "away"
        if self.window_open(layout, room, states):
            return "window open"
        held = self.window.held() if self.window else {}
        if any(v in held for v in self.valves_in(room)):
            return "window open"
        return None

    async def preheat(self, states: dict, now: float) -> list[dict]:
        s = self.settings()
        if not s["preheat_enabled"]:
            return []
        layout, cfg = self.layout(), self.rooms_cfg()
        done: dict = self.store.get("climate_preheat_done", {}) or {}
        calls = []
        for room in layout.get("rooms", []):
            if not cfg.get(room.get("id"), {}).get("preheat"):
                continue
            p = self.plan(room, states, now, s["max_lead_min"] * 60)
            if not p or p["cur"] is None or p["start"] is None or now < p["start"] or p["at"] - now < MIN_LEAD:
                continue
            key = f"{p['sid']}|{int(p['at'])}|{room['id']}"
            if key in done:
                continue
            if p["cur"] >= p["setpoint"] - REACHED:
                continue  # already warm
            ids = [v for v in p["valves"] if (self._valve(states, v)[0] or 0) < p["setpoint"]]
            if not ids:
                continue  # already set to it (by hand, or a previous preheat)
            why = self.blocked(room, layout, states)
            if why:
                continue
            done[key] = now  # recorded before the call: never twice for this occurrence, even if it fails
            self.store.put("climate_preheat_done", {k: v for k, v in done.items() if now - v < 3 * 86400})
            data = {"entity_id": ids, "temperature": p["setpoint"]}
            when = datetime.fromtimestamp(p["at"], self.tz).strftime("%H:%M")
            note = f"{p['setpoint']:g}° now, {round((p['at'] - now) / 60)} min before “{p['name']}” at {when} ({p['cur']:.1f}° now)"
            try:
                with acting("smart preheat"):
                    await self.call("climate", "set_temperature", data)
                self._log(room["name"], "Preheat started", note)
            except Exception as e:
                log.warning("preheat %s failed: %s", room["name"], e)
                self._log(room["name"], "Preheat failed", f"Home Assistant error: {e}")
            calls.append(data)
        return calls

    # ---- damp ----
    def readings(self, room: dict, states: dict) -> dict:
        """Humidity and temperature of a room: its chosen humidity sensor, else devices placed in it."""
        layout, devs = self.layout(), self.live.devices
        ids = placed_in(layout, room, list(devs))
        items = [build_device(devs[e], states) for e in ids if devs[e].kind in ("valve", "dehumidifier")]
        h, hum_src = None, None
        he = self.room_cfg(room["id"])["humidity_entity"]
        if he:
            raw = states.get(he) or {}
            h = None if raw.get("state") in BAD else _num(raw.get("state"))
            hum_src = (raw.get("attributes") or {}).get("friendly_name") or he
        if h is None:
            for it in items:  # a dehumidifier's humidity, or a valve that reports one (most KE100s don't)
                attrs = (states.get(it["entity_id"]) or {}).get("attributes") or {}
                v = it.get("current_humidity") if it["kind"] == "dehumidifier" else _num(attrs.get("current_humidity"))
                if v is not None and it["state"] not in BAD:
                    h, hum_src = v, it["name"]
                    break
        t = _mean(it.get("current_temperature") for it in items if it["kind"] == "valve" and it["state"] not in BAD)
        if t is None:
            t = _mean(it.get("current_temperature") for it in items if it["kind"] == "dehumidifier" and it["state"] not in BAD)
        return {"humidity": None if h is None else round(h, 1), "temperature": None if t is None else round(t, 1),
                "humidity_source": hum_src}

    def dehumidifier_for(self, room: dict, states: dict) -> dict | None:
        devs = self.live.devices
        dh = [e for e, d in devs.items() if d.kind == "dehumidifier" and not d.hidden]
        if not dh:
            return None
        here = placed_in(self.layout(), room, dh)
        e = sorted(here)[0] if here else sorted(dh)[0]
        it = build_device(devs[e], states)
        return {"entity_id": e, "name": it["name"], "state": it["state"], "in_room": bool(here)}

    async def check_damp(self, states: dict, now: float) -> list[dict]:
        s, layout, sent, changed = self.settings(), self.layout(), [], False
        rooms = {r.get("id"): r for r in layout.get("rooms", [])}
        for rid in [k for k in self.damp if k not in rooms]:
            del self.damp[rid]
            changed = True
        for rid, room in rooms.items():
            rd = self.readings(room, states)
            h, t = rd["humidity"], rd["temperature"]
            if h is None or t is None:
                continue  # no reading: keep whatever we had, decide when it's back
            m = self.damp.setdefault(rid, {"since": None, "pushed": None})
            at_risk = h >= s["damp_humidity"] and t <= s["damp_temp"]
            still = m["since"] is not None and h >= s["damp_humidity"] - HYST_H and t <= s["damp_temp"] + HYST_T
            if at_risk or still:
                if m["since"] is None:
                    m["since"], changed = now, True
                due = now - m["since"] >= s["damp_minutes"] * 60
                cool = m["pushed"] is None or now - m["pushed"] >= s["damp_cooldown_h"] * 3600
                if due and cool and s["damp_push"]:
                    m["pushed"], changed = now, True
                    self.store.put("climate_damp", self.damp)  # marked first: a failing push can't repeat it
                    hours = (now - m["since"]) / 3600
                    span = f"{hours:.0f} h" if hours >= 1.5 else f"{round(hours * 60)} min"
                    body = f"Humidity {h:.0f} % at {t:.1f}° for {span}."
                    dh = self.dehumidifier_for(room, states)
                    if dh:
                        body += f" Run the {dh['name']}" + (" — it's off." if dh["state"] == "off" else ".")
                    else:
                        body += " Air the room or run a dehumidifier."
                    payload = {"title": f"Damp risk: {room.get('name') or 'Room'}", "body": body,
                               "tag": f"damp-{rid}", "url": "/?climate"}
                    await self.notify(payload)
                    self._log(room.get("name"), "Damp warning sent", body)
                    sent.append(payload)
            elif m["since"] is not None:
                m["since"], changed = None, True
        for rid in [k for k, m in self.damp.items() if m["since"] is None and m["pushed"] is None]:
            del self.damp[rid]
        if changed:
            self.store.put("climate_damp", self.damp)
        return sent

    # ---- loop ----
    async def tick(self) -> None:
        if not self.live.devices and self.ensure_devices:
            try:
                await self.ensure_devices()
            except Exception as e:
                log.warning("climate: device discovery failed: %s", e)
        states, now = self.live.states, self.clock()
        if not self.live.devices or not states:
            return
        self.observe_valves(states, now)
        async with self.lock:
            await self.preheat(states, now)
        await self.check_damp(states, now)

    async def _run(self, every: float, delay: float) -> None:
        await asyncio.sleep(delay)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("climate check failed: %s", e)
            try:
                await asyncio.wait_for(self.wake.wait(), every)
                await asyncio.sleep(DEBOUNCE)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    def start(self, every: float | None = None, delay: float | None = None) -> None:
        self._task = asyncio.create_task(self._run(every or CHECK_EVERY, STARTUP_DELAY if delay is None else delay))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ---- API ----
    def status(self) -> dict:
        s, layout, states, now = self.settings(), self.layout(), self.live.states, self.clock()
        cfg, rooms = self.rooms_cfg(), []
        done = self.store.get("climate_preheat_done", {}) or {}
        for room in layout.get("rooms", []):
            rid = room.get("id")
            valves = self.valves_in(room)
            c = {**ROOM_DEFAULTS, **cfg.get(rid, {})}
            rate = self.room_rate(valves)
            p = self.plan(room, states, now, LOOK_AHEAD) if valves else None
            nxt = None
            if p:
                nxt = {"name": p["name"], "at": int(p["at"] * 1000), "setpoint": p["setpoint"],
                       "start": int(p["start"] * 1000) if p["start"] is not None else None,
                       "lead_min": round(p["lead"] / 60) if p["lead"] is not None else None}
                started = done.get(f"{p['sid']}|{int(p['at'])}|{rid}")
                nxt["started"] = int(started * 1000) if started else None
            rd = self.readings(room, states)
            m = self.damp.get(rid) or {}
            since = m.get("since")
            damp = {**rd, "at_risk": since is not None,
                    "since": int(since * 1000) if since else None,
                    "sustained": since is not None and now - since >= s["damp_minutes"] * 60,
                    "pushed": int(m["pushed"] * 1000) if m.get("pushed") else None,
                    "dehumidifier": self.dehumidifier_for(room, states) if since is not None else None}
            rooms.append({"id": rid, "name": room.get("name") or "Room", **c,
                          "valves": [{"entity_id": v, "name": self.live.devices[v].name} for v in valves],
                          "rate": rate["rate"], "rate_n": rate["n"],
                          "learned_at": int(rate["at"] * 1000) if rate["at"] else None,
                          "next": nxt, "blocked": self.blocked(room, layout, states) if valves else None, "damp": damp})
        sensors = [{"entity_id": e, "name": (x.get("attributes") or {}).get("friendly_name") or e}
                   for e, x in sorted(states.items()) if e.startswith("sensor.")
                   and ((x.get("attributes") or {}).get("device_class") == "humidity" or e.endswith("_humidity"))]
        return {"settings": s, "rooms": rooms, "humidity_sensors": sensors, "default_rate": DEFAULT_RATE,
                "mode": self.mode().get("mode"), "now": int(now * 1000),
                "log": [{**x, "at": int(x["at"] * 1000)} for x in (self.store.get("climate_log", []) or [])]}


def add_routes(app, climate: Climate, ha, ensure_states, json_body) -> None:
    """GET /api/climate, PUT /api/climate/settings, PUT /api/climate/rooms/{room_id},
    POST /api/climate/rooms/{room_id}/learn (re-learn the room's warm-up rate from 7 days of HA history).
    Also runs the climate loop for the app's lifetime (wraps the app's lifespan)."""
    from contextlib import asynccontextmanager

    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        async with inner(a) as state:
            climate.start()
            try:
                yield state
            finally:
                await climate.stop()
    app.router.lifespan_context = lifespan
    app.state.climate = climate

    def room_of(rid: str) -> dict:
        room = next((r for r in climate.layout().get("rooms", []) if r.get("id") == rid), None)
        if room is None:
            raise HTTPException(404, "unknown room")
        return room

    async def learn(room: dict) -> dict:
        valves = climate.valves_in(room)
        if not valves:
            return {"warmups": 0, "error": "no radiator valve in this room"}
        try:
            r = await climate.learn_history(ha, valves)
        except Exception as e:  # HA history down: keep what was learnt, say so
            log.warning("climate: history for %s failed: %s", room.get("name"), e)
            return {"warmups": 0, "error": f"Home Assistant history: {e}"}
        climate._log(room.get("name"), "Learnt from history", f"{r['warmups']} warm-up{'s' if r['warmups'] != 1 else ''} in {LEARN_DAYS} days")
        return r

    @app.get("/api/climate")
    async def get_climate():
        await ensure_states()
        return climate.status()

    @app.put("/api/climate/settings")
    async def put_climate_settings(request: Request):
        try:
            climate.put_settings(await json_body(request))
        except ClimateError as e:
            raise HTTPException(400, str(e))
        climate.wake.set()
        await ensure_states()
        return climate.status()

    @app.put("/api/climate/rooms/{room_id}")
    async def put_climate_room(room_id: str, request: Request):
        await ensure_states()
        room = room_of(room_id)
        old = climate.room_cfg(room_id)
        try:
            new = climate.put_room(room, await json_body(request))
        except ClimateError as e:
            raise HTTPException(400, str(e))
        climate.wake.set()
        out = climate.status()
        if new["preheat"] and not old["preheat"] and not climate.room_rate(climate.valves_in(room))["n"]:
            out["learn"] = await learn(room)  # first time on: start from what history already shows
            out = {**climate.status(), "learn": out["learn"]}
        return out

    @app.post("/api/climate/rooms/{room_id}/learn")
    async def learn_climate_room(room_id: str):
        await ensure_states()
        r = await learn(room_of(room_id))
        return {**climate.status(), "learn": r}
