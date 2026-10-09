"""Octopus Energy tariffs (Agile, Go, …): half-hourly unit rates from Octopus's public API, the cheapest time to run the
washer / dishwasher / dryer, an optional reminder before that time, and the rates behind energy and run costs.

- Off by default. Settings (⋯ → Tariff…, admins): product code (Agile, Go, Intelligent Go presets or any code) and
  region letter A–P; "use for costs"; the reminder (off by default) and how long before the cheap window it comes.
- Rates come from GET {OCTOPUS_API}/products/{product}/electricity-tariffs/E-1R-{product}-{region}/standard-unit-rates/
  (public, no key; prices in p/kWh including VAT). They are kept in SQLite (`tariff_rates`), so the app works offline
  with the last known rates and survives restarts. Fetches: once on enabling, then each time new rates are due —
  Agile publishes tomorrow's at about 16:00 UK time, so from 15:45 every 15 min until tomorrow is complete, else every
  6 h; after an error 5, 10, 20, … min, at most hourly. One request each (page_size 1500, `next` followed at most 4
  times, only on the configured host). OCTOPUS_API (default https://api.octopus.energy/v1) points tests at a fake.
- Cheapest window: for each linked washer, dishwasher and dryer, the contiguous span of its typical run length (the
  appliance stats' average cycle, backend/appliance_stats.py; else its last runs from the run log; else a default)
  with the lowest average rate among the rates known from now on, starting now or on a half hour. Estimated cost =
  typical kWh per cycle × that average (assumes an even draw). Suggestions only: nothing is ever switched on.
- Reminder (opt-in): one push "Cheap electricity from 02:00" remind_lead_min before the window starts, at most one per
  appliance per window and per 12 h, marked as sent before it goes out; never during quiet hours (it is skipped, not
  held — a morning digest saying "02:00 was cheap" is no use); not while the appliance is running, nor when the window
  is under 0.5p/kWh cheaper than now.
- Costs: when enabled with "use for costs", the Energy sheet (backend/energy.py `pricer`) and the run log
  (backend/runlog.py) price each half-hour at its own rate; half-hours without a known rate use the flat unit rate
  (Energy → Set tariff), else the average of the known rates.
The loop (every CHECK_EVERY s, own task) also drives the run log's timers. All timing uses the injected clock.
"""
import asyncio
import logging
import math
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from .appliances import linked
from .energy import midnight, tariff as flat_tariff
from .history import ms
from .live import build_device

log = logging.getLogger("homecontrol.tariff")

OCTOPUS_API = "https://api.octopus.energy/v1"
SLOT = 1_800_000  # ms: one half-hour
CHECK_EVERY = 30
STARTUP_DELAY = 15
DEBOUNCE = 1.0
REGIONS = {"A": "Eastern England", "B": "East Midlands", "C": "London", "D": "Merseyside and North Wales",
           "E": "West Midlands", "F": "North Eastern England", "G": "North Western England", "H": "Southern England",
           "J": "South Eastern England", "K": "South Wales", "L": "South Western England", "M": "Yorkshire",
           "N": "Southern Scotland", "P": "Northern Scotland"}
PRESETS = [{"product": "AGILE-24-10-01", "name": "Agile Octopus"},
           {"product": "GO-VAR-22-10-14", "name": "Octopus Go"},
           {"product": "INTELLI-VAR-22-10-14", "name": "Intelligent Octopus Go"}]
DEFAULTS = {"enabled": False, "product": "AGILE-24-10-01", "region": "C", "use_for_costs": True,
            "remind": False, "remind_lead_min": 15}
