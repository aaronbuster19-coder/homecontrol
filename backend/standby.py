"""Standby saver: switch chosen plugs off at night when nothing is using them, and back on in the morning.

Per plug, opt-in (off by default). At the night time (default 01:00) the plug is turned off only if its power has
stayed below its standby threshold for the previous STEADY seconds (HA history plus the live reading) — never cut
something in use. At the morning time (default 07:00) it is turned back on only if the saver turned it off and
nobody touched it since (ownership): turning it on by hand in between hands it back to the user. While Away the
morning switch-on waits and happens when you're Home again.

Safety, as for schedules (and with their timing helpers: wall-clock times in the local zone, DST-safe): every
occurrence is handled at most once — (plug, kind, local date) is written to SQLite before the call; nothing missed is
replayed (at most GRACE late, never before the moment the plug was enabled or edited); everything due in one tick
is one service call per action; a failed switch-off is logged and not retried (the plug just stays on); a failed
switch-on is retried twice with back-off, then given up. Keep-on plugs and plugs a fridge / freezer
is linked to (layout furniture with "plug") can never be enabled.
"""
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone

from .history import by_entity, ms, numeric_state, samples, segments
from .schedules import GRACE, next_run, occurrence

log = logging.getLogger("homecontrol.standby")
DEFAULTS = {"enabled": False, "threshold_w": None, "off_at": "01:00", "on_at": "07:00"}
STEADY = 15 * 60          # power below the threshold for this long before switching off
SETTLE = 60               # after our own off call, an "on" reading this soon is the call still landing
THRESHOLD = (2.0, 500.0)  # W
MIN_THRESHOLD, MARGIN = 2.0, 5.0
LOG_MAX = 40
RETRY, ON_TRIES = 300, 3   # a failed morning switch-on is tried again after 5, then 10 min, then given up
COLD = ("fridge", "freezer", "fridge_freezer")
HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
EVERY_DAY = list(range(7))


class StandbyError(ValueError):
    pass


def default_threshold(standby_w: float | None) -> float:
    """The plug's measured overnight standby + 5 W, at least 2 W."""
    return round(max(MIN_THRESHOLD, (standby_w or 0.0) + MARGIN), 1)


def blocked_reason(eid: str, layout: dict) -> str | None:
    s = layout.get("settings") or {}
    if eid in (s.get("keep_on") or []):
        return "keep on"
    for f in layout.get("furniture") or []:
        if f.get("type") in COLD and f.get("plug") == eid:
            return "fridge"
    return None


def validate_plug(data, current: dict) -> dict:
    if not isinstance(data, dict) or not set(data) <= {"enabled", "threshold_w", "off_at", "on_at"}:
        raise StandbyError("send {enabled, threshold_w, off_at, on_at}")
    out = {**DEFAULTS, **current}
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise StandbyError("enabled must be true or false")
        out["enabled"] = data["enabled"]
    if "threshold_w" in data:
        t = data["threshold_w"]
        if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t)
                              or not THRESHOLD[0] <= t <= THRESHOLD[1]):
            raise StandbyError(f"threshold_w must be {THRESHOLD[0]:g}–{THRESHOLD[1]:g} W")
        out["threshold_w"] = None if t is None else round(float(t), 1)
    for k in ("off_at", "on_at"):
        if k in data:
            if not isinstance(data[k], str) or not HHMM.match(data[k]):
                raise StandbyError(f"{k} must be HH:MM")
            out[k] = data[k]
    if out["off_at"] == out["on_at"]:
        raise StandbyError("off and on times must differ")
    return out


def hours_off(off_at: str, on_at: str) -> float:
    a, b = (int(x[:2]) * 60 + int(x[3:]) for x in (off_at, on_at))
    return ((b - a) % 1440) / 60


def _sched(at: str) -> dict:
    return {"days": EVERY_DAY, "time": {"type": "fixed", "at": at}}


