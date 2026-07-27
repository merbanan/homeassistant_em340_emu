import asyncio
import contextlib
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


async def _exchange(server: ModbusGatewayServer, request: bytes, *, response_size: int = 64, timeout: float = 2) -> bytes:
    """Start a mock gateway (a plain TCP server), have `server` dial into it
    via serve_as_client(), write `request` from the gateway side, and
    return whatever comes back -- or b"" if nothing arrives before timeout
    (e.g. testing that a wrong unit id gets no response).

    Every real gateway this project talks to is itself a TCP server (see
    server.py's module docstring), so this is the only shape end-to-end
    tests need: our code always dials out, never listens.
    """
    result: dict = {}
    done = asyncio.Event()

    async def _gateway_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(request)
        await writer.drain()
        try:
            result["response"] = await asyncio.wait_for(reader.read(response_size), timeout=timeout)
        except asyncio.TimeoutError:
            result["response"] = b""
        done.set()
        writer.close()

    gateway = await asyncio.start_server(_gateway_handler, "127.0.0.1", 0)
    server.host = "127.0.0.1"
    server.port = gateway.sockets[0].getsockname()[1]

    task = asyncio.create_task(server.serve_as_client(connect_retry=2, retry_interval=0.1))
    try:
        # The outer wait must exceed the inner wait_for's own `timeout` --
        # its clock starts later (only once connected), so a tight/equal
        # outer budget can time out first under load, discarding a response
        # that was still legitimately in flight (this used to be a flaky
        # polling loop with a same-size budget; +1s makes it strictly safe).
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(done.wait(), timeout=timeout + 1)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        gateway.close()
        await gateway.wait_closed()
    return result.get("response", b"")


