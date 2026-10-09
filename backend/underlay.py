"""Floor-plan photo underlay: one image drawn under the plan in edit mode so walls can be traced over it.

Kept apart from the layout (the layout JSON doesn't change): the image is a file next to DB_PATH (underlay.img) and
its placement is one row in the same SQLite database. Placement: x/y is the image centre (m), width its width (m;
the height follows the image's aspect), rot whole-ish degrees clockwise, opacity 0.05–1. show_view also draws it
outside edit mode; invert_dark inverts it in the dark theme (black-on-white plans read better as white-on-dark).

Routes (all behind the app's sign-in, like every /api path):
  GET    /api/underlay        placement + image info, or {"image": null}
  PUT    /api/underlay        change any placement fields (the rest stay as stored)
  POST   /api/underlay/image  raw PNG / JPEG / WebP body (Content-Type image/...), at most MAX_BYTES
  GET    /api/underlay/image  the image itself (?v=<version> makes it cacheable)
  DELETE /api/underlay        remove image and placement
Uploading and removing change what everyone sees: pass a FastAPI dependency as `admin` to restrict them.
"""
import json
import math
import os
import sqlite3
import struct
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

MAX_BYTES = 10 * 1024 * 1024
MAX_PX = 12000  # each side
TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
WIDTH = (0.5, 200.0)  # metres
OPACITY = (0.05, 1.0)
COORD_MAX = 1000.0
DEFAULTS = {"x": 5.0, "y": 4.0, "width": 10.0, "rot": 0.0, "opacity": 0.5, "show_view": False, "invert_dark": True}


class UnderlayError(ValueError):
    pass


def _png(b: bytes):
    if b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) >= 24 and b[12:16] == b"IHDR":
        return struct.unpack(">II", b[16:24])
    return None


def _jpeg(b: bytes):
    if b[:3] != b"\xff\xd8\xff":
        return None
    i = 2
    while i + 9 < len(b):
        if b[i] != 0xFF:
            return None
        m = b[i + 1]
        if m == 0xFF:  # fill byte
            i += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:  # markers without a length
            i += 2
            continue
        ln = struct.unpack(">H", b[i + 2:i + 4])[0]
        if ln < 2:
            return None
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):  # start of frame: height, width
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
            return w, h
        i += 2 + ln
    return None


