"""Away / Home mode: state in SQLite, and the plan of service calls for each switch."""
import json
import math
import os
import sqlite3
import threading
import time

DEFAULT_STATE = {"mode": "home", "since": None, "away_temp": 16.0, "tv_off": True, "targets": {}, "prev_alerts_enabled": None}
AWAY_TEMP = (5, 25)
TARGET = (5, 35)


class ModeError(ValueError):
    pass


def validate_mode_settings(data) -> dict:
    """Away settings: away_temp (radiators while away) and/or tv_off (Away turns TVs off, default true)."""
    if not isinstance(data, dict) or not ({"away_temp", "tv_off"} & set(data)):
        raise ModeError("away_temp required")
    if set(data) - {"away_temp", "tv_off"}:
        raise ModeError(f"unknown setting {sorted(set(data) - {'away_temp', 'tv_off'})[0]!r}")
    out = {}
    if "away_temp" in data:
        t = data["away_temp"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not AWAY_TEMP[0] <= t <= AWAY_TEMP[1]:
            raise ModeError(f"away_temp must be {AWAY_TEMP[0]}–{AWAY_TEMP[1]}")
        out["away_temp"] = round(float(t) * 2) / 2
    if "tv_off" in data:
        if not isinstance(data["tv_off"], bool):
            raise ModeError("tv_off must be true or false")
        out["tv_off"] = data["tv_off"]
    return out


class ModeStore:
    def __init__(self, path: str):
        self.path, self._lock = path, threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS mode_state (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def get(self) -> dict:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT data FROM mode_state WHERE id = 1").fetchone()
        return {**DEFAULT_STATE, **(json.loads(row[0]) if row else {})}

    def put(self, s: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO mode_state (id, data) VALUES (1, ?) "
                      "ON CONFLICT(id) DO UPDATE SET data = excluded.data", (json.dumps(s),))


def public(s: dict) -> dict:
    return {"mode": s["mode"], "since": s["since"], "away_temp": s["away_temp"], "tv_off": s.get("tv_off", True)}


def current_targets(valve_ids, states: dict) -> dict[str, float]:
    out = {}
    for eid in valve_ids:
        t = ((states.get(eid) or {}).get("attributes") or {}).get("temperature")
        if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t):
            out[eid] = float(t)
    return out


def restore_groups(targets: dict, valve_ids) -> dict[float, list[str]]:
    """Remembered targets of valves that still exist, clamped, grouped by temperature (one call per value)."""
    groups: dict[float, list[str]] = {}
    for eid in sorted(targets):
        if eid in valve_ids:
            t = min(TARGET[1], max(TARGET[0], float(targets[eid])))
            groups.setdefault(t, []).append(eid)
    return groups


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