async def test_rtu_framing_end_to_end():
    state = MeterState()
    state.l1.voltage = 231.5
    server = ModbusGatewayServer(state=state, host="", port=0, unit_id=1, framing="rtu")
    response = await _exchange(server, _rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
    assert response[0] == 1  # unit id
    assert response[1] == 0x03  # function code
    payload = response[3 : 3 + response[2]]
    raw = (int.from_bytes(payload[2:4], "big") << 16) | int.from_bytes(payload[0:2], "big")
    assert raw == 2315


async def test_tcp_framing_end_to_end():
    state = MeterState()
    state.l1.current = 6.5
    server = ModbusGatewayServer(state=state, host="", port=0, unit_id=1, framing="tcp")
    response = await _exchange(server, _mbap_request(0x0001, 1, bytes([0x04, 0x00, 0x0C, 0x00, 0x02])))
    assert response[6] == 1  # unit id
    pdu = response[7:]
    assert pdu[0] == 0x04
    payload = pdu[2:]
    raw = (int.from_bytes(payload[2:4], "big") << 16) | int.from_bytes(payload[0:2], "big")
    assert raw == 6500


async def test_auto_detects_rtu_framing():
    server = ModbusGatewayServer(state=MeterState(), host="", port=0, unit_id=1, framing="auto")
    response = await _exchange(server, _rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
    assert response[0] == 1 and response[1] == 0x03


async def test_auto_detect_resolves_mbap_ambiguous_register_zero_read():
    # An RTU read of register 0x0000 also structurally satisfies the MBAP
    # heuristic (see framing.looks_like_mbap); auto-detect must still land
    # on RTU here because the CRC is checked first.
    state = MeterState()
    state.l1.voltage = 240.0
    server = ModbusGatewayServer(state=state, host="", port=0, unit_id=1, framing="auto")
    response = await _exchange(server, _rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
    assert response[0] == 1 and response[1] == 0x03
    raw = (int.from_bytes(response[5:7], "big") << 16) | int.from_bytes(response[3:5], "big")
    assert raw == 2400


async def test_wrong_unit_id_gets_no_response():
    server = ModbusGatewayServer(state=MeterState(), host="", port=0, unit_id=1, framing="rtu")
    response = await _exchange(server, _rtu_request(2, bytes([0x03, 0x00, 0x00, 0x00, 0x02])), timeout=0.3)
    assert response == b""


async def test_seconds_since_last_request_tracks_requests_addressed_to_us():
    server = ModbusGatewayServer(state=MeterState(), host="", port=0, unit_id=1, framing="rtu")
    assert server.seconds_since_last_request() is None  # nothing ever arrived

    await _exchange(server, _rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
    elapsed = server.seconds_since_last_request()
    assert elapsed is not None and elapsed < 2.0


async def test_seconds_since_last_request_ignores_wrong_unit_id():
    server = ModbusGatewayServer(state=MeterState(), host="", port=0, unit_id=1, framing="rtu")
    await _exchange(server, _rtu_request(2, bytes([0x03, 0x00, 0x00, 0x00, 0x02])), timeout=0.3)
    assert server.seconds_since_last_request() is None  # unit 2 isn't us


async def test_unimplemented_address_returns_courtesy_zero_by_default():
    # RegisterMap() defaults to courtesy mode (see its docstring): an
    # address we haven't implemented reads back as 0 rather than raising,
    # so the server never rejects a register some charger's detection
    # routine happens to probe that we haven't anticipated.
    server = ModbusGatewayServer(state=MeterState(), host="", port=0, unit_id=1, framing="rtu")
    response = await _exchange(server, _rtu_request(1, bytes([0x03, 0x99, 0x99, 0x00, 0x01])))
    assert response[1] == 0x03  # function code, not an exception
    assert response[2] == 2  # byte count
    assert response[3:5] == b"\x00\x00"


async def test_illegal_address_returns_exception_frame_in_strict_mode():
    server = ModbusGatewayServer(state=MeterState(), host="", port=0, unit_id=1, framing="rtu", registers=RegisterMap(strict=True))
    response = await _exchange(server, _rtu_request(1, bytes([0x03, 0x99, 0x99, 0x00, 0x01])))
    assert response[1] == 0x83  # function | 0x80
    assert response[2] == 0x02  # illegal data address


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
    server = ModbusGatewayServer(state=MeterState(), host="", port=0, unit_id=1, framing="rtu")
    await _exchange(server, _rtu_request(1, bytes([0x03, 0x00, 0x0B, 0x00, 0x01])))

    assert any("FC03 read addr=0x000B count=1" in record.message for record in caplog.records)
    assert any("-> ok" in record.message for record in caplog.records)


async def test_serve_as_client_dials_out_and_answers():
    # For a gateway that is itself a TCP server (the only arrangement this
    # project needs -- see server.py's module docstring): our server dials
    # out to it instead of listening.
    state = MeterState()
    state.l1.voltage = 231.0
    client_server = ModbusGatewayServer(state=state, host="127.0.0.1", port=0, unit_id=1, framing="rtu")

    received: list = []

    async def _gateway_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(_rtu_request(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02])))
        await writer.drain()
        response = await asyncio.wait_for(reader.read(64), timeout=2)
        received.append(response)
        writer.close()

    gateway = await asyncio.start_server(_gateway_handler, "127.0.0.1", 0)
    gateway_port = gateway.sockets[0].getsockname()[1]
    client_server.host = "127.0.0.1"
    client_server.port = gateway_port

    task = asyncio.create_task(client_server.serve_as_client(connect_retry=2, retry_interval=0.1))
    try:
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gateway.close()
        await gateway.wait_closed()

    # serve_as_client reconnects forever by design (see its docstring), and
    # this test's mock gateway keeps accepting, so more than one cycle may
    # well have happened by the time we cancel -- that's expected, not a
    # bug; what matters is that at least one came through correctly.
    assert len(received) >= 1
    response = received[0]
    assert response[0] == 1 and response[1] == 0x03
    raw = (int.from_bytes(response[5:7], "big") << 16) | int.from_bytes(response[3:5], "big")
    assert raw == 2310


async def test_serve_as_client_retries_until_gateway_available():
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    connected: list = []

    async def _gateway_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connected.append(True)
        writer.close()

    async def _start_gateway_late():
        await asyncio.sleep(0.3)
        return await asyncio.start_server(_gateway_handler, "127.0.0.1", port)

    gateway_task = asyncio.create_task(_start_gateway_late())
    state = MeterState()
    client_server = ModbusGatewayServer(state=state, host="127.0.0.1", port=port, unit_id=1, framing="rtu")
    task = asyncio.create_task(client_server.serve_as_client(connect_retry=5, retry_interval=0.1))
    try:
        for _ in range(100):
            if connected:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gateway = await gateway_task
        gateway.close()
        await gateway.wait_closed()

    assert len(connected) >= 1  # reconnects forever by design; at least one connection is what matters here


async def test_connect_with_retry_gives_up_after_max_wait():
    from em340_emu.server import connect_with_retry

    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    attempts: list = []
    with pytest.raises(OSError):
        await connect_with_retry(
            "127.0.0.1", port, max_wait=0.3, retry_interval=0.1,
            on_retry=lambda attempt, exc, remaining: attempts.append(attempt),
        )
    assert len(attempts) >= 1


async def test_connect_with_retry_never_gives_up_when_max_wait_is_none():
    # None is the default for serve_as_client (see its docstring): a
    # persistent background service should never stop trying to reach a
    # gateway that's meant to always be there.
    from em340_emu.server import connect_with_retry

    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    attempts: list = []
    remainings: list = []

    async def _accept_and_close(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Must close, or Server.wait_closed() below hangs waiting for this
        # still-open connection to finish.
        writer.close()

    async def _start_gateway_late():
        await asyncio.sleep(0.3)  # outlasts what a bounded max_wait would allow
        return await asyncio.start_server(_accept_and_close, "127.0.0.1", port)

    gateway_task = asyncio.create_task(_start_gateway_late())
    try:
        reader, writer = await asyncio.wait_for(
            connect_with_retry(
                "127.0.0.1", port, max_wait=None, retry_interval=0.05,
                on_retry=lambda attempt, exc, remaining: (attempts.append(attempt), remainings.append(remaining)),
            ),
            timeout=5,
        )
        writer.close()
    finally:
        gateway = await gateway_task
        gateway.close()
        await gateway.wait_closed()

    assert len(attempts) >= 1  # retried past where a short bounded max_wait would have raised
    assert all(r == float("inf") for r in remainings)
