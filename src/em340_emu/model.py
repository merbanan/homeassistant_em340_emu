"""Live electrical state of the emulated meter.

Field names follow the Swedish "H1-port" (Energiforetagen branschrekommendation
for lokalt kundgranssnitt, Bilaga 3) parameter list, since that is the set of
values a P1/HAN reader can realistically supply: per-phase voltage and current,
per-phase/system active and reactive power split into import ("uttag") and
export ("inmatning"), and cumulative active/reactive import/export energy.

Apparent power and power factor are not part of that source data, so they are
derived here from active/reactive power.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class PhaseState:
    voltage: float = 230.0  # V RMS, line-to-neutral
    current: float = 0.0  # A RMS
    active_power_import: float = 0.0  # W ("uttag"), always >= 0
    active_power_export: float = 0.0  # W ("inmatning"), always >= 0
    reactive_power_import: float = 0.0  # var, always >= 0
    reactive_power_export: float = 0.0  # var, always >= 0

    @property
    def active_power(self) -> float:
        """Net signed active power; positive = import, matching EM340 sign convention."""
        return self.active_power_import - self.active_power_export

    @property
    def reactive_power(self) -> float:
        return self.reactive_power_import - self.reactive_power_export

    @property
    def apparent_power(self) -> float:
        return math.hypot(self.active_power, self.reactive_power)

    @property
    def power_factor(self) -> float:
        s = self.apparent_power
        if s <= 0:
            return 1.0
        return _clamp(self.active_power / s, -1.0, 1.0)


@dataclass
class MeterState:
    """Mutable snapshot of everything the register map reads from.

    Safe to mutate from another thread/coroutine between Modbus requests:
    reads are simple attribute lookups with no cross-field invariants enforced
    at write time, so partial updates never leave the object in a state the
    register encoder cannot serialize.
    """

    l1: PhaseState = field(default_factory=PhaseState)
    l2: PhaseState = field(default_factory=PhaseState)
    l3: PhaseState = field(default_factory=PhaseState)

    frequency: float = 50.0  # Hz
    phase_sequence: int = 0  # 0 = L1-L2-L3, 1 = L1-L3-L2

    # Cumulative absolute meter readings ("matarstallning"), kWh / kvarh.
    # These are expected to be passed straight through from the source meter,
    # not integrated locally.
    energy_active_import: float = 0.0
    energy_active_export: float = 0.0
    energy_reactive_import: float = 0.0
    energy_reactive_export: float = 0.0

    # Running peak of |system active power|, tracked as registers are read.
    demand_peak_w: float = 0.0

    def phases(self) -> tuple[PhaseState, PhaseState, PhaseState]:
        return (self.l1, self.l2, self.l3)

    @property
    def voltage_ln_avg(self) -> float:
        return sum(p.voltage for p in self.phases()) / 3

    @property
    def voltage_ll_avg(self) -> float:
        # No line-line source data is available from a P1/HAN feed; approximate
        # from the phase-neutral average assuming a balanced 3-phase system.
        return self.voltage_ln_avg * math.sqrt(3)

    @property
    def active_power_total(self) -> float:
        return sum(p.active_power for p in self.phases())

    @property
    def reactive_power_total(self) -> float:
        return sum(p.reactive_power for p in self.phases())

    @property
    def apparent_power_total(self) -> float:
        return math.hypot(self.active_power_total, self.reactive_power_total)

    @property
    def power_factor_total(self) -> float:
        s = self.apparent_power_total
        if s <= 0:
            return 1.0
        return _clamp(self.active_power_total / s, -1.0, 1.0)

    def sample_demand_peak(self) -> float:
        """Update and return the running demand peak.

        Called from the register getter itself (a read-time side effect)
        so the peak tracks reality without needing a separate polling task.
        """
        self.demand_peak_w = max(self.demand_peak_w, abs(self.active_power_total))
        return self.demand_peak_w
