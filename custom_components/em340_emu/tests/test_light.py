"""Health light: green only while the Wallbox is actively reading
registers from us AND the fail-safe hasn't had to engage due to stale
P1/HAN data; red otherwise (see light.py's module docstring)."""
import asyncio
import socket

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from em340_emu import codec
from custom_components.em340_emu.const import DOMAIN
from custom_components.em340_emu.light import GREEN, RED

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


def _the_light(hass):
    lights = hass.states.async_all("light")
    assert len(lights) == 1
    return lights[0]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_health_light_stays_red_when_wallbox_never_reads(hass, socket_enabled):
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
    assert _the_light(hass).attributes["rgb_color"] == RED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_health_light_green_once_wallbox_reads_red_once_failsafe_engages(hass, socket_enabled):
    hass.states.async_set("sensor.han_power_import_l1", "0.5", {"unit_of_measurement": "kW"})
    gateway, port, done = await _start_answering_gateway(_rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
    try:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "host": "127.0.0.1",
                "port": port,
                "unit_id": 1,
                "framing": "rtu",
                "mapping": {"active_power_import_l1": "sensor.han_power_import_l1"},
                # Long enough to survive the first (~2.1s) wait below without
                # engaging, short enough that the second wait pushes past it
                # -- while both stay well under light.py's 10s Wallbox
                # activity timeout, so that's isolated as the only variable.
                "failsafe_timeout": 5.0,
                "failsafe_import_limit_w": 9000,
            },
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await asyncio.wait_for(done.wait(), timeout=3)
        await asyncio.sleep(2.1)  # let a periodic sensor-refresh tick (every 2s) pick up the change
        assert _the_light(hass).attributes["rgb_color"] == GREEN

        # The light only updates on push (should_poll=False): it needs a
        # periodic refresh tick (every 2s) to happen *after* the fail-safe
        # actually engages at the 5s mark, not just to reach the 5s mark
        # itself -- 5.5s more (~7.6s total) comfortably covers both.
        await asyncio.sleep(5.5)
        assert _the_light(hass).attributes["rgb_color"] == RED  # stale P1 data, even though the Wallbox read recently

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        gateway.close()
        await gateway.wait_closed()
