"""Live Home Assistant state: websocket subscription (with polling fallback) and SSE fan-out to browsers."""
import asyncio
import json
import logging
import time

from .discovery import Device, number
from .ha import HAClient

log = logging.getLogger("homecontrol.live")
POLL_INTERVAL = 10
FRESH_FOR = 15  # a poll this recent counts as fresh while the websocket is down
MAX_BACKOFF = 60
PING_EVERY = 20
QUEUE_SIZE = 100


def ws_url(ha_url: str) -> str:
    base = ha_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[8:]
    elif base.startswith("http://"):
        base = "ws://" + base[7:]
    return base + "/api/websocket"


def build_device(d: Device, states: dict[str, dict]) -> dict:
    s = states.get(d.entity_id, {})
    attrs = s.get("attributes", {})
    item = d.to_dict()
    item["state"] = s.get("state", "unavailable")
    if d.kind == "valve":
        for k in ("current_temperature", "temperature", "min_temp", "max_temp", "target_temp_step"):
            item[k] = attrs.get(k)
    if d.kind == "light":
        item.update(light_caps(attrs))
    for role in ("power", "energy_today"):
        v = number(states.get(d.related.get(role, "")))
        if v is not None:
            item[role] = round(v, 3)
    v = number(states.get(d.related.get("battery", "")))
    if v is not None:
        item["battery"] = int(round(v))
    low = states.get(d.related.get("battery_low", ""), {}).get("state")
    if low in ("on", "off"):
        item["battery_low"] = low == "on"
    return item


COLOR_MODES = {"hs", "rgb", "rgbw", "rgbww", "xy"}
DIM_MODES = COLOR_MODES | {"color_temp", "brightness", "white"}


def light_caps(attrs: dict) -> dict:
    """What the light sheet needs: current brightness/colour and what the bulb supports."""
    modes = [m for m in attrs.get("supported_color_modes") or [] if isinstance(m, str)]
    out = {k: attrs.get(k) for k in ("brightness", "color_mode", "hs_color", "rgb_color", "color_temp_kelvin")}
    out["supported_color_modes"] = modes
    out["supports_brightness"] = bool(DIM_MODES.intersection(modes))
    out["supports_color"] = bool(COLOR_MODES.intersection(modes))
    out["supports_color_temp"] = "color_temp" in modes
    if out["supports_color_temp"]:
        out["min_color_temp_kelvin"] = attrs.get("min_color_temp_kelvin") or 2500
        out["max_color_temp_kelvin"] = attrs.get("max_color_temp_kelvin") or 6500
    return out


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


class Client:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)


class Live:
    def __init__(self, ha: HAClient, url: str, token: str, use_ws: bool = True):
        self.ha, self.url, self.token, self.use_ws = ha, url, token, use_ws
        self.states: dict[str, dict] = {}
        self.devices: dict[str, Device] = {}
        self.index: dict[str, set[str]] = {}  # entity id -> primary ids it feeds
        self.clients: set[Client] = set()
        self.ws_up = False
        self.observers: list = []  # fn(device_item, raw_primary_state) on every published device change
        self.polled_at = 0.0
        self._task: asyncio.Task | None = None

    # ---- state ----
    def fresh(self) -> bool:
        return self.ws_up or time.monotonic() - self.polled_at < FRESH_FOR

    def set_devices(self, devices: dict[str, Device]) -> None:
        self.devices = devices
        self.index = {}
        for d in devices.values():
            for eid in (d.entity_id, *d.related.values()):
                self.index.setdefault(eid, set()).add(d.entity_id)

    def device_list(self) -> list[dict]:
        out = [build_device(d, self.states) for d in self.devices.values()]
        out.sort(key=lambda x: (x["kind"], x["name"].lower()))
        return out

    def load_states(self, states: list[dict]) -> None:
        new = {s["entity_id"]: s for s in states if "entity_id" in s}
        changed = {e for e in self.index if new.get(e) != self.states.get(e)}
        self.states, self.polled_at = new, time.monotonic()
        self._publish(changed)

    def handle(self, msg) -> None:
        for m in msg if isinstance(msg, list) else [msg]:
            ev = m.get("event") if m.get("type") == "event" else None
            if not ev or ev.get("event_type") != "state_changed":
                continue
            data = ev.get("data", {})
            eid, new = data.get("entity_id"), data.get("new_state")
            if not eid:
                continue
            if new is None:
                self.states.pop(eid, None)
            else:
                self.states[eid] = new
            self._publish({eid})

    # ---- fan-out ----
    def subscribe(self) -> Client:
        c = Client()
        self.clients.add(c)
        return c

    def unsubscribe(self, c: Client) -> None:
        self.clients.discard(c)

    def add_observer(self, fn) -> None:
        self.observers.append(fn)

    def republish(self, primary_ids) -> None:
        """Push fresh device items (e.g. after a rename) to observers and browsers."""
        self._publish({e for e in primary_ids if e in self.devices})

    def set_ws(self, up: bool) -> None:
        if up != self.ws_up:
            self.ws_up = up
            self._broadcast(sse("status", {"ws": up}))

    def _publish(self, entity_ids) -> None:
        primaries = set().union(*(self.index.get(e, ()) for e in entity_ids)) if entity_ids else set()
        for pid in sorted(primaries):
            item = build_device(self.devices[pid], self.states)
            for fn in self.observers:
                try:
                    fn(item, self.states.get(pid))
                except Exception as e:
                    log.warning("observer failed: %s", e)
            self._broadcast(sse("device", item))

    def _broadcast(self, msg: str) -> None:
        for c in list(self.clients):
            try:
                c.queue.put_nowait(msg)
            except asyncio.QueueFull:  # slow client: drop it, it will reconnect and get a fresh snapshot
                self.clients.discard(c)
                while not c.queue.empty():
                    c.queue.get_nowait()
                c.queue.put_nowait(None)

    async def stream(self, c: Client, snapshot: list[dict], ping_every: float = PING_EVERY):
        try:
            yield sse("snapshot", snapshot)
            yield sse("status", {"ws": self.ws_up})
            while True:
                try:
                    item = await asyncio.wait_for(c.queue.get(), ping_every)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    return
                yield item
        finally:
            self.unsubscribe(c)

    # ---- background connection ----
    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _poll(self) -> None:
        try:
            self.load_states(await self.ha.states())
        except Exception as e:  # keep the last known states
            log.warning("polling Home Assistant failed: %s", e)

    async def _poll_for(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            if time.monotonic() - self.polled_at >= POLL_INTERVAL:
                await self._poll()
            left = end - time.monotonic()
            if left <= 0:
                return
            await asyncio.sleep(min(left, POLL_INTERVAL))

    async def _run(self) -> None:
        delay = 1
        while True:
            if self.use_ws:
                try:
                    await self._session()
                    delay = 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("HA websocket down (%s: %s), retry in %ss", type(e).__name__, e, delay)
                finally:
                    self.set_ws(False)
            await self._poll_for(delay if self.use_ws else POLL_INTERVAL)
            delay = min(delay * 2, MAX_BACKOFF)

    async def _session(self) -> None:
        import websockets
        async with websockets.connect(self.url, max_size=None, open_timeout=10, ping_interval=30) as ws:
            if json.loads(await ws.recv()).get("type") == "auth_required":
                await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                reply = json.loads(await ws.recv())
                if reply.get("type") != "auth_ok":
                    raise RuntimeError(f"auth failed: {reply.get('message') or reply.get('type')}")
            await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
            self.load_states(await self.ha.states())
            self.set_ws(True)
            log.info("HA websocket connected")
            async for raw in ws:
                self.handle(json.loads(raw))
