import re
import math
from dataclasses import dataclass, asdict, field

PLUG_MODELS = {"P100", "P105", "P110", "P115", "TP11"}
SETTINGS_SUFFIX = re.compile(r"_(auto_off_enabled|auto_update_enabled|led|child_lock)(_\d+)?$")
SENSOR_PREFIX = "binary_sensor.contact_sensor_door"
DIAGNOSTIC_SUFFIX = re.compile(r"_(cloud_connection|battery_low|low_battery|battery|overheated|overloaded|tamper|update)(_\d+)?$")
# Dehumidifiers that only show up as switches: the device's extra feature switches are not the power switch.
DEHUM_FEATURE_SWITCH = re.compile(
    r"_(child_lock|lock|ionizer|anion|sleep|light|led|sound|buzzer|beep|swing|uv|defrost|filter\w*|auto_off\w*|timer\w*)(_\d+)?$")
TANK_WORDS = re.compile(r"tank|full|water|bucket")
NOT_TANK = re.compile(r"defrost|filter|battery")


@dataclass
class Device:
    entity_id: str
    kind: str  # light | plug | valve | sensor | dehumidifier | media (TV / speaker) | person (presence for Auto Away, never on the plan)
    name: str
    model: str
    # role (power|energy_today|battery|battery_low; dehumidifiers also humidity|temperature|tank) -> entity_id
    related: dict[str, str] = field(default_factory=dict)
    ha_name: str = ""     # HA's own name; `name` is the display name (custom name from the layout, else ha_name)
    hidden: bool = False  # hidden from the plan, lists and sheets (layout settings.hidden)

    def __post_init__(self):
        self.ha_name = self.ha_name or self.name

    def to_dict(self) -> dict:
        d = asdict(self)
        del d["related"]
        return d


def _is_tplink(manufacturer: str) -> bool:
    m = manufacturer.lower().replace("-", "").replace(" ", "")
    return m.startswith("tplink")


def _is_tuya(manufacturer: str) -> bool:
    return "tuya" in manufacturer.lower()


def _says_dehum(*texts: str) -> bool:
    return any("dehumid" in (t or "").lower() for t in texts)


def _model_matches(model: str, wanted: str) -> bool:
    # HA sometimes reports e.g. "P110(UK)" or "KE100 EU"
    return model.upper().startswith(wanted)


def classify(domain: str, entity_id: str, manufacturer: str, model: str, name: str = "", device_class: str = "") -> str | None:
    if domain == "person" and entity_id.startswith("person."):
        return "person"
    if domain == "tracker" and entity_id.startswith("device_tracker.") and manufacturer in ("gps", "router"):
        return "tracker"  # becomes a person only when HA has no person entities (see parse_template_output)
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
    if domain == "humidifier":
        # HA's own device_class, or any Tuya humidifier entity (the flat's only one is the dehumidifier), or the name says so
        if device_class.lower() == "dehumidifier" or _is_tuya(manufacturer) or _says_dehum(entity_id, name, model):
            return "dehumidifier"
        return None
    if domain == "media" and entity_id.startswith("media_player."):
        return "media"  # TVs (Samsung via SmartThings / Samsung Smart TV, LG, Android TV, …), speakers, Chromecasts
    if domain == "switch" and _says_dehum(entity_id, name, model) and not DEHUM_FEATURE_SWITCH.search(entity_id):
        return "dehumidifier"  # switch-only dehumidifier (TP-Link switches never get here)
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


def pick_dehum_related(rows: list[Related]) -> dict[str, str]:
    """A dehumidifier's humidity / temperature sensors and its tank-full binary sensor."""
    out: dict[str, str] = {}
    for r in rows:
        dc = r.device_class.lower()
        if r.entity_id.startswith("sensor.") and dc == "humidity" and r.unit == "%":
            out.setdefault("humidity", r.entity_id)
        elif r.entity_id.startswith("sensor.") and dc == "temperature" and r.unit in ("°C", "°F"):
            out.setdefault("temperature", r.entity_id)
    binaries = [r for r in rows if r.entity_id.startswith("binary_sensor.") and r.device_class.lower() not in ("battery", "connectivity")
                and not NOT_TANK.search(f"{r.entity_id} {r.name}".lower())]
    tank = [r for r in binaries if TANK_WORDS.search(f"{r.entity_id} {r.name}".lower())] or \
        [r for r in binaries if r.device_class.lower() in ("problem", "moisture")]
    if tank:
        out["tank"] = tank[0].entity_id
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


