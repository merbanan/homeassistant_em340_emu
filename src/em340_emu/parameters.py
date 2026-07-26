"""Canonical description of every P1/HAN-port parameter this emulator
understands, keyed by the same field names sources.apply_values() expects.

This is the single source of truth for the parameter list: it backs
`em340-emu view-readings`, and custom_components/em340_emu/const.py's
config-flow field groups are checked against it in tests/test_parameters.py
so the two can't silently drift apart.

Fields/units/OBIS codes are taken from Bilaga 3 ("Forslag pa
datarepresentation") of Energiforetagen's "Branschrekommendation for lokalt
kundgranssnitt for elmatare" (v2.0, 2019-12-03), a Swedish document; labels
here are English translations of that document's Swedish parameter names
("Uttag" = import/withdrawal from the grid, "Inmatning" = export/feed-in).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Parameter:
    key: str
    unit: str
    label: str
    obis: str | None


PARAMETERS: list[Parameter] = [
    Parameter("voltage_l1", "V", "Phase voltage L1", "1-32.7.0"),
    Parameter("voltage_l2", "V", "Phase voltage L2", "1-52.7.0"),
    Parameter("voltage_l3", "V", "Phase voltage L3", "1-72.7.0"),
    Parameter("current_l1", "A", "Phase current L1", "1-31.7.0"),
    Parameter("current_l2", "A", "Phase current L2", "1-51.7.0"),
    Parameter("current_l3", "A", "Phase current L3", "1-71.7.0"),
    Parameter("active_power_import_l1", "W", "Active power L1 import", "1-21.7.0"),
    Parameter("active_power_export_l1", "W", "Active power L1 export", "1-22.7.0"),
    Parameter("active_power_import_l2", "W", "Active power L2 import", "1-41.7.0"),
    Parameter("active_power_export_l2", "W", "Active power L2 export", "1-42.7.0"),
    Parameter("active_power_import_l3", "W", "Active power L3 import", "1-61.7.0"),
    Parameter("active_power_export_l3", "W", "Active power L3 export", "1-62.7.0"),
    Parameter("reactive_power_import_l1", "var", "Reactive power L1 import", "1-23.7.0"),
    Parameter("reactive_power_export_l1", "var", "Reactive power L1 export", "1-24.7.0"),
    Parameter("reactive_power_import_l2", "var", "Reactive power L2 import", "1-43.7.0"),
    Parameter("reactive_power_export_l2", "var", "Reactive power L2 export", "1-44.7.0"),
    Parameter("reactive_power_import_l3", "var", "Reactive power L3 import", "1-63.7.0"),
    Parameter("reactive_power_export_l3", "var", "Reactive power L3 export", "1-64.7.0"),
    Parameter("energy_active_import", "kWh", "Cumulative active energy import", "1-1.8.0"),
    Parameter("energy_active_export", "kWh", "Cumulative active energy export", "1-2.8.0"),
    Parameter("energy_reactive_import", "kVArh", "Cumulative reactive energy import", "1-3.8.0"),
    Parameter("energy_reactive_export", "kVArh", "Cumulative reactive energy export", "1-4.8.0"),
    Parameter("frequency", "Hz", "Grid frequency", None),
]

PARAMETERS_BY_KEY: dict[str, Parameter] = {p.key: p for p in PARAMETERS}
