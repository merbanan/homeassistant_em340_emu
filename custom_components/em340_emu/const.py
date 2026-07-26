"""Constants and P1/HAN-to-EM340 field mapping metadata."""
from __future__ import annotations

from em340_emu.parameters import PARAMETERS_BY_KEY

DOMAIN = "em340_emu"

CONF_UNIT_ID = "unit_id"
CONF_FRAMING = "framing"
CONF_MAPPING = "mapping"
CONF_FAILSAFE_TIMEOUT = "failsafe_timeout"
CONF_FAILSAFE_IMPORT_LIMIT_W = "failsafe_import_limit_w"

DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 1
DEFAULT_FRAMING = "auto"
FRAMING_OPTIONS = ["auto", "rtu", "tcp"]

# Seconds without an entity update before the fail-safe engages; <= 0 disables it.
DEFAULT_FAILSAFE_TIMEOUT = 60
# Total active power import (W, split across phases) ramped towards -- and
# export ramped towards 0 -- over em340_emu.failsafe.DEFAULT_RAMP_SECONDS
# once the fail-safe engages. Defaults to a common Swedish 16A/phase 3-phase
# main fuse; should be set to the installation's actual rating.
DEFAULT_FAILSAFE_IMPORT_LIMIT_W = 11000

# Config-flow field groups: which em340_emu.parameters keys are collected on
# each mapping step, and in what order. Labels/units/OBIS codes live in
# em340_emu.parameters (the single source of truth, also used by
# `em340-emu view-readings`); this only decides UI grouping/ordering.
MAPPING_STEPS: dict[str, list[str]] = {
    "voltages_currents": ["voltage_l1", "voltage_l2", "voltage_l3", "current_l1", "current_l2", "current_l3"],
    "active_power": [
        "active_power_import_l1", "active_power_export_l1",
        "active_power_import_l2", "active_power_export_l2",
        "active_power_import_l3", "active_power_export_l3",
    ],
    "reactive_power": [
        "reactive_power_import_l1", "reactive_power_export_l1",
        "reactive_power_import_l2", "reactive_power_export_l2",
        "reactive_power_import_l3", "reactive_power_export_l3",
    ],
    "energy": [
        "energy_active_import", "energy_active_export",
        "energy_reactive_import", "energy_reactive_export",
        "frequency",
    ],
}

ALL_MAPPING_FIELDS: list[str] = [key for keys in MAPPING_STEPS.values() for key in keys]

assert set(ALL_MAPPING_FIELDS) <= set(PARAMETERS_BY_KEY), "MAPPING_STEPS references a key not in em340_emu.parameters"

# Normalizes a HA entity's unit_of_measurement onto the unit each MeterState
# field expects (V, A, W, var, kWh, kVArh, Hz). Unrecognized/missing units
# are passed through unscaled.
UNIT_MULTIPLIERS = {
    "w": 1.0,
    "kw": 1000.0,
    "mw": 1_000_000.0,
    "var": 1.0,
    "kvar": 1000.0,
    "a": 1.0,
    "ma": 0.001,
    "v": 1.0,
    "hz": 1.0,
    "kwh": 1.0,
    "wh": 0.001,
    "mwh": 1000.0,
    "kvarh": 1.0,
    "varh": 0.001,
}


def normalize_unit(value: float, unit: str | None) -> float:
    if not unit:
        return value
    multiplier = UNIT_MULTIPLIERS.get(unit.strip().lower())
    return value * multiplier if multiplier is not None else value
