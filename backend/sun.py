"""Sunrise / sunset for a location, from the NOAA solar calculator equations (no network, no dependencies).

Accurate to about a minute at mid latitudes. Times are UTC epoch seconds; None when the sun doesn't rise or set
that day (polar day/night).
"""
import math
from datetime import date, datetime, timezone

ZENITH = 90.833  # sun's centre 50' below the horizon: refraction + its radius (the "official" sunrise)


def _jd(d: date) -> float:
    """Julian day at 00:00 UTC of a calendar date."""
    return d.toordinal() + 1721424.5


def _cent(jd: float) -> float:
    return (jd - 2451545.0) / 36525.0


def _sun(t: float) -> tuple[float, float]:
    """(declination in degrees, equation of time in minutes) at Julian century t."""
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t)) + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
         + math.sin(3 * mr) * 0.000289)
    omega = 125.04 - 1934.136 * t
    lam = l0 + c - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    seconds = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    eps = 23 + (26 + seconds / 60) / 60 + 0.00256 * math.cos(math.radians(omega))
    dec = math.degrees(math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(lam))))
    y = math.tan(math.radians(eps) / 2) ** 2
    l0r = math.radians(l0)
    eq = (y * math.sin(2 * l0r) - 2 * e * math.sin(mr) + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
          - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
    return dec, math.degrees(eq) * 4


def _minutes(rising: bool, jd: float, lat: float, lon: float) -> float | None:
    """Minutes after 00:00 UTC of the day starting at jd (may be <0 or >1440 far from Greenwich)."""
    utc = 720 - 4 * lon  # first guess: solar noon
    for _ in range(3):  # refine with the sun's position at the estimated time
        dec, eq = _sun(_cent(jd + utc / 1440))
        la, de = math.radians(lat), math.radians(dec)
        x = math.cos(math.radians(ZENITH)) / (math.cos(la) * math.cos(de)) - math.tan(la) * math.tan(de)
        if not -1 <= x <= 1:
            return None
        ha = math.degrees(math.acos(x))
        utc = 720 - 4 * (lon + (ha if rising else -ha)) - eq
    return utc


def sun_event(d: date, event: str, lat: float, lon: float) -> float | None:
    """UTC epoch seconds of 'sunrise' or 'sunset' on date d (as seen at lat/lon, east positive)."""
    mins = _minutes(event == "sunrise", _jd(d), lat, lon)
    if mins is None:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() + mins * 60