def _one_dehumidifier_per_device(devices: list[Device], keys: dict[str, tuple]) -> list[Device]:
    """One HA device = one dehumidifier: its humidifier entity wins over switches; else its main power switch."""
    groups: dict[tuple, list[Device]] = {}
    for d in devices:
        if d.kind == "dehumidifier":
            groups.setdefault(keys[d.entity_id], []).append(d)
    drop = set()
    for ds in groups.values():
        rank = lambda d: (not d.entity_id.startswith("humidifier."),
                          not re.search(r"_(power|switch)(_\d+)?$", d.entity_id), len(d.entity_id), d.entity_id)
        drop |= {d.entity_id for d in sorted(ds, key=rank)[1:]}
    return [d for d in devices if d.entity_id not in drop]


def _maker_model(manufacturer: str, model: str) -> str:
    if manufacturer and model.lower().startswith(manufacturer.split()[0].lower()):
        return model
    return " ".join(filter(None, (manufacturer, model)))


def _one_media_per_device(devices: list[Device], keys: dict[str, tuple], feats: dict[str, int]) -> list[Device]:
    """One HA device = one media player: when two integrations (SmartThings and Samsung Smart TV) put theirs on the same
    device, the one that can do the most wins."""
    groups: dict[tuple, list[Device]] = {}
    for d in devices:
        if d.kind == "media" and len(keys[d.entity_id]) > 1:  # only devices with a name group; nameless ones stay apart
            groups.setdefault(keys[d.entity_id], []).append(d)
    drop = set()
    for ds in groups.values():
        rank = lambda d: (-bin(feats.get(d.entity_id, 0)).count("1"), len(d.entity_id), d.entity_id)
        drop |= {d.entity_id for d in sorted(ds, key=rank)[1:]}
    return [d for d in devices if d.entity_id not in drop]


def _presence(devices: list[Device], sources: dict[str, str]) -> list[Device]:
    """person.* entities are the presence; GPS / router device_trackers count only when there are none."""
    if any(d.kind == "person" for d in devices):
        return [d for d in devices if d.kind != "tracker"]
    for d in devices:
        if d.kind == "tracker":
            d.kind, d.model = "person", "Phone (GPS)" if sources.get(d.entity_id) == "gps" else "Router"
    return devices


def parse_template_output(text: str) -> list[Device]:
    devices: list[Device] = []
    seen: set[str] = set()
    rel: dict[str, list[Related]] = {}
    classes: dict[str, str] = {}
    feats: dict[str, int] = {}
    primaries = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.strip().split("|")]
        if len(parts) == 7 and parts[0] == "rel":
            rel.setdefault(parts[1], []).append(Related(*parts[2:]))
        elif len(parts) == 3 and parts[0] == "dc":
            classes[parts[1]] = parts[2]
        elif len(parts) == 3 and parts[0] == "sf":
            feats[parts[1]] = int(parts[2]) if parts[2].isdigit() else 0
        elif len(parts) == 5:
            primaries.append(parts)
    keys = {}
    for domain, entity_id, name, manufacturer, model in primaries:
        kind = classify(domain, entity_id, manufacturer, model, name, classes.get(entity_id, ""))
        if kind and entity_id not in seen:
            seen.add(entity_id)
            # media: "Samsung QE55Q80A" — the manufacturer helps tell a TV from a speaker (backend/media.py)
            shown = "Person" if kind == "person" else _maker_model(manufacturer, model) if kind == "media" else model
            devices.append(Device(entity_id, kind, name or entity_id, shown))
            keys[entity_id] = (name, manufacturer, model) if name else (entity_id,)
    devices = _one_dehumidifier_per_device(devices, keys)
    devices = _one_media_per_device(devices, keys, feats)
    devices = _presence(devices, {p[1]: p[3] for p in primaries})
    for d in devices:
        d.related = pick_related(rel.get(d.entity_id, []))
        if d.kind == "dehumidifier":
            d.related.update(pick_dehum_related(rel.get(d.entity_id, [])))
    return devices


def apply_names(devices: dict[str, Device], settings: dict | None) -> set[str]:
    """Custom names / hidden flags from the layout settings onto the devices; returns the ids that changed."""
    names, hidden = (settings or {}).get("names") or {}, set((settings or {}).get("hidden") or [])
    changed = set()
    for eid, d in devices.items():
        name, hid = names.get(eid) or d.ha_name, eid in hidden
        if (d.name, d.hidden) != (name, hid):
            d.name, d.hidden = name, hid
            changed.add(eid)
    return changed
