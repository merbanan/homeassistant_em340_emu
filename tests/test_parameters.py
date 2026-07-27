"""Keeps the canonical parameter list in sync with sources.py's vocabulary
and with the Home Assistant integration's config-flow field groups.

const.py is loaded directly from its file, bypassing
custom_components/em340_emu/__init__.py (which imports homeassistant) --
const.py itself has no such dependency, and this test suite is meant to
stay usable without Home Assistant installed.
"""
import importlib.util
from pathlib import Path

from em340_emu.parameters import PARAMETERS
from em340_emu.sources import PHASE_FIELDS, PHASES, SYSTEM_FIELDS

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_ha_const():
    path = REPO_ROOT / "custom_components" / "em340_emu" / "const.py"
    spec = importlib.util.spec_from_file_location("em340_emu_ha_const", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parameters_match_sources_vocabulary():
    expected = {f"{prefix}_{phase}" for prefix in PHASE_FIELDS for phase in PHASES}
    expected |= set(SYSTEM_FIELDS)
    got = {p.key for p in PARAMETERS}
    assert got == expected


def test_parameters_match_ha_config_flow_fields():
    # ALL_MAPPING_FIELDS is intentionally a *subset* of the canonical
    # parameter list (see MAPPING_STEPS's comment in const.py): trimmed to
    # exactly what two live Wallbox captures showed this unit actually
    # reads. Pin down exactly what's excluded, so an accidental future
    # removal (or an unnoticed restoration) of anything else gets caught.
    ha_const = _load_ha_const()
    all_params = {p.key for p in PARAMETERS}
    mapped = set(ha_const.ALL_MAPPING_FIELDS)
    assert mapped <= all_params
    intentionally_excluded = {
        "reactive_power_import_l1", "reactive_power_export_l1",
        "reactive_power_import_l2", "reactive_power_export_l2",
        "reactive_power_import_l3", "reactive_power_export_l3",
        "energy_reactive_import", "energy_reactive_export",
        "frequency",
    }
    assert all_params - mapped == intentionally_excluded