class StandbySaver:
    """settings: AutoStore "standby_plugs" {eid: {enabled, threshold_w, off_at, on_at, armed_at}};
    state: "standby_state" {eid: {"off": local date handled, "on": local date handled, "owned": ts | None,
    "owed": bool (morning switch-on still owed: Away or plug offline at the time)}}; log: "standby_log"."""

    def __init__(self, store, call, ha, layout, mode, clock=time.time, tz=None):
        self.store, self.call, self.ha, self.layout, self.mode, self.clock, self.tz = store, call, ha, layout, mode, clock, tz
        self.state: dict = store.get("standby_state", {}) or {}

    # ---- settings ----
    def plugs(self) -> dict:
        return self.store.get("standby_plugs", {}) or {}

    def plug(self, eid: str) -> dict:
        return {**DEFAULTS, **self.plugs().get(eid, {})}

    def put_plug(self, eid: str, new: dict, name: str = "") -> dict:
        old, all_ = self.plug(eid), self.plugs()
        timing = (new["off_at"], new["on_at"]) != (old["off_at"], old["on_at"])
        if new["enabled"] and (timing or not old["enabled"]):
            new = {**new, "armed_at": self.clock()}  # nothing before this moment is caught up
        elif "armed_at" in old:
            new = {**new, "armed_at": old["armed_at"]}
        if old["enabled"] != new["enabled"]:
            self._log(eid, name, "enabled" if new["enabled"] else "disabled",
                      f"off {new['off_at']} if below {new['threshold_w']:g} W, on {new['on_at']}" if new["enabled"] else "")
            if not new["enabled"]:  # switched off: no more automatic switching for this plug
                self.state.pop(eid, None)
                self._save()
        all_[eid] = new
        self.store.put("standby_plugs", all_)
        return new

    def _save(self) -> None:
        self.store.put("standby_state", self.state)

    def _log(self, eid: str, name: str, action: str, note: str = "") -> None:
        logs = self.store.get("standby_log", []) or []
        logs.insert(0, {"at": self.clock(), "entity_id": eid, "name": name or eid, "action": action, "note": note})
        self.store.put("standby_log", logs[:LOG_MAX])
        log.info("standby saver: %s %s %s", eid, action, note)

    def logs(self) -> list[dict]:
        return self.store.get("standby_log", []) or []

    # ---- timing ----
    def _due(self, at: str, armed: float, now: float) -> str | None:
        """Local date of an occurrence of `at` that is due now (within GRACE, not before armed), else None."""
        today = datetime.fromtimestamp(now, self.tz).date()
        for d in (today - timedelta(days=1), today):
            ts = occurrence(_sched(at), d, self.tz, 0, 0)
            if ts is not None and ts <= now < ts + GRACE and ts >= armed:
                return d.isoformat()
        return None

    def next_due(self) -> float | None:
        now, ts = self.clock(), []
        for eid, p in self.plugs().items():
            if p.get("enabled"):
                ts += [t for at in (p["off_at"], p["on_at"]) if (t := next_run(_sched(at), now, self.tz, 0, 0))]
        return min(ts) if ts else None

    def next_times(self, p: dict) -> dict:
        now = self.clock()
        return {k: int(next_run(_sched(p[k]), now, self.tz, 0, 0) * 1000) for k in ("off_at", "on_at")}

    # ---- power ----
    async def _peak(self, devices: dict, ids: list[str], states: dict, now: float) -> dict[str, float | None]:
        """Highest power of each plug over the last STEADY seconds (history + live), None if any of it is unknown."""
        pids = {e: devices[e].related.get("power") for e in ids}
        want = sorted(p for p in pids.values() if p)
        rows = {}
        if want:
            t0, t1 = datetime.fromtimestamp(now - STEADY, timezone.utc), datetime.fromtimestamp(now, timezone.utc)
            try:
                rows = by_entity(await self.ha.history(t0, t1, want))
            except Exception as e:
                log.warning("standby saver: power history failed: %s", e)
                return {e: None for e in ids}
        a, b = int((now - STEADY) * 1000), int(now * 1000)
        out = {}
        for e, pid in pids.items():
            live = numeric_state(states.get(pid) or {}) if pid and (states.get(pid) or {}).get("state") not in ("unavailable", "unknown") else None
            segs = segments(samples(rows.get(pid, []), numeric_state), a, b) if pid else []
            covered = sum(s1 - s0 for s0, s1, _ in segs)
            if live is None or not segs or any(v is None for _, _, v in segs) or covered < (b - a) * 0.9:
                out[e] = None
                continue
            unit = ((states.get(pid) or {}).get("attributes") or {}).get("unit_of_measurement")
            k = 1000 if unit == "kW" else 1
            out[e] = max([v * k for _, _, v in segs] + [live * k])
        return out

    # ---- loop ----
    def observe(self, item: dict) -> None:
        """Every published plug change: on by hand after we switched it off -> it's the user's again."""
        st = self.state.get(item.get("entity_id"))
        if st and st.get("owned") and item.get("state") == "on" and self.clock() - st["owned"] > SETTLE:
            st["owned"], st["owed"] = None, False
            self._save()
            self._log(item["entity_id"], item.get("name", ""), "released", "turned on by hand — left alone")

    async def tick(self, devices: dict, states: dict) -> list[dict]:
        """Returns the service calls made (for tests / logs)."""
        now, layout = self.clock(), self.layout()
        away = self.mode().get("mode") == "away"
        offs, ons, changed = [], [], False
        for eid, p in sorted(self.plugs().items()):
            d = devices.get(eid)
            if not p.get("enabled") or d is None or d.kind != "plug":
                continue
            p = {**DEFAULTS, **p}
            st = self.state.setdefault(eid, {"off": None, "on": None, "owned": None, "owed": False})
            why = blocked_reason(eid, layout)
            if why:
                self.put_plug(eid, {**p, "enabled": False}, d.name)
                self._log(eid, d.name, "disabled", "it's a keep-on plug" if why == "keep on" else "a fridge is linked to it")
                continue
            cur = (states.get(eid) or {}).get("state")
            if st["owned"] and cur == "on" and now - st["owned"] > SETTLE:
                st["owned"], st["owed"], changed = None, False, True
                self._log(eid, d.name, "released", "turned on by hand — left alone")
            armed = p.get("armed_at", 0)
            day = self._due(p["off_at"], armed, now)
            if day and st["off"] != day:
                st["off"], changed = day, True  # handled, whatever happens next
                if cur != "on":
                    self._log(eid, d.name, "skipped", "already off" if cur == "off" else f"plug {cur or 'unavailable'}")
                else:
                    offs.append(eid)
            day = self._due(p["on_at"], armed, now)
            if day and st["on"] != day:
                st["on"], changed = day, True
                if st["owned"] and cur == "off" and not away:
                    ons.append(eid)
                elif st["owned"]:  # Away, or the plug is offline: owed, switched on once you're home / it's back
                    st["owed"] = True
                    self._log(eid, d.name, "waiting", "Away — back on when you're home" if away else
                              f"plug {cur or 'unavailable'} — back on when it reappears")
            elif st.get("owed") and st["owned"] and cur == "off" and not away and now >= st.get("retry_at", 0):
                st["owed"], changed = False, True
                ons.append(eid)
        if changed:
            self._save()  # before any call: a crash never fires these occurrences again
        calls = []
        if offs:
            peaks = await self._peak(devices, offs, states, now)
            go = []
            for eid in offs:
                thr, w, name = self.plug(eid)["threshold_w"], peaks[eid], devices[eid].name
                if thr is None:
                    self._log(eid, name, "skipped", "no threshold set")
                elif w is None:
                    self._log(eid, name, "skipped", "no steady power reading for the last 15 min")
                elif w >= thr:
                    self._log(eid, name, "skipped", f"in use ({w:.1f} W, threshold {thr:g} W)")
                else:
                    go.append((eid, w))
            if go:
                calls.append(await self._call("turn_off", go, devices, now))
        if ons:
            calls.append(await self._call("turn_on", [(e, None) for e in ons], devices, now))
        return calls

    async def _call(self, service: str, items, devices: dict, now: float) -> dict:
        ids = sorted(e for e, _ in items)
        data = {"entity_id": ids}
        try:
            await self.call("switch", service, data)
            ok = True
        except Exception as e:
            log.warning("standby saver: switch.%s %s failed: %s", service, ids, e)
            ok = False
        for eid, w in items:
            st, name = self.state.setdefault(eid, {}), devices[eid].name
            if not ok:
                if service == "turn_off":  # stays on: the safe side, tried again tomorrow night
                    self._log(eid, name, "failed", "Home Assistant error turning it off")
                    continue
                st["fails"] = st.get("fails", 0) + 1
                if st["fails"] >= ON_TRIES:
                    st.update(owned=None, owed=False, fails=0, retry_at=0)
                    self._log(eid, name, "failed", f"Home Assistant error turning it on — gave up after {ON_TRIES} tries")
                else:
                    wait = RETRY * 2 ** (st["fails"] - 1)
                    st.update(owed=True, retry_at=now + wait)
                    self._log(eid, name, "failed", f"Home Assistant error turning it on — trying again in {wait // 60} min")
                continue
            if service == "turn_off":
                st["owned"] = now
                self._log(eid, name, "off", f"standby {w:.1f} W")
            else:
                st.update(owned=None, owed=False, fails=0, retry_at=0)
                self._log(eid, name, "on", "")
        self._save()
        return {"domain": "switch", "service": service, "data": data, "ok": ok}

    # ---- API ----
    def listing(self, devices: dict, states: dict, standby: dict[str, float | None], layout: dict, rate_p) -> dict:
        from .live import build_device
        out = []
        for eid, d in sorted(devices.items(), key=lambda x: x[1].name.lower()):
            if d.kind != "plug":
                continue
            p, st = self.plug(eid), self.state.get(eid) or {}
            sw = standby.get(eid)
            item = build_device(d, states)
            year_kwh = None if sw is None else sw * hours_off(p["off_at"], p["on_at"]) * 365 / 1000
            out.append({"entity_id": eid, "name": d.name, "hidden": d.hidden, "state": item["state"], "power": item.get("power"),
                        "has_power": bool(d.related.get("power")), "standby_w": sw, "suggested_w": default_threshold(sw),
                        **{k: p[k] for k in ("enabled", "threshold_w", "off_at", "on_at")},
                        "blocked": blocked_reason(eid, layout), "owned": bool(st.get("owned")),
                        "owned_since": int(st["owned"] * 1000) if st.get("owned") else None,
                        "owed": bool(st.get("owed")),
                        "year_kwh": None if year_kwh is None else round(year_kwh, 1),
                        "year_p": None if year_kwh is None or rate_p is None else round(year_kwh * rate_p, 2),
                        "next": self.next_times(p) if p["enabled"] else None})
        return {"rate_p": rate_p, "now": int(self.clock() * 1000), "plugs": out,
                "log": [{**x, "at": int(x["at"] * 1000)} for x in self.logs()]}


