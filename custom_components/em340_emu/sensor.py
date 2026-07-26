"""Sensor entities exposing the emulated EM340's live meter state.

These exist purely for observability -- so the values being served out to
the Wallbox over Modbus (and the activity feeding them) can be watched
from a normal Home Assistant dashboard, instead of only via debug logs or
a Modbus client of your own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from em340_emu import ModbusGatewayServer
from em340_emu.model import MeterState

from .const import DOMAIN, signal_update


@dataclass(frozen=True)
class _SensorSpec:
    key: str
    name: str
    unit: str | None
    device_class: SensorDeviceClass | None
    state_class: SensorStateClass | None
    getter: Callable[[MeterState], float]
    precision: int = 2
    diagnostic: bool = False


def _phase_specs(letter: str) -> list[_SensorSpec]:
    upper = letter.upper()
    return [
        _SensorSpec(
            f"voltage_{letter}", f"Voltage {upper}", UnitOfElectricPotential.VOLT,
            SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT,
            lambda s, a=letter: getattr(s, a).voltage,
        ),
        _SensorSpec(
            f"current_{letter}", f"Current {upper}", UnitOfElectricCurrent.AMPERE,
            SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT,
            lambda s, a=letter: getattr(s, a).current,
        ),
        _SensorSpec(
            f"active_power_{letter}", f"Active power {upper}", UnitOfPower.WATT,
            SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT,
            lambda s, a=letter: getattr(s, a).active_power,
        ),
        _SensorSpec(
            f"reactive_power_{letter}", f"Reactive power {upper}", "var",
            None, SensorStateClass.MEASUREMENT,
            lambda s, a=letter: getattr(s, a).reactive_power,
        ),
        _SensorSpec(
            f"power_factor_{letter}", f"Power factor {upper}", None,
            SensorDeviceClass.POWER_FACTOR, SensorStateClass.MEASUREMENT,
            lambda s, a=letter: getattr(s, a).power_factor, precision=3,
        ),
    ]


METER_SENSOR_SPECS: list[_SensorSpec] = [
    *_phase_specs("l1"),
    *_phase_specs("l2"),
    *_phase_specs("l3"),
    _SensorSpec(
        "active_power_total", "Active power total", UnitOfPower.WATT,
        SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, lambda s: s.active_power_total,
    ),
    _SensorSpec(
        "reactive_power_total", "Reactive power total", "var",
        None, SensorStateClass.MEASUREMENT, lambda s: s.reactive_power_total,
    ),
    _SensorSpec(
        "frequency", "Frequency", UnitOfFrequency.HERTZ,
        SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT, lambda s: s.frequency,
    ),
    _SensorSpec(
        "energy_active_import", "Active energy import", UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, lambda s: s.energy_active_import,
    ),
    _SensorSpec(
        "energy_active_export", "Active energy export", UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, lambda s: s.energy_active_export,
    ),
    _SensorSpec(
        "energy_reactive_import", "Reactive energy import", "kvarh",
        None, SensorStateClass.TOTAL_INCREASING, lambda s: s.energy_reactive_import,
    ),
    _SensorSpec(
        "energy_reactive_export", "Reactive energy export", "kvarh",
        None, SensorStateClass.TOTAL_INCREASING, lambda s: s.energy_reactive_export,
    ),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    state: MeterState = runtime["state"]
    server: ModbusGatewayServer = runtime["server"]
    entity_update_counter = runtime["entity_update_counter"]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Carlo Gavazzi (emulated)",
        model="EM340",
    )

    entities: list[SensorEntity] = [
        _Em340MeterSensor(entry, state, spec, device_info) for spec in METER_SENSOR_SPECS
    ]
    entities.append(
        _Em340CounterSensor(
            entry, "modbus_requests", "Modbus requests from gateway", device_info,
            lambda: server.request_count,
        )
    )
    entities.append(
        _Em340CounterSensor(
            entry, "modbus_responses", "Modbus responses answered", device_info,
            lambda: server.response_count,
        )
    )
    entities.append(
        _Em340CounterSensor(
            entry, "entity_updates", "Entity value updates received", device_info,
            lambda: entity_update_counter.value,
        )
    )
    async_add_entities(entities)


class _Em340SensorBase(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str, name: str, device_info: DeviceInfo) -> None:
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = device_info
        self._signal = signal_update(entry.entry_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self._signal, self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class _Em340MeterSensor(_Em340SensorBase):
    def __init__(self, entry: ConfigEntry, state: MeterState, spec: _SensorSpec, device_info: DeviceInfo) -> None:
        super().__init__(entry, spec.key, spec.name, device_info)
        self._state_obj = state
        self._spec = spec
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = spec.device_class
        self._attr_state_class = spec.state_class

    @property
    def native_value(self) -> float:
        return round(self._spec.getter(self._state_obj), self._spec.precision)


class _Em340CounterSensor(_Em340SensorBase):
    """Diagnostic counter: how much traffic/activity this entry has seen,
    for confirming the Wallbox is actually polling and the P1/HAN mapping
    is actually receiving updates -- without needing to enable debug logs.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, entry: ConfigEntry, key: str, name: str, device_info: DeviceInfo, getter: Callable[[], int]) -> None:
        super().__init__(entry, key, name, device_info)
        self._getter = getter

    @property
    def native_value(self) -> int:
        return self._getter()