def _webp(b: bytes):
    if b[:4] != b"RIFF" or b[8:12] != b"WEBP" or len(b) < 30:
        return None
    kind = b[12:16]
    if kind == b"VP8 " and b[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", b[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if kind == b"VP8L" and b[20] == 0x2F:
        v = int.from_bytes(b[21:25], "little")
        return (v & 0x3FFF) + 1, ((v >> 14) & 0x3FFF) + 1
    if kind == b"VP8X":
        return int.from_bytes(b[24:27], "little") + 1, int.from_bytes(b[27:30], "little") + 1
    return None


def sniff(data: bytes) -> tuple[str, int, int]:
    """(type, width px, height px) of a PNG, JPEG or WebP image, read from its bytes (the Content-Type is only a hint).
    Anything else (SVG, HEIC, GIF, HTML…) is refused."""
    for name, parse in (("png", _png), ("jpeg", _jpeg), ("webp", _webp)):
        try:
            dims = parse(data)
        except (struct.error, IndexError):
            dims = None
        if dims:
            w, h = dims
            if not (0 < w <= MAX_PX and 0 < h <= MAX_PX):
                raise UnderlayError(f"image must be at most {MAX_PX} × {MAX_PX} pixels")
            return name, w, h
    raise UnderlayError("not a PNG, JPEG or WebP image")


def _num(v, what: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise UnderlayError(f"{what} must be a number")
    if not lo <= v <= hi:
        raise UnderlayError(f"{what} must be {lo:g}–{hi:g}")
    return float(v)


def validate_placement(data) -> dict:
    """Only the fields sent; unknown fields are refused so typos don't pass silently."""
    if not isinstance(data, dict):
        raise UnderlayError("send an object")
    extra = set(data) - set(DEFAULTS)
    if extra:
        raise UnderlayError(f"unknown field {sorted(extra)[0]!r}")
    out = {}
    for k in ("x", "y"):
        if k in data:
            out[k] = round(_num(data[k], k, -COORD_MAX, COORD_MAX), 3)
    if "width" in data:
        out["width"] = round(_num(data["width"], "width (m)", *WIDTH), 3)
    if "rot" in data:
        r = _num(data["rot"], "rot", -3600, 3600) % 360
        out["rot"] = round(r - 360 if r > 180 else r, 2)
    if "opacity" in data:
        out["opacity"] = round(_num(data["opacity"], "opacity", *OPACITY), 3)
    for k in ("show_view", "invert_dark"):
        if k in data:
            if not isinstance(data[k], bool):
                raise UnderlayError(f"{k} must be true or false")
            out[k] = data[k]
    return out


class UnderlayStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        d = os.path.dirname(db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.file = os.path.join(d or ".", "underlay.img")
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS underlay (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _row(self) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT data FROM underlay WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else None

    def _write(self, data: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO underlay (id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                      (json.dumps(data),))

    def get(self) -> dict:
        with self._lock:
            row = self._row()
        if not row or not row.get("image") or not os.path.isfile(self.file):
            return {"image": None}
        return {**DEFAULTS, **row}

    def update(self, changes: dict) -> dict:
        with self._lock:
            row = self._row()
            if not row or not row.get("image"):
                raise LookupError("no photo uploaded")
            row.update(changes)
            self._write(row)
        return self.get()

    def put_image(self, data: bytes) -> dict:
        kind, w, h = sniff(data)
        tmp = self.file + ".tmp"
        with self._lock:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, self.file)
            row = self._row() or {}
            fresh = not row.get("image")  # a replacement keeps where the old photo was
            row["image"] = {"type": kind, "w": w, "h": h, "bytes": len(data), "version": format(time.time_ns(), "x")}
            self._write(row)
        return {**self.get(), "fresh": fresh}

    def delete(self) -> bool:
        with self._lock:
            had = bool(self._row())
            with self._conn() as c:
                c.execute("DELETE FROM underlay WHERE id = 1")
            try:
                os.remove(self.file)
                had = True
            except FileNotFoundError:
                pass
        return had

    def read_image(self) -> tuple[bytes, str, str] | None:
        info = self.get()["image"]
        if not info:
            return None
        try:
            with open(self.file, "rb") as f:
                return f.read(), TYPES[info["type"]], info["version"]
        except FileNotFoundError:
            return None


async def read_limited(request: Request, limit: int = MAX_BYTES) -> bytes:
    too_big = HTTPException(413, f"image must be at most {limit // (1024 * 1024)} MB")
    try:
        if int(request.headers.get("content-length") or 0) > limit:
            raise too_big
    except ValueError:
        raise HTTPException(400, "bad Content-Length")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise too_big
    return bytes(buf)


def router(db_path: str, admin=None) -> APIRouter:
    """admin: optional FastAPI dependency guarding upload and delete (e.g. an admin-role check)."""
    store = UnderlayStore(db_path)
    guard = [Depends(admin)] if admin else []
    r = APIRouter()

    @r.get("/api/underlay")
    async def get_underlay():
        return store.get()

    @r.put("/api/underlay", dependencies=guard)
    async def put_underlay(request: Request):
        try:
            changes = validate_placement(await request.json())
        except ValueError as e:  # bad JSON or a bad field (UnderlayError is a ValueError)
            raise HTTPException(400, str(e) if isinstance(e, UnderlayError) else "invalid JSON")
        try:
            return store.update(changes)
        except LookupError as e:
            raise HTTPException(404, str(e))

    @r.post("/api/underlay/image", dependencies=guard)
    async def upload_image(request: Request):
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in TYPES.values():
            raise HTTPException(415, "upload a PNG, JPEG or WebP image")
        data = await read_limited(request)
        if not data:
            raise HTTPException(400, "empty upload")
        try:
            return store.put_image(data)
        except UnderlayError as e:
            raise HTTPException(415, str(e))

    @r.get("/api/underlay/image")
    async def get_image(v: str = ""):
        got = store.read_image()
        if got is None:
            raise HTTPException(404, "no photo uploaded")
        content, ctype, version = got
        cache = "private, max-age=31536000, immutable" if v and v == version else "private, no-cache"
        return Response(content, media_type=ctype, headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff",
                                                            "Content-Disposition": "inline"})

    @r.delete("/api/underlay", dependencies=guard)
    async def delete_underlay():
        return {"removed": store.delete()}

    return r
