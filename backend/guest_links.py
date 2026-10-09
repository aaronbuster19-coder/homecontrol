"""Guest links: a time-limited link / QR code that lets a visitor switch one room's lights (or devices an admin chose)
and nothing else, without an account.

- An admin creates a link: POST /api/guest/links {label, room | devices, minutes}. The answer carries the token once;
  only its SHA-256 is stored, so a lost link can't be shown again (revoke it and make a new one).
- The visitor opens /guest.html#<token>. The token sits in the URL fragment, which browsers never send to a server or
  put in a Referer. guest.js swaps it for an HttpOnly cookie (POST /api/guest/redeem, rate-limited) scoped to
  /api/guest, so it never travels with any other request.
- GuestLinkMiddleware (outermost) turns that cookie into a "link" session for /api/guest/session* only. backend/roles.py
  lets the link role call exactly the LINK routes below, so every other route (current or future) answers 403, and
  AuthMiddleware ignores the cookie everywhere else.
- Every session request re-checks the link (expired or revoked -> 401 at once) and re-resolves its scope from the
  current layout. Only in-scope devices are ever sent, with just the fields the guest page needs. Protected plugs
  (keep on, fridge / freezer / home server) are never in scope.
"""
import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time

from fastapi import HTTPException

from .appliances import protected_plugs
from .auth import RateLimiter, client_key, cookie_value
from .geometry import in_room
from .live import build_device, sse

COOKIE = "hc_guest"
COOKIE_PATH = "/api/guest"
SESSION = "/api/guest/session"
TOKEN_BYTES = 32  # 256 bits from secrets: unguessable, so a fast hash at rest is enough (no salt / KDF needed)
MIN_MINUTES, MAX_MINUTES = 15, 30 * 24 * 60
MAX_ACTIVE = 20
MAX_DEVICES = 20
LABEL_MAX = 40
KEEP_ENDED = 7 * 86400  # ended links stay listed for a week, then go
TOUCH_EVERY = 60  # last_used is written at most once a minute per link
CHECK_EVERY = 10  # an open event stream re-checks its link (expiry, revoke, scope) this often
LINK_ID = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
LIGHT_FIELDS = ("brightness", "color_mode", "hs_color", "rgb_color", "color_temp_kelvin", "supports_brightness",
                "supports_color", "supports_color_temp", "min_color_temp_kelvin", "max_color_temp_kelvin")
GONE = "This guest link has expired or been revoked."


