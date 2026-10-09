"""Device history from HA's recorder (/api/history/period): downsampled series, on/off timelines, door log."""
import math
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .discovery import Device

RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
DOOR_RANGES = ("24h", "7d")
CACHE_TTL = {"24h": 60, "7d": 600, "30d": 600}
MAX_POINTS = 300
BAD = ("unavailable", "unknown", "", None)


class RangeError(ValueError):
    pass


def check_range(r: str, allowed=tuple(RANGES)) -> str:
    if r not in allowed:
        raise RangeError(f"range must be one of {', '.join(allowed)}")
    return r


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def parse_ts(s) -> int | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ms(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def to_float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---- raw HA rows -> segments ----
def samples(rows: list[dict], value, attrs: bool = False) -> list[tuple[int, object]]:
    """[(t_ms, value-or-None)] in time order. value(row) -> parsed value or None (gap)."""
    out = []
    for r in rows:
        t = parse_ts((r.get("last_updated") if attrs else None) or r.get("last_changed") or r.get("last_updated"))
        if t is not None:
            out.append((t, None if r.get("state") in BAD else value(r)))
    out.sort(key=lambda x: x[0])
    return out


def segments(pts: list[tuple[int, object]], start: int, end: int) -> list[tuple[int, int, object]]:
    """Step function: each value holds until the next sample; clipped to [start, end]."""
    out = []
    for i, (t, v) in enumerate(pts):
        t1 = pts[i + 1][0] if i + 1 < len(pts) else end
        a, b = max(t, start), min(t1, end)
        if b > a:
            out.append((a, b, v))
    return out


# ---- downsampling ----
def downsample(segs: list[tuple[int, int, object]], start: int, end: int, n: int = MAX_POINTS, steps: bool = True) -> list[list]:
    """Numeric segments -> [[t, v|None]] (None breaks the line). Few changes: exact steps (or the samples themselves for
    a smooth line when steps=False); else time-weighted bucket means."""
    if not segs:
        return []
    if not steps and len(segs) < n:
        out = []
        for i, (a, b, v) in enumerate(segs):
            if v is None:
                if out and out[-1][1] is not None:
                    out.append([a, None])
                continue
            out.append([a, v])
            if i + 1 == len(segs) or segs[i + 1][2] is None:
                out.append([b, v])
        return out
    if 2 * len(segs) <= n:
        out: list[list] = []
        for a, b, v in segs:
            if v is None:
                if out and out[-1][1] is not None:
                    out.append([a, None])
                continue
            if out and out[-1][1] == v and out[-1][0] == a:
                out[-1][0] = b  # extend a flat run
                continue
            out += [[a, v], [b, v]]
        return out
    width = (end - start) / n
    sums, cover = [0.0] * n, [0.0] * n
    for a, b, v in segs:
        if v is None:
            continue
        i = max(0, min(n - 1, int((a - start) / width)))
        while i < n:
            b0, b1 = start + i * width, start + (i + 1) * width
            o = min(b, b1) - max(a, b0)
            if o > 0:
                sums[i] += v * o
                cover[i] += o
            if b <= b1:
                break
            i += 1
    out = []
    for i in range(n):
        t = int(start + (i + 0.5) * width)
        if cover[i] > 0:
            out.append([t, round(sums[i] / cover[i], 2)])
        elif out and out[-1][1] is not None:
            out.append([t, None])
    return out


def runs(segs: list[tuple[int, int, object]], n: int = MAX_POINTS) -> list[dict]:
    """On/off segments -> merged runs; gaps (None) are dropped. Short runs are folded away until <= n remain."""
    def merge(items):
        out = []
        for a, b, s in items:
            if out and out[-1]["state"] == s and out[-1]["end"] >= a:
                out[-1]["end"] = max(out[-1]["end"], b)
            else:
                out.append({"state": s, "start": a, "end": b})
        return out
    out = merge([s for s in segs if s[2] is not None])
    if len(out) <= n or not out:
        return out
    span = out[-1]["end"] - out[0]["start"]
    min_len = span / (n * 4)
    while len(out) > n:
        kept = []
        for r in out:
            if r["end"] - r["start"] < min_len and kept and kept[-1]["end"] == r["start"]:
                kept[-1]["end"] = r["end"]  # previous state covers the blip
            else:
                kept.append(r)
        out = merge([(r["start"], r["end"], r["state"]) for r in kept])
        min_len *= 2
    return out


def energy_kwh(segs: list[tuple[int, int, object]]) -> float:
    return sum(v * (b - a) for a, b, v in segs if v is not None) / 3_600_000 / 1000


# ---- per-kind assembly ----
def state_of(on: str, off: str):
    return lambda r: on if r.get("state") == "on" else off if r.get("state") == "off" else None


def attr(name: str):
    return lambda r: to_float((r.get("attributes") or {}).get(name))


def numeric_state(r):
    return to_float(r.get("state"))


def build(dev: Device, rng: str, start: int, end: int, rows_by_id: dict[str, list[dict]], item: dict | None = None) -> dict:
    out = {"entity_id": dev.entity_id, "kind": dev.kind, "range": rng, "start": start, "end": end, "series": [], "timeline": []}
    if dev.kind == "plug":
        pid = dev.related.get("power")
        if pid:
            segs = segments(samples(rows_by_id.get(pid, []), numeric_state), start, end)
            out["series"].append({"name": "Power", "unit": "W", "points": downsample(segs, start, end)})
            out["energy_kwh"] = round(energy_kwh(segs), 3)
        out["timeline"] = runs(segments(samples(rows_by_id.get(dev.entity_id, []), state_of("on", "off")), start, end))
        if rng == "24h" and item and item.get("energy_today") is not None:
            out["energy_today_kwh"] = item["energy_today"]
    elif dev.kind == "valve":
        rows = rows_by_id.get(dev.entity_id, [])
        cur = segments(samples(rows, attr("current_temperature"), True), start, end)
        tgt = segments(samples(rows, attr("temperature"), True), start, end)
        out["series"] = [{"name": "Current", "unit": "°C", "points": downsample(cur, start, end, steps=False)},
                         {"name": "Target", "unit": "°C", "points": downsample(tgt, start, end), "step": True}]
    elif dev.kind == "dehumidifier":
        rows = rows_by_id.get(dev.entity_id, [])
        cur = segments(samples(rows, attr("current_humidity"), True), start, end)
        if not any(v is not None for _, _, v in cur):  # older HA / switch-only: the humidity sensor instead
            cur = segments(samples(rows_by_id.get(dev.related.get("humidity", ""), []), numeric_state), start, end)
        out["series"] = [{"name": "Current", "unit": "%", "points": downsample(cur, start, end, steps=False)}]
        tgt = segments(samples(rows, attr("humidity"), True), start, end)
        if any(v is not None for _, _, v in tgt):
            out["series"].append({"name": "Target", "unit": "%", "points": downsample(tgt, start, end), "step": True})
        out["timeline"] = runs(segments(samples(rows, state_of("on", "off"), True), start, end))
    elif dev.kind == "light":
        out["timeline"] = runs(segments(samples(rows_by_id.get(dev.entity_id, []), state_of("on", "off")), start, end))
    elif dev.kind == "sensor":
        out["timeline"] = runs(segments(samples(rows_by_id.get(dev.entity_id, []), state_of("open", "closed")), start, end))
    return out


def by_entity(data) -> dict[str, list[dict]]:
    """HA returns one list per entity; only the first item is guaranteed to carry entity_id."""
    out = {}
    for lst in data if isinstance(data, list) else []:
        if isinstance(lst, list) and lst and isinstance(lst[0], dict) and lst[0].get("entity_id"):
            out[lst[0]["entity_id"]] = [r for r in lst if isinstance(r, dict)]
    return out


# ---- door log ----
def door_log(rows: list[dict], start: int, now: int, today_start: int) -> dict:
    """Open/close events newest first with durations, plus a summary. The first row is the state at `start`."""
    pts = [(t, v) for t, v in samples(rows, state_of("open", "closed")) if v is not None]
    events, state, since = [], None, None
    for i, (t, v) in enumerate(pts):
        if v == state:
            continue
        if i == 0 and t <= start + 1000:  # state carried in from before the range, not a real change
            state, since = v, (t if v == "open" else None)
            continue
        events.append({"t": t, "state": v})
        state, since = v, (t if v == "open" else None)
    longest = 0
    open_at = pts[0][0] if pts and pts[0][1] == "open" and pts[0][0] <= start + 1000 else None
    for e in events:  # pair opens with the following close
        if e["state"] == "open":
            open_at = e["t"]
        elif open_at is not None:
            e["open_ms"] = e["t"] - open_at
            longest = max(longest, e["open_ms"])
            open_at = None
    if state == "open" and since is not None:
        longest = max(longest, now - since)
    events.reverse()
    return {"events": events, "state": state,
            "summary": {"opens_today": sum(1 for e in events if e["state"] == "open" and e["t"] >= today_start),
                        "longest_open_ms": longest, "open_since": since if state == "open" else None,
                        "open_since_before_range": state == "open" and since is not None and since <= start + 1000}}


def local_midnight(now: datetime, tz: str | None) -> int:
    try:
        z = ZoneInfo(tz) if tz else None
    except Exception:
        z = None
    loc = now.astimezone(z)
    return ms(loc.replace(hour=0, minute=0, second=0, microsecond=0))


class History:
    """Fetches and caches history per (key, range)."""

    def __init__(self, ha, clock=time.monotonic):
        self.ha, self.clock, self.cache = ha, clock, {}

    def _get(self, key, rng):
        hit = self.cache.get(key)
        if hit and self.clock() - hit[0] < CACHE_TTL[rng]:
            return hit[1]
        return None

    def _put(self, key, val):
        if len(self.cache) > 200:
            self.cache.clear()
        self.cache[key] = (self.clock(), val)
        return val

    async def device(self, dev: Device, rng: str, item: dict | None = None, now: datetime | None = None) -> dict:
        key = ("dev", dev.entity_id, rng)
        if (v := self._get(key, rng)) is not None:
            return v
        now = now or datetime.now(timezone.utc)
        t0 = now - RANGES[rng]
        if dev.kind == "valve":
            data = await self.ha.history(t0, now, [dev.entity_id], attributes=True)
        elif dev.kind == "dehumidifier":
            ids = [dev.entity_id] + ([dev.related["humidity"]] if dev.related.get("humidity") else [])
            data = await self.ha.history(t0, now, ids, attributes=True)
        else:
            ids = [dev.entity_id] + ([dev.related["power"]] if dev.kind == "plug" and dev.related.get("power") else [])
            data = await self.ha.history(t0, now, ids)
        return self._put(key, build(dev, rng, ms(t0), ms(now), by_entity(data), item))

    async def doors(self, devs: list[Device], rng: str, tz: str | None = None, now: datetime | None = None) -> dict:
        key = ("doors", rng, tz, tuple(d.entity_id for d in devs))
        if (v := self._get(key, rng)) is not None:
            return v
        now = now or datetime.now(timezone.utc)
        t0 = now - RANGES[rng]
        rows = by_entity(await self.ha.history(t0, now, [d.entity_id for d in devs])) if devs else {}
        today = local_midnight(now, tz)
        out = {"range": rng, "start": ms(t0), "end": ms(now), "today_start": today, "doors": [
            {"entity_id": d.entity_id, "name": d.name, **door_log(rows.get(d.entity_id, []), ms(t0), ms(now), today)}
            for d in sorted(devs, key=lambda d: d.name.lower())]}
        return self._put(key, out)
