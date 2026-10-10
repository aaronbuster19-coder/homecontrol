"""Scenes: user-defined one-tap presets ("Movie night", "Bedtime") for lights, plugs and TVs, optionally starting a disco.

A scene is a list of actions, one per device:
  light  {"entity_id", "on", "brightness_pct"?, one of "hs_color" / "color_temp_kelvin" / "rgb_color"?}
  plug   {"entity_id", "on"}
  media  {"entity_id", "on", "source"?}           (TVs and other media players)
plus an optional name, room (where the room view shows it) and disco ({"preset", "speed", "minutes"}: the scene's
colour lights that it switches on start a disco afterwards; only presets without the photosensitivity warning).

Build one by capturing the current state of chosen devices (POST /api/scenes/capture) or by hand. Running a scene is
one batched set of HA calls: one call per distinct payload (all lights going to 40 % warm white in one light.turn_on),
switch-ons first, then sources, then switch-offs. Safety:
- protected devices (fridge / freezer / home-server plugs and keep-on plugs) are never switched off: a scene can't be
  saved with one off, and one that became protected since is skipped at run time;
- devices that are unavailable or gone from HA are skipped, not retried; a failed call doesn't stop the others;
- a double tap doesn't send everything twice (RERUN_GAP); runs are serialised;
- guests may run (and see) only scenes made of lights alone, and start a disco only as the disco itself allows.
Scenes are stored in their own SQLite table (one JSON row, like the quick tiles); the layout is untouched.
"""
import asyncio
import json
import math
import secrets
import sqlite3
import time

from .activity import acting
from .appliances import protected_plugs
from .live import light_caps
from .media import decode, features, is_on
from .roles import LIGHTS, allowed

MAX_SCENES = 30
MAX_ACTIONS = 40
NAME_MAX = 40
SOURCE_MAX = 100
KINDS = ("light", "plug", "media")
DOMAIN = {"light": "light", "plug": "switch", "media": "media_player"}
COLOUR_KEYS = ("hs_color", "color_temp_kelvin", "rgb_color")
RERUN_GAP = 2.0  # seconds: the same scene again this soon (a double tap) sends nothing new
BAD = ("unavailable", "unknown")


class SceneError(ValueError):
    pass


class Forbidden(SceneError):
    pass


def protected(layout: dict) -> set[str]:
    """Plugs nothing here may switch off: keep-on plugs and those a fridge / freezer / home server is linked to."""
    return set((layout.get("settings") or {}).get("keep_on") or []) | protected_plugs(layout)


