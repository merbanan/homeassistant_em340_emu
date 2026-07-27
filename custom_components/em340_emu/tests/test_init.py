"""End-to-end test of the integration against a real (test) Home Assistant
core: entity mapping, unit normalization, live state updates, the embedded
emulator actually answering a Modbus request over a dialed-out connection,
and the diagnostic counters it exposes."""
import asyncio
import contextlib
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


async def _start_answering_gateway(request: bytes, *, response_size: int = 64, timeout: float = 2.0):
    """A plain TCP server playing the role of the RS485-to-Ethernet gateway
    (every real one this project has seen is itself a TCP server -- see
    server.py's module docstring): writes `request` to whatever dials in,
    then reads back a response. Everything happens inside one callback
    coroutine (rather than handing reader/writer out to be used later),
    since splitting that across the HA test event loop was observed to
    hang indefinitely instead of the read just timing out as expected.

    Stops listening after answering once: serve_as_client() reconnects
    forever by design, and leaving the gateway accepting those reconnects
    races its own teardown against ours at the end of the test (a harmless
    but noisy asyncio internals error) for no benefit -- one exchange is
    all any of these tests need.
    """
    result: dict = {}
    done = asyncio.Event()
    gateway_box: dict = {}

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(request)
        await writer.drain()
        try:
            result["response"] = await asyncio.wait_for(reader.read(response_size), timeout=timeout)
        except asyncio.TimeoutError:
            result["response"] = b""
        done.set()
        writer.close()
        gateway_box["server"].close()

    gateway = await asyncio.start_server(_handler, "127.0.0.1", 0)
    gateway_box["server"] = gateway
    port = gateway.sockets[0].getsockname()[1]
    return gateway, port, done, result


async def test_setup_seeds_from_existing_entity_states(hass, socket_enabled):
    hass.states.async_set("sensor.han_voltage_l1", "231.4", {"unit_of_measurement": "V"})
    hass.states.async_set("sensor.han_power_import_l1", "1.4", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.han_energy_import", "12345.6", {"unit_of_measurement": "kWh"})

    gateway, port, done, result = await _start_answering_gateway(
        _rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02]))
    )
    try:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "host": "127.0.0.1",
                "port": port,
                "unit_id": 1,
                "framing": "rtu",
                "retry_interval": 0.1,
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
        # Seeding from existing entity states counts as entity updates too.
        assert runtime["entity_update_counter"].value >= 3

        # And it's really on the wire: read V L1-N over the dialed-out connection.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(done.wait(), timeout=3)
        response = result.get("response", b"")
        assert response, "gateway never received a response"
        raw = (int.from_bytes(response[5:7], "big") << 16) | int.from_bytes(response[3:5], "big")
        assert raw == 2314  # 231.4 V * 10

        assert runtime["server"].request_count >= 1
        assert runtime["server"].response_count >= 1

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        gateway.close()
        await gateway.wait_closed()


async def test_state_change_updates_meter_live(hass, socket_enabled):
    hass.states.async_set("sensor.han_current_l2", "0.0", {"unit_of_measurement": "A"})

    port = _free_port()  # nothing needs to actually answer for this test
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


async def test_unload_cancels_background_tasks(hass, socket_enabled):
    # Connecting out, the fail-safe watchdog, and the sensor refresh timer
    # are all background tasks; unloading must cancel every one of them, or
    # they'd keep running (and retrying a connection) against a torn-down
    # entry indefinitely.
    port = _free_port()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "127.0.0.1", "port": port, "unit_id": 1, "framing": "rtu", "mapping": {}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    monitor_task = runtime["monitor_task"]
    connection_task = runtime["connection_task"]
    refresh_task = runtime["refresh_task"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert monitor_task.cancelled()
    assert connection_task.cancelled()
    assert refresh_task.cancelled()


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
