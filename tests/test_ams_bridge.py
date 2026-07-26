"""Field mapping tests using real payload samples captured from a live
AMS-to-MQTT bridge device (topic base "energy-meter")."""
from em340_emu import ams_bridge

REAL_POWER_PAYLOAD = {
    "lv": "ADN9", "id": "", "type": "6534",
    "P": 146, "P1": 47.00, "P2": 49.00, "P3": 50.00,
    "Q": 0,
    "PO": 0, "PO1": 0.00, "PO2": 0.00, "PO3": 0.00,
    "QO": 387,
    "I1": 0.50, "I2": 0.60, "I3": 0.60,
    "U1": 233.20, "U2": 236.00, "U3": 236.00,
    "PF": 1.00, "PF1": 1.00, "PF2": 1.00, "PF3": 1.00,
}

REAL_ENERGY_PAYLOAD = {"tPI": 24208.34, "tPO": 12099.35, "tQI": 2874.71, "tQO": 3775.11, "rtc": 1778110110}

REAL_REALTIME_PAYLOAD = {
    "max": 3.6, "peaks": [0.91, 0.69, 9.23], "threshold": 5,
    "hour": {"use": 0.04, "cost": 0.0, "produced": 0.0, "income": 0.0},
}


def test_translate_power_topic():
    values = ams_bridge.translate("energy-meter/power", REAL_POWER_PAYLOAD)
    assert values["voltage_l1"] == 233.20
    assert values["voltage_l2"] == 236.00
    assert values["voltage_l3"] == 236.00
    assert values["current_l1"] == 0.50
    assert values["active_power_import_l1"] == 47.00
    assert values["active_power_export_l1"] == 0.00


def test_translate_power_splits_system_reactive_power_across_phases():
    values = ams_bridge.translate("energy-meter/power", REAL_POWER_PAYLOAD)
    assert values["reactive_power_export_l1"] == 387 / 3
    assert values["reactive_power_export_l2"] == 387 / 3
    assert values["reactive_power_export_l3"] == 387 / 3
    assert values["reactive_power_import_l1"] == 0.0


def test_translate_energy_topic():
    values = ams_bridge.translate("energy-meter/energy", REAL_ENERGY_PAYLOAD)
    assert values == {
        "energy_active_import": 24208.34,
        "energy_active_export": 12099.35,
        "energy_reactive_import": 2874.71,
        "energy_reactive_export": 3775.11,
    }


def test_translate_ignores_unrelated_topics():
    assert ams_bridge.translate("energy-meter/realtime", REAL_REALTIME_PAYLOAD) == {}
    assert ams_bridge.translate("energy-meter/state", {"rssi": -60, "vcc": 3.3}) == {}


def test_translate_handles_missing_fields_gracefully():
    assert ams_bridge.translate_power({"U1": 230.0}) == {"voltage_l1": 230.0}
    assert ams_bridge.translate_energy({}) == {}
