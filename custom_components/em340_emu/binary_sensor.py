"""Status indicators, split into the two independent things that can go
wrong: whether the Wallbox is actively reading registers from us, and
whether the linked P1/HAN entities are updating recently enough that the
fail-safe hasn't had to engage. Kept as two entities rather than one
combined one so a problem on either side is visible at a glance instead
of both collapsing into a single "unhealthy" state.

Deliberately binary_sensor, not light: a light entity always renders with
an on/off toggle control (on the device page, and in any card type),
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
# hiccup while still catching a real stall quickly.
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
    async_add_entities(
        [
            _Em340WallboxActivitySensor(entry, server, device_info),
            _Em340P1UpdatesSensor(entry, monitor, device_info),
        ]
    )


class _Em340BinarySensorBase(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY  # "on" = connected/healthy

    def __init__(self, entry: ConfigEntry, unique_id_suffix: str, device_info: DeviceInfo) -> None:
        self._attr_unique_id = f"{entry.entry_id}_{unique_id_suffix}"
        self._attr_device_info = device_info
        self._signal = signal_update(entry.entry_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self._signal, self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class _Em340WallboxActivitySensor(_Em340BinarySensorBase):
    """On while the Wallbox has read a register from us recently."""

    _attr_name = "Wallbox reading"

    def __init__(self, entry: ConfigEntry, server: ModbusGatewayServer, device_info: DeviceInfo) -> None:
        super().__init__(entry, "wallbox_reading", device_info)
        self._server = server

    @property
    def is_on(self) -> bool:
        elapsed = self._server.seconds_since_last_request()
        return elapsed is not None and elapsed < WALLBOX_ACTIVITY_TIMEOUT


class _Em340P1UpdatesSensor(_Em340BinarySensorBase):
    """On while the linked P1/HAN entities are updating recently enough
    that the fail-safe hasn't had to engage (see FailSafeMonitor)."""

    _attr_name = "P1 data updating"

    def __init__(self, entry: ConfigEntry, monitor: FailSafeMonitor, device_info: DeviceInfo) -> None:
        super().__init__(entry, "p1_data_updating", device_info)
        self._monitor = monitor

    @property
    def is_on(self) -> bool:
        return not self._monitor.engaged
