from backend.discovery import classify, parse_template_output
from backend.tests.conftest import TEMPLATE_OUTPUT


def test_parse_classifies_expected_devices():
    kinds = {d.entity_id: d.kind for d in parse_template_output(TEMPLATE_OUTPUT)}
    assert kinds == {
        "light.kitchen_1": "light",
        "light.strip": "light",
        "switch.fan": "plug",
        "switch.kettle": "plug",
        "climate.lounge_valve": "valve",
        "binary_sensor.contact_sensor_door": "sensor",
    }


def test_settings_switches_excluded():
    for eid in ("switch.fan_auto_off_enabled", "switch.fan_auto_off_enabled_2",
                "switch.fan_auto_update_enabled", "switch.fan_led", "switch.fan_led_3",
                "switch.fan_child_lock"):
        assert classify("switch", eid, "TP-Link", "P110") is None


def test_plug_models():
    assert classify("switch", "switch.a", "TP-Link", "TP11") == "plug"
    assert classify("switch", "switch.a", "TP-Link", "P110") == "plug"
    assert classify("switch", "switch.a", "TP-Link", "KH100") is None
    assert classify("switch", "switch.a", "Tuya", "P110") is None


def test_valves_and_sensors():
    assert classify("climate", "climate.x", "TP-Link", "KE100") == "valve"
    assert classify("climate", "climate.x", "Tuya", "Dehum") is None
    assert classify("binary", "binary_sensor.contact_sensor_door_2", "TP-Link", "T110") == "sensor"
    assert classify("binary", "binary_sensor.motion", "TP-Link", "T110") is None
    assert classify("binary", "binary_sensor.contact_sensor_door_bedroom", "TP-Link", "T110") == "sensor"
    assert classify("binary", "binary_sensor.contact_sensor_door_2_cloud_connection", "TP-Link", "T110") is None


def test_malformed_lines_ignored():
    assert parse_template_output("garbage\n\nlight|only|three") == []
