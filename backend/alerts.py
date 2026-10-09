"""Door-open alerts: VAPID keys, push subscriptions + settings in SQLite, open-door watcher, Web Push sending."""
import asyncio
import base64
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("homecontrol.alerts")
CHECK_EVERY = 15
DEFAULT_SUBJECT = "mailto:admin@localhost"
DEFAULT_SETTINGS = {"enabled": True, "door_open_minutes": 5, "notify_on_close": False,
                    # automations (backend/automations.py)
                    "window_heating_enabled": True, "window_open_minutes": 2, "window_off_temp": 7.0, "window_notify": True,
                    "health_battery": True, "health_unavailable": True, "health_unavailable_minutes": 30,
                    "weekly_summary": True, "dehumidifier_tank": True, "appliance_done": True,
                    # quiet hours for automation pushes (backend/quiet.py); door alerts and tests always go through
                    "quiet_hours": True, "quiet_from": "23:00", "quiet_to": "07:00", "mute_until": None}
BOOL_SETTINGS = ("enabled", "notify_on_close", "window_heating_enabled", "window_notify", "health_battery",
                 "health_unavailable", "weekly_summary", "quiet_hours", "dehumidifier_tank", "appliance_done")
TIME_SETTINGS = ("quiet_from", "quiet_to")
HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_MUTE = 48 * 3600
INT_SETTINGS = {"door_open_minutes": (1, 120), "window_open_minutes": (1, 30), "health_unavailable_minutes": (10, 240)}
WINDOW_OFF_TEMP = (5, 15)
GONE = (404, 410)
NOT_OPEN = ("unavailable", "unknown")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# ---- VAPID keys ----
class Vapid:
    def __init__(self, private_pem: bytes, public_key: str, subject: str):
        self.private_pem, self.public_key, self.subject = private_pem, public_key, subject

    def signer(self):
        from py_vapid import Vapid01
        return Vapid01.from_pem(self.private_pem)


def _public_b64(key) -> str:
    from cryptography.hazmat.primitives import serialization
    return _b64(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))


def _pem(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def _key_from_env(value: str):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    value = value.strip()
    if value.startswith("-----"):
        return serialization.load_pem_private_key(value.replace("\\n", "\n").encode(), None)
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(raw) == 32:  # raw private scalar, as printed by most VAPID generators
        return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    return serialization.load_der_private_key(raw, None)


def load_vapid(db_path: str, env=os.environ) -> Vapid:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    subject = env.get("VAPID_SUBJECT") or DEFAULT_SUBJECT
    if env.get("VAPID_PRIVATE_KEY"):
        key = _key_from_env(env["VAPID_PRIVATE_KEY"])
    else:
        path = Path(db_path).resolve().parent / "vapid_private.pem"
        try:
            key = serialization.load_pem_private_key(path.read_bytes(), None)
        except FileNotFoundError:
            key = ec.generate_private_key(ec.SECP256R1())
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(_pem(key))
            log.info("generated VAPID key %s", path)
    return Vapid(_pem(key), env.get("VAPID_PUBLIC_KEY") or _public_b64(key), subject)


# ---- storage ----
class SettingsError(ValueError):
    pass


def validate_settings(data, current: dict) -> dict:
    if not isinstance(data, dict):
        raise SettingsError("settings must be an object")
    out = dict(current)
    for k in BOOL_SETTINGS:
        if k in data:
            if not isinstance(data[k], bool):
                raise SettingsError(f"{k} must be true or false")
            out[k] = data[k]
    for k, (lo, hi) in INT_SETTINGS.items():
        if k in data:
            m = data[k]
            if isinstance(m, bool) or not isinstance(m, int) or not lo <= m <= hi:
                raise SettingsError(f"{k} must be a whole number from {lo} to {hi}")
            out[k] = m
    if "window_off_temp" in data:
        t, (lo, hi) = data["window_off_temp"], WINDOW_OFF_TEMP
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not lo <= t <= hi:
            raise SettingsError(f"window_off_temp must be {lo}–{hi}")
        out["window_off_temp"] = round(float(t) * 2) / 2
    for k in TIME_SETTINGS:
        if k in data:
            if not isinstance(data[k], str) or not HHMM.match(data[k]):
                raise SettingsError(f"{k} must be HH:MM")
            out[k] = data[k]
    if "mute_until" in data:  # epoch seconds, or null to cancel
        m = data["mute_until"]
        if m is not None and (isinstance(m, bool) or not isinstance(m, (int, float)) or not math.isfinite(m)
                              or not 0 < m - time.time() <= MAX_MUTE):
            raise SettingsError("mute_until must be null or a time within the next 48 h")
        out["mute_until"] = m
    return out


def validate_subscription(sub) -> dict:
    if not isinstance(sub, dict):
        raise SettingsError("subscription must be an object")
    ep, keys = sub.get("endpoint"), sub.get("keys")
    if not isinstance(ep, str) or not ep.startswith("https://") and not ep.startswith("http://") or len(ep) > 2000:
        raise SettingsError("subscription needs an endpoint URL")
    if not isinstance(keys, dict) or not all(isinstance(keys.get(k), str) and keys[k] for k in ("p256dh", "auth")):
        raise SettingsError("subscription needs keys.p256dh and keys.auth")
    return {"endpoint": ep, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}


class AlertStore:
    def __init__(self, path: str):
        self.path, self._lock = path, threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS push_subs (endpoint TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL)")
            c.execute("CREATE TABLE IF NOT EXISTS alert_settings (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def subs(self) -> list[dict]:
        with self._lock, self._conn() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT data FROM push_subs ORDER BY created")]

    def add(self, sub: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO push_subs (endpoint, data, created) VALUES (?, ?, ?) "
                      "ON CONFLICT(endpoint) DO UPDATE SET data = excluded.data", (sub["endpoint"], json.dumps(sub), time.time()))

    def remove(self, endpoint: str) -> bool:
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,)).rowcount > 0

    def settings(self) -> dict:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT data FROM alert_settings WHERE id = 1").fetchone()
        return {**DEFAULT_SETTINGS, **(json.loads(row[0]) if row else {})}

    def put_settings(self, s: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO alert_settings (id, data) VALUES (1, ?) "
                      "ON CONFLICT(id) DO UPDATE SET data = excluded.data", (json.dumps(s),))