def add_routes(app, saver: StandbySaver, devices, plug_devices, energy, layout, live, json_body, wake) -> None:
    """GET /api/standby (plugs, savings, recent actions), PUT /api/standby/{entity_id} (opt in / settings)."""
    from fastapi import HTTPException, Request

    from .energy import tariff

    async def standby_w(plugs) -> dict:
        try:
            s = await energy.standby(plugs, layout(), datetime.fromtimestamp(saver.clock(), timezone.utc))
        except Exception as e:  # HA history down: the list still works, without the measured standby
            log.warning("standby saver: overnight standby unknown: %s", e)
            return {}
        return {r["entity_id"]: r["avg_w"] for r in s["plugs"]}

    async def listing() -> dict:
        plugs, _ = await plug_devices()
        L = layout()
        return saver.listing(await devices(), live.states, await standby_w(plugs), L, tariff(L)["rate_p"])

    @app.get("/api/standby")
    async def get_standby_saver():
        return await listing()

    @app.put("/api/standby/{entity_id}")
    async def put_standby_plug(entity_id: str, request: Request):
        d = (await devices()).get(entity_id)
        if d is None:
            raise HTTPException(404, "unknown device")
        if d.kind != "plug":
            raise HTTPException(400, f"not supported for {d.kind}")
        try:
            p = validate_plug(await json_body(request), saver.plug(entity_id))
        except StandbyError as e:
            raise HTTPException(400, str(e))
        if p["enabled"]:
            why = blocked_reason(entity_id, layout())
            if why == "keep on":
                raise HTTPException(400, "keep-on plugs can't use the standby saver")
            if why == "fridge":
                raise HTTPException(400, "a fridge or freezer is linked to this plug: it must stay on")
            if not d.related.get("power"):
                raise HTTPException(400, "this plug doesn't measure power, so the saver can't tell if it's in use")
            if p["threshold_w"] is None:
                p["threshold_w"] = default_threshold((await standby_w([d])).get(entity_id))
        saver.put_plug(entity_id, p, d.name)
        wake()
        return await listing()
