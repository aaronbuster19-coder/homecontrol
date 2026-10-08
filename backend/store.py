import json
import math
import os
import sqlite3
import threading

DEFAULT_LAYOUT = {"unit": "m", "rooms": [], "placements": []}


class LayoutError(ValueError):
    pass


def _num(v, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise LayoutError(f"{what} must be a finite number")
    return float(v)


def validate_layout(data, known_entities: set[str]) -> dict:
    if not isinstance(data, dict):
        raise LayoutError("layout must be an object")
    unit = data.get("unit", "m")
    if unit not in ("m", "ft"):
        raise LayoutError("unit must be 'm' or 'ft'")
    rooms_in = data.get("rooms", [])
    places_in = data.get("placements", [])
    if not isinstance(rooms_in, list) or not isinstance(places_in, list):
        raise LayoutError("rooms and placements must be lists")
    rooms, ids = [], set()
    for i, r in enumerate(rooms_in):
        if not isinstance(r, dict):
            raise LayoutError(f"room {i} must be an object")
        rid = r.get("id")
        if not isinstance(rid, str) or not rid or rid in ids:
            raise LayoutError(f"room {i} needs a unique id")
        ids.add(rid)
        name = r.get("name")
        if not isinstance(name, str) or not name.strip():
            raise LayoutError(f"room {i} needs a name")
        w, h = _num(r.get("w"), f"room {i} w"), _num(r.get("h"), f"room {i} h")
        if w <= 0 or h <= 0:
            raise LayoutError(f"room {i} must have positive size")
        rooms.append({"id": rid, "name": name.strip()[:60],
                      "x": _num(r.get("x"), f"room {i} x"), "y": _num(r.get("y"), f"room {i} y"),
                      "w": w, "h": h})
    places, placed = [], set()
    for i, p in enumerate(places_in):
        if not isinstance(p, dict):
            raise LayoutError(f"placement {i} must be an object")
        eid = p.get("entity_id")
        if eid not in known_entities:
            raise LayoutError(f"placement {i}: unknown entity {eid!r}")
        if eid in placed:
            raise LayoutError(f"placement {i}: {eid} placed twice")
        placed.add(eid)
        places.append({"entity_id": eid, "x": _num(p.get("x"), f"placement {i} x"),
                       "y": _num(p.get("y"), f"placement {i} y")})
    return {"unit": unit, "rooms": rooms, "placements": places}


class LayoutStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS layout (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def get(self) -> dict:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT data FROM layout WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else dict(DEFAULT_LAYOUT)

    def put(self, layout: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO layout (id, data) VALUES (1, ?) "
                      "ON CONFLICT(id) DO UPDATE SET data = excluded.data", (json.dumps(layout),))
