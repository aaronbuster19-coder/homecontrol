"""Monthly energy report: what each appliance (a plug's linked appliance, else the plug) cost this month, its share of
the total and how that compares with last month.

Home Assistant's recorder keeps only ~10 days of history by default, so a month can't be read back from HA later. Each
finished day is therefore rolled up once into SQLite (`energy_daily`: kWh and covered time per plug per local day),
from the same power history the Energy sheet integrates. The rollup is read-only (it never switches anything), runs
hourly in the background and on demand, newest missing days first, at most BATCH_DAYS per HA history call. Days HA
no longer has are stored with zero coverage, so the report can say how many days it actually has data for.
"""
import asyncio
import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

from .appliances import TYPE_NAME, linked
from .energy import cost_p, midnight, standing_total, tariff
from .history import by_entity, energy_kwh, ms, numeric_state, samples, segments

log = logging.getLogger("homecontrol.energy_report")

BATCH_DAYS = 7          # finished days per HA history call
REQUEST_DAYS = 14       # a report request rolls up at most this many missing days of each month before answering
KEEP_DAYS = 400         # rows older than this are pruned (13 months of reports)
MONTHS_BACK = 12        # oldest month the report offers: 12 months before the current one
RETRY_AFTER = 300       # after a failed HA call, the next rollup waits this long
ROLLUP_EVERY = 3600
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


class ReportError(ValueError):
    pass


class DailyStore:
    """kWh per plug per local day: energy_daily(day 'YYYY-MM-DD', entity_id, kwh, covered_ms)."""

    def __init__(self, path: str):
        self.path, self._lock = path, threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS energy_daily (day TEXT NOT NULL, entity_id TEXT NOT NULL, "
                      "kwh REAL NOT NULL, covered_ms INTEGER NOT NULL, PRIMARY KEY (day, entity_id))")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def put_day(self, day: str, rows: dict[str, tuple[float, int]]) -> None:
        """Adds the plugs the day has no row for yet; never overwrites (a plug found later must not replace a day
        rolled up while HA still had it with a purged, empty one)."""
        with self._lock, self._conn() as c:
            c.executemany("INSERT OR IGNORE INTO energy_daily (day, entity_id, kwh, covered_ms) VALUES (?, ?, ?, ?)",
                          [(day, e, k, cv) for e, (k, cv) in rows.items()])

    def between(self, d0: date, d1: date) -> list[tuple[str, str, float, int]]:
        """Rows for days d0..d1 inclusive."""
        with self._lock, self._conn() as c:
            return c.execute("SELECT day, entity_id, kwh, covered_ms FROM energy_daily WHERE day >= ? AND day <= ? "
                             "ORDER BY day", (d0.isoformat(), d1.isoformat())).fetchall()

    def first_day(self) -> str | None:
        with self._lock, self._conn() as c:
            r = c.execute("SELECT MIN(day) FROM energy_daily WHERE covered_ms > 0").fetchone()
        return r[0] if r else None

    def prune(self, before: date) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM energy_daily WHERE day < ?", (before.isoformat(),))


