"""Role enforcement (backend/roles.py): every route is classified, unclassified ones are admin-only, and each role
gets exactly what the policy table says on every route."""
import base64
import re

import pytest
from starlette.routing import Route

from backend import roles
from backend.roles import ADMIN, GUEST, LIGHTS, LINK, MEMBER, POLICY, allowed, classified_routes

MEMBER_PW, GUEST_PW = "member-pass-1", "guest-pass-1"


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def people(client):
    """client (the APP_USER admin) plus a member "mia" and a guest "gus": role -> request headers."""
    for name, role, pw in (("mia", "member", MEMBER_PW), ("gus", "guest", GUEST_PW)):
        r = client.post("/api/users", json={"username": name, "password": pw, "role": role})
        assert r.status_code == 200, r.text
    return {ADMIN: {}, MEMBER: basic("mia", MEMBER_PW), GUEST: basic("gus", GUEST_PW)}


def test_every_route_is_classified(client):
    """A new endpoint (from any branch) must get a line in roles.POLICY; until then it is admin-only."""
    served = set(classified_routes(client.app))
    missing = sorted(served - set(POLICY))
    assert not missing, f"classify these in backend/roles.py POLICY: {missing}"
    stale = sorted(set(POLICY) - served)
    assert not stale, f"POLICY lists routes that no longer exist: {stale}"


def test_policy_levels_are_known():
    assert set(POLICY.values()) <= {GUEST, MEMBER, ADMIN, LIGHTS, LINK}


def test_allowed_matrix():
    for role, want in ((GUEST, [1, 0, 0]), (MEMBER, [1, 1, 0]), (ADMIN, [1, 1, 1])):
        assert [allowed(role, lvl, {}) for lvl in (GUEST, MEMBER, ADMIN)] == [bool(x) for x in want]
    assert allowed(GUEST, LIGHTS, {"entity_id": "light.kitchen"})
    assert not allowed(GUEST, LIGHTS, {"entity_id": "switch.fan"})
    assert not allowed(GUEST, LIGHTS, {})
    assert allowed(MEMBER, LIGHTS, {"entity_id": "switch.fan"})
    assert not allowed(None, GUEST, {}) and not allowed("root", GUEST, {})
    assert not allowed(MEMBER, "typo", {}) and allowed(ADMIN, "typo", {})
    # A guest-link session gets LINK routes and nothing else; LINK routes are for link sessions only.
    assert allowed(LINK, LINK, {"entity_id": "switch.fan"})
    for level in (GUEST, MEMBER, ADMIN, LIGHTS, "typo"):
        assert not allowed(LINK, level, {"entity_id": "light.kitchen"}), level
    for role in (GUEST, MEMBER, ADMIN, None):
        assert not allowed(role, LINK, {}), role


def test_unclassified_route_is_admin_only(client, people):
    async def secret(request):
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True})

    client.app.router.routes.insert(0, Route("/api/brand-new", secret, methods=["GET", "POST"]))
    assert ("GET", "/api/brand-new") not in POLICY
    assert client.get("/api/brand-new").status_code == 200
    for role in (MEMBER, GUEST):
        r = client.get("/api/brand-new", headers=people[role])
        assert r.status_code == 403, role
        assert client.post("/api/brand-new", headers=people[role]).status_code == 403
        assert client.head("/api/brand-new", headers=people[role]).status_code == 403


def sample(path: str, entity: str = "light.kitchen_1") -> str:
    return re.sub(r"\{(\w+)\}", lambda m: {"entity_id": entity, "username": "nobody"}.get(m.group(1), "x"), path)


SKIP = {("GET", "/api/events"), ("*", "")}  # an endless stream; the static mount (tested below)


def cases():
    for (method, path), level in sorted(POLICY.items()):
        if (method, path) in SKIP:
            continue
        yield method, path, level, "light.kitchen_1"
        if level == LIGHTS:
            yield method, path, level, "switch.fan"


