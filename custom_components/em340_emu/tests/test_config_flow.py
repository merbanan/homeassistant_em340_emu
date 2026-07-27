"""Config flow step sequence: trimmed to only what live captures showed
this Wallbox actually reads (see README "Confirmed via live capture") --
voltages/currents, active power, and active energy. No reactive power step,
no reactive energy/frequency fields."""
import pytest
from homeassistant import config_entries

from custom_components.em340_emu.const import DOMAIN

pytestmark = pytest.mark.asyncio


async def test_setup_flow_has_no_reactive_power_step(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "host": "192.168.200.7",
            "port": 12345,
            "unit_id": 1,
            "framing": "rtu",
            "retry_interval": 2.0,
        },
    )
    assert result["step_id"] == "voltages_currents"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "active_power"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    # Straight to energy -- no reactive_power step in between.
    assert result["step_id"] == "energy"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"energy_active_import": "sensor.han_energy_import"}
    )
    assert result["step_id"] == "failsafe"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"failsafe_timeout": 60.0, "failsafe_import_limit_w": 11000.0}
    )
    assert result["type"] == "create_entry"
    assert result["data"]["mapping"] == {"energy_active_import": "sensor.han_energy_import"}