# ---- sending ----
def webpush_sender(vapid: Vapid):
    """Returns send(sub, payload) -> HTTP status; blocking, so call it in a thread."""
    signer = vapid.signer()

    def send(sub: dict, payload: dict) -> int:
        from pywebpush import WebPushException, webpush
        try:
            r = webpush(sub, json.dumps(payload), vapid_private_key=signer,
                        vapid_claims={"sub": vapid.subject}, ttl=3600, timeout=10)
            return r.status_code
        except WebPushException as e:
            return e.response.status_code if e.response is not None else 0
    return send


class Pusher:
    def __init__(self, store: AlertStore, send):
        self.store, self.send = store, send

    def send_all(self, payload: dict) -> dict:
        sent = pruned = failed = 0
        for sub in self.store.subs():
            try:
                code = self.send(sub, payload)
            except Exception as e:  # network errors etc.: keep the subscription, try next time
                log.warning("push failed: %s", e)
                code = 0
            if 200 <= code < 300:
                sent += 1
            elif code in GONE:
                self.store.remove(sub["endpoint"])
                pruned += 1
            else:
                failed += 1
        log.info("push %r: sent %d, pruned %d, failed %d", payload.get("title"), sent, pruned, failed)
        return {"sent": sent, "pruned": pruned, "failed": failed}

    async def notify(self, payload: dict) -> dict:
        return await asyncio.to_thread(self.send_all, payload)


# ---- watcher ----
def parse_time(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Watcher:
    """Tracks how long each door/window sensor has been open; check() returns notifications to send."""

    def __init__(self, settings, clock=time.time):
        self.settings, self.clock = settings, clock  # settings: callable returning the settings dict
        self.open: dict[str, dict] = {}
        self.pending: list[dict] = []

    def observe(self, eid: str, name: str, state: str | None, last_changed=None) -> None:
        now = self.clock()
        entry = self.open.get(eid)
        if state == "on":
            if entry:
                entry["avail"], entry["name"] = True, name  # name: follows a rename while open
            else:
                since = parse_time(last_changed)
                self.open[eid] = {"name": name, "since": min(since, now) if since else now, "alerted": False, "avail": True}
        elif state in NOT_OPEN or state is None:
            if entry:
                entry["avail"] = False  # pause; don't reset or alert again on a brief dropout
        elif entry:
            del self.open[eid]
            s = self.settings()
            if entry["alerted"] and s["enabled"] and s["notify_on_close"]:
                self.pending.append(payload(eid, name, f"{name} closed", "Closed again."))

    def check(self) -> list[dict]:
        out, self.pending = self.pending, []
        s = self.settings()
        if not s["enabled"]:
            return out
        limit, now = s["door_open_minutes"] * 60, self.clock()
        for eid, e in self.open.items():
            if e["avail"] and not e["alerted"] and now - e["since"] >= limit:
                e["alerted"] = True
                mins = int((now - e["since"]) // 60)
                out.append(payload(eid, e["name"], f"{e['name']} open", f"{e['name']} has been open for {mins} min"))
        return out


def payload(eid: str, name: str, title: str, body: str) -> dict:
    return {"title": title, "body": body, "tag": f"door-{eid}", "url": "/"}


class Alerts:
    """Glue: hooks the watcher into Live and runs the periodic check."""

    def __init__(self, store: AlertStore, pusher: Pusher, live, ensure_devices=None, clock=time.time):
        self.store, self.pusher, self.live, self.ensure_devices = store, pusher, live, ensure_devices
        self.watcher = Watcher(store.settings, clock)
        self.wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        live.add_observer(self.on_device)

    def on_device(self, item: dict, raw: dict | None) -> None:
        if item.get("kind") != "sensor":
            return
        n = len(self.watcher.pending)
        self.watcher.observe(item["entity_id"], item["name"], item.get("state"), (raw or {}).get("last_changed"))
        if len(self.watcher.pending) != n:
            self.wake.set()

    def sync(self) -> None:
        for d in list(self.live.devices.values()):
            if d.kind == "sensor":
                raw = self.live.states.get(d.entity_id)
                self.watcher.observe(d.entity_id, d.name, (raw or {}).get("state"), (raw or {}).get("last_changed"))

    async def tick(self) -> None:
        if not self.live.devices and self.ensure_devices:
            try:
                await self.ensure_devices()
            except Exception as e:
                log.warning("alerts: device discovery failed: %s", e)
        self.sync()
        for p in self.watcher.check():
            await self.pusher.notify(p)

    async def _run(self, every: float) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("alerts check failed: %s", e)
            try:
                await asyncio.wait_for(self.wake.wait(), every)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    def start(self, every: float = CHECK_EVERY) -> None:
        self._task = asyncio.create_task(self._run(every))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
