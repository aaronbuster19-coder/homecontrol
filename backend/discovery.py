import re
import math
from dataclasses import dataclass, asdict, field

PLUG_MODELS = {"P100", "P105", "P110", "P115", "TP11"}
SETTINGS_SUFFIX = re.compile(r"_(auto_off_enabled|auto_update_enabled|led|child_lock)(_\d+)?$")
SENSOR_PREFIX = "binary_sensor.contact_sensor_door"
DIAGNOSTIC_SUFFIX = re.compile(r"_(cloud_connection|battery_low|low_battery|battery|overheated|overloaded|tamper|update)(_\d+)?$")


@dataclass
class Device:
    entity_id: str
    kind: str  # light | plug | valve | sensor
    name: str
    model: str
    related: dict[str, str] = field(default_factory=dict)  # role (power|energy_today|battery|battery_low) -> entity_id

    def to_dict(self) -> dict:
        d = asdict(self)
        del d["related"]
        return d


def _is_tplink(manufacturer: str) -> bool:
    m = manufacturer.lower().replace("-", "").replace(" ", "")
    return m.startswith("tplink")


def _model_matches(model: str, wanted: str) -> bool:
    # HA sometimes reports e.g. "P110(UK)" or "KE100 EU"
    return model.upper().startswith(wanted)


def classify(domain: str, entity_id: str, manufacturer: str, model: str) -> str | None:
    if domain == "light" and _is_tplink(manufacturer):
        return "light"
    if domain == "switch" and _is_tplink(manufacturer):
        if SETTINGS_SUFFIX.search(entity_id):
            return None
        if any(_model_matches(model, m) for m in PLUG_MODELS):
            return "plug"
        return None
    if domain == "climate" and _model_matches(model, "KE100"):
        return "valve"
    if domain == "binary" and _model_matches(model, "T110") and entity_id.startswith(SENSOR_PREFIX) \
            and not DIAGNOSTIC_SUFFIX.search(entity_id):
        return "sensor"
    return None


@dataclass
class Related:
    entity_id: str
    device_class: str
    unit: str
    state_class: str
    name: str


def pick_related(rows: list[Related]) -> dict[str, str]:
    """Choose the power / today's-energy / battery entities of one device from its sensors."""
    out: dict[str, str] = {}
    for r in rows:
        dc, sensor = r.device_class.lower(), r.entity_id.startswith("sensor.")
        if sensor and dc == "power" and r.unit in ("W", "kW"):
            out.setdefault("power", r.entity_id)
        elif sensor and dc == "energy" and r.unit in ("kWh", "Wh") and \
                ("today" in r.entity_id.lower() or "today" in r.name.lower()):
            out.setdefault("energy_today", r.entity_id)
        elif sensor and dc == "battery" and r.unit == "%":
            out.setdefault("battery", r.entity_id)
        elif r.entity_id.startswith("binary_sensor.") and dc == "battery":
            out.setdefault("battery_low", r.entity_id)
    return out


def number(state: dict | None) -> float | None:
    """Numeric value of an HA state, scaled to W / kWh; None if unavailable or non-numeric."""
    if not state:
        return None
    try:
        v = float(state.get("state"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    unit = (state.get("attributes") or {}).get("unit_of_measurement")
    return v * 1000 if unit == "kW" else v / 1000 if unit == "Wh" else v


def parse_template_output(text: str) -> list[Device]:
    devices: list[Device] = []
    seen: set[str] = set()
    rel: dict[str, list[Related]] = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.strip().split("|")]
        if len(parts) == 7 and parts[0] == "rel":
            rel.setdefault(parts[1], []).append(Related(*parts[2:]))
            continue
        if len(parts) != 5:
            continue
        domain, entity_id, name, manufacturer, model = parts
        kind = classify(domain, entity_id, manufacturer, model)
        if kind and entity_id not in seen:
            seen.add(entity_id)
            devices.append(Device(entity_id, kind, name or entity_id, model))
    for d in devices:
        d.related = pick_related(rel.get(d.entity_id, []))
    return devices
