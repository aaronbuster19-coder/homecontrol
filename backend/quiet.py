"""Quiet hours and mute for automation pushes.

Every push has a category. Door alerts ("door") and the explicit test ("test") always go straight through; the
automations' pushes ("health", "summary", "window") are held during quiet hours (default 23:00–07:00) or while
muted, kept in SQLite (de-duplicated), and delivered as ONE digest push once the quiet time is over.
"""
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, time as dtime, timedelta

log = logging.getLogger("homecontrol.quiet")
BYPASS = ("door", "test")
HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_MUTE = 48 * 3600
MAX_LINES = 8


def _hm(s: str) -> dtime:
    h, m = map(int, s.split(":"))
    return dtime(h, m)


def in_window(now: float, tz, start: str, end: str) -> bool:
    """Local wall-clock time within [start, end); across midnight when start > end; start == end means never."""
    t = datetime.fromtimestamp(now, tz).time().replace(second=0, microsecond=0)
    a, b = _hm(start), _hm(end)
    if a == b:
        return False
    return a <= t < b if a < b else (t >= a or t < b)


def next_local(now: float, tz, hhmm: str) -> float:
    """The next time the wall clock in tz shows hhmm, strictly after now (DST-safe)."""
    d = datetime.fromtimestamp(now, tz).date()
    for i in range(3):
        ts = datetime.combine(d + timedelta(days=i), _hm(hhmm), tzinfo=tz).timestamp()
        if ts > now:
            return ts
    raise AssertionError("unreachable")


class HeldStore:
    def __init__(self, path: str):
        self.path, self._lock = path, threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS held_pushes (key TEXT PRIMARY KEY, category TEXT NOT NULL, "
                      "data TEXT NOT NULL, created REAL NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def add(self, key: str, category: str, payload: dict, at: float) -> None:
        """Same key again (same tag and title): keep one, the newest."""
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO held_pushes (key, category, data, created) VALUES (?, ?, ?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET data = excluded.data, created = excluded.created",
                      (key, category, json.dumps(payload), at))

    def count(self) -> int:
        with self._lock, self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM held_pushes").fetchone()[0]

    def take(self) -> list[dict]:
        """Read and delete in one transaction: whatever happens to the digest, nothing is delivered twice."""
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT category, data FROM held_pushes ORDER BY created").fetchall()
            c.execute("DELETE FROM held_pushes")
        return [{"category": cat, **json.loads(d)} for cat, d in rows]


def digest(items: list[dict]) -> dict:
    lines = [f"{i['title']}" + (f" — {i['body']}" if i.get("body") and i["category"] != "summary" else "") for i in items]
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES - 1] + [f"and {len(lines) - MAX_LINES + 1} more"]
    only_summary = all(i["category"] == "summary" for i in items)
    return {"title": "While you were asleep", "body": "\n".join(lines), "tag": "digest",
            "url": "/?summary" if only_summary else "/"}


class Quiet:
    """Wraps the pusher. settings: callable returning the alert settings (quiet_hours, quiet_from, quiet_to, mute_until)."""

    def __init__(self, store: HeldStore, settings, pusher, clock=time.time, tz=None):
        self.store, self.settings, self.pusher, self.clock, self.tz = store, settings, pusher, clock, tz

    def state(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        s = self.settings()
        mute = s.get("mute_until")
        muted = isinstance(mute, (int, float)) and mute > now
        quiet = bool(s.get("quiet_hours")) and in_window(now, self.tz, s["quiet_from"], s["quiet_to"])
        until = None
        if quiet:
            until = next_local(now, self.tz, s["quiet_to"])
        if muted:
            until = max(until or 0, mute)
        return {"active": quiet or muted, "quiet": quiet, "muted": muted, "mute_until": int(mute * 1000) if muted else None,
                "until": int(until * 1000) if until else None, "held": self.store.count()}

    def mute_until(self, kind: str) -> float | None:
        """'1h' -> an hour from now; 'morning' -> the next quiet-hours end time (07:00 by default); 'off' -> None."""
        now = self.clock()
        if kind == "1h":
            return now + 3600
        if kind == "morning":
            return next_local(now, self.tz, self.settings()["quiet_to"])
        return None

    async def notify(self, payload: dict, category: str) -> dict | None:
        if category in BYPASS or not self.state()["active"]:
            return await self.pusher.notify(payload)
        key = f"{payload.get('tag', '')}|{payload.get('title', '')}"
        self.store.add(key, category, payload, self.clock())
        log.info("quiet: held %s push %r", category, payload.get("title"))
        return None

    async def tick(self) -> dict | None:
        """Quiet time over and something was held: send the digest (once)."""
        if self.state()["active"] or not self.store.count():
            return None
        items = self.store.take()
        if not items:
            return None
        return await self.pusher.notify(digest(items))
