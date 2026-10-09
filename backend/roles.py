"""Who may call what: one policy table for every route, enforced by an ASGI middleware inside the auth guard.

Roles (backend/users.py): admin > member > guest.
  admin   everything: settings, layout, schedules, users.
  member  controls every device and Away/Home, reads everything; no settings, layout or user changes.
  guest   sees the plan and switches / dims the lights; nothing else.

POLICY maps (METHOD, route path template) -> the least role allowed. HEAD counts as GET; a Mount is ("*", its path).
A route that is NOT in the table is admin-only, so a new endpoint is safe by default; backend/tests/test_roles.py fails
until it is classified. To open a new endpoint to members or guests add one line here, e.g.
    ("GET", "/api/brief"): MEMBER,
LIGHTS: guests may call it only for a light.* {entity_id}; members and admins always.
"""
import json

from starlette.routing import Match, Mount

from .auth import is_public

GUEST, MEMBER, ADMIN = "guest", "member", "admin"
LIGHTS = "lights"
RANK = {GUEST: 0, MEMBER: 1, ADMIN: 2}

POLICY: dict[tuple[str, str], str] = {
    # ---- public (the auth guard lets these through signed out) and the app shell ----
    ("POST", "/api/login"): GUEST,
    ("GET", "/healthz"): GUEST,
    ("GET", "/sw.js"): GUEST,
    ("GET", "/manifest.webmanifest"): GUEST,
    ("*", ""): GUEST,  # the static frontend mounted at "/"
    # ---- own session ----
    ("POST", "/api/logout"): GUEST,
    ("GET", "/api/me"): GUEST,
    ("POST", "/api/me/password"): GUEST,
    # ---- what the plan needs to draw ----
    ("GET", "/api/devices"): GUEST,
    ("GET", "/api/events"): GUEST,
    ("GET", "/api/layout"): GUEST,
    ("GET", "/api/mode"): GUEST,
    ("GET", "/api/weather"): GUEST,
    # ---- lights: guests too ----
    ("POST", "/api/devices/{entity_id}/toggle"): LIGHTS,
    ("POST", "/api/devices/{entity_id}/turn_off"): LIGHTS,
    ("POST", "/api/devices/{entity_id}/light"): LIGHTS,
    # ---- device control (members) ----
    ("POST", "/api/devices/{entity_id}/humidity"): MEMBER,
    ("POST", "/api/devices/{entity_id}/mode"): MEMBER,
    ("POST", "/api/devices/{entity_id}/media"): MEMBER,
    ("POST", "/api/devices/{entity_id}/temperature"): MEMBER,
    ("GET", "/api/media/{entity_id}/artwork"): MEMBER,
    ("POST", "/api/bulk"): MEMBER,
    ("POST", "/api/valves/temperature"): MEMBER,
    ("POST", "/api/mode"): MEMBER,  # Away / I'm home
    ("POST", "/api/devices/refresh"): MEMBER,
    ("POST", "/api/weather/refresh"): MEMBER,
    ("POST", "/api/alerts/mute"): MEMBER,  # a temporary mute of pushes, not a setting
    # ---- push on this device (members) ----
    ("GET", "/api/push/key"): MEMBER,
    ("POST", "/api/push/subscribe"): MEMBER,
    ("POST", "/api/push/unsubscribe"): MEMBER,
    ("POST", "/api/push/test"): MEMBER,
    # ---- reading history, energy, settings (members) ----
    ("GET", "/api/history/{entity_id}"): MEMBER,
    ("GET", "/api/doors/log"): MEMBER,
    ("GET", "/api/alerts/settings"): MEMBER,
    ("GET", "/api/alerts/quiet"): MEMBER,
    ("GET", "/api/schedules"): MEMBER,
    ("GET", "/api/automations/status"): MEMBER,
    ("GET", "/api/appliances"): MEMBER,
    ("GET", "/api/appliances/{fid}/stats"): MEMBER,
    ("GET", "/api/summary/latest"): MEMBER,
    ("POST", "/api/summary/preview"): MEMBER,  # computes a preview, changes nothing
    ("GET", "/api/energy/settings"): MEMBER,
    ("GET", "/api/energy"): MEMBER,
    ("GET", "/api/energy/standby"): MEMBER,
    ("GET", "/api/activity"): MEMBER,
    ("GET", "/api/presence"): MEMBER,
    ("GET", "/api/standby"): MEMBER,
    # ---- settings, layout, automations, users: admin (listed so the table is complete; unlisted = admin too) ----
    ("PUT", "/api/alerts/settings"): ADMIN,
    ("PUT", "/api/schedules/settings"): ADMIN,
    ("POST", "/api/schedules"): ADMIN,
    ("PUT", "/api/schedules/{sid}"): ADMIN,
    ("DELETE", "/api/schedules/{sid}"): ADMIN,
    ("PUT", "/api/mode/settings"): ADMIN,
    ("PUT", "/api/layout"): ADMIN,
    ("PUT", "/api/devices/{entity_id}/meta"): ADMIN,
    ("PUT", "/api/energy/settings"): ADMIN,
    ("PUT", "/api/weather/settings"): ADMIN,
    ("PUT", "/api/presence/settings"): ADMIN,
    ("PUT", "/api/standby/{entity_id}"): ADMIN,
    ("GET", "/api/users"): ADMIN,
    ("POST", "/api/users"): ADMIN,
    ("PATCH", "/api/users/{username}"): ADMIN,
    ("PUT", "/api/users/{username}/password"): ADMIN,
    ("DELETE", "/api/users/{username}"): ADMIN,
    # ---- v9 features (merged before users) ----
    ("GET", "/api/brief"): MEMBER,
    ("GET", "/api/energy/report"): MEMBER,
    ("GET", "/api/climate"): MEMBER,
    ("PUT", "/api/climate/settings"): ADMIN,
    ("PUT", "/api/climate/rooms/{room_id}"): ADMIN,
    ("POST", "/api/climate/rooms/{room_id}/learn"): ADMIN,
    ("GET", "/api/underlay"): GUEST,
    ("GET", "/api/underlay/image"): GUEST,
    ("PUT", "/api/underlay"): ADMIN,
    ("POST", "/api/underlay/image"): ADMIN,
    ("DELETE", "/api/underlay"): ADMIN,
    ("GET", "/api/tiles"): GUEST,
    ("PUT", "/api/tiles"): MEMBER,
    ("POST", "/api/tiles/{entity_id}"): MEMBER,
    ("DELETE", "/api/tiles/{entity_id}"): MEMBER,
    # disco mode (backend/disco.py): guests too; the handlers check every light taking part is one the role may control
    ("GET", "/api/disco"): GUEST,
    ("POST", "/api/disco/start"): GUEST,
    ("POST", "/api/disco/stop"): GUEST,
    ("GET", "/openapi.json"): ADMIN,
    ("GET", "/docs"): ADMIN,
    ("GET", "/docs/oauth2-redirect"): ADMIN,
    ("GET", "/redoc"): ADMIN,
}