LEAD_RANGE = (5, 120)
PRODUCT = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,48}$")
SUGGEST = ("washer", "dishwasher", "dryer")
DEFAULT_MIN = {"washer": 120, "dishwasher": 150, "dryer": 120}
TYPE_NAME = {"washer": "Washing machine", "dishwasher": "Dishwasher", "dryer": "Tumble dryer"}
PUBLISH_FROM = (15, 45)    # local time from which tomorrow's Agile rates may appear
SOON, LATER = 15 * 60, 6 * 3600
MIN_SAVING = 0.5           # p/kWh: a window cheaper than now by less than this isn't worth a reminder
REMIND_GAP = 12 * 3600
TYPICAL_TTL = 3600
KEEP_RATES_DAYS = 120
MAX_PAGES = 5
REFRESH_MIN_GAP = 30       # s between forced refreshes
SETTINGS_KEY, STATE_KEY, REMINDED_KEY = "tariff_settings", "tariff_state", "tariff_reminded"


class TariffError(ValueError):
    pass


def tariff_code(product: str, region: str) -> str:
    return f"E-1R-{product}-{region}"


def preset_name(product: str) -> str:
    return next((p["name"] for p in PRESETS if p["product"] == product), product)


def validate_settings(body, old: dict) -> dict:
    if not isinstance(body, dict):
        raise TariffError("send a JSON object")
    unknown = set(body) - set(DEFAULTS)
    if unknown:
        raise TariffError(f"unknown setting {sorted(unknown)[0]!r}")
    s = {**DEFAULTS, **old}
    for k in ("enabled", "use_for_costs", "remind"):
        if k in body:
            if not isinstance(body[k], bool):
                raise TariffError(f"{k} must be true or false")
            s[k] = body[k]
    if "product" in body:
        p = body["product"].strip().upper() if isinstance(body["product"], str) else None
        if not p or not PRODUCT.match(p):
            raise TariffError("product must be an Octopus product code such as AGILE-24-10-01")
        s["product"] = p
    if "region" in body:
        r = body["region"].strip().upper() if isinstance(body["region"], str) else None
        if r not in REGIONS:
            raise TariffError("region must be one of " + ", ".join(REGIONS))
        s["region"] = r
    if "remind_lead_min" in body:
        v = body["remind_lead_min"]
        if isinstance(v, bool) or not isinstance(v, int) or not LEAD_RANGE[0] <= v <= LEAD_RANGE[1]:
            raise TariffError(f"remind_lead_min must be {LEAD_RANGE[0]}–{LEAD_RANGE[1]} minutes")
        s["remind_lead_min"] = v
    return s


