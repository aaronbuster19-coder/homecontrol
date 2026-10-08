import re
from dataclasses import dataclass, asdict

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

    def to_dict(self) -> dict:
        return asdict(self)


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


def parse_template_output(text: str) -> list[Device]:
    devices: list[Device] = []
    seen: set[str] = set()
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 5:
            continue
        domain, entity_id, name, manufacturer, model = (p.strip() for p in parts)
        kind = classify(domain, entity_id, manufacturer, model)
        if kind and entity_id not in seen:
            seen.add(entity_id)
            devices.append(Device(entity_id, kind, name or entity_id, model))
    return devices
