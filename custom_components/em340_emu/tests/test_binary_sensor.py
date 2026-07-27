"""Two independent status indicators (see binary_sensor.py's module
docstring): "Wallbox reading" (on while the Wallbox has read a register
from us recently) and "P1 data updating" (on while the fail-safe hasn't
had to engage due to stale P1/HAN data). Split so a problem on either
side is visible on its own, not collapsed into one combined state.
binary_sensor specifically so no card/page ever shows a toggle control
for either -- unlike a light, which always would."""
import asyncio
import socket

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from em340_emu import codec
from custom_components.em340_emu.const import DOMAIN

pytestmark = pytest.mark.asyncio


def _rtu_request(unit: int, pdu: bytes) -> bytes:
    body = bytes([unit]) + pdu
    return body + codec.crc16_bytes(body)


async def _start_answering_gateway(request: bytes):
    done = asyncio.Event()
    gateway_box: dict = {}

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(request)
        await writer.drain()
        try:
            await asyncio.wait_for(reader.read(64), timeout=2)
        except asyncio.TimeoutError:
            pass
        done.set()
        writer.close()
        gateway_box["server"].close()

    gateway = await asyncio.start_server(_handler, "127.0.0.1", 0)
    gateway_box["server"] = gateway
    port = gateway.sockets[0].getsockname()[1]
    return gateway, port, done


def _find_binary_sensor(hass, unique_id_suffix: str):
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.domain == "binary_sensor" and entry.unique_id.endswith(unique_id_suffix):
            state = hass.states.get(entry.entity_id)
            assert state is not None
            return state
    raise AssertionError(f"no binary_sensor with unique_id suffix {unique_id_suffix!r}")


def _wallbox_sensor(hass):
    return _find_binary_sensor(hass, "_wallbox_reading")


def _p1_sensor(hass):
    return _find_binary_sensor(hass, "_p1_data_updating")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_wallbox_sensor_off_until_a_register_is_read(hass, socket_enabled):
    port = _free_port()  # nothing ever listens here
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "127.0.0.1",
            "port": port,
            "unit_id": 1,
            "framing": "rtu",
            "retry_interval": 0.1,
            "mapping": {},
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await asyncio.sleep(2.1)  # well past a periodic refresh tick, no connection ever succeeds
    assert _wallbox_sensor(hass).state == "off"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_wallbox_sensor_turns_on_once_a_register_is_read(hass, socket_enabled):
    gateway, port, done = await _start_answering_gateway(_rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
    try:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"host": "127.0.0.1", "port": port, "unit_id": 1, "framing": "rtu", "mapping": {}},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await asyncio.wait_for(done.wait(), timeout=3)
        await asyncio.sleep(2.1)  # let a periodic refresh tick (every 2s) pick up the change
        assert _wallbox_sensor(hass).state == "on"

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        gateway.close()
        await gateway.wait_closed()


async def test_p1_sensor_on_while_fresh_off_once_failsafe_engages(hass, socket_enabled):
    hass.states.async_set("sensor.han_power_import_l1", "0.5", {"unit_of_measurement": "kW"})
    port = _free_port()  # the Wallbox side is irrelevant to this sensor
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "127.0.0.1",
            "port": port,
            "unit_id": 1,
            "framing": "rtu",
            "mapping": {"active_power_import_l1": "sensor.han_power_import_l1"},
            "failsafe_timeout": 0.5,
            "failsafe_import_limit_w": 9000,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert _p1_sensor(hass).state == "on"  # seeded from the existing entity state at setup

    # Needs a periodic refresh tick (every 2s) to happen *after* the
    # fail-safe actually engages at the 0.5s mark, not just to reach it.
    await asyncio.sleep(2.5)
    assert _p1_sensor(hass).state == "off"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
