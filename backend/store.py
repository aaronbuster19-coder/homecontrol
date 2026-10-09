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


CORNERS = ("nw", "ne", "sw", "se")
OPENING_LEN = (0.2, 5.0)


def _cut(c, w: float, h: float, i: int) -> dict:
    if not isinstance(c, dict) or c.get("corner") not in CORNERS:
        raise LayoutError(f"room {i} cut needs a corner (nw/ne/sw/se)")
    cw, ch = _num(c.get("w"), f"room {i} cut w"), _num(c.get("h"), f"room {i} cut h")
    if not (0 < cw < w and 0 < ch < h):
        raise LayoutError(f"room {i} cut must be smaller than the room")
    return {"corner": c["corner"], "w": cw, "h": ch}


def _openings(items, known_entities: set[str]) -> list[dict]:
    if not isinstance(items, list):
        raise LayoutError("openings must be a list")
    out, ids = [], set()
    for i, o in enumerate(items):
        if not isinstance(o, dict):
            raise LayoutError(f"opening {i} must be an object")
        oid = o.get("id")
        if not isinstance(oid, str) or not oid or oid in ids:
            raise LayoutError(f"opening {i} needs a unique id")
        ids.add(oid)
        if o.get("type") not in ("door", "window") or o.get("orient") not in ("h", "v"):
            raise LayoutError(f"opening {i} needs type door/window and orient h/v")
        ln = _num(o.get("len"), f"opening {i} len")
        if not OPENING_LEN[0] <= ln <= OPENING_LEN[1]:
            raise LayoutError(f"opening {i} len must be {OPENING_LEN[0]}–{OPENING_LEN[1]} m")
        item = {"id": oid, "type": o["type"], "x": _num(o.get("x"), f"opening {i} x"),
                "y": _num(o.get("y"), f"opening {i} y"), "len": ln, "orient": o["orient"]}
        eid = o.get("entity_id")
        if eid is not None:
            if eid not in known_entities:
                raise LayoutError(f"opening {i}: unknown entity {eid!r}")
            item["entity_id"] = eid
        out.append(item)
    return out


NAME_MAX = 40
RATE_MAX, STANDING_MAX = 200, 500  # pence per kWh / pence per day
SETTINGS_CARRIED = ("names", "hidden", "energy")  # kept by a PUT that leaves them out (older clients, imports)


def _names(v, known: set[str]) -> dict:
    if not isinstance(v, dict):
        raise LayoutError("settings.names must be an object {entity_id: name}")
    out = {}
    for eid, name in v.items():
        if eid not in known:
            raise LayoutError(f"settings.names: unknown entity {eid!r}")
        if name is None:
            continue
        if not isinstance(name, str):
            raise LayoutError(f"settings.names: name for {eid} must be text")
        name = " ".join(name.split())
        if len(name) > NAME_MAX:
            raise LayoutError(f"settings.names: name for {eid} is longer than {NAME_MAX} characters")
        if name:
            out[eid] = name
    return dict(sorted(out.items()))


def _hidden(v, known: set[str]) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(e, str) for e in v):
        raise LayoutError("settings.hidden must be a list of entity ids")
    bad = [e for e in v if e not in known]
    if bad:
        raise LayoutError(f"settings.hidden: unknown entity {bad[0]!r}")
    return sorted(set(v))


def _pence(v, what: str, hi: float) -> float | None:
    if v is None or v == "":
        return None
    f = _num(v, what)
    if not 0 <= f <= hi:
        raise LayoutError(f"{what} must be 0–{hi}")
    return round(f, 2)


def validate_energy(v) -> dict:
    """Tariff: {"rate_p": pence per kWh, "standing_p": pence per day}; either may be None (not set)."""
    if not isinstance(v, dict):
        raise LayoutError("settings.energy must be an object")
    return {"rate_p": _pence(v.get("rate_p"), "unit rate (p/kWh)", RATE_MAX),
            "standing_p": _pence(v.get("standing_p"), "standing charge (p/day)", STANDING_MAX)}


def _settings(data, known_plugs: set[str] | None, known_entities: set[str] | None = None) -> dict | None:
    s = data.get("settings")
    if s is None:
        return None
    if not isinstance(s, dict):
        raise LayoutError("settings must be an object")
    keep = s.get("keep_on", [])
    if not isinstance(keep, list) or not all(isinstance(e, str) for e in keep):
        raise LayoutError("settings.keep_on must be a list of entity ids")
    if known_plugs is not None:
        bad = [e for e in keep if e not in known_plugs]
        if bad:
            raise LayoutError(f"settings.keep_on: unknown plug {bad[0]!r}")
    out = {"keep_on": sorted(set(keep))}
    known = known_entities if known_entities is not None else set(known_plugs or ())
    if s.get("names") is not None:
        out["names"] = _names(s["names"], known)
    if s.get("hidden") is not None:
        out["hidden"] = _hidden(s["hidden"], known)
    if s.get("energy") is not None:
        out["energy"] = validate_energy(s["energy"])
    return out


def carry_settings(new: dict, old: dict, raw) -> dict:
    """Settings a PUT didn't mention (names, hidden, energy) stay as stored: an older cached app or an older export
    must not wipe them. Send e.g. "names": {} to clear."""
    sent = raw.get("settings") if isinstance(raw, dict) and isinstance(raw.get("settings"), dict) else {}
    kept = {k: v for k, v in (old.get("settings") or {}).items() if k in SETTINGS_CARRIED and k not in sent}
    if kept:
        new["settings"] = {"keep_on": [], **new.get("settings", {}), **kept}
    return new


def stored_refs(layout: dict) -> set[str]:
    """Entity ids a stored layout refers to: they stay valid even while HA is briefly missing them."""
    s = layout.get("settings") or {}
    return ({p["entity_id"] for p in layout.get("placements", [])}
            | {o["entity_id"] for o in layout.get("openings", []) if o.get("entity_id")}
            | set(s.get("names") or {}) | set(s.get("hidden") or []))


def validate_layout(data, known_entities: set[str], known_plugs: set[str] | None = None) -> dict:
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
        room = {"id": rid, "name": name.strip()[:60],
                "x": _num(r.get("x"), f"room {i} x"), "y": _num(r.get("y"), f"room {i} y"), "w": w, "h": h}
        if r.get("cut") is not None:
            room["cut"] = _cut(r["cut"], w, h, i)
        rooms.append(room)
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
    openings = _openings(data.get("openings", []), known_entities)
    out = {"unit": unit, "rooms": rooms, "placements": places, "openings": openings}
    settings = _settings(data, known_plugs if known_plugs is not None else known_entities, known_entities)
    if settings is not None:
        out["settings"] = settings
    return out


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
