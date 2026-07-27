"""Status light: green when the Wallbox is actively reading register
values from this emulator *and* the linked P1/HAN entities are updating
recently enough that the fail-safe hasn't had to engage; red otherwise.

Implemented as an always-on light entity (rather than a binary_sensor)
specifically so it renders as a colored dot on a dashboard -- color is
what communicates health here, not on/off.
"""
from __future__ import annotations

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from em340_emu import ModbusGatewayServer
from em340_emu.failsafe import FailSafeMonitor

from .const import DOMAIN, signal_update

GREEN = (0, 255, 0)
RED = (255, 0, 0)

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
    async_add_entities([_Em340HealthLight(entry, server, monitor, device_info)])


class _Em340HealthLight(LightEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Data flow healthy"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_is_on = True  # always lit; the color is what communicates health

    def __init__(
        self, entry: ConfigEntry, server: ModbusGatewayServer, monitor: FailSafeMonitor, device_info: DeviceInfo
    ) -> None:
        self._server = server
        self._monitor = monitor
        self._attr_unique_id = f"{entry.entry_id}_health_light"
        self._attr_device_info = device_info
        self._signal = signal_update(entry.entry_id)

    @property
    def rgb_color(self) -> tuple[int, int, int]:
        return GREEN if self._is_healthy() else RED

    def _is_healthy(self) -> bool:
        elapsed = self._server.seconds_since_last_request()
        wallbox_active = elapsed is not None and elapsed < WALLBOX_ACTIVITY_TIMEOUT
        return wallbox_active and not self._monitor.engaged

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self._signal, self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        pass  # read-only status indicator; state is fully computed

    async def async_turn_off(self, **kwargs) -> None:
        pass