DENIED = {GUEST: "Guests can only switch the lights.", MEMBER: "Only an admin can change settings."}


def route_key(route, method: str) -> tuple[str, str]:
    if isinstance(route, Mount):
        return ("*", route.path)
    return ("GET" if method == "HEAD" else method, route.path)


def level_for(key: tuple[str, str]) -> str:
    return POLICY.get(key, ADMIN)


def allowed(role: str | None, level: str, params: dict) -> bool:
    if role not in RANK:
        return False
    if level == LIGHTS:
        return RANK[role] >= RANK[MEMBER] or str(params.get("entity_id", "")).startswith("light.")
    return RANK[role] >= RANK.get(level, RANK[ADMIN])  # an unknown level is admin-only too


def find_route(routes, scope):
    """The route Starlette will dispatch to (first full match; else the first path-only match, which answers 405)."""
    partial = None
    for route in routes:
        match, child = route.matches(scope)
        if match == Match.FULL:
            return route, child
        if match == Match.PARTIAL and partial is None:
            partial = (route, child)
    return partial or (None, {})


def classified_routes(app) -> list[tuple[str, str]]:
    """Every (method, path) key the app serves, for the completeness test."""
    keys = []
    for route in app.router.routes:
        if isinstance(route, Mount):
            keys.append(route_key(route, "*"))
        else:
            keys += [route_key(route, m) for m in sorted(getattr(route, "methods", None) or ["GET"]) if m != "HEAD"]
    return keys


class RoleMiddleware:
    """Pure ASGI, added inside the auth guard (which has put "user" and "role" into scope["state"])."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        role = (scope.get("state") or {}).get("role")
        if role is None and is_public(scope["path"]):
            return await self.app(scope, receive, send)
        route, child = find_route(scope["app"].router.routes, scope)
        if route is None:  # nothing serves this path: let the router answer 404
            if role in RANK:
                return await self.app(scope, receive, send)
            return await self._deny(send, ADMIN)
        level = level_for(route_key(route, scope["method"]))
        if allowed(role, level, child.get("path_params") or {}):
            return await self.app(scope, receive, send)
        await self._deny(send, role)

    @staticmethod
    async def _deny(send, role):
        body = json.dumps({"detail": DENIED.get(role, "Only an admin can do that.")}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
