"""Weekly summary: Sunday 19:00 local push "Your week at home" built from HA history (energy, doors, room temperatures)."""
import logging
import os
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from .geometry import placed_in
from .history import attr, by_entity, energy_kwh, numeric_state, samples, segments, state_of

log = logging.getLogger("homecontrol.summary")
DEFAULT_TZ = "Europe/London"
SEND_AT = dtime(19, 0)      # Sunday
CATCH_UP_UNTIL = dtime(12, 0)  # Monday: a summary missed while the server was down is still sent until then
RETRY_AFTER = 300


def local_tz(name: str | None = None):
    try:
        return ZoneInfo(name or os.environ.get("TZ_NAME") or DEFAULT_TZ)
    except Exception:
        log.warning("unknown TZ_NAME %r, using UTC", name or os.environ.get("TZ_NAME"))
        return timezone.utc


def at(d: date, t: dtime, tz) -> float:
    """Wall-clock time on a date in tz -> epoch seconds (DST-safe: 19:00 is 19:00 in GMT and BST)."""
    return datetime.combine(d, t, tzinfo=tz).timestamp()


def week_window(now: float, tz) -> tuple[str, date, float, float]:
    """The most recent Sunday 19:00 at or before now: (ISO week key, that Sunday, due ts, catch-up deadline ts)."""
    today = datetime.fromtimestamp(now, tz).date()
    sunday = today - timedelta(days=(today.weekday() - 6) % 7)
    if at(sunday, SEND_AT, tz) > now:
        sunday -= timedelta(days=7)
    y, w, _ = sunday.isocalendar()
    return f"{y}-W{w:02d}", sunday, at(sunday, SEND_AT, tz), at(sunday + timedelta(days=1), CATCH_UP_UNTIL, tz)


def _mean(segs) -> float | None:
    tot = sum(b - a for a, b, v in segs if v is not None)
    return sum(v * (b - a) for a, b, v in segs if v is not None) / tot if tot else None


def _opens(rows: list[dict], start: int, end: int) -> int:
    n, prev = 0, None
    for t, v in samples(rows, state_of("open", "closed")):
        if v is None:
            continue
        if v == "open" and prev != "open" and start < t <= end:
            n += 1
        prev = v
    return n


def summary_text(s: dict) -> str:
    parts = []
    e = s["energy"]
    if e["this_kwh"] is not None:
        txt = f"{e['this_kwh']:.1f} kWh"
        if e.get("change_pct") is not None:
            p = e["change_pct"]
            txt += f" ({'+' if p >= 0 else '−'}{abs(p)}% vs last week)"
        parts.append(txt)
    if s.get("biggest_plug"):
        parts.append(f"{s['biggest_plug']['name']} used most")
    if s.get("top_door"):
        parts.append(f"{s['top_door']['name']} opened {s['top_door']['opens']}×")
    if s["rooms"]:
        parts.append(", ".join(f"{r['name']} {r['avg_temp']:.1f}°" for r in s["rooms"][:3]))
    return " · ".join(parts) or "Not much to report this week."


