import asyncio
import logging

import pytest

from em340_emu import codec
from em340_emu.model import MeterState
from em340_emu.registers import RegisterMap
from em340_emu.server import ModbusGatewayServer, _describe_pdu


def _rtu_request(unit: int, pdu: bytes) -> bytes:
    body = bytes([unit]) + pdu
    return body + codec.crc16_bytes(body)


def _mbap_request(transaction_id: int, unit: int, pdu: bytes) -> bytes:
    length = 1 + len(pdu)
    header = bytes([transaction_id >> 8, transaction_id & 0xFF, 0, 0, length >> 8, length & 0xFF, unit])
    return header + pdu


async def _start_server(**kwargs) -> tuple[ModbusGatewayServer, int]:
    state = kwargs.pop("state", None) or MeterState()
    server = ModbusGatewayServer(state=state, host="127.0.0.1", port=0, **kwargs)
    await server.start()
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_rtu_framing_end_to_end():
    state = MeterState()
    state.l1.voltage = 231.5
    server, port = await _start_server(state=state, unit_id=1, framing="rtu")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
        await writer.drain()
        response = await asyncio.wait_for(reader.read(64), timeout=2)
        assert response[0] == 1  # unit id
        assert response[1] == 0x03  # function code
        payload = response[3 : 3 + response[2]]
        raw = (int.from_bytes(payload[2:4], "big") << 16) | int.from_bytes(payload[0:2], "big")
        assert raw == 2315
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_tcp_framing_end_to_end():
    state = MeterState()
    state.l1.current = 6.5
    server, port = await _start_server(state=state, unit_id=1, framing="tcp")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_mbap_request(0x0001, 1, bytes([0x04, 0x00, 0x0C, 0x00, 0x02])))
        await writer.drain()
        response = await asyncio.wait_for(reader.read(64), timeout=2)
        assert response[6] == 1  # unit id
        pdu = response[7:]
        assert pdu[0] == 0x04
        payload = pdu[2:]
        raw = (int.from_bytes(payload[2:4], "big") << 16) | int.from_bytes(payload[0:2], "big")
        assert raw == 6500
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_auto_detects_rtu_framing():
    server, port = await _start_server(unit_id=1, framing="auto")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
        await writer.drain()
        response = await asyncio.wait_for(reader.read(64), timeout=2)
        assert response[0] == 1 and response[1] == 0x03
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_auto_detect_resolves_mbap_ambiguous_register_zero_read():
    # An RTU read of register 0x0000 also structurally satisfies the MBAP
    # heuristic (see framing.looks_like_mbap); auto-detect must still land
    # on RTU here because the CRC is checked first.
    state = MeterState()
    state.l1.voltage = 240.0
    server, port = await _start_server(state=state, unit_id=1, framing="auto")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
        await writer.drain()
        response = await asyncio.wait_for(reader.read(64), timeout=2)
        assert response[0] == 1 and response[1] == 0x03
        raw = (int.from_bytes(response[5:7], "big") << 16) | int.from_bytes(response[3:5], "big")
        assert raw == 2400
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_wrong_unit_id_gets_no_response():
    server, port = await _start_server(unit_id=1, framing="rtu")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_rtu_request(2, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
        await writer.drain()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(64), timeout=0.3)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_unimplemented_address_returns_courtesy_zero_by_default():
    # RegisterMap() defaults to courtesy mode (see its docstring): an
    # address we haven't implemented reads back as 0 rather than raising,
    # so the server never rejects a register some charger's detection
    # routine happens to probe that we haven't anticipated.
    server, port = await _start_server(unit_id=1, framing="rtu")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_rtu_request(1, bytes([0x03, 0x99, 0x99, 0x00, 0x01])))
        await writer.drain()
        response = await asyncio.wait_for(reader.read(64), timeout=2)
        assert response[1] == 0x03  # function code, not an exception
        assert response[2] == 2  # byte count
        assert response[3:5] == b"\x00\x00"
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_illegal_address_returns_exception_frame_in_strict_mode():
    server, port = await _start_server(unit_id=1, framing="rtu", registers=RegisterMap(strict=True))
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_rtu_request(1, bytes([0x03, 0x99, 0x99, 0x00, 0x01])))
        await writer.drain()
        response = await asyncio.wait_for(reader.read(64), timeout=2)
        assert response[1] == 0x83  # function | 0x80
        assert response[2] == 0x02  # illegal data address
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


def test_describe_pdu_read_request():
    assert _describe_pdu(bytes([0x03, 0x00, 0x0B, 0x00, 0x01])) == "FC03 read addr=0x000B count=1"
    assert _describe_pdu(bytes([0x04, 0x01, 0x03, 0x00, 0x02])) == "FC04 read addr=0x0103 count=2"


def test_describe_pdu_write_request():
    assert _describe_pdu(bytes([0x06, 0x11, 0x03, 0x00, 0x01])) == "FC06 write addr=0x1103 value=0x0001"


def test_describe_pdu_diagnostics():
    assert _describe_pdu(bytes([0x08, 0x00, 0x00, 0x12, 0x34])) == "FC08 diagnostics sub=0x0000 data=1234"


def test_describe_pdu_empty_and_unknown():
    assert _describe_pdu(b"") == "<empty>"
    assert _describe_pdu(bytes([0x2B, 0x0E, 0x01])) == "FC2B data=0e01"


async def test_debug_log_shows_requests_including_the_contested_id_code_address(caplog):
    # Watching this is how we'd empirically confirm whether a real Wallbox
    # ever probes 0x000B (the address the official protocol doc reserves
    # for an identification code, but which we give to V L3-L1 instead --
    # see registers.py's module docstring).
    caplog.set_level(logging.DEBUG, logger="em340_emu.server")
    server, port = await _start_server(unit_id=1, framing="rtu")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_rtu_request(1, bytes([0x03, 0x00, 0x0B, 0x00, 0x01])))
        await writer.drain()
        await asyncio.wait_for(reader.read(64), timeout=2)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    assert any("FC03 read addr=0x000B count=1" in record.message for record in caplog.records)
    assert any("-> ok" in record.message for record in caplog.records)