class SceneStore:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS scenes (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def all(self) -> list[dict]:
        with self._conn() as c:
            row = c.execute("SELECT data FROM scenes WHERE id = 1").fetchone()
        try:
            out = json.loads(row[0]).get("scenes") if row else []
        except (ValueError, AttributeError):
            return []
        return [s for s in out if isinstance(s, dict) and isinstance(s.get("id"), str)] if isinstance(out, list) else []

    def save(self, scenes: list[dict]) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO scenes (id, data) VALUES (1, ?)", (json.dumps({"scenes": scenes}),))

    def get(self, sid: str) -> dict | None:
        return next((s for s in self.all() if s["id"] == sid), None)


# ---------------- validation ----------------
def _num(v, lo, hi, what, integer=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise SceneError(f"{what} must be {lo}–{hi}")
    return int(round(v)) if integer else round(float(v), 1)


def validate_action(a, devs: dict, layout: dict, stored: dict[str, dict]) -> dict:
    """One action. devs: entity id -> Device. A device HA doesn't list right now may stay if the scene already had it."""
    if not isinstance(a, dict):
        raise SceneError("each action must be an object")
    eid = a.get("entity_id")
    if not isinstance(eid, str):
        raise SceneError("each action needs an entity_id")
    d = devs.get(eid)
    if d is None:
        if eid in stored:
            return stored[eid]
        raise SceneError(f"unknown device {eid!r}")
    if d.kind not in KINDS:
        raise SceneError(f"{d.name} can't be in a scene")
    allowed_keys = {"entity_id", "on"} | ({"brightness_pct", *COLOUR_KEYS} if d.kind == "light" else set()) \
        | ({"source"} if d.kind == "media" else set())
    extra = set(a) - allowed_keys
    if extra:
        raise SceneError(f"{d.name}: unexpected field {sorted(extra)[0]!r}")
    on = a.get("on")
    if not isinstance(on, bool):
        raise SceneError(f"{d.name}: on must be true or false")
    if not on and eid in protected(layout):
        raise SceneError(f"{d.name} is protected — scenes never switch it off")
    out = {"entity_id": eid, "on": on}
    if d.kind == "light" and on:
        if a.get("brightness_pct") is not None:
            out["brightness_pct"] = _num(a["brightness_pct"], 1, 100, f"{d.name}: brightness_pct", integer=True)
        given = [k for k in COLOUR_KEYS if a.get(k) is not None]
        if len(given) > 1:
            raise SceneError(f"{d.name}: give only one of hs_color, color_temp_kelvin, rgb_color")
        if a.get("hs_color") is not None:
            hs = a["hs_color"]
            if not isinstance(hs, list) or len(hs) != 2:
                raise SceneError(f"{d.name}: hs_color must be [hue, saturation]")
            out["hs_color"] = [_num(hs[0], 0, 360, f"{d.name}: hue"), _num(hs[1], 0, 100, f"{d.name}: saturation")]
        if a.get("color_temp_kelvin") is not None:
            out["color_temp_kelvin"] = _num(a["color_temp_kelvin"], 1000, 12000, f"{d.name}: color_temp_kelvin", integer=True)
        if a.get("rgb_color") is not None:
            rgb = a["rgb_color"]
            if not isinstance(rgb, list) or len(rgb) != 3:
                raise SceneError(f"{d.name}: rgb_color must be three values 0–255")
            out["rgb_color"] = [_num(v, 0, 255, f"{d.name}: rgb_color", integer=True) for v in rgb]
    if d.kind == "media" and on and a.get("source") is not None:
        src = a["source"]
        if not isinstance(src, str) or not src or len(src) > SOURCE_MAX:
            raise SceneError(f"{d.name}: source must be text")
        out["source"] = src
    return out


def validate_scene(body, devs: dict, layout: dict, old: dict | None = None) -> dict:
    from .disco import MAX_MINUTES, PRESETS, SPEEDS
    if not isinstance(body, dict):
        raise SceneError("send {name, actions, room?, disco?}")
    extra = set(body) - {"name", "actions", "room", "disco", "id"}
    if extra:
        raise SceneError(f"unexpected field {sorted(extra)[0]!r}")
    name = body.get("name")
    if not isinstance(name, str) or not " ".join(name.split()):
        raise SceneError("give the scene a name")
    name = " ".join(name.split())
    if len(name) > NAME_MAX:
        raise SceneError(f"name must be at most {NAME_MAX} characters")
    room = body.get("room")
    if room is not None and (not isinstance(room, str) or not any(r.get("id") == room for r in layout.get("rooms", []))):
        raise SceneError("unknown room")
    acts = body.get("actions", [])
    if not isinstance(acts, list):
        raise SceneError("actions must be a list")
    if len(acts) > MAX_ACTIONS:
        raise SceneError(f"at most {MAX_ACTIONS} devices in a scene")
    stored = {a["entity_id"]: a for a in (old or {}).get("actions", [])}
    actions, seen = [], set()
    for a in acts:
        v = validate_action(a, devs, layout, stored)
        if v["entity_id"] in seen:
            raise SceneError(f"{v['entity_id']} is in the scene twice")
        seen.add(v["entity_id"])
        actions.append(v)
    disco = body.get("disco")
    if disco is not None:
        if not isinstance(disco, dict) or set(disco) - {"preset", "speed", "minutes"}:
            raise SceneError("disco must be {preset, speed, minutes}")
        preset, speed, minutes = disco.get("preset", "rainbow"), disco.get("speed", "normal"), disco.get("minutes", 30)
        if preset not in PRESETS or PRESETS[preset]["warning"]:
            raise SceneError("a scene's disco must be one of " + ", ".join(k for k, p in PRESETS.items() if not p["warning"]))
        if speed not in SPEEDS:
            raise SceneError(f"disco speed must be one of {', '.join(SPEEDS)}")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= MAX_MINUTES:
            raise SceneError(f"disco minutes must be a whole number 1–{MAX_MINUTES}")
        if not any(a["on"] and a["entity_id"].startswith("light.") for a in actions):
            raise SceneError("a disco needs at least one light the scene switches on")
        disco = {"preset": preset, "speed": speed, "minutes": minutes}
    if not actions and disco is None:
        raise SceneError("add at least one device")
    return {"id": (old or {}).get("id") or "s" + secrets.token_hex(4), "name": name, "room": room,
            "actions": actions, "disco": disco}


# ---------------- capture ----------------
def capture(entity_ids, devs: dict, states: dict, layout: dict) -> tuple[list[dict], list[dict]]:
    """Actions for the devices' current state -> (actions, skipped [{entity_id, reason}])."""
    if not isinstance(entity_ids, list) or not all(isinstance(e, str) for e in entity_ids):
        raise SceneError("entity_ids must be a list of device ids")
    if len(entity_ids) > MAX_ACTIONS:
        raise SceneError(f"at most {MAX_ACTIONS} devices in a scene")
    keep = protected(layout)
    actions, skipped = [], []
    for eid in dict.fromkeys(entity_ids):
        d = devs.get(eid)
        if d is None or d.kind not in KINDS:
            raise SceneError(f"can't capture {eid!r}")
        s = states.get(eid) or {}
        state, attrs = s.get("state", "unavailable"), s.get("attributes") or {}
        if state in BAD:
            skipped.append({"entity_id": eid, "reason": state})
            continue
        if d.kind == "media":
            on = is_on(state)
            a = {"entity_id": eid, "on": on}
            src = attrs.get("source")
            if on and isinstance(src, str) and src and src in (attrs.get("source_list") or []):
                a["source"] = src[:SOURCE_MAX]
        else:
            on = state == "on"
            a = {"entity_id": eid, "on": on}
            if d.kind == "light" and on:
                caps = light_caps(attrs)
                b = attrs.get("brightness")
                if caps["supports_brightness"] and isinstance(b, (int, float)) and not isinstance(b, bool):
                    a["brightness_pct"] = max(1, min(100, round(b / 2.55)))
                mode = attrs.get("color_mode")
                if mode == "color_temp" and attrs.get("color_temp_kelvin"):
                    a["color_temp_kelvin"] = int(attrs["color_temp_kelvin"])
                elif caps["supports_color"] and isinstance(attrs.get("hs_color"), (list, tuple)) and len(attrs["hs_color"]) == 2:
                    a["hs_color"] = [round(float(v), 1) for v in attrs["hs_color"]]
                elif caps["supports_color"] and isinstance(attrs.get("rgb_color"), (list, tuple)) and len(attrs["rgb_color"]) == 3:
                    a["rgb_color"] = [int(v) for v in attrs["rgb_color"]]
        if not a["on"] and eid in keep:
            skipped.append({"entity_id": eid, "reason": "protected"})
            continue
        actions.append(a)
    return actions, skipped


# ---------------- running ----------------
def plan(scene: dict, devs: dict, states: dict, layout: dict) -> tuple[list[tuple[str, str, dict]], list[dict]]:
    """The batched HA calls a scene makes now -> ([(domain, service, data)], skipped [{entity_id, name, reason}])."""
    keep = protected(layout)
    ons: dict[tuple[str, str], list[str]] = {}
    sources: dict[str, list[str]] = {}
    offs: dict[str, list[str]] = {}
    payload: dict[tuple[str, str], dict] = {}
    skipped = []

    def skip(eid, reason):
        d = devs.get(eid)
        skipped.append({"entity_id": eid, "name": d.name if d else eid, "reason": reason})

    for a in scene.get("actions", []):
        eid = a["entity_id"]
        d = devs.get(eid)
        if d is None or d.kind not in KINDS:
            skip(eid, "not in Home Assistant")
            continue
        s = states.get(eid) or {}
        state, attrs = s.get("state", "unavailable"), s.get("attributes") or {}
        if state in BAD:
            skip(eid, state)
            continue
        dom = DOMAIN[d.kind]
        if not a["on"]:
            if eid in keep:
                skip(eid, "protected")
            elif d.kind == "media" and not decode(features(attrs))["turn_off"]:
                skip(eid, "can't be turned off")
            elif d.kind == "media" and not is_on(state):
                pass  # already off: nothing to send
            else:
                offs.setdefault(dom, []).append(eid)
            continue
        if d.kind == "media":
            sup = decode(features(attrs))
            if not is_on(state):
                if not sup["turn_on"]:
                    skip(eid, "can't be turned on")
                    continue
                ons.setdefault((dom, "{}"), []).append(eid)
                payload[(dom, "{}")] = {}
            src = a.get("source")
            if src and src != attrs.get("source"):
                lst = attrs.get("source_list") or []
                if not sup["select_source"] or (lst and src not in lst):
                    skip(eid, f"no source {src}")
                else:
                    sources.setdefault(src, []).append(eid)
            continue
        data = {k: a[k] for k in ("brightness_pct", *COLOUR_KEYS) if k in a} if d.kind == "light" else {}
        key = (dom, json.dumps(data, sort_keys=True))
        ons.setdefault(key, []).append(eid)
        payload[key] = data
    calls = [(dom, "turn_on", {"entity_id": ids, **payload[(dom, k)]}) for (dom, k), ids in ons.items()]
    calls += [("media_player", "select_source", {"entity_id": ids, "source": src}) for src, ids in sources.items()]
    calls += [(dom, "turn_off", {"entity_id": ids}) for dom, ids in offs.items()]
    return calls, skipped


def guest_ok(scene: dict, role: str | None) -> bool:
    return all(allowed(role, LIGHTS, {"entity_id": a["entity_id"]}) for a in scene.get("actions", []))


class Scenes:
    """call(domain, service, data): the logged HA call; devices(): async discovered devices; states(): live raw states;
    layout(): the plan; disco: the Disco (interrupted for the scene's lights, started for a scene with a disco)."""

    def __init__(self, store: SceneStore, call, devices, states, layout, disco=None, clock=time.time):
        self.store, self.call, self.devices, self.states, self.layout, self.disco = store, call, devices, states, layout, disco
        self.clock = clock
        self._lock = asyncio.Lock()
        self._last: dict[str, tuple[float, dict]] = {}  # scene id -> (when, result) of its last run

    def listing(self, role: str | None) -> dict:
        from .disco import PRESETS, SPEEDS
        scenes = [{**s, "guest_ok": guest_ok(s, "guest")} for s in self.store.all()]
        if role == "guest":
            scenes = [s for s in scenes if s["guest_ok"]]
        return {"scenes": scenes, "max": MAX_SCENES, "max_actions": MAX_ACTIONS,
                "disco_presets": [{"id": k, "name": p["name"]} for k, p in PRESETS.items() if not p["warning"]],
                "disco_speeds": list(SPEEDS)}

    async def create(self, body) -> dict:
        scenes = self.store.all()
        if len(scenes) >= MAX_SCENES:
            raise SceneError(f"at most {MAX_SCENES} scenes")
        s = validate_scene(body, await self.devices(), self.layout())
        self.store.save([*scenes, s])
        return s

    async def update(self, sid: str, body) -> dict:
        scenes = self.store.all()
        old = next((s for s in scenes if s["id"] == sid), None)
        if old is None:
            raise KeyError(sid)
        s = validate_scene(body, await self.devices(), self.layout(), old)
        self.store.save([s if x["id"] == sid else x for x in scenes])
        self._last.pop(sid, None)
        return s

    def delete(self, sid: str) -> bool:
        scenes = self.store.all()
        left = [s for s in scenes if s["id"] != sid]
        self.store.save(left)
        self._last.pop(sid, None)
        return len(left) != len(scenes)

    async def capture(self, body) -> dict:
        ids = body.get("entity_ids") if isinstance(body, dict) else None
        actions, skipped = capture(ids, await self.devices(), self.states(), self.layout())
        return {"actions": actions, "skipped": skipped}

    async def run(self, sid: str, role: str | None = "admin", user: str | None = None) -> dict:
        scene = self.store.get(sid)
        if scene is None:
            raise KeyError(sid)
        if not guest_ok(scene, role) and not allowed(role, "member", {}):
            raise Forbidden("Guests can only run scenes of lights.")
        async with self._lock:
            last = self._last.get(sid)
            if last and 0 <= self.clock() - last[0] < RERUN_GAP:
                return {**last[1], "repeat": True}
            devs, layout = await self.devices(), self.layout()
            calls, skipped = plan(scene, devs, self.states(), layout)
            lights = [a["entity_id"] for a in scene["actions"] if a["entity_id"].startswith("light.")]
            if self.disco is not None and lights:
                await self.disco.interrupt("manual", lights)  # the scene's lights take the scene's state
            failed, sent = [], 0
            with acting(f"Scene “{scene['name']}”"):
                for domain, service, data in calls:
                    try:
                        await self.call(domain, service, data)
                        sent += 1
                    except Exception as e:  # one failed group doesn't keep the rest of the scene from happening
                        failed += [{"entity_id": e2, "name": devs[e2].name if e2 in devs else e2, "reason": str(e)[:120]}
                                   for e2 in data["entity_id"]]
            disco = None
            if scene.get("disco") and self.disco is not None:
                on = {e for _, svc, data in calls if svc == "turn_on" for e in data["entity_id"]}
                on -= {f["entity_id"] for f in failed}
                states = self.states()
                ids = [a["entity_id"] for a in scene["actions"] if a["on"] and a["entity_id"] in devs
                       and devs[a["entity_id"]].kind == "light"
                       and (a["entity_id"] in on or (states.get(a["entity_id"]) or {}).get("state") == "on")
                       and light_caps((states.get(a["entity_id"]) or {}).get("attributes") or {})["supports_color"]]
                if ids:
                    from .disco import DiscoError
                    try:
                        await self.disco.start({**scene["disco"], "entity_ids": ids}, role, user)
                        disco = {"started": True, "n": len(ids)}
                    except DiscoError as e:
                        disco = {"started": False, "reason": str(e)}
                    except Exception as e:  # HA failed on the disco's first step: the scene itself still happened
                        disco = {"started": False, "reason": f"Home Assistant: {str(e)[:120]}"}
                else:
                    disco = {"started": False, "reason": "no colour lights on"}
            result = {"ok": not failed, "id": sid, "name": scene["name"], "calls": sent, "skipped": skipped,
                      "failed": failed, "disco": disco}
            self._last[sid] = (self.clock(), result)
            return result


def add_routes(app, scenes: Scenes, ensure_states, json_body) -> None:
    """GET/POST /api/scenes, PUT/DELETE /api/scenes/{sid}, POST /api/scenes/capture, POST /api/scenes/{sid}/run."""
    from fastapi import HTTPException, Request

    @app.get("/api/scenes")
    async def list_scenes(request: Request):
        return scenes.listing(request.state.role)

    @app.post("/api/scenes")
    async def create_scene(request: Request):
        body = await json_body(request)
        try:
            return await scenes.create(body)
        except SceneError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/scenes/capture")
    async def capture_scene(request: Request):
        body = await json_body(request)
        await ensure_states()
        try:
            return await scenes.capture(body)
        except SceneError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/scenes/{sid}")
    async def update_scene(sid: str, request: Request):
        body = await json_body(request)
        try:
            return await scenes.update(sid, body)
        except KeyError:
            raise HTTPException(404, "unknown scene")
        except SceneError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/scenes/{sid}")
    async def delete_scene(sid: str):
        if not scenes.delete(sid):
            raise HTTPException(404, "unknown scene")
        return {"ok": True}

    @app.post("/api/scenes/{sid}/run")
    async def run_scene(sid: str, request: Request):
        await ensure_states()
        try:
            return await scenes.run(sid, request.state.role, request.state.user)
        except KeyError:
            raise HTTPException(404, "unknown scene")
        except Forbidden as e:
            raise HTTPException(403, str(e))
