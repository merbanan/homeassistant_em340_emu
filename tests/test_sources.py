from em340_emu.model import MeterState
from em340_emu.sources import apply_values


def test_apply_values_sets_known_keys():
    state = MeterState()
    apply_values(
        state,
        {
            "voltage_l1": 231.4,
            "current_l1": 6.2,
            "active_power_import_l1": 1400.0,
            "active_power_export_l2": 300.0,
            "energy_active_import": 1000.5,
            "energy_active_export": 2.0,
            "frequency": 49.98,
        },
    )
    assert state.l1.voltage == 231.4
    assert state.l1.current == 6.2
    assert state.l1.active_power_import == 1400.0
    assert state.l2.active_power_export == 300.0
    assert state.energy_active_import == 1000.5
    assert state.energy_active_export == 2.0
    assert state.frequency == 49.98


def test_apply_values_ignores_unknown_and_none():
    state = MeterState()
    state.l1.voltage = 230.0
    apply_values(state, {"voltage_l1": None, "not_a_real_key": 42})
    assert state.l1.voltage == 230.0
