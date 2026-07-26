"""Config flow: network settings, then P1/HAN entity -> EM340 field mapping."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CONNECT_RETRY,
    CONF_FAILSAFE_IMPORT_LIMIT_W,
    CONF_FAILSAFE_TIMEOUT,
    CONF_FRAMING,
    CONF_MAPPING,
    CONF_RETRY_INTERVAL,
    CONF_UNIT_ID,
    DEFAULT_CONNECT_RETRY,
    DEFAULT_FAILSAFE_IMPORT_LIMIT_W,
    DEFAULT_FAILSAFE_TIMEOUT,
    DEFAULT_FRAMING,
    DEFAULT_PORT,
    DEFAULT_RETRY_INTERVAL,
    DEFAULT_UNIT_ID,
    DOMAIN,
    FRAMING_OPTIONS,
    MAPPING_STEPS,
)

_MAPPING_STEP_ORDER = list(MAPPING_STEPS)


def _network_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Required(CONF_UNIT_ID, default=defaults.get(CONF_UNIT_ID, DEFAULT_UNIT_ID)): int,
            vol.Required(CONF_FRAMING, default=defaults.get(CONF_FRAMING, DEFAULT_FRAMING)): vol.In(FRAMING_OPTIONS),
            vol.Required(
                CONF_CONNECT_RETRY, default=defaults.get(CONF_CONNECT_RETRY, DEFAULT_CONNECT_RETRY)
            ): vol.Coerce(float),
            vol.Required(
                CONF_RETRY_INTERVAL, default=defaults.get(CONF_RETRY_INTERVAL, DEFAULT_RETRY_INTERVAL)
            ): vol.Coerce(float),
        }
    )


def _mapping_schema(step_id: str, mapping: dict[str, str]) -> vol.Schema:
    schema_dict: dict[Any, Any] = {}
    for key in MAPPING_STEPS[step_id]:
        schema_dict[vol.Optional(key, description={"suggested_value": mapping.get(key)})] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )
    return vol.Schema(schema_dict)


def _failsafe_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_FAILSAFE_TIMEOUT, default=defaults.get(CONF_FAILSAFE_TIMEOUT, DEFAULT_FAILSAFE_TIMEOUT)
            ): vol.Coerce(float),
            vol.Required(
                CONF_FAILSAFE_IMPORT_LIMIT_W,
                default=defaults.get(CONF_FAILSAFE_IMPORT_LIMIT_W, DEFAULT_FAILSAFE_IMPORT_LIMIT_W),
            ): vol.Coerce(float),
        }
    )


class _MappingFlowMixin:
    """Shared logic for walking the 4 mapping steps, used by both the
    initial config flow and the options (reconfigure) flow."""

    _data: dict[str, Any]

    async def _async_mapping_step(self, step_id: str, user_input: dict[str, Any] | None):
        if user_input is not None:
            mapping = self._data.setdefault(CONF_MAPPING, {})
            for key, value in user_input.items():
                if value:
                    mapping[key] = value
                else:
                    mapping.pop(key, None)
            next_index = _MAPPING_STEP_ORDER.index(step_id) + 1
            if next_index < len(_MAPPING_STEP_ORDER):
                return await self._async_step_by_name(_MAPPING_STEP_ORDER[next_index])
            return await self._async_step_by_name("failsafe")

        mapping = self._data.get(CONF_MAPPING, {})
        return self.async_show_form(step_id=step_id, data_schema=_mapping_schema(step_id, mapping))

    async def async_step_failsafe(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return self._async_finish()
        return self.async_show_form(step_id="failsafe", data_schema=_failsafe_schema(self._data))

    async def _async_step_by_name(self, step_id: str):
        return await getattr(self, f"async_step_{step_id}")()

    def _async_finish(self):
        raise NotImplementedError


class Em340EmuConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup: network settings, then field mapping."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_voltages_currents()
        return self.async_show_form(step_id="user", data_schema=_network_schema(self._data), errors=errors)

    async def async_step_voltages_currents(self, user_input: dict[str, Any] | None = None):
        return await self._async_mapping_step("voltages_currents", user_input)

    async def async_step_active_power(self, user_input: dict[str, Any] | None = None):
        return await self._async_mapping_step("active_power", user_input)

    async def async_step_reactive_power(self, user_input: dict[str, Any] | None = None):
        return await self._async_mapping_step("reactive_power", user_input)

    async def async_step_energy(self, user_input: dict[str, Any] | None = None):
        return await self._async_mapping_step("energy", user_input)

    def _async_finish(self):
        title = f"EM340 emulator ({self._data[CONF_HOST]}:{self._data[CONF_PORT]})"
        return self.async_create_entry(title=title, data=self._data)

    _async_mapping_step = _MappingFlowMixin._async_mapping_step
    async_step_failsafe = _MappingFlowMixin.async_step_failsafe
    _async_step_by_name = _MappingFlowMixin._async_step_by_name

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> Em340EmuOptionsFlow:
        return Em340EmuOptionsFlow(config_entry)


class Em340EmuOptionsFlow(config_entries.OptionsFlow):
    """Reconfigure network settings and/or the entity mapping."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._data: dict[str, Any] = {**config_entry.data, **config_entry.options}

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_voltages_currents()
        return self.async_show_form(step_id="init", data_schema=_network_schema(self._data))

    async def async_step_voltages_currents(self, user_input: dict[str, Any] | None = None):
        return await self._async_mapping_step("voltages_currents", user_input)

    async def async_step_active_power(self, user_input: dict[str, Any] | None = None):
        return await self._async_mapping_step("active_power", user_input)

    async def async_step_reactive_power(self, user_input: dict[str, Any] | None = None):
        return await self._async_mapping_step("reactive_power", user_input)

    async def async_step_energy(self, user_input: dict[str, Any] | None = None):
        return await self._async_mapping_step("energy", user_input)

    def _async_finish(self):
        return self.async_create_entry(title="", data=self._data)

    _async_mapping_step = _MappingFlowMixin._async_mapping_step
    async_step_failsafe = _MappingFlowMixin.async_step_failsafe
    _async_step_by_name = _MappingFlowMixin._async_step_by_name
