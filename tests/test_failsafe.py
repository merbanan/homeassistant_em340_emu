from em340_emu.failsafe import FailSafeConfig, FailSafeMonitor
from em340_emu.model import MeterState


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _state_with_load() -> MeterState:
    state = MeterState()
    for phase in state.phases():
        phase.active_power_import = 1000.0
        phase.active_power_export = 500.0
    return state


def test_disabled_when_timeout_not_positive():
    state = _state_with_load()
    clock = FakeClock()
    monitor = FailSafeMonitor(state, FailSafeConfig(timeout=0, import_limit_w=9000), clock=clock)
    clock.advance(10_000)
    monitor.tick()
    assert not monitor.engaged
    assert state.l1.active_power_import == 1000.0  # untouched


def test_does_not_engage_before_timeout():
    state = _state_with_load()
    clock = FakeClock()
    monitor = FailSafeMonitor(state, FailSafeConfig(timeout=60, import_limit_w=9000), clock=clock)
    clock.advance(59)
    monitor.tick()
    assert not monitor.engaged
    assert state.l1.active_power_import == 1000.0


def test_engages_and_ramps_linearly_over_ramp_seconds():
    state = _state_with_load()
    clock = FakeClock()
    monitor = FailSafeMonitor(state, FailSafeConfig(timeout=60, import_limit_w=9000, ramp_seconds=30), clock=clock)

    clock.advance(60)  # cross the staleness threshold
    monitor.tick()
    assert monitor.engaged
    # at the instant it engages, progress = 0 -> values unchanged yet
    assert state.l1.active_power_import == 1000.0
    assert state.l1.active_power_export == 500.0

    clock.advance(15)  # halfway through the 30s ramp
    monitor.tick()
    target_per_phase = 9000 / 3
    expected_import = 1000.0 + (target_per_phase - 1000.0) * 0.5
    expected_export = 500.0 + (0.0 - 500.0) * 0.5
    assert state.l1.active_power_import == expected_import
    assert state.l1.active_power_export == expected_export

    clock.advance(15)  # ramp complete
    monitor.tick()
    assert state.l1.active_power_import == target_per_phase
    assert state.l1.active_power_export == 0.0
    assert state.l2.active_power_import == target_per_phase
    assert state.l3.active_power_import == target_per_phase

    clock.advance(100)  # stays at target, doesn't overshoot
    monitor.tick()
    assert state.l1.active_power_import == target_per_phase


def test_touch_recovers_immediately_mid_ramp():
    state = _state_with_load()
    clock = FakeClock()
    monitor = FailSafeMonitor(state, FailSafeConfig(timeout=60, import_limit_w=9000, ramp_seconds=30), clock=clock)

    clock.advance(75)  # engage and get partway through the ramp
    monitor.tick()
    assert monitor.engaged

    monitor.touch()
    assert not monitor.engaged

    # a fresh live value arrives (simulating the real source overwriting it)
    state.l1.active_power_import = 250.0
    clock.advance(1)
    monitor.tick()
    assert not monitor.engaged
    assert state.l1.active_power_import == 250.0  # left alone, not overridden


def test_only_active_power_fields_are_touched():
    state = _state_with_load()
    state.l1.voltage = 231.0
    state.l1.current = 5.0
    state.l1.reactive_power_import = 42.0
    state.energy_active_import = 123.4
    clock = FakeClock()
    monitor = FailSafeMonitor(state, FailSafeConfig(timeout=10, import_limit_w=9000, ramp_seconds=10), clock=clock)

    clock.advance(20)
    monitor.tick()

    assert state.l1.voltage == 231.0
    assert state.l1.current == 5.0
    assert state.l1.reactive_power_import == 42.0
    assert state.energy_active_import == 123.4


def test_re_engages_after_a_second_stale_period():
    state = _state_with_load()
    clock = FakeClock()
    monitor = FailSafeMonitor(state, FailSafeConfig(timeout=10, import_limit_w=9000, ramp_seconds=10), clock=clock)

    clock.advance(10)
    monitor.tick()
    assert monitor.engaged

    monitor.touch()
    state.l1.active_power_import = 700.0
    assert not monitor.engaged

    clock.advance(10)
    monitor.tick()
    assert monitor.engaged
    # ramp restarts from the value at the moment of the second engagement
    assert state.l1.active_power_import == 700.0