# ---------------- rates ----------------
def iso_z(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s) -> int | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ms(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def parse_rates(results, a: int, b: int) -> dict[int, float]:
    """Octopus standard-unit-rates results -> {half-hour start ms: p/kWh inc VAT} within [a, b).
    An entry may span many half-hours (Go's cheap night, a fixed rate with valid_to null = until b). Where a product
    lists both, the direct-debit price wins over the non-direct-debit one."""
    out: dict[int, float] = {}
    rows = [r for r in results or [] if isinstance(r, dict)]
    rows.sort(key=lambda r: 0 if r.get("payment_method") == "NON_DIRECT_DEBIT" else 1)
    for r in rows:
        v = r.get("value_inc_vat")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            continue
        vf, vt = _ts(r.get("valid_from")), _ts(r.get("valid_to"))
        if vf is None:
            continue
        lo, hi = max(vf, a), min(vt if vt is not None else b, b)
        s = lo - lo % SLOT
        if s < lo:
            s += SLOT
        while s + SLOT <= hi:
            out[s] = round(float(v), 4)
            s += SLOT
    return out


def window_avg(rates: dict[int, float], a: int, b: int) -> float | None:
    """Time-weighted average p/kWh over [a, b) ms; None if any part has no known rate."""
    if b <= a:
        return None
    total, t = 0.0, a
    while t < b:
        s = t - t % SLOT
        p = rates.get(s)
        if p is None:
            return None
        e = min(b, s + SLOT)
        total += p * (e - t)
        t = e
    return total / (b - a)


def cheapest(rates: dict[int, float], now: int, minutes: float) -> dict | None:
    """Lowest-average contiguous window of `minutes` among the known rates, starting now or on a later half hour.
    Ties go to the earliest. None when not enough rates are known ahead."""
    span = int(minutes * 60_000)
    starts = [now] + list(range(now - now % SLOT + SLOT, now + 2 * 86_400_000, SLOT))
    best, known = None, False
    for a in starts:
        if (a - a % SLOT) not in rates:
            if known:
                break  # rates are contiguous; past the last known one nothing more fits
            continue
        known = True
        avg = window_avg(rates, a, a + span)
        if avg is None:
            continue
        if best is None or avg < best["avg_p"] - 1e-9:
            best = {"start": a, "end": a + span, "avg_p": round(avg, 2)}
    return best


def price_power(rates: dict[int, float], p_w: float, t0: int, t1: int, fallback: float | None) -> tuple[float, float | None]:
    """A constant draw of p_w watts over [t0, t1) ms: (kWh, pence) with each half-hour at its own rate (fallback where
    unknown; pence None when a part has neither)."""
    kwh = pence = 0.0
    priced, t = True, t0
    while t < t1:
        s = t - t % SLOT
        e = min(t1, s + SLOT)
        k = p_w * (e - t) / 3.6e9
        kwh += k
        r = rates.get(s, fallback)
        if r is None:
            priced = False
        else:
            pence += k * r
        t = e
    return kwh, (pence if priced else None)


def price_segs(rates: dict[int, float], segs, a: int, b: int, fallback: float | None) -> tuple[float, float | None]:
    """Power segments [(start ms, end ms, W | None)] clipped to [a, b): (kWh, pence | None)."""
    kwh, pence, priced = 0.0, 0.0, True
    for s, e, v in segs:
        lo, hi = max(s, a), min(e, b)
        if v is None or hi <= lo or v <= 0:
            continue
        k, c = price_power(rates, v, lo, hi, fallback)
        kwh += k
        if c is None:
            priced = False
        else:
            pence += c
    return kwh, (pence if priced else None)


def day_slots(d: date, tz) -> list[int]:
    """Half-hour starts of a local day (46 or 50 on DST days)."""
    a, b = ms(midnight(d, tz)), ms(midnight(d + timedelta(days=1), tz))
    return list(range(a, b, SLOT))


def day_summary(rates: dict[int, float], d: date, tz, label: str) -> dict:
    starts = day_slots(d, tz)
    slots = [{"start": s, "p": rates.get(s)} for s in starts]
    known = [x for x in slots if x["p"] is not None]
    out = {"date": d.isoformat(), "label": label, "slots": slots, "complete": len(known) == len(slots),
           "known": len(known), "min": None, "max": None, "avg": None, "cheapest": None}
    if known:
        lo = min(known, key=lambda x: (x["p"], x["start"]))
        out.update(min=lo["p"], max=max(x["p"] for x in known), avg=round(sum(x["p"] for x in known) / len(known), 2),
                   cheapest={"start": lo["start"], "p": lo["p"]})
    return out


def next_fetch(now: float, tz, tomorrow_complete: bool) -> float:
    """When to look for new rates after a good fetch (epoch s)."""
    loc = datetime.fromtimestamp(now, tz)
    publish = datetime.combine(loc.date(), datetime.min.time().replace(hour=PUBLISH_FROM[0], minute=PUBLISH_FROM[1]),
                               tzinfo=tz).timestamp()
    if tomorrow_complete:
        return now + LATER
    if now >= publish:
        return now + SOON
    return min(publish, now + LATER)


def backoff(fails: int) -> float:
    return min(3600.0, 300.0 * 2 ** max(0, fails - 1))


async def http_json(url: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url, headers={"Accept": "application/json", "User-Agent": "homecontrol"})
        r.raise_for_status()
        return r.json()


class RateStore:
    def __init__(self, path: str):
        self.path, self._lock = path, threading.Lock()
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS tariff_rates (code TEXT NOT NULL, start INTEGER NOT NULL, p REAL NOT NULL, "
                      "PRIMARY KEY (code, start))")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def load(self, code: str, since: int) -> dict[int, float]:
        with self._lock, self._conn() as c:
            return {s: p for s, p in c.execute("SELECT start, p FROM tariff_rates WHERE code = ? AND start >= ?", (code, since))}

    def merge(self, code: str, rates: dict[int, float]) -> None:
        with self._lock, self._conn() as c:
            c.executemany("INSERT INTO tariff_rates (code, start, p) VALUES (?, ?, ?) "
                          "ON CONFLICT(code, start) DO UPDATE SET p = excluded.p", [(code, s, p) for s, p in rates.items()])

    def prune(self, before: int) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM tariff_rates WHERE start < ?", (before,))


class Tariff:
    """kv: AutoStore; layout: callable; live: Live (plug items for the run log); runlog: RunLog; appliances: the live
    cycle trackers; stats: ApplianceStats; devices: async -> {entity id: Device}; notify: async fn(payload);
    quiet_active: fn() -> bool; clock: epoch seconds; fetch: async fn(url) -> JSON (tests inject a fake)."""

    def __init__(self, kv, rates: RateStore, layout, live, runlog, appliances, stats, devices, notify, quiet_active,
                 clock=time.time, tz=None, fetch=None, base: str | None = None):
        self.kv, self.store, self.layout, self.live, self.runlog = kv, rates, layout, live, runlog
        self.appliances, self.stats, self.devices, self.notify, self.quiet_active = appliances, stats, devices, notify, quiet_active
        self.clock, self.tz = clock, tz or timezone.utc
        self.fetch = fetch or http_json
        self.base = (base or os.environ.get("OCTOPUS_API") or OCTOPUS_API).rstrip("/")
        self.wake = asyncio.Event()
        self._task = None
        self._typical: dict = {}
        self._pruned = None
        self._forced_at = 0.0
        self.rates: dict[int, float] = {}
        self._code = None
        self._load()

    # ---- settings / state ----
    def settings(self) -> dict:
        return {**DEFAULTS, **(self.kv.get(SETTINGS_KEY, {}) or {})}

    def state(self) -> dict:
        return {"code": None, "fetched_at": None, "tried_at": None, "error": None, "fails": 0, "next_at": 0.0,
                **(self.kv.get(STATE_KEY, {}) or {})}

    def _put_state(self, **kw) -> None:
        self.kv.put(STATE_KEY, {**self.state(), **kw})

    def code(self, s: dict | None = None) -> str:
        s = s or self.settings()
        return tariff_code(s["product"], s["region"])

    def _load(self) -> None:
        self._code = self.code()
        self.rates = self.store.load(self._code, int((self.clock() - 45 * 86400) * 1000))

    def put_settings(self, body) -> dict:
        old = self.settings()
        s = validate_settings(body, old)
        self.kv.put(SETTINGS_KEY, s)
        if self.code(s) != self.code(old) or (s["enabled"] and not old["enabled"]):
            self._load()
            self._put_state(error=None, fails=0, next_at=0.0)  # fetch on the next check
            self.wake.set()
        return s

    def active(self) -> bool:
        s = self.settings()
        return s["enabled"] and s["use_for_costs"] and bool(self.rates)

    def info(self) -> dict | None:
        """For backend/energy.py: which tariff prices the costs, or None when the flat unit rate does."""
        if not self.active():
            return None
        s = self.settings()
        return {"name": preset_name(s["product"]), "product": s["product"], "region": s["region"],
                "region_name": REGIONS[s["region"]]}

    def fallback(self) -> float | None:
        flat = flat_tariff(self.layout())["rate_p"]
        if flat is not None:
            return flat
        return round(sum(self.rates.values()) / len(self.rates), 4) if self.rates else None

    # ---- prices for other modules ----
    def pricer(self, segs, a: int, b: int, kwh: float, flat: float | None) -> float | None:
        """backend/energy.py: pence for kwh used over [a, b) ms with power segments segs; None = use the flat rate."""
        if not self.active():
            return None
        fb = flat if flat is not None else self.fallback()
        hk, pence = price_segs(self.rates, segs or [], a, b, fb)
        if pence is None:
            return None
        if hk > 1e-9:
            return round(pence * kwh / hk, 2)
        if kwh <= 0:
            return 0.0
        r = self.rates.get(b - b % SLOT, fb)
        return round(kwh * r, 2) if r is not None else None

    def run_price(self, p_w: float, t0: float, t1: float) -> tuple[float, float | None]:
        """backend/runlog.py: (kWh, pence | None) for p_w watts over [t0, t1) epoch seconds."""
        a, b = int(t0 * 1000), int(t1 * 1000)
        if self.active():
            return price_power(self.rates, p_w, a, b, self.fallback())
        kwh = p_w * (b - a) / 3.6e9
        flat = flat_tariff(self.layout())["rate_p"]
        return kwh, (kwh * flat if flat is not None else None)

    # ---- fetching ----
    def _range(self, now: float) -> tuple[int, int]:
        today = datetime.fromtimestamp(now, self.tz).date()
        a = min(today.replace(day=1), today - timedelta(days=today.weekday()))
        need = range(ms(midnight(a, self.tz)), ms(midnight(today + timedelta(days=1), self.tz)), SLOT)
        if all(s in self.rates for s in need):  # the past is complete: only today and tomorrow
            a = today
        return ms(midnight(a, self.tz)), ms(midnight(today + timedelta(days=2), self.tz))

    async def _fetch_all(self, a: int, b: int, s: dict) -> list:
        q = urlencode({"period_from": iso_z(a), "period_to": iso_z(b), "page_size": 1500})
        url = f"{self.base}/products/{s['product']}/electricity-tariffs/{self.code(s)}/standard-unit-rates/?{q}"
        out = []
        for _ in range(MAX_PAGES):
            data = await self.fetch(url)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise TariffError("Octopus sent something unexpected")
            out += data["results"]
            nxt = data.get("next")
            if not isinstance(nxt, str) or not nxt.startswith(self.base + "/"):
                break
            url = nxt
        return out

    async def refresh(self, force: bool = False) -> None:
        s, now = self.settings(), self.clock()
        if not s["enabled"]:
            return
        st = self.state()
        if not force and now < (st.get("next_at") or 0):
            return
        code = self.code(s)
        if code != self._code:
            self._load()
        a, b = self._range(now)
        self._put_state(tried_at=now)
        try:
            new = parse_rates(await self._fetch_all(a, b, s), a, b)
            if not new:
                raise TariffError(f"Octopus has no rates for {s['product']} in region {s['region']} — check the product code")
        except Exception as e:
            fails = st["fails"] + 1
            msg = str(e)
            if isinstance(e, httpx.HTTPStatusError):
                msg = (f"Octopus doesn't know {s['product']} in region {s['region']} (HTTP 404) — check the product code"
                       if e.response.status_code == 404 else f"Octopus answered HTTP {e.response.status_code}")
            elif isinstance(e, httpx.HTTPError):
                msg = f"Octopus unreachable ({type(e).__name__})"
            log.warning("tariff: fetching %s failed (%d in a row): %s", code, fails, msg)
            self._put_state(error=msg, fails=fails, next_at=now + backoff(fails), code=code)
            return
        self.store.merge(code, new)
        if code == self._code:
            self.rates.update(new)
        tomorrow = datetime.fromtimestamp(now, self.tz).date() + timedelta(days=1)
        complete = all(x in self.rates for x in day_slots(tomorrow, self.tz))
        log.info("tariff: %d rates for %s; tomorrow %s", len(new), code, "complete" if complete else "not yet")
        self._put_state(error=None, fails=0, fetched_at=now, next_at=next_fetch(now, self.tz, complete), code=code)

    async def force_refresh(self) -> None:
        now = self.clock()
        if not self.settings()["enabled"] or now - self._forced_at < REFRESH_MIN_GAP:
            return
        self._forced_at = now
        await self.refresh(force=True)

    # ---- suggestions ----
    async def typical(self, f: dict, dev) -> dict:
        key = (f["id"], f["plug"], f["type"], repr(sorted((f.get("thresholds") or {}).items())))
        hit, now = self._typical.get(key), self.clock()
        if hit and now - hit[0] < TYPICAL_TTL:
            return hit[1]
        out = {"minutes": DEFAULT_MIN[f["type"]], "source": "default", "kwh": None}
        c = {}
        try:
            if dev is not None:
                c = (await self.stats.stats(f, dev, self.layout())).get("cycles") or {}
        except Exception as e:  # HA history down: the run log or the default
            log.info("tariff: stats for %s unavailable: %s", f["id"], e)
            c = {}
        if c.get("avg_min"):
            out.update(minutes=c["avg_min"], source="history", kwh=c.get("kwh_per_cycle"))
        else:
            mins = sorted(self.runlog.store.recent_minutes(f["id"])) if self.runlog else []
            if mins:
                out.update(minutes=round(mins[len(mins) // 2]), source="runs")
        if len(self._typical) > 50:
            self._typical.clear()
        self._typical[key] = (now, out)
        return out

    async def suggestions(self) -> list[dict]:
        now_ms = int(self.clock() * 1000)
        try:
            devs = await self.devices()
        except Exception:
            devs = {}
        out = []
        for f in linked(self.layout()):
            if f["type"] not in SUGGEST:
                continue
            t = await self.typical(f, devs.get(f["plug"]))
            c = self.appliances.cycles.get(self.appliances.key(f)) or {}
            g = {"fid": f["id"], "name": f.get("label") or TYPE_NAME[f["type"]], "type": f["type"], "plug": f["plug"],
                 "minutes": t["minutes"], "source": t["source"], "kwh": t["kwh"], "running": c.get("phase") == "running",
                 "start": None, "end": None, "avg_p": None, "est_p": None, "now_avg_p": None, "saving_p": None,
                 "starts_now": False}
            w = cheapest(self.rates, now_ms, t["minutes"])
            if w:
                g.update(w, starts_now=w["start"] == now_ms)
                nowavg = window_avg(self.rates, now_ms, now_ms + int(t["minutes"] * 60_000))
                g["now_avg_p"] = round(nowavg, 2) if nowavg is not None else None
                if t["kwh"]:
                    g["est_p"] = round(t["kwh"] * w["avg_p"], 1)
                    if nowavg is not None:
                        g["saving_p"] = round(t["kwh"] * (nowavg - w["avg_p"]), 1)
            out.append(g)
        return out

    async def _remind(self) -> None:
        s = self.settings()
        if not (s["enabled"] and s["remind"] and self.rates):
            return
        now, lead = self.clock(), s["remind_lead_min"] * 60
        sent = self.kv.get(REMINDED_KEY, {}) or {}
        for g in await self.suggestions():
            if g["start"] is None or g["starts_now"] or g["running"]:
                continue
            start = g["start"] / 1000
            if not start - lead <= now < start:
                continue
            if g["now_avg_p"] is not None and g["now_avg_p"] - g["avg_p"] < MIN_SAVING:
                continue
            last = sent.get(g["fid"]) or {}
            if last.get("start") == g["start"] or now - (last.get("at") or 0) < REMIND_GAP:
                continue
            sent[g["fid"]] = {"start": g["start"], "at": now}
            self.kv.put(REMINDED_KEY, sent)  # marked before the push: never twice for this window
            if self.quiet_active():
                log.info("tariff: reminder for %s skipped (quiet hours)", g["fid"])
                continue
            hm = lambda t: datetime.fromtimestamp(t / 1000, self.tz).strftime("%H:%M")  # noqa: E731
            body = f"Good time to start the {g['name']}: {hm(g['start'])}–{hm(g['end'])} averages {g['avg_p']:.1f}p/kWh"
            if g["est_p"] is not None:
                body += f" (≈{round(g['est_p'])}p a run" + (f", {round(g['saving_p'])}p less than now" if g["saving_p"] and g["saving_p"] >= 1 else "") + ")"
            body += ". Nothing is switched on automatically."
            log.info("tariff: reminder for %s (%s)", g["fid"], hm(g["start"]))
            await self.notify({"title": f"Cheap electricity from {hm(g['start'])}", "body": body,
                               "tag": f"tariff-{g['fid']}", "url": "/?tariff"})

    # ---- loop ----
    def _items(self) -> dict:
        return {e: build_device(d, self.live.states) for e, d in self.live.devices.items() if d.kind == "plug"}

    async def tick(self) -> None:
        if self.runlog and self.live.devices and self.live.states:
            self.runlog.tick(self._items())
        await self.refresh()
        await self._remind()
        day = datetime.fromtimestamp(self.clock(), self.tz).date()
        if self._pruned != day:
            self._pruned = day
            self.store.prune(int((self.clock() - KEEP_RATES_DAYS * 86400) * 1000))
            if self.runlog:
                self.runlog.prune()

    async def _run(self, every: float, delay: float) -> None:
        await asyncio.sleep(delay)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("tariff check failed: %s", e)
            try:
                await asyncio.wait_for(self.wake.wait(), every)
                await asyncio.sleep(DEBOUNCE)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(CHECK_EVERY, STARTUP_DELAY))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ---- API ----
    async def status(self) -> dict:
        s, st, now = self.settings(), self.state(), self.clock()
        today = datetime.fromtimestamp(now, self.tz).date()
        now_ms = int(now * 1000)
        cur_slot = now_ms - now_ms % SLOT
        cur = {"start": cur_slot, "end": cur_slot + SLOT, "p": self.rates[cur_slot]} if cur_slot in self.rates else None
        nxt = cur_slot + SLOT
        return {"settings": s, "enabled": s["enabled"], "product": s["product"], "name": preset_name(s["product"]),
                "region": s["region"], "region_name": REGIONS[s["region"]], "code": self.code(s),
                "now": now_ms, "current": cur,
                "next": {"start": nxt, "p": self.rates[nxt]} if nxt in self.rates else None,
                "today": day_summary(self.rates, today, self.tz, "Today"),
                "tomorrow": day_summary(self.rates, today + timedelta(days=1), self.tz, "Tomorrow"),
                "fetched_at": int(st["fetched_at"] * 1000) if st.get("fetched_at") else None,
                "next_fetch": int(st["next_at"] * 1000) if s["enabled"] and st.get("next_at") else None,
                "error": st.get("error") if s["enabled"] else None,
                "stale": bool(s["enabled"] and st.get("error") and self.rates),
                "costs": self.active(), "flat_rate_p": flat_tariff(self.layout())["rate_p"],
                "suggestions": await self.suggestions() if s["enabled"] else [],
                "regions": REGIONS, "presets": PRESETS}


def add_routes(app, tariff: Tariff, json_body) -> None:
    """GET /api/tariff, PUT /api/tariff/settings (partial), POST /api/tariff/refresh. Runs the tariff loop (rates,
    reminders, run-log timers) for the app's lifetime by wrapping the app's lifespan."""
    from contextlib import asynccontextmanager

    from fastapi import HTTPException, Request

    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        async with inner(a) as state:
            tariff.start()
            try:
                yield state
            finally:
                await tariff.stop()
    app.router.lifespan_context = lifespan
    app.state.tariff = tariff

    @app.get("/api/tariff")
    async def get_tariff():
        return await tariff.status()

    @app.put("/api/tariff/settings")
    async def put_tariff_settings(request: Request):
        try:
            tariff.put_settings(await json_body(request))
        except TariffError as e:
            raise HTTPException(400, str(e))
        return await tariff.status()

    @app.post("/api/tariff/refresh")
    async def refresh_tariff():
        await tariff.force_refresh()
        return await tariff.status()