@pytest.mark.parametrize("role", [ADMIN, MEMBER, GUEST])
def test_every_route_per_role(client, people, fake_ha, role):
    """Each route answers 403 exactly when the policy says the role can't use it, and a refused call never reaches HA."""
    for method, path, level, entity in cases():
        url = sample(path, entity)
        before = len(fake_ha.service_calls())
        kw = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
        r = client.request(method, url, headers=people[role], **kw)
        want = allowed(role, level, {"entity_id": entity})
        if want:
            assert r.status_code != 403, (role, method, url, r.text)
        else:
            assert r.status_code == 403, (role, method, url, r.status_code)
            assert r.json()["detail"] in (*roles.DENIED.values(), roles.LINK_ONLY)
            assert len(fake_ha.service_calls()) == before, (role, method, url)


def test_guest_lights_only(client, people, fake_ha):
    g = people[GUEST]
    assert client.post("/api/devices/light.kitchen_1/toggle", headers=g).json() == {"ok": True}
    assert client.post("/api/devices/light.kitchen_1/light", headers=g, json={"brightness_pct": 40}).status_code == 200
    assert client.post("/api/devices/light.kitchen_1/turn_off", headers=g).status_code == 200
    r = client.post("/api/devices/switch.fan/toggle", headers=g)
    assert r.status_code == 403 and r.json() == {"detail": "Guests can only switch the lights."}
    assert client.post("/api/devices/climate.lounge_valve/temperature", headers=g, json={"temperature": 20}).status_code == 403
    assert client.post("/api/bulk", headers=g, json={"action": "turn_off", "entity_ids": ["light.kitchen_1"]}).status_code == 403
    assert client.post("/api/mode", headers=g, json={"mode": "away"}).status_code == 403
    assert [p for p, _ in fake_ha.service_calls()] == ["/api/services/light/toggle", "/api/services/light/turn_on",
                                                       "/api/services/light/turn_off"]
    # What the plan needs still loads.
    for p in ("/api/devices", "/api/layout", "/api/mode", "/api/me", "/", "/app.js", "/users.js"):
        assert client.get(p, headers=g).status_code == 200, p
    assert client.get("/api/me", headers=g).json()["role"] == "guest"


def test_member_controls_but_no_settings(client, people, fake_ha):
    m = people[MEMBER]
    assert client.post("/api/devices/switch.fan/toggle", headers=m).status_code == 200
    assert client.post("/api/devices/climate.lounge_valve/temperature", headers=m, json={"temperature": 20}).status_code == 200
    assert client.post("/api/bulk", headers=m, json={"action": "turn_off", "entity_ids": ["switch.fan"]}).status_code == 200
    assert client.get("/api/schedules", headers=m).status_code == 200
    layout = client.get("/api/layout", headers=m).json()
    r = client.put("/api/layout", headers=m, json=layout)
    assert r.status_code == 403 and r.json() == {"detail": "Only an admin can change settings."}
    assert client.put("/api/alerts/settings", headers=m, json={"enabled": True}).status_code == 403
    assert client.put("/api/devices/switch.fan/meta", headers=m, json={"name": "x"}).status_code == 403
    assert client.get("/api/users", headers=m).status_code == 403
    assert client.post("/api/users", headers=m, json={"username": "eve", "password": "12345678", "role": "admin"}).status_code == 403
    assert client.get("/api/users").json()["users"][-1]["username"] != "eve"


def test_unknown_path_is_404_not_403(client, people):
    assert client.get("/nope.js", headers=people[GUEST]).status_code == 404
    assert client.get("/api/nope", headers=people[GUEST]).status_code == 404


def test_wrong_method_still_refused_for_non_admins(client, people):
    # A method the route doesn't serve falls to the static mount (405) for everyone; nothing leaks either way.
    assert client.delete("/api/layout", headers=people[GUEST]).status_code in (403, 405)