async def build_summary(ha, devices: dict, layout: dict, start: float, end: float) -> dict:
    """Numbers for [start, end) and the 7 days before it (energy comparison)."""
    s_ms, e_ms, p_ms = int(start * 1000), int(end * 1000), int((start - (end - start)) * 1000)
    plugs = sorted((d for d in devices.values() if d.kind == "plug" and d.related.get("power")), key=lambda d: d.name.lower())
    sensors = sorted((d for d in devices.values() if d.kind == "sensor"), key=lambda d: d.name.lower())
    valve_ids = {e for e, d in devices.items() if d.kind == "valve"}
    room_valves = [(r, placed_in(layout, r, valve_ids)) for r in layout.get("rooms", [])]
    room_valves = [(r, vs) for r, vs in room_valves if vs]
    t_prev, t_end = datetime.fromtimestamp(p_ms / 1000, timezone.utc), datetime.fromtimestamp(end, timezone.utc)
    ids = [d.related["power"] for d in plugs] + [d.entity_id for d in sensors]
    rows = by_entity(await ha.history(t_prev, t_end, ids)) if ids else {}
    vids = sorted({v for _, vs in room_valves for v in vs})
    vrows = by_entity(await ha.history(datetime.fromtimestamp(start, timezone.utc), t_end, vids, attributes=True)) if vids else {}

    plug_out, this_tot, last_tot = [], 0.0, 0.0
    for d in plugs:
        pts = samples(rows.get(d.related["power"], []), numeric_state)
        this, last = energy_kwh(segments(pts, s_ms, e_ms)), energy_kwh(segments(pts, p_ms, s_ms))
        this_tot, last_tot = this_tot + this, last_tot + last
        plug_out.append({"entity_id": d.entity_id, "name": d.name, "kwh": round(this, 2), "last_kwh": round(last, 2)})
    plug_out.sort(key=lambda p: -p["kwh"])
    doors = [{"entity_id": d.entity_id, "name": d.name, "opens": _opens(rows.get(d.entity_id, []), s_ms, e_ms)} for d in sensors]
    doors.sort(key=lambda x: -x["opens"])
    rooms = []
    for r, vs in room_valves:
        means = [m for v in vs if (m := _mean(segments(samples(vrows.get(v, []), attr("current_temperature"), True), s_ms, e_ms))) is not None]
        if means:
            rooms.append({"id": r["id"], "name": r["name"], "avg_temp": round(sum(means) / len(means), 1)})
    change = round((this_tot - last_tot) / last_tot * 100) if plugs and last_tot > 0.05 else None
    out = {"start": s_ms, "end": e_ms, "generated_at": int(time.time() * 1000),
           "energy": {"this_kwh": round(this_tot, 2) if plugs else None, "last_kwh": round(last_tot, 2) if plugs else None,
                      "change_pct": change},
           "plugs": plug_out, "biggest_plug": plug_out[0] if plug_out and plug_out[0]["kwh"] > 0 else None,
           "doors": doors, "top_door": doors[0] if doors and doors[0]["opens"] > 0 else None, "rooms": rooms}
    out["text"] = summary_text(out)
    return out


class WeeklySummary:
    def __init__(self, store, settings, ha, devices, layout, notify, clock=time.time, tz=None):
        self.store, self.settings, self.ha, self.devices, self.layout = store, settings, ha, devices, layout
        self.notify, self.clock, self.tz = notify, clock, tz or local_tz()
        self.failed_at = -1e18

    async def build(self, start: float, end: float) -> dict:
        return await build_summary(self.ha, self.devices(), self.layout(), start, end)

    async def preview(self) -> dict:
        now = self.clock()
        loc = datetime.fromtimestamp(now, self.tz)
        start = at(loc.date() - timedelta(days=7), loc.time().replace(tzinfo=None), self.tz)
        return {**await self.build(start, now), "week": None, "preview": True}

    async def tick(self) -> None:
        if not self.settings()["weekly_summary"]:
            return
        now = self.clock()
        week, sunday, due, deadline = week_window(now, self.tz)
        if not due <= now < deadline or self.store.get("summary_sent_week") == week:
            return
        if now - self.failed_at < RETRY_AFTER or not self.devices():
            return
        try:
            data = await self.build(at(sunday - timedelta(days=7), SEND_AT, self.tz), due)
        except Exception as e:
            self.failed_at = now
            log.warning("weekly summary: building failed, retrying later: %s", e)
            return
        data["week"] = week
        self.store.put_summary(week, data)
        self.store.put("summary_sent_week", week)  # before sending: a crash mid-send must not send twice
        await self.notify({"title": "Your week at home", "body": data["text"], "tag": "weekly-summary", "url": "/?summary"})
