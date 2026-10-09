"""Auto Away: switch to Away when everyone has left, back Home when someone arrives (opt-in, off by default).

Presence comes from Home Assistant's person.* entities (discovery kind "person"; GPS / router device_trackers only
when there are no person entities). A person is home when the state is "home"; any other state (not_home, a zone
name) is out; unavailable / unknown counts as unchanged and never triggers anything.

The switching itself is the same Away / Home the ⋯ menu does (go_away / go_home in backend/app.py, injected as
`actions`), run under the automations lock. Safety rules, because this switches real devices:
- Away only once everyone tracked has been out for N minutes without a break (the delay is the hysteresis);
  one Away per "everyone out" stretch, never again until someone has been home in between;
- manual wins: never Away within MANUAL_WINS of the mode being set by hand, and a manual change during a stretch
  ends that stretch's automation;
- Home only on an arrival (out -> home) that has held for ARRIVE_CONFIRM; arriving cancels a pending Away;
- the action is written to SQLite before it runs, so a restart never replays it; a failure backs off (5 min, doubling
  up to an hour) before the next try.
"""
import logging
import re
import time

from .quiet import in_window

log = logging.getLogger("homecontrol.presence")
DEFAULT_SETTINGS = {"enabled": False, "people": None, "away_minutes": 10, "come_home": True,
                    "only_between": False, "from": "08:00", "to": "23:00", "notify": True}
AWAY_MINUTES = (2, 120)
MANUAL_WINS = 30 * 60
ARRIVE_CONFIRM = 60
RETRY, RETRY_MAX = 300, 3600
LOG_MAX = 30
MAX_PEOPLE = 20
HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
UNKNOWN = ("unavailable", "unknown", "", None)


class PresenceError(ValueError):
    pass


def validate_settings(data, current: dict) -> dict:
    if not isinstance(data, dict):
        raise PresenceError("settings must be an object")
    out = dict(current)
    for k in ("enabled", "come_home", "only_between", "notify"):
        if k in data:
            if not isinstance(data[k], bool):
                raise PresenceError(f"{k} must be true or false")
            out[k] = data[k]
    if "away_minutes" in data:
        m, (lo, hi) = data["away_minutes"], AWAY_MINUTES
        if isinstance(m, bool) or not isinstance(m, int) or not lo <= m <= hi:
            raise PresenceError(f"away_minutes must be a whole number from {lo} to {hi}")
        out["away_minutes"] = m
    for k in ("from", "to"):
        if k in data:
            if not isinstance(data[k], str) or not HHMM.match(data[k]):
                raise PresenceError(f"{k} must be HH:MM")
            out[k] = data[k]
    if "people" in data:  # null = everyone Home Assistant knows; a list = only these
        p = data["people"]
        if p is not None and (not isinstance(p, list) or len(p) > MAX_PEOPLE or
                              not all(isinstance(e, str) and e.startswith(("person.", "device_tracker.")) for e in p)):
            raise PresenceError(f"people must be null or a list of up to {MAX_PEOPLE} person entity ids")
        out["people"] = None if p is None else sorted(set(p))
    return out


def is_home(raw: dict | None) -> bool | None:
    st = (raw or {}).get("state")
    return None if st in UNKNOWN else st == "home"


def _hm(ts: float, tz) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


