"""Dehumidifier: device fields, humidity/mode validation and the tank-full push.

Supported HA shapes (classified in discovery.py): a humidifier.* entity (device_class dehumidifier, any Tuya one, or
"dehumid" in its name/model), or a switch.* whose device name/model/entity id says "dehumid". Related sensors on the
same HA device give current humidity, temperature and the tank-full binary sensor.
"""
import logging
import math

from .discovery import Device, number

log = logging.getLogger("homecontrol.dehumidifier")
HA_MIN_HUMIDITY, HA_MAX_HUMIDITY = 0, 100  # HA's defaults when the entity doesn't say


class DehumError(ValueError):
    pass


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return v


def is_humidifier(d: Device) -> bool:
    return d.entity_id.startswith("humidifier.")


def fields(d: Device, attrs: dict, states: dict[str, dict]) -> dict:
    """Extra keys of a dehumidifier in /api/devices and SSE."""
    hum = is_humidifier(d)
    out = {"control": "humidifier" if hum else "switch", "current_humidity": None, "current_temperature": None,
           "target_humidity": None, "min_humidity": None, "max_humidity": None, "mode": None, "available_modes": [],
           "action": None}
    cur = _num(attrs.get("current_humidity")) if hum else None
    if cur is None:
        cur = number(states.get(d.related.get("humidity", "")))
    if cur is not None:
        out["current_humidity"] = round(cur, 1)
    temp = number(states.get(d.related.get("temperature", "")))
    if temp is not None:
        out["current_temperature"] = round(temp, 1)
    if hum:
        out["target_humidity"] = _num(attrs.get("humidity"))
        out["min_humidity"] = _num(attrs.get("min_humidity")) if _num(attrs.get("min_humidity")) is not None else HA_MIN_HUMIDITY
        out["max_humidity"] = _num(attrs.get("max_humidity")) if _num(attrs.get("max_humidity")) is not None else HA_MAX_HUMIDITY
        modes = attrs.get("available_modes")
        out["available_modes"] = [m for m in modes if isinstance(m, str)] if isinstance(modes, list) else []
        out["mode"] = attrs.get("mode") if isinstance(attrs.get("mode"), str) else None
        out["action"] = attrs.get("action") if isinstance(attrs.get("action"), str) else None
    tank = (states.get(d.related.get("tank", "")) or {}).get("state")
    if tank in ("on", "off"):
        out["tank_full"] = tank == "on"
    return out


def check_humidity(body, item: dict) -> int:
    if item.get("control") != "humidifier":
        raise DehumError("this dehumidifier can only be switched on and off")
    h = body.get("humidity") if isinstance(body, dict) else None
    if _num(h) is None or h != int(h):
        raise DehumError("humidity must be a whole number")
    lo = item.get("min_humidity") if item.get("min_humidity") is not None else HA_MIN_HUMIDITY
    hi = item.get("max_humidity") if item.get("max_humidity") is not None else HA_MAX_HUMIDITY
    if not lo <= h <= hi:
        raise DehumError(f"humidity must be {lo:g}–{hi:g} %")
    return int(h)


def check_mode(body, item: dict) -> str:
    modes = item.get("available_modes") or []
    if not modes:
        raise DehumError("this dehumidifier has no modes")
    m = body.get("mode") if isinstance(body, dict) else None
    if not isinstance(m, str) or m not in modes:
        raise DehumError(f"mode must be one of: {', '.join(modes)}")
    return m


class TankAlert:
    """One push when a dehumidifier's tank is full; reset once it reads not-full again. Marks survive restarts."""

    def __init__(self, store, settings, notify):
        self.store, self.settings, self.notify = store, settings, notify
        self.marks: set[str] = set(store.get("tank_full", []) or [])

    def observe(self, item: dict) -> bool:
        """Should the automation loop wake up for this device update?"""
        return item.get("kind") == "dehumidifier" and item.get("tank_full") is True and item["entity_id"] not in self.marks

    async def tick(self, items: list[dict]) -> None:
        s, changed = self.settings(), False
        for it in items:
            eid, full = it["entity_id"], it.get("tank_full")
            if full is True and eid not in self.marks and s.get("dehumidifier_tank", True):
                self.marks.add(eid)  # marked first: a failing push service can't make this repeat
                changed = True
                log.info("tank full: %s", eid)
                await self.notify({"title": f"{it['name']}: tank full", "body": "Empty the water tank — it has stopped drying.",
                                   "tag": f"tank-{eid}", "url": "/"})
            elif full is False and eid in self.marks:
                self.marks.discard(eid)
                changed = True
        if changed:
            self.store.put("tank_full", sorted(self.marks))
