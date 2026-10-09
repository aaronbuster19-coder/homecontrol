"""Quick tiles (/?view=tiles): the user's pinned favourite devices, in their order. Own SQLite table, shared by all
devices signed in; the layout is untouched. Pins only choose what the tiles view shows — every tap there goes
through the same device endpoints and the same fridge / keep-on / home-server protections as the device sheets."""
import json
import sqlite3

MAX_PINS = 24
KINDS = ("light", "plug", "valve", "dehumidifier", "media", "sensor")


class TilesError(ValueError):
    pass


class TileStore:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS tiles (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def pins(self) -> list[str]:
        with self._conn() as c:
            row = c.execute("SELECT data FROM tiles WHERE id = 1").fetchone()
        if not row:
            return []
        try:
            pins = json.loads(row[0]).get("pins")
        except (ValueError, AttributeError):
            return []
        return [p for p in pins if isinstance(p, str)] if isinstance(pins, list) else []

    def save(self, pins: list[str]) -> list[str]:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO tiles (id, data) VALUES (1, ?)", (json.dumps({"pins": pins}),))
        return pins


def validate_pins(pins, known: dict, stored: list[str]) -> list[str]:
    """known: entity id → Device. A pin must be a controllable device HA lists now, or one already pinned (so a device
    that is offline or briefly missing from discovery keeps its place). Duplicates are dropped, order kept."""
    if not isinstance(pins, list) or not all(isinstance(p, str) for p in pins):
        raise TilesError("pins must be a list of entity ids")
    out = list(dict.fromkeys(pins))
    if len(out) > MAX_PINS:
        raise TilesError(f"at most {MAX_PINS} tiles")
    for eid in out:
        d = known.get(eid)
        if d is None and eid not in stored:
            raise TilesError(f"unknown device {eid!r}")
        if d is not None and d.kind not in KINDS:
            raise TilesError(f"{eid!r} can't be a tile")
    return out


def add_routes(app, store: TileStore, devices, json_body) -> None:
    """GET /api/tiles, PUT /api/tiles {"pins": [...]} (order / remove), POST and DELETE /api/tiles/{entity_id}."""
    from fastapi import HTTPException, Request

    async def save(pins: list[str]) -> dict:
        try:
            return {"pins": store.save(validate_pins(pins, await devices(), store.pins()))}
        except TilesError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/tiles")
    async def get_tiles():
        return {"pins": store.pins()}

    @app.put("/api/tiles")
    async def put_tiles(request: Request):
        body = await json_body(request)
        if not isinstance(body, dict) or "pins" not in body:
            raise HTTPException(400, "body must be {\"pins\": [entity ids]}")
        return await save(body["pins"])

    @app.post("/api/tiles/{entity_id}")
    async def pin_tile(entity_id: str):
        pins = store.pins()
        return await save(pins if entity_id in pins else [*pins, entity_id])

    @app.delete("/api/tiles/{entity_id}")
    async def unpin_tile(entity_id: str):
        return {"pins": store.save([p for p in store.pins() if p != entity_id])}
