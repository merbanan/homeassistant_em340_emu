"""EM340 Modbus emulator, fed by mapped P1/HAN-port sensor entities."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event

from em340_emu import MeterState, ModbusGatewayServer
from em340_emu.failsafe import FailSafeConfig, FailSafeMonitor
from em340_emu.sources import apply_values

from .const import (
    CONF_FAILSAFE_IMPORT_LIMIT_W,
    CONF_FAILSAFE_TIMEOUT,
    CONF_FRAMING,
    CONF_MAPPING,
    CONF_RETRY_INTERVAL,
    CONF_UNIT_ID,
    DEFAULT_FAILSAFE_IMPORT_LIMIT_W,
    DEFAULT_FAILSAFE_TIMEOUT,
    DEFAULT_RETRY_INTERVAL,
    DOMAIN,
    normalize_unit,
    signal_update,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "light"]

# How often sensor entities refresh from the shared MeterState/counters even
# absent a fresh entity update -- e.g. so the fail-safe's ramp is visible
# live, not just when a mapped P1/HAN entity happens to change.
SENSOR_REFRESH_INTERVAL = 2.0


class _Counter:
    """Plain-int counters are immutable, so a nested closure can't mutate
    one in the enclosing scope without `nonlocal` -- this small mutable
    holder lets sensor.py read the live value by reference instead."""

    value: int = 0

    def increment(self) -> None:
        self.value += 1


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = {**entry.data, **entry.options}
    mapping: dict[str, str] = data.get(CONF_MAPPING, {})

    state = MeterState()
    server = ModbusGatewayServer(
        state=state,
        unit_id=data[CONF_UNIT_ID],
        host=data[CONF_HOST],
        port=data[CONF_PORT],
        framing=data[CONF_FRAMING],
    )
    # Every gateway actually used with this project has turned out to be a
    # TCP server in its own right, so we always dial out to it rather than
    # listen (see server.py's module docstring) -- retrying forever (and
    # reconnecting if it later drops) in the background; connect_retry is
    # intentionally left at its default of None (no give-up) since a
    # persistent integration should never stop trying.
    connection_task = asyncio.ensure_future(
        server.serve_as_client(retry_interval=data.get(CONF_RETRY_INTERVAL, DEFAULT_RETRY_INTERVAL))
    )
    _LOGGER.info(
        "EM340 emulator connecting out to %s:%s (unit id %s, framing=%s)",
        data[CONF_HOST], data[CONF_PORT], data[CONF_UNIT_ID], data[CONF_FRAMING],
    )

    monitor = FailSafeMonitor(
        state,
        FailSafeConfig(
            timeout=data.get(CONF_FAILSAFE_TIMEOUT, DEFAULT_FAILSAFE_TIMEOUT),
            import_limit_w=data.get(CONF_FAILSAFE_IMPORT_LIMIT_W, DEFAULT_FAILSAFE_IMPORT_LIMIT_W),
        ),
    )
    if monitor.enabled:
        _LOGGER.info(
            "fail-safe armed: %.0fs without an update ramps active power import to %.0fW (export to 0) over %.0fs",
            monitor.config.timeout, monitor.config.import_limit_w, monitor.config.ramp_seconds,
        )
    monitor_task = asyncio.ensure_future(monitor.run_forever())

    entity_to_field = {entity_id: field for field, entity_id in mapping.items()}
    entity_update_counter = _Counter()
    signal = signal_update(entry.entry_id)

    def _apply_entity_state(field: str, value_str: str, unit: str | None) -> None:
        try:
            value = float(value_str)
        except (TypeError, ValueError):
            return
        apply_values(state, {field: normalize_unit(value, unit)})
        monitor.touch()
        entity_update_counter.increment()
        _LOGGER.debug("entity update #%d: %s = %s", entity_update_counter.value, field, value)
        async_dispatcher_send(hass, signal)

    @callback
    def _handle_entity_change(event: Event) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]
        field = entity_to_field.get(entity_id)
        if field is None or new_state is None:
            return
        _apply_entity_state(field, new_state.state, new_state.attributes.get("unit_of_measurement"))

    unsub = None
    if mapping:
        unsub = async_track_state_change_event(hass, list(mapping.values()), _handle_entity_change)
        # Seed from whatever these entities already report, so the emulator
        # doesn't sit at all-zero until the next upstream state change.
        for field, entity_id in mapping.items():
            current = hass.states.get(entity_id)
            if current is not None and current.state not in (None, "unknown", "unavailable"):
                _apply_entity_state(field, current.state, current.attributes.get("unit_of_measurement"))
    else:
        _LOGGER.warning("EM340 emulator entry %s has no entities mapped; all values will read 0", entry.entry_id)

    async def _periodic_refresh() -> None:
        # Keeps sensors (voltage/power/etc., and the fail-safe's ramp) live
        # even between entity updates -- e.g. while ramping towards the
        # fail-safe limit, or just to reflect the Modbus request counters.
        while True:
            await asyncio.sleep(SENSOR_REFRESH_INTERVAL)
            async_dispatcher_send(hass, signal)

    refresh_task = asyncio.ensure_future(_periodic_refresh())

    async def _async_stop(_event: Event | None = None) -> None:
        monitor_task.cancel()
        connection_task.cancel()
        refresh_task.cancel()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "server": server,
        "state": state,
        "monitor": monitor,
        "monitor_task": monitor_task,
        "connection_task": connection_task,
        "refresh_task": refresh_task,
        "entity_update_counter": entity_update_counter,
        "unsub": unsub,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if runtime is None:
        return True
    if runtime["unsub"] is not None:
        runtime["unsub"]()
    runtime["monitor_task"].cancel()
    runtime["connection_task"].cancel()
    runtime["refresh_task"].cancel()
    return True
