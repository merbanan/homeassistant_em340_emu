"""Status indicator: on ("connected"/healthy) while the Wallbox is
actively reading register values from this emulator *and* the linked
P1/HAN entities are updating recently enough that the fail-safe hasn't
had to engage; off otherwise.

Deliberately a binary_sensor, not a light: a light entity always renders
with an on/off toggle control (on the device page, and in any card type),
which makes no sense for a fully-computed, read-only status -- there's
nothing to turn on or off by hand. binary_sensor has no such service at
all, so Home Assistant never shows a toggle for it anywhere.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from em340_emu import ModbusGatewayServer
from em340_emu.failsafe import FailSafeMonitor

from .const import DOMAIN, signal_update

# How long since the Wallbox last read a register from us before it's no
# longer considered "actively polling". Live captures against a real
# Wallbox showed a steady-state cycle every ~0.4-0.9s (see README
# "Confirmed via live capture"), so 10s comfortably absorbs any brief
# hiccup while still catching a real stall quickly. "The P1 meter has
# updated its values" reuses the fail-safe's own (separately configurable)
# staleness check instead of a second timeout, since that's exactly what
# it already means for FailSafeMonitor.engaged to be True.
WALLBOX_ACTIVITY_TIMEOUT = 10.0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    server: ModbusGatewayServer = runtime["server"]
    monitor: FailSafeMonitor = runtime["monitor"]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Carlo Gavazzi (emulated)",
        model="EM340",
    )
    async_add_entities([_Em340HealthSensor(entry, server, monitor, device_info)])


class _Em340HealthSensor(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Data flow healthy"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY  # "on" = connected/healthy

    def __init__(
        self, entry: ConfigEntry, server: ModbusGatewayServer, monitor: FailSafeMonitor, device_info: DeviceInfo
    ) -> None:
        self._server = server
        self._monitor = monitor
        self._attr_unique_id = f"{entry.entry_id}_data_flow_healthy"
        self._attr_device_info = device_info
        self._signal = signal_update(entry.entry_id)

    @property
    def is_on(self) -> bool:
        elapsed = self._server.seconds_since_last_request()
        wallbox_active = elapsed is not None and elapsed < WALLBOX_ACTIVITY_TIMEOUT
        return wallbox_active and not self._monitor.engaged

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self._signal, self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
