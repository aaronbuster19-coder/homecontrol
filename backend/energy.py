"""Energy costs for the smart plugs: kWh per plug from HA history × a single unit rate, plus overnight standby.

Only plugs with a power (or today's-energy) sensor are measured; this is never the whole flat. The daily standing
charge is reported on its own, never split across plugs. All boundaries are local wall-clock time (DST-safe).
"""
import time
from datetime import date, datetime, time as dtime, timedelta, timezone

from .history import by_entity, energy_kwh, ms, numeric_state, samples, segments
from .summary import local_tz

RANGES = ("today", "week", "month")
STANDBY_FROM, STANDBY_TO, NIGHTS = dtime(1, 0), dtime(5, 0), 7
MIN_STANDBY_MS = 3_600_000  # less than an hour of overnight data in a week: no estimate
PAST_TTL, TODAY_TTL = 1800, 60


class EnergyError(ValueError):
    pass


def tariff(layout: dict) -> dict:
    e = (layout.get("settings") or {}).get("energy") or {}
    return {"rate_p": e.get("rate_p"), "standing_p": e.get("standing_p")}


def cost_p(kwh: float | None, rate_p: float | None) -> float | None:
    """Pence for kWh at the unit rate, to 2 dp; None when either is unknown."""
    if kwh is None or rate_p is None:
        return None
    return round(kwh * rate_p, 2)


def yearly(avg_w: float, rate_p: float | None) -> tuple[float, float | None]:
    """An always-on load of avg_w for a year: (kWh, pence). W × 24 × 365 / 1000 × rate."""
    kwh = avg_w * 24 * 365 / 1000
    return round(kwh, 1), cost_p(kwh, rate_p)


def midnight(d: date, tz) -> datetime:
    return datetime.combine(d, dtime(0), tzinfo=tz)


def range_start(rng: str, now: datetime, tz) -> tuple[datetime, int]:
    """Local start of today / this week (Monday) / this calendar month, and how many days it spans so far."""
    if rng not in RANGES:
        raise EnergyError(f"range must be one of {', '.join(RANGES)}")
    today = now.astimezone(tz).date()
    first = today if rng == "today" else today - timedelta(days=today.weekday()) if rng == "week" else today.replace(day=1)
    return midnight(first, tz), (today - first).days + 1


def standing_total(standing_p: float | None, days: int) -> float | None:
    return None if standing_p is None else round(standing_p * days, 2)


def night_windows(now: datetime, tz, nights: int = NIGHTS) -> list[tuple[int, int]]:
    """[01:00, 05:00) local for the last `nights` nights that are over, newest first (3 or 5 real hours on DST nights)."""
    today, out = now.astimezone(tz).date(), []
    for i in range(nights + 1):
        d = today - timedelta(days=i)
        a, b = datetime.combine(d, STANDBY_FROM, tzinfo=tz), datetime.combine(d, STANDBY_TO, tzinfo=tz)
        if b <= now:
            out.append((ms(a), ms(b)))
        if len(out) == nights:
            break
    return out


def kwh_between(segs, a: int, b: int) -> float:
    return energy_kwh([(max(s, a), min(e, b), v) for s, e, v in segs if min(e, b) > max(s, a)])


def window_mean(segs, windows) -> tuple[float | None, int]:
    """Time-weighted mean power over the windows, skipping gaps (unavailable / no data): (W, covered ms)."""
    covered, wsum = 0, 0.0
    for a, b in windows:
        for s, e, v in segs:
            if v is None:
                continue
            o = min(e, b) - max(s, a)
            if o > 0:
                covered += o
                wsum += v * o
    return (wsum / covered if covered else None), covered


