"""Floor-plan geometry in Python, mirroring frontend/floorplan.js: L-shaped rooms, walls, which room a window is in."""

WALL_TOL = 0.05  # metres: an opening's midpoint this close to a wall belongs to that room


def _cut_map(r: dict):
    c = (r.get("cut") or {}).get("corner", "ne")
    fx, fy = "w" in c, "s" in c

    def to(u: float, v: float) -> tuple[float, float]:
        return (r["x"] + r["w"] - u if fx else r["x"] + u, r["y"] + r["h"] - v if fy else r["y"] + v)
    return fx, fy, to


def room_poly(r: dict) -> list[tuple[float, float]]:
    x, y, w, h = r["x"], r["y"], r["w"], r["h"]
    if not r.get("cut"):
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    _, _, to = _cut_map(r)
    a, b = w - r["cut"]["w"], r["cut"]["h"]
    return [to(u, v) for u, v in ((0, 0), (a, 0), (a, b), (w, b), (w, h), (0, h))]


def in_room(r: dict, px: float, py: float) -> bool:
    """Point inside the room (edges count), respecting an L-cut — same rule as inRoom() in floorplan.js."""
    if px < r["x"] or px > r["x"] + r["w"] or py < r["y"] or py > r["y"] + r["h"]:
        return False
    if not r.get("cut"):
        return True
    fx, fy, _ = _cut_map(r)
    u = r["x"] + r["w"] - px if fx else px - r["x"]
    v = r["y"] + r["h"] - py if fy else py - r["y"]
    return not (u > r["w"] - r["cut"]["w"] and v < r["cut"]["h"])


def walls(r: dict) -> list[tuple[tuple[float, float], tuple[float, float], str]]:
    p = room_poly(r)
    out = []
    for i, a in enumerate(p):
        b = p[(i + 1) % len(p)]
        out.append((a, b, "h" if abs(a[1] - b[1]) < 1e-9 else "v"))
    return out


def opening_mid(o: dict) -> tuple[float, float]:
    return (o["x"] + o["len"] / 2, o["y"]) if o["orient"] == "h" else (o["x"], o["y"] + o["len"] / 2)


def _dist_to_wall(pt, a, b, orient) -> float:
    i = 0 if orient == "h" else 1  # axis the wall runs along
    lo, hi = min(a[i], b[i]), max(a[i], b[i])
    along = min(hi, max(lo, pt[i]))
    return ((pt[i] - along) ** 2 + (pt[1 - i] - a[1 - i]) ** 2) ** 0.5


def opening_rooms(layout: dict, o: dict, tol: float = WALL_TOL) -> list[dict]:
    """Rooms with a wall (same orientation, L-cut inner walls included) through the opening's midpoint.
    A window on a wall shared by two rooms belongs to both."""
    m = opening_mid(o)
    return [r for r in layout.get("rooms", [])
            if any(orient == o["orient"] and _dist_to_wall(m, a, b, orient) <= tol for a, b, orient in walls(r))]


def placed_in(layout: dict, room: dict, entity_ids) -> list[str]:
    wanted = set(entity_ids)
    return [p["entity_id"] for p in layout.get("placements", [])
            if p["entity_id"] in wanted and in_room(room, p["x"], p["y"])]