class LinkError(ValueError):
    pass


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LinkStore:
    """SQLite: id, sha256(token), label, scope JSON, created/expires/revoked (epoch s), created_by, last_used, uses."""

    def __init__(self, path: str, clock=time.time):
        self.path, self.clock, self._lock = path, clock, threading.Lock()
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS guest_links (id TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, "
                      "label TEXT NOT NULL, scope TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, "
                      "revoked REAL, created_by TEXT, last_used REAL, uses INTEGER NOT NULL DEFAULT 0)")
        self._touched: dict[str, float] = {}

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["scope"] = json.loads(d["scope"])
        return d

    def active(self, link: dict | None) -> bool:
        return bool(link) and link["revoked"] is None and link["expires"] > self.clock()

    def by_token(self, token) -> dict | None:
        """The live link for a token, else None (unknown, expired or revoked)."""
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            return None
        h = token_hash(token)
        with self._conn() as c:
            r = c.execute("SELECT * FROM guest_links WHERE hash = ?", (h,)).fetchone()
        if r is None:
            return None
        link = self._row(r)
        return link if self.active(link) else None

    def get(self, lid: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM guest_links WHERE id = ?", (lid,)).fetchone()
        return self._row(r) if r else None

    def all(self) -> list[dict]:
        now = self.clock()
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM guest_links WHERE MIN(expires, COALESCE(revoked, expires)) < ?", (now - KEEP_ENDED,))
            rows = c.execute("SELECT * FROM guest_links ORDER BY created DESC").fetchall()
        return [self._row(r) for r in rows]

    def create(self, label: str, scope: dict, minutes: int, by: str | None) -> tuple[dict, str]:
        token, lid, now = secrets.token_urlsafe(TOKEN_BYTES), secrets.token_urlsafe(9), self.clock()
        with self._lock, self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM guest_links WHERE revoked IS NULL AND expires > ?", (now,)).fetchone()[0]
            if n >= MAX_ACTIVE:
                raise LinkError(f"at most {MAX_ACTIVE} live guest links: revoke one first")
            c.execute("INSERT INTO guest_links (id, hash, label, scope, created, expires, created_by) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (lid, token_hash(token), label, json.dumps(scope), now, now + minutes * 60, by))
        return self.get(lid), token

    def revoke(self, lid: str) -> dict | None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE guest_links SET revoked = ? WHERE id = ? AND revoked IS NULL", (self.clock(), lid))
        return self.get(lid)

    def used(self, lid: str, redeemed: bool = False) -> None:
        now = self.clock()
        if not redeemed and now - self._touched.get(lid, 0) < TOUCH_EVERY:
            return
        self._touched[lid] = now
        with self._lock, self._conn() as c:
            c.execute("UPDATE guest_links SET last_used = ?, uses = uses + ? WHERE id = ?", (now, int(redeemed), lid))


def check_label(v) -> str:
    if not isinstance(v, str):
        raise LinkError("label: give the link a name, e.g. who it's for")
    v = " ".join(v.split())
    if not 1 <= len(v) <= LABEL_MAX:
        raise LinkError(f"label: 1–{LABEL_MAX} characters")
    return v


def check_minutes(v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not MIN_MINUTES <= v <= MAX_MINUTES:
        raise LinkError(f"minutes: a whole number from {MIN_MINUTES} to {MAX_MINUTES} (30 days)")
    return v


def guest_item(item: dict) -> dict:
    """Only what the guest page draws: no related sensors, power, model or anything else."""
    out = {k: item.get(k) for k in ("entity_id", "kind", "name", "state")}
    if item.get("kind") == "light":
        out.update({k: item.get(k) for k in LIGHT_FIELDS if k in item})
    return out


class GuestLinks:
    def __init__(self, store: LinkStore, devices, layout, live, ha_states, clock=time.time):
        self.store, self.devices, self.layout, self.live, self.ha_states, self.clock = \
            store, devices, layout, live, ha_states, clock
        self.limiter = RateLimiter(max_fails=10, window=600, max_global=200)

    # ---- scope ----
    @staticmethod
    def blocked_plugs(layout: dict) -> set[str]:
        return set((layout.get("settings") or {}).get("keep_on") or []) | protected_plugs(layout)

    def room(self, layout: dict, rid) -> dict | None:
        return next((r for r in layout.get("rooms", []) if r.get("id") == rid), None)

    def resolve(self, scope: dict, devs: dict, layout: dict) -> list[str]:
        """The entity ids a link may see and switch right now (a layout edit can move a light out of the room)."""
        blocked = self.blocked_plugs(layout)

        def ok(e):
            d = devs.get(e)
            return d is not None and not d.hidden and (d.kind == "light" or (d.kind == "plug" and e not in blocked))
        if "room" in scope:
            room = self.room(layout, scope["room"])
            if room is None:
                return []
            ids = [p["entity_id"] for p in layout.get("placements", []) if in_room(room, p["x"], p["y"])]
            return sorted({e for e in ids if ok(e) and devs[e].kind == "light"})
        return sorted({e for e in scope.get("devices", []) if ok(e)})

    async def validate(self, data) -> tuple[str, dict, int]:
        if not isinstance(data, dict) or not set(data) <= {"label", "room", "devices", "minutes"}:
            raise LinkError("send {label, room or devices, minutes}")
        label, minutes = check_label(data.get("label")), check_minutes(data.get("minutes"))
        if ("room" in data) == ("devices" in data):
            raise LinkError("give either a room or a list of devices")
        devs, layout = await self.devices(), self.layout()
        if "room" in data:
            if not isinstance(data["room"], str) or self.room(layout, data["room"]) is None:
                raise LinkError("unknown room")
            scope = {"room": data["room"]}
            if not self.resolve(scope, devs, layout):
                raise LinkError("that room has no lights on the plan")
            return label, scope, minutes
        ids = data["devices"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_DEVICES or not all(isinstance(e, str) for e in ids):
            raise LinkError(f"devices: 1–{MAX_DEVICES} lights or plugs")
        ids = sorted(set(ids))
        allowed = set(self.resolve({"devices": ids}, devs, layout))
        for e in ids:
            if e not in allowed:
                d = devs.get(e)
                if d is not None and d.kind == "plug" and e in self.blocked_plugs(layout):
                    raise LinkError(f"{d.name} is kept on (fridge, home server or Keep on): guests can't switch it")
                raise LinkError(f"not a light or plug: {e}")
        return label, {"devices": ids}, minutes

    def public(self, link: dict, devs: dict | None = None, layout: dict | None = None) -> dict:
        """For the admin's list: never the token or its hash."""
        out = {k: link[k] for k in ("id", "label", "scope", "created", "expires", "revoked", "created_by", "last_used",
                                    "uses")}
        out["active"] = self.store.active(link)
        if devs is not None and layout is not None:
            room = self.room(layout, link["scope"].get("room"))
            out["room_name"] = room.get("name") if room else None
            out["devices"] = [{"entity_id": e, "name": devs[e].name, "kind": devs[e].kind}
                              for e in self.resolve(link["scope"], devs, layout)]
        return out

    # ---- a guest's view ----
    async def session(self, link: dict) -> dict:
        devs, layout = await self.devices(), self.layout()
        await self.ha_states()
        ids = self.resolve(link["scope"], devs, layout)
        room = self.room(layout, link["scope"].get("room"))
        return {"label": link["label"], "room": room.get("name") if room else None, "expires": link["expires"],
                "devices": [guest_item(build_device(devs[e], self.live.states)) for e in ids]}

    async def in_scope(self, link: dict, entity_id: str, kinds: tuple[str, ...]):
        devs = await self.devices()
        if entity_id not in self.resolve(link["scope"], devs, self.layout()):
            raise HTTPException(404, "not part of this guest link")  # the same for devices that don't exist
        if devs[entity_id].kind not in kinds:
            raise HTTPException(400, f"not supported for {devs[entity_id].kind}")
        return devs[entity_id]

    async def stream(self, lid: str, client, ids: set[str], first: dict):
        """SSE for one guest: their devices' changes only, then `end` (and close) once the link stops working."""
        live = self.live
        try:
            yield sse("snapshot", first)
            next_check = time.monotonic() + CHECK_EVERY
            while True:
                link = self.store.get(lid)
                if not self.store.active(link):
                    yield sse("end", {"detail": GONE})
                    return
                if time.monotonic() >= next_check:  # a light moved into or out of the room: a fresh snapshot
                    now_ids = set(self.resolve(link["scope"], await self.devices(), self.layout()))
                    if now_ids != ids:
                        ids = now_ids
                        yield sse("snapshot", await self.session(link))
                    next_check = time.monotonic() + CHECK_EVERY
                wait = max(0.05, min(CHECK_EVERY, link["expires"] - self.clock() + 0.05, next_check - time.monotonic()))
                try:
                    msg = await asyncio.wait_for(client.queue.get(), wait)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if msg is None:  # dropped as a slow client: the page reconnects
                    return
                if not msg.startswith("event: device\n"):
                    continue  # status, appliances, disco …: nothing a guest needs, so nothing leaks
                try:
                    item = json.loads(msg.split("data: ", 1)[1])
                except (IndexError, ValueError):
                    continue
                if item.get("entity_id") in ids:
                    yield sse("device", guest_item(item))
        finally:
            live.unsubscribe(client)


class GuestLinkMiddleware:
    """Outermost, pure ASGI. For /api/guest/session* with a guest cookie: the link must be live, else 401 (and the
    attempt counts towards the rate limit). A live link becomes scope state user / role="link" / link=id, which
    AuthMiddleware passes through and RoleMiddleware checks against the LINK routes. Without a guest cookie the request
    goes the normal way (and a signed-in account gets 403 there: those routes are for link sessions only)."""

    def __init__(self, app, links: GuestLinks):
        self.app, self.links = app, links

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path == SESSION or path.startswith(SESSION + "/")):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        token = cookie_value(headers.get("cookie", ""), COOKIE)
        if not token:
            return await self.app(scope, receive, send)
        client = client_key(headers, (scope.get("client") or (None,))[0])
        if self.links.limiter.blocked(client):
            return await deny(send, 429, "too many attempts, try again later")
        link = self.links.store.by_token(token)
        if link is None:
            self.links.limiter.fail(client)
            return await deny(send, 401, GONE)
        self.links.store.used(link["id"])
        scope.setdefault("state", {}).update(user=f"Guest link: {link['label']}", role="link", link=link["id"])
        return await self.app(scope, receive, send)


async def deny(send, status: int, detail: str):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": json.dumps({"detail": detail}).encode()})


def add_routes(app, links: GuestLinks, json_body, light_data, before_switch, call_service, record) -> None:
    """Admin: GET/POST /api/guest/links, DELETE /api/guest/links/{lid}. Public: POST /api/guest/redeem.
    Link sessions only: GET /api/guest/session, GET /api/guest/session/events,
    POST /api/guest/session/devices/{entity_id}/toggle, POST /api/guest/session/devices/{entity_id}/light."""
    from fastapi import Request
    from fastapi.responses import JSONResponse, StreamingResponse

    from .auth import is_https

    store = links.store

    def link_of(request: Request) -> dict:
        link = store.get(getattr(request.state, "link", None) or "")
        if not store.active(link):  # GuestLinkMiddleware checked it; re-read in case it was revoked just now
            raise HTTPException(401, GONE)
        return link

    @app.get("/api/guest/links")
    async def list_links():
        devs, layout = await links.devices(), links.layout()
        return {"links": [links.public(x, devs, layout) for x in store.all()], "now": links.clock()}

    @app.post("/api/guest/links")
    async def create_link(request: Request):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "send JSON")  # a cross-site <form> can't send JSON
        try:
            label, scope, minutes = await links.validate(await json_body(request))
            link, token = store.create(label, scope, minutes, request.state.user)
        except LinkError as e:
            raise HTTPException(400, str(e))
        record("guest_link", action="created", by=request.state.user, label=label, link=link["id"],
               expires=link["expires"])
        return {**links.public(link, await links.devices(), links.layout()), "token": token,
                "path": f"/guest.html#{token}"}

    @app.delete("/api/guest/links/{lid}")
    async def revoke_link(lid: str, request: Request):
        link = store.get(lid) if LINK_ID.match(lid) else None
        if link is None:
            raise HTTPException(404, "no such guest link")
        was_active = store.active(link)
        link = store.revoke(lid)
        if was_active:
            record("guest_link", action="revoked", by=request.state.user, label=link["label"], link=lid)
            links.live.broadcast("guest_link", {})  # wakes open guest streams: they re-check and end at once
        return links.public(link)

    @app.post("/api/guest/redeem")
    async def redeem(request: Request):
        key = client_key(request.headers, request.client.host if request.client else None)
        if links.limiter.blocked(key):
            raise HTTPException(429, "too many attempts, try again later")
        data = await json_body(request)
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str):
            raise HTTPException(400, "send {token}")
        link = store.by_token(token)
        if link is None:
            links.limiter.fail(key)
            raise HTTPException(401, GONE)
        store.used(link["id"], redeemed=True)
        record("guest_link", action="opened", label=link["label"], link=link["id"], ip=key)
        resp = JSONResponse({"label": link["label"], "expires": link["expires"]})
        resp.set_cookie(COOKIE, token, max_age=max(1, int(link["expires"] - links.clock())), path=COOKIE_PATH,
                        httponly=True, samesite="strict", secure=is_https(request))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get(SESSION)
    async def guest_session(request: Request):
        return JSONResponse(await links.session(link_of(request)), headers={"Cache-Control": "no-store"})

    @app.get(SESSION + "/events")
    async def guest_events(request: Request):
        link = link_of(request)
        first = await links.session(link)
        client = links.live.subscribe()
        ids = {d["entity_id"] for d in first["devices"]}
        return StreamingResponse(links.stream(link["id"], client, ids, first), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post(SESSION + "/devices/{entity_id}/toggle")
    async def guest_toggle(entity_id: str, request: Request):
        await links.in_scope(link_of(request), entity_id, ("light", "plug"))
        await before_switch([entity_id])
        await call_service(entity_id.split(".", 1)[0], "toggle", {"entity_id": entity_id})
        return {"ok": True}

    @app.post(SESSION + "/devices/{entity_id}/light")
    async def guest_light(entity_id: str, request: Request):
        await links.in_scope(link_of(request), entity_id, ("light",))
        data = light_data(await json_body(request))
        await before_switch([entity_id])
        await call_service("light", "turn_on", {"entity_id": entity_id, **data})
        return {"ok": True}
