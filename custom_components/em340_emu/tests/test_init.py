"""End-to-end test of the integration against a real (test) Home Assistant
core: entity mapping, unit normalization, live state updates, and that the
embedded emulator actually answers a Modbus request over the wire."""
import asyncio
import socket

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from em340_emu import codec
from custom_components.em340_emu.const import DOMAIN

pytestmark = pytest.mark.asyncio


def _rtu_request(unit: int, pdu: bytes) -> bytes:
    body = bytes([unit]) + pdu
    return body + codec.crc16_bytes(body)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_setup_seeds_from_existing_entity_states(hass, socket_enabled):
    hass.states.async_set("sensor.han_voltage_l1", "231.4", {"unit_of_measurement": "V"})
    hass.states.async_set("sensor.han_power_import_l1", "1.4", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.han_energy_import", "12345.6", {"unit_of_measurement": "kWh"})

    port = _free_port()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "127.0.0.1",
            "port": port,
            "unit_id": 1,
            "framing": "rtu",
            "mapping": {
                "voltage_l1": "sensor.han_voltage_l1",
                "active_power_import_l1": "sensor.han_power_import_l1",
                "energy_active_import": "sensor.han_energy_import",
            },
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    state = runtime["state"]
    assert state.l1.voltage == 231.4
    assert state.l1.active_power_import == 1400.0  # kW -> W
    assert state.energy_active_import == 12345.6

    # And it's really on the wire: read V L1-N over a live socket.
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
    await writer.drain()
    response = await asyncio.wait_for(reader.read(64), timeout=2)
    raw = (int.from_bytes(response[5:7], "big") << 16) | int.from_bytes(response[3:5], "big")
    assert raw == 2314  # 231.4 V * 10
    writer.close()
    await writer.wait_closed()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises((ConnectionRefusedError, OSError)):
        await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=1)


async def test_state_change_updates_meter_live(hass, socket_enabled):
    hass.states.async_set("sensor.han_current_l2", "0.0", {"unit_of_measurement": "A"})

    port = _free_port()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "127.0.0.1",
            "port": port,
            "unit_id": 1,
            "framing": "rtu",
            "mapping": {"current_l2": "sensor.han_current_l2"},
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.data[DOMAIN][entry.entry_id]["state"]
    assert state.l2.current == 0.0

    hass.states.async_set("sensor.han_current_l2", "6.321", {"unit_of_measurement": "A"})
    await hass.async_block_till_done()
    assert state.l2.current == 6.321

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_with_no_mapping_still_starts(hass, socket_enabled):
    port = _free_port()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "127.0.0.1", "port": port, "unit_id": 1, "framing": "rtu", "mapping": {}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_connect_mode_dials_out_to_gateway(hass, socket_enabled):
    # "connect" mode is for a gateway that is itself a TCP server (the real
    # case that motivated adding it -- see const.py's CONNECTION_MODE_*
    # docstring): the integration must dial out to host/port instead of
    # listening on them, or setup fails with "could not bind" since that
    # address belongs to the gateway, not to Home Assistant.
    hass.states.async_set("sensor.han_voltage_l1", "230.0", {"unit_of_measurement": "V"})

    received: list = []

    async def _gateway_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(_rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
        await writer.drain()
        response = await asyncio.wait_for(reader.read(64), timeout=2)
        received.append(response)
        writer.close()

    gateway = await asyncio.start_server(_gateway_handler, "127.0.0.1", 0)
    gateway_port = gateway.sockets[0].getsockname()[1]

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "127.0.0.1",
            "port": gateway_port,
            "unit_id": 1,
            "framing": "rtu",
            "connection_mode": "connect",
            "connect_retry": 5,
            "retry_interval": 0.1,
            "mapping": {"voltage_l1": "sensor.han_voltage_l1"},
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    try:
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.05)
    finally:
        gateway.close()
        await gateway.wait_closed()

    assert len(received) >= 1
    response = received[0]
    raw = (int.from_bytes(response[5:7], "big") << 16) | int.from_bytes(response[3:5], "big")
    assert raw == 2300  # 230.0 V * 10

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime["connection_task"] is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert runtime["connection_task"].cancelled()


async def test_failsafe_engages_and_ramps_when_updates_stop(hass, socket_enabled):
    hass.states.async_set("sensor.han_power_import_l1", "0.5", {"unit_of_measurement": "kW"})
    port = _free_port()
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

    runtime = hass.data[DOMAIN][entry.entry_id]
    state = runtime["state"]
    monitor = runtime["monitor"]
    assert state.l1.active_power_import == 500.0  # seeded: 0.5kW -> 500W
    assert not monitor.engaged

    await asyncio.sleep(2.5)  # exceed the 0.5s timeout across a couple of 1s watchdog ticks
    assert monitor.engaged
    assert state.l1.active_power_import > 500.0  # ramping up towards the limit
    assert state.l1.active_power_export == 0.0

    # a fresh update recovers immediately, overriding the fail-safe value
    hass.states.async_set("sensor.han_power_import_l1", "0.6", {"unit_of_measurement": "kW"})
    await hass.async_block_till_done()
    assert not monitor.engaged
    assert state.l1.active_power_import == 600.0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
