"""Applies a flat dict of P1/HAN-style readings onto a MeterState.

Key names mirror the Swedish "H1-port" parameter list (Energiforetagen
branschrekommendation, Bilaga 3 / OBIS codes) so the same vocabulary is used
by the standalone JSON value source, the demo simulator and the Home
Assistant integration's entity-mapping config flow.

+---------------------------------+-------------------+----------------+
| key                             | OBIS              | unit expected  |
+---------------------------------+-------------------+----------------+
| voltage_l1/l2/l3                | 1-32/52/72.7.0    | V              |
| current_l1/l2/l3                | 1-31/51/71.7.0    | A              |
| active_power_import_l1/l2/l3    | 1-21/41/61.7.0    | W              |
| active_power_export_l1/l2/l3    | 1-22/42/62.7.0    | W              |
| reactive_power_import_l1/l2/l3  | 1-23/43/63.7.0    | var            |
| reactive_power_export_l1/l2/l3  | 1-24/44/64.7.0    | var            |
| energy_active_import            | 1-1.8.0           | kWh            |
| energy_active_export            | 1-2.8.0           | kWh            |
| energy_reactive_import          | 1-3.8.0           | kVArh          |
| energy_reactive_export          | 1-4.8.0           | kVArh          |
| frequency                       | (not in Bilaga 3) | Hz             |
+---------------------------------+-------------------+----------------+

All keys are optional; anything missing leaves the current MeterState value
untouched, so a source only needs to publish what it actually measures.
"""
from __future__ import annotations

from .model import MeterState, PhaseState

PHASES = ("l1", "l2", "l3")

PHASE_FIELDS = {
    "voltage": "voltage",
    "current": "current",
    "active_power_import": "active_power_import",
    "active_power_export": "active_power_export",
    "reactive_power_import": "reactive_power_import",
    "reactive_power_export": "reactive_power_export",
}

SYSTEM_FIELDS = {
    "energy_active_import": "energy_active_import",
    "energy_active_export": "energy_active_export",
    "energy_reactive_import": "energy_reactive_import",
    "energy_reactive_export": "energy_reactive_export",
    "frequency": "frequency",
}


def apply_values(state: MeterState, values: dict[str, float]) -> None:
    for phase in PHASES:
        phase_state: PhaseState = getattr(state, phase)
        for key_prefix, attr in PHASE_FIELDS.items():
            key = f"{key_prefix}_{phase}"
            if key in values and values[key] is not None:
                setattr(phase_state, attr, float(values[key]))

    for key, attr in SYSTEM_FIELDS.items():
        if key in values and values[key] is not None:
            setattr(state, attr, float(values[key]))