def month_first(d: date, back: int = 0) -> date:
    """First day of d's month, `back` months earlier."""
    n = d.year * 12 + d.month - 1 - back
    return date(n // 12, n % 12 + 1, 1)


def month_last(first: date) -> date:
    return month_first(first, -1) - timedelta(days=1)


def parse_month(s: str) -> date:
    try:
        y, m = s.split("-")
        if len(y) != 4 or len(m) != 2:
            raise ValueError
        return date(int(y), int(m), 1)
    except (ValueError, TypeError):
        raise ReportError("month must be YYYY-MM")


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_label(d: date) -> str:
    return f"{MONTHS[d.month - 1]} {d.year}"


def appliance_names(layout: dict) -> dict[str, dict]:
    """plug entity -> {id, type, name} of the appliance it's linked to."""
    out = {}
    for f in linked(layout):
        name = f.get("label") or TYPE_NAME.get(f["type"]) or f["type"].replace("_", " ").capitalize()
        out[f["plug"]] = {"id": f.get("id"), "type": f["type"], "name": name}
    return out


def day_rows(rows_by_id: dict, plugs, d: date, tz) -> dict[str, tuple[float, int]]:
    """kWh and covered ms per plug on local day d (DST days are 23 / 25 h)."""
    a, b = ms(midnight(d, tz)), ms(midnight(d + timedelta(days=1), tz))
    out = {}
    for p in plugs:
        segs = segments(samples(rows_by_id.get(p.related["power"], []), numeric_state), a, b)
        out[p.entity_id] = (round(energy_kwh(segs), 4), sum(e - s for s, e, v in segs if v is not None))
    return out


class Rollup:
    def __init__(self, store: DailyStore, ha, tz, clock=time.time):
        self.store, self.ha, self.tz, self.clock = store, ha, tz, clock
        self.failed_at = -1e18
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    def today(self) -> date:
        return datetime.fromtimestamp(self.clock(), self.tz).date()

    def missing(self, plugs, oldest: date | None = None, newest: date | None = None) -> list[date]:
        """Finished days from `oldest` (default: the 1st of last month) to `newest` (default and at most: yesterday)
        that lack a row for a plug, newest first."""
        today = self.today()
        oldest = oldest or month_first(today, 1)
        newest = min(newest or today, today - timedelta(days=1))
        ids = {p.entity_id for p in plugs}
        have: dict[str, set] = {}
        for day, eid, _, _ in self.store.between(oldest, newest):
            have.setdefault(day, set()).add(eid)
        out, d = [], newest
        while d >= oldest:
            if not ids <= have.get(d.isoformat(), set()):
                out.append(d)
            d -= timedelta(days=1)
        return out

    async def run(self, plugs, max_days: int = BATCH_DAYS, oldest: date | None = None, newest: date | None = None) -> int:
        """Roll up to max_days missing days (newest first), BATCH_DAYS per HA call. Returns the days stored."""
        plugs = [p for p in plugs if p.related.get("power")]
        if not plugs:
            return 0  # not discovered yet: never mark days as empty for that
        async with self._lock:
            done = 0
            while done < max_days and self.clock() - self.failed_at >= RETRY_AFTER:
                days = self.missing(plugs, oldest, newest)[:min(BATCH_DAYS, max_days - done)]
                if not days:
                    break
                lo, hi = min(days), max(days)
                ids = sorted({p.related["power"] for p in plugs})
                try:
                    rows = by_entity(await self.ha.history(midnight(lo, self.tz).astimezone(timezone.utc),
                                                           midnight(hi + timedelta(days=1), self.tz).astimezone(timezone.utc), ids))
                except Exception as e:
                    self.failed_at = self.clock()
                    log.warning("energy rollup: history failed, retrying later: %s", e)
                    break
                for d in days:
                    self.store.put_day(d.isoformat(), day_rows(rows, plugs, d, self.tz))
                done += len(days)
            self.store.prune(self.today() - timedelta(days=KEEP_DAYS))
            return done

    # ---- background ----
    async def _loop(self, plug_devices, every: float, delay: float) -> None:
        await asyncio.sleep(delay)
        while True:
            try:
                plugs, _ = await plug_devices()
                await self.run(plugs)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("energy rollup failed: %s", e)
            await asyncio.sleep(every)

    def start(self, plug_devices, every: float = ROLLUP_EVERY, delay: float = 60) -> None:
        self._task = asyncio.create_task(self._loop(plug_devices, every, delay))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


def totals(rows: list[tuple[str, str, float, int]]) -> tuple[dict[str, float], set[str]]:
    """Sum kWh per entity, and the days that have any data."""
    kwh: dict[str, float] = {}
    days = set()
    for day, eid, k, covered in rows:
        kwh[eid] = kwh.get(eid, 0.0) + k
        if covered > 0:
            days.add(day)
    return kwh, days


def change_pct(this: float | None, last: float | None) -> int | None:
    if this is None or last is None or last <= 0.05:
        return None
    return round((this - last) / last * 100)


def period(first: date, last: date, ndays: int, kwh: dict, days_with: set, rate) -> dict:
    total = sum(kwh.values())
    return {"month": month_key(first), "label": month_label(first), "from": first.isoformat(), "to": last.isoformat(),
            "days": ndays, "days_with_data": len(days_with), "total_kwh": round(total, 3), "total_p": cost_p(total, rate)}


async def monthly(rollup: Rollup, plugs, devices: dict, layout: dict, month: str | None = None) -> dict:
    """The report for `month` (YYYY-MM; default this month, or last month on the 1st). This month covers its finished
    days and is compared with the same days of last month; a finished month is compared with all of last month."""
    today = rollup.today()
    yesterday = today - timedelta(days=1)
    first = parse_month(month) if month else month_first(yesterday)
    earliest = month_first(today, MONTHS_BACK)
    if first > month_first(today) or first < earliest:
        raise ReportError(f"month must be between {month_key(earliest)} and {month_key(today)}")
    current = first == month_first(today)
    last = yesterday if current else month_last(first)
    ndays = (last - first).days + 1  # 0 on the 1st: nothing finished yet
    p_first = month_first(first, 1)
    p_last = min(p_first + timedelta(days=ndays - 1), month_last(p_first)) if current else month_last(p_first)
    p_days = (p_last - p_first).days + 1 if ndays else 0
    if ndays:  # fill what this report needs first: its own days, then the days it's compared with
        await rollup.run(plugs, REQUEST_DAYS, oldest=first, newest=last)
        await rollup.run(plugs, REQUEST_DAYS, oldest=p_first, newest=p_last)
    this_kwh, this_days = totals(rollup.store.between(first, last)) if ndays else ({}, set())
    last_kwh, last_days = totals(rollup.store.between(p_first, p_last)) if p_days else ({}, set())

    t = tariff(layout)
    rate = t["rate_p"]
    appl = appliance_names(layout)
    total = sum(this_kwh.values())
    rows = []
    for eid in sorted(set(this_kwh) | set(last_kwh)):
        k, lk = this_kwh.get(eid, 0.0), last_kwh.get(eid)
        if k < 0.0005 and (lk or 0) < 0.0005:
            continue
        d, a = devices.get(eid), appl.get(eid)
        rows.append({"entity_id": eid, "name": a["name"] if a else d.name if d else eid,
                     "plug_name": d.name if d else eid, "appliance": a, "hidden": bool(d and d.hidden),
                     "kwh": round(k, 3), "cost_p": cost_p(k, rate), "share_pct": round(k / total * 100, 1) if total > 0 else 0.0,
                     "last_kwh": round(lk, 3) if lk is not None else None, "last_cost_p": cost_p(lk, rate),
                     "change_pct": change_pct(k, lk)})
    rows.sort(key=lambda r: (-r["kwh"], r["name"].lower()))
    this = period(first, last, ndays, this_kwh, this_days, rate)
    prev = period(p_first, p_last, p_days, last_kwh, last_days, rate)
    this.update(standing_total_p=standing_total(t["standing_p"], ndays) if ndays else None)
    prev.update(full=p_last == month_last(p_first))
    return {**this, "current": current, **t, "previous": prev, "change_pct": change_pct(this["total_kwh"], prev["total_kwh"]) if prev["days_with_data"] else None,
            "rows": rows, "prev_month": month_key(p_first) if p_first >= earliest else None,
            "next_month": month_key(month_first(first, -1)) if not current else None,
            "collecting_since": rollup.store.first_day()}
