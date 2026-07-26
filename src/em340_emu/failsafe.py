"""Fail-safe watchdog for a stale P1/HAN live-value source.

Without this, a MeterState just keeps reporting whatever it was last told
forever if updates stop arriving (broker down, reader offline, network
blip) -- a real charger could keep acting on now-untrustworthy "everything's
fine, lots of headroom" numbers. Once `timeout` seconds pass with no
update, this ramps active power import (per phase) up towards a
conservative configured limit and active power export (assumed solar) down
to 0, linearly over `ramp_seconds`, so a Wallbox reading these registers
backs off instead. Recovers immediately, with no ramp, the moment a fresh
update arrives.

Only active_power_import/export are touched; voltage, current, reactive
power and energy totals are left exactly as they were (frozen at their
last known values), matching what was actually asked for -- no invented
consistency adjustments to other fields.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable

from .model import MeterState

DEFAULT_RAMP_SECONDS = 30.0


@dataclass
class FailSafeConfig:
    timeout: float  # seconds without an update before engaging; <= 0 disables the watchdog entirely
    import_limit_w: float  # total W (split evenly across 3 phases) ramped towards once engaged
    ramp_seconds: float = DEFAULT_RAMP_SECONDS
    check_interval: float = 1.0


class FailSafeMonitor:
    def __init__(
        self,
        state: MeterState,
        config: FailSafeConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state = state
        self.config = config
        self._clock = clock
        self._last_update = clock()
        self._engaged = False
        self._engaged_at: float | None = None
        self._ramp_start: dict[str, list[float]] | None = None

    @property
    def enabled(self) -> bool:
        return self.config.timeout > 0

    @property
    def engaged(self) -> bool:
        return self._engaged

    def is_stale(self) -> bool:
        return self.enabled and (self._clock() - self._last_update) >= self.config.timeout

    def touch(self) -> None:
        """Call whenever a live update is applied. Exits fail-safe immediately."""
        self._last_update = self._clock()
        if self._engaged:
            self._engaged = False
            self._engaged_at = None
            self._ramp_start = None

    def tick(self) -> None:
        """Advance the watchdog; call periodically (see run_forever)."""
        if not self.is_stale():
            return

        now = self._clock()
        if not self._engaged:
            self._engaged = True
            self._engaged_at = now
            self._ramp_start = {
                "import": [p.active_power_import for p in self.state.phases()],
                "export": [p.active_power_export for p in self.state.phases()],
            }

        assert self._engaged_at is not None and self._ramp_start is not None
        elapsed = now - self._engaged_at
        progress = 1.0 if self.config.ramp_seconds <= 0 else min(1.0, elapsed / self.config.ramp_seconds)
        target_import = self.config.import_limit_w / 3

        for i, phase in enumerate(self.state.phases()):
            start_import = self._ramp_start["import"][i]
            start_export = self._ramp_start["export"][i]
            phase.active_power_import = start_import + (target_import - start_import) * progress
            phase.active_power_export = start_export + (0.0 - start_export) * progress

    async def run_forever(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(self.config.check_interval)