class Energy:
    """Fetches plug power history (one call for the finished days, cached; one for today, cached briefly)."""

    def __init__(self, ha, tz=None, clock=time.monotonic):
        self.ha, self.tz, self.clock, self.cache = ha, tz or local_tz(), clock, {}

    def _cached(self, key, ttl):
        hit = self.cache.get(key)
        return hit[1] if hit and self.clock() - hit[0] < ttl else None

    async def _fetch(self, key, ttl, start: datetime, end: datetime, ids: list[str]) -> dict:
        if (v := self._cached(key, ttl)) is not None:
            return v
        rows = by_entity(await self.ha.history(start.astimezone(timezone.utc), end.astimezone(timezone.utc), ids)) if ids else {}
        if len(self.cache) > 20:
            self.cache.clear()
        self.cache[key] = (self.clock(), rows)
        return rows

    async def segs(self, plugs, now: datetime) -> tuple[dict[str, list], int, int]:
        """Power segments per plug entity from the earliest boundary we need up to now: (segs, from_ms, midnight_ms)."""
        today = now.astimezone(self.tz).date()
        first = min(today.replace(day=1), today - timedelta(days=today.weekday()), today - timedelta(days=NIGHTS))
        t0, t_mid = midnight(first, self.tz), midnight(today, self.tz)
        ids = sorted(d.related["power"] for d in plugs if d.related.get("power"))
        past = await self._fetch(("past", ms(t0), ms(t_mid), tuple(ids)), PAST_TTL, t0, t_mid, ids) if t0 < t_mid else {}
        cur = await self._fetch(("today", ms(t_mid), tuple(ids)), TODAY_TTL, t_mid, now, ids)
        out = {}
        for d in plugs:
            pid = d.related.get("power")
            if pid:
                rows = past.get(pid, []) + cur.get(pid, [])
                out[d.entity_id] = segments(samples(rows, numeric_state), ms(t0), ms(now))
        return out, ms(t0), ms(t_mid)

    async def totals(self, plugs, items: dict[str, dict], layout: dict, rng: str, now: datetime | None = None) -> dict:
        """kWh and pence per plug for today / this week / this month. Today uses the plug's own today's-energy
        meter where it has one (matches the plug sheet), else the integrated power history."""
        now = now or datetime.now(timezone.utc)
        start, days = range_start(rng, now, self.tz)
        plugs = [d for d in plugs if d.related.get("power") or (items.get(d.entity_id) or {}).get("energy_today") is not None]
        segs, _, t_mid = await self.segs(plugs, now)
        t = tariff(layout)
        rows, total = [], 0.0
        for d in plugs:
            meter = (items.get(d.entity_id) or {}).get("energy_today")
            s = segs.get(d.entity_id)
            today_kwh = meter if meter is not None else kwh_between(s, t_mid, ms(now)) if s else 0.0
            past_kwh = kwh_between(s, ms(start), t_mid) if s else 0.0
            kwh = past_kwh + today_kwh
            total += kwh
            rows.append({"entity_id": d.entity_id, "name": d.name, "hidden": d.hidden, "kwh": round(kwh, 3),
                         "cost_p": cost_p(kwh, t["rate_p"]), "source": "meter" if meter is not None else "history"})
        rows.sort(key=lambda r: (-r["kwh"], r["name"].lower()))
        return {"range": rng, "start": ms(start), "end": ms(now), "days": days, **t,
                "total_kwh": round(total, 3), "total_p": cost_p(total, t["rate_p"]),
                "standing_total_p": standing_total(t["standing_p"], days), "plugs": rows}

    async def standby(self, plugs, layout: dict, now: datetime | None = None) -> dict:
        """Average W between 01:00 and 05:00 over the last 7 nights per plug, and what that costs over a year."""
        now = now or datetime.now(timezone.utc)
        plugs = [d for d in plugs if d.related.get("power")]
        segs, _, _ = await self.segs(plugs, now)
        wins = night_windows(now, self.tz)
        rate = tariff(layout)["rate_p"]
        rows = []
        for d in plugs:
            w, covered = window_mean(segs.get(d.entity_id, []), wins)
            row = {"entity_id": d.entity_id, "name": d.name, "hidden": d.hidden, "avg_w": None,
                   "coverage_h": round(covered / 3_600_000, 1), "year_kwh": None, "year_p": None}
            if w is not None and covered >= MIN_STANDBY_MS:
                row["avg_w"] = round(w, 1)
                row["year_kwh"], row["year_p"] = yearly(w, rate)
            rows.append(row)
        rows.sort(key=lambda r: (r["avg_w"] is None, -(r["avg_w"] or 0), r["name"].lower()))
        return {"from": STANDBY_FROM.strftime("%H:%M"), "to": STANDBY_TO.strftime("%H:%M"), "nights": len(wins),
                "rate_p": rate, "plugs": rows}