class Presence:
    """Called from the automations loop (tick) and by the API (status, settings, manual)."""

    def __init__(self, store, notify, mode, clock=time.time, tz=None):
        self.store, self.notify, self.mode, self.clock, self.tz = store, notify, mode, clock, tz
        self.actions = None  # (go_away, go_home) coroutines, set by backend/app.py
        self.s: dict = {"known": {}, "out_since": None, "done_for": None, "manual_at": None, "arrival": None,
                        "fail_n": 0, "retry_at": None, "last": None, **(store.get("presence_state", {}) or {})}
        self.devices: dict = {}

    # ---- settings / state ----
    def settings(self) -> dict:
        return {**DEFAULT_SETTINGS, **(self.store.get("presence_settings", {}) or {})}

    def put_settings(self, new: dict) -> dict:
        old = self.settings()
        if new["enabled"] and not old["enabled"]:  # switching on: the N minutes count from now, nothing is caught up
            if self.s["out_since"] is not None:
                self.s["out_since"] = self.clock()
            self.s["done_for"], self.s["arrival"], self.s["fail_n"], self.s["retry_at"] = None, None, 0, None
            self._log("Auto Away switched on")
        elif old["enabled"] and not new["enabled"]:
            self.s["arrival"] = None
            self._log("Auto Away switched off")
        self.store.put("presence_settings", new)
        self._save()
        return new

    def _save(self) -> None:
        self.store.put("presence_state", self.s)

    def _log(self, text: str) -> None:
        logs = self.store.get("presence_log", []) or []
        logs.insert(0, {"at": self.clock(), "text": text})
        self.store.put("presence_log", logs[:LOG_MAX])
        log.info("auto away: %s", text)

    def manual(self, mode: str) -> None:
        """Away / Home pressed by hand: that wins over the automation."""
        now = self.clock()
        self.s["manual_at"] = now
        if self.s["out_since"] is not None:
            self.s["done_for"] = self.s["out_since"]  # this "everyone out" stretch is the user's call now
        self.s["arrival"] = None
        self._save()

    def tracked(self, devices: dict) -> list[str]:
        people = self.settings()["people"]
        ids = sorted(e for e, d in devices.items() if d.kind == "person")
        return ids if people is None else [e for e in ids if e in people]

    # ---- decisions ----
    def _away_at(self, st: dict) -> float | None:
        if self.s["out_since"] is None or self.s["done_for"] == self.s["out_since"]:
            return None
        return self.s["out_since"] + st["away_minutes"] * 60

    def _blocked(self, st: dict, now: float) -> str | None:
        if st["only_between"] and not in_window(now, self.tz, st["from"], st["to"]):
            return "hours"
        if self.s["manual_at"] is not None and now - self.s["manual_at"] < MANUAL_WINS:
            return "manual"
        return None

    def next_due(self) -> float | None:
        st = self.settings()
        if not st["enabled"]:
            return None
        ts = [t for t in (self._away_at(st),
                          self.s["arrival"] and self.s["arrival"]["at"] + ARRIVE_CONFIRM,
                          self.s["retry_at"],
                          self.s["manual_at"] and self.s["manual_at"] + MANUAL_WINS) if t]
        now = self.clock()
        return min((t for t in ts if t > now), default=None)

    async def tick(self, devices: dict, states: dict) -> str | None:
        """Returns "away" / "home" when it switched (for tests and logs)."""
        self.devices = devices
        st, now, changed = self.settings(), self.clock(), False
        ids = self.tracked(devices)
        known = self.s["known"]
        arrived = []
        for e in ids:
            h = is_home(states.get(e))
            if h is None:
                continue  # unavailable / unknown: unchanged
            if known.get(e) is False and h:
                arrived.append(e)
            if known.get(e) != h:
                known[e], changed = h, True
        everyone_out = bool(ids) and all(known.get(e) is False for e in ids)
        if everyone_out and self.s["out_since"] is None:
            self.s["out_since"], changed = now, True
        elif not everyone_out and self.s["out_since"] is not None:
            if st["enabled"] and self._away_at(st) is not None and self.mode().get("mode") == "home":
                self._log("Away cancelled — someone is home")
            self.s["out_since"], changed = None, True
        if not st["enabled"]:
            if changed:
                self._save()
            return None

        mode = self.mode().get("mode")
        home_now = [e for e in ids if known.get(e) is True]
        if arrived and st["come_home"] and mode == "away" and self.s["arrival"] is None:
            self.s["arrival"], changed = {"at": now, "who": arrived}, True
        if self.s["arrival"] and (mode != "away" or not home_now or not st["come_home"]):
            self.s["arrival"], changed = None, True  # left again before it held, or already home
        retry_ok = self.s["retry_at"] is None or now >= self.s["retry_at"]
        result = None
        try:
            arr = self.s["arrival"]
            if arr and now - arr["at"] >= ARRIVE_CONFIRM and retry_ok:
                if self._blocked(st, now) == "hours":
                    self.s["arrival"], changed = None, True
                    self._log("Arrival outside the active hours — staying Away")
                else:
                    result = await self._switch("home", st, arr.get("who") or home_now)
                    changed = True
            elif everyone_out and mode == "away" and self.s["done_for"] != self.s["out_since"]:
                self.s["done_for"], changed = self.s["out_since"], True  # already Away: nothing to do this stretch
            elif everyone_out and mode == "home" and retry_ok:
                due = self._away_at(st)
                if due is not None and now >= due and self._blocked(st, now) is None:
                    result = await self._switch("away", st, ids)
                    changed = True
        finally:
            if changed:
                self._save()
        return result

    def _names(self, ids) -> str:
        names = [self.devices[e].name if e in self.devices else e for e in ids]
        return " & ".join(names) if len(names) <= 2 else f"{', '.join(names[:-1])} & {names[-1]}"

    async def _switch(self, to: str, st: dict, who) -> str | None:
        if not self.actions:
            return None
        go_away, go_home = self.actions
        now = self.clock()
        # written before acting: a crash or restart never runs this again
        if to == "away":
            self.s["done_for"] = self.s["out_since"]
        else:
            self.s["arrival"] = None
        self.s["last"] = {"type": to, "at": now, "status": "running"}
        self._save()
        try:
            r = await (go_away() if to == "away" else go_home())
        except Exception as e:
            self.s["fail_n"] += 1
            wait = min(RETRY_MAX, RETRY * 2 ** (self.s["fail_n"] - 1))
            self.s["retry_at"] = now + wait
            if to == "away":
                self.s["done_for"] = None
            else:
                self.s["arrival"] = {"at": now - ARRIVE_CONFIRM, "who": list(who)}
            self.s["last"] = {"type": to, "at": now, "status": "failed"}
            self._log(f"{'Away' if to == 'away' else 'Home'} failed ({e}) — next try in {wait // 60} min")
            return None
        self.s["fail_n"], self.s["retry_at"] = 0, None
        self.s["last"] = {"type": to, "at": now, "status": "done"}
        if to == "away":
            n = len(r.get("turned_off", []))
            body = f"{n} light{'s' if n != 1 else ''} and plugs off" + (f", radiators {r['away_temp']:g}°" if r.get("valves") else "") + "."
            self._log(f"Switched to Away — everyone out since {_hm(self.s['out_since'] or now, self.tz)}")
            payload = {"title": "Switched to Away — everyone left", "body": body, "tag": "presence", "url": "/"}
        else:
            self._log(f"Switched to Home — {self._names(who)} arrived")
            payload = {"title": "Welcome home", "body": f"{self._names(who)} arrived — radiators back to their targets.",
                       "tag": "presence", "url": "/"}
        if st["notify"]:
            try:
                await self.notify(payload)
            except Exception as e:
                log.warning("auto away push failed: %s", e)
        return to

    # ---- API ----
    def status(self, devices: dict, states: dict) -> dict:
        st, now = self.settings(), self.clock()
        people = []
        tracked = set(self.tracked(devices))
        for e, d in sorted(devices.items(), key=lambda x: x[1].name.lower()):
            if d.kind != "person":
                continue
            raw = states.get(e) or {}
            people.append({"entity_id": e, "name": d.name, "model": d.model, "state": raw.get("state", "unavailable"),
                           "home": is_home(raw), "known_home": self.s["known"].get(e), "tracked": e in tracked})
        ms = lambda t: int(t * 1000) if t else None
        away_at = self._away_at(st) if self.mode().get("mode") == "home" else None
        blocked = self._blocked(st, now) if st["enabled"] else None
        return {**st, "mode": self.mode().get("mode"), "now": ms(now), "people": people,
                "everyone_out": self.s["out_since"] is not None, "out_since": ms(self.s["out_since"]),
                "away_at": ms(away_at), "blocked": blocked,
                "manual_until": ms(self.s["manual_at"] + MANUAL_WINS) if blocked == "manual" else None,
                "arrival_at": ms(self.s["arrival"]["at"] + ARRIVE_CONFIRM) if self.s["arrival"] else None,
                "retry_at": ms(self.s["retry_at"]),
                "last": {**self.s["last"], "at": ms(self.s["last"]["at"])} if self.s["last"] else None,
                "log": [{"at": ms(x["at"]), "text": x["text"]} for x in self.store.get("presence_log", []) or []]}


def add_routes(app, presence: Presence, devices, live, ha, json_body, wake) -> None:
    """GET /api/presence (settings + live status + log), PUT /api/presence/settings."""
    from fastapi import HTTPException, Request

    async def status() -> dict:
        devs = await devices()
        if not live.fresh():
            live.load_states(await ha.states())
        return presence.status(devs, live.states)

    @app.get("/api/presence")
    async def get_presence():
        return await status()

    @app.put("/api/presence/settings")
    async def put_presence_settings(request: Request):
        try:
            s = validate_settings(await json_body(request), presence.settings())
        except PresenceError as e:
            raise HTTPException(400, str(e))
        presence.put_settings(s)
        wake()
        return await status()
