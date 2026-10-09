"""Usage statistics for a linked appliance, from its plug's power history in HA (history.py / energy.py helpers).

Per rule: washer / dryer / dishwasher — cycles this week and last week, average length, kWh and cost per cycle (the
same cycle detector as the live tracker, replayed over the history); kettle and other short-burst appliances — uses
today and this week; fan / heater / lamp / TV / desktop PC / home server — hours on this week and what they cost;
fridge / freezer — average kWh per day over the last 7 full days; hoover — charges this week and the average charge
time (the live charge detector, replayed). Weeks start on Monday, local time. Cost only when a unit rate is set.
Results are cached briefly per appliance.
"""
import json
import time
from datetime import datetime, timedelta, timezone

from .appliances import APPLIANCES, LEFT_W, charge_step, step, thresholds
from .energy import cost_p, kwh_between, midnight, tariff
from .history import by_entity, ms, numeric_state, samples, segments
from .summary import local_tz

TTL = 120
DAY_MS = 86_400_000
USES = {"kettle": "Boils"}            # label for the count of busy runs; others say "Uses"
HOURS_TYPES = ("fan", "floor_lamp", "heater", "tv", "desktop_pc", "home_server")
DAILY_TYPES = ("fridge", "freezer")


def find_cycles(segs, th: dict) -> list[tuple[float, float]]:
    """Finished cycles [(start s, end s)] in power segments [(a ms, b ms, W|None)], by the live tracker's rules."""
    c = {"phase": "idle", "run_start": None, "finished_at": None, "notified": False, "high_since": None, "low_since": None}
    out = []
    for a, b, v in segs:
        item = {"state": "on", "power": v} if v is not None else {"state": "unavailable"}
        for t in (a / 1000, (b - 1) / 1000):  # power is constant over the segment: its start and its end decide
            if step(c, item, th, t) == "finished":
                out.append((c["run_start"], c["finished_at"]))
    return out


def find_charges(segs, th: dict) -> list[tuple[float, float]]:
    """Finished charges [(start s, charged s)] in power segments, by the live charge tracker's rules."""
    c = {"phase": "idle", "charge_start": None, "charged_at": None, "notified": False, "off_done": False, "low_since": None}
    out = []
    for a, b, v in segs:
        item = {"state": "on", "power": v} if v is not None else {"state": "unavailable"}
        for t in (a / 1000, (b - 1) / 1000):
            if charge_step(c, item, th, t) == "charged":
                out.append((c["charge_start"], c["charged_at"]))
    return out


def busy_runs(segs, on_w: float) -> list[int]:
    """Start times (ms) of runs drawing over on_w (a boil, a toast…)."""
    out, busy = [], False
    for a, _, v in segs:
        now_busy = v is not None and v > on_w
        if now_busy and not busy:
            out.append(a)
        if v is not None:
            busy = now_busy
    return out


def hours_over(segs, w: float, a: int, b: int) -> float:
    return sum(min(e, b) - max(s, a) for s, e, v in segs if v is not None and v > w and min(e, b) > max(s, a)) / 3_600_000


def covered_days(segs, a: int, b: int) -> float:
    return sum(min(e, b) - max(s, a) for s, e, v in segs if v is not None and min(e, b) > max(s, a)) / DAY_MS


def compute(f: dict, segs, now: datetime, tz, rate_p) -> dict:
    """Pure: the stats for piece f from power segments covering last Monday 00:00 (local) up to now."""
    rule, th = APPLIANCES[f["type"]][0], thresholds(f)
    today_d = now.astimezone(tz).date()
    today, week = midnight(today_d, tz), midnight(today_d - timedelta(days=today_d.weekday()), tz)
    last_week = midnight(today_d - timedelta(days=today_d.weekday() + 7), tz)
    t_now, t_week, t_last, t_today = ms(now), ms(week), ms(last_week), ms(today)
    kwh_week = kwh_between(segs, t_week, t_now)
    out = {"type": f["type"], "rule": rule, "rate_p": rate_p,
           "week": {"kwh": round(kwh_week, 3), "cost_p": cost_p(kwh_week, rate_p)}}
    if rule == "cycle":
        cycles = find_cycles(segs, th)
        this = [c for c in cycles if c[0] * 1000 >= t_week]
        last = [c for c in cycles if t_last <= c[0] * 1000 < t_week]
        both = this + last
        per = [kwh_between(segs, int(a * 1000), int(b * 1000)) for a, b in both]
        avg_kwh = sum(per) / len(per) if per else None
        out["cycles"] = {"this_week": len(this), "last_week": len(last),
                         "avg_min": round(sum(b - a for a, b in both) / len(both) / 60) if both else None,
                         "kwh_per_cycle": round(avg_kwh, 3) if avg_kwh is not None else None,
                         "cost_per_cycle_p": cost_p(avg_kwh, rate_p)}
    elif rule == "charge":
        charges = find_charges(segs, th)
        this = [c for c in charges if c[0] * 1000 >= t_week]
        both = this + [c for c in charges if t_last <= c[0] * 1000 < t_week]
        out["charges"] = {"this_week": len(this),
                          "avg_min": round(sum(b - a for a, b in both) / len(both) / 60) if both else None}
    elif f["type"] in DAILY_TYPES:
        a = ms(midnight(today_d - timedelta(days=7), tz))
        days = covered_days(segs, a, t_today)
        kwh = kwh_between(segs, a, t_today)
        per_day = kwh / days if days >= 1 else None
        out["daily"] = {"kwh": round(per_day, 3) if per_day is not None else None, "cost_p": cost_p(per_day, rate_p),
                        "days": round(days, 1)}
    elif f["type"] in HOURS_TYPES:  # a PC asleep still draws a few W: count it on only above its "On" level
        w = th["on_w"] if f["type"] == "desktop_pc" else LEFT_W
        out["hours_week"] = round(hours_over(segs, w, t_week, t_now), 2)
    elif rule == "busy":
        starts = busy_runs(segs, th["on_w"])
        out["uses"] = {"label": USES.get(f["type"], "Uses"), "today": sum(t >= t_today for t in starts),
                       "week": sum(t >= t_week for t in starts)}
    return out


class ApplianceStats:
    def __init__(self, ha, tz=None, clock=time.monotonic, now=None):
        self.ha, self.tz, self.clock, self.now, self.cache = ha, tz or local_tz(), clock, now, {}

    async def stats(self, f: dict, dev, layout: dict) -> dict:
        rate = tariff(layout)["rate_p"]
        pid = dev.related.get("power")
        if not pid:
            return {"type": f["type"], "rule": APPLIANCES[f["type"]][0], "rate_p": rate, "no_power": True}
        key = (f["id"], f["plug"], pid, f["type"], json.dumps(f.get("thresholds") or {}, sort_keys=True), rate)
        hit = self.cache.get(key)
        if hit and self.clock() - hit[0] < TTL:
            return hit[1]
        now = self.now() if self.now else datetime.now(timezone.utc)
        d = now.astimezone(self.tz).date()
        t0 = midnight(d - timedelta(days=d.weekday() + 7), self.tz)
        rows = by_entity(await self.ha.history(t0.astimezone(timezone.utc), now.astimezone(timezone.utc), [pid])).get(pid, [])
        out = compute(f, segments(samples(rows, numeric_state), ms(t0), ms(now)), now, self.tz, rate)
        if len(self.cache) > 50:
            self.cache.clear()
        self.cache[key] = (self.clock(), out)
        return out
