import asyncio

import pytest

from em340_emu import codec
from em340_emu.cli import _format_readings_table, _run_serve, _run_sniff, build_parser


def test_format_readings_table_shows_values_and_placeholders():
    text = _format_readings_table({"voltage_l1": 231.4}, set(), None)
    assert "Phase voltage L1" in text
    assert "231.4" in text
    assert "—" in text  # unset fields
    assert "Waiting for the first message" in text


def test_format_readings_table_flags_unknown_keys():
    text = _format_readings_table({}, {"weird_key"}, 12.3)
    assert "weird_key" in text
    assert "Last message" in text


def test_serve_defaults_to_mqtt_source():
    parser = build_parser()
    args = parser.parse_args(["serve", "--host", "192.168.200.7", "--port", "12345"])
    assert args.mqtt_host == "192.168.200.142"
    assert args.mqtt_topic == "energy-meter/#"
    assert args.no_mqtt is False
    assert args.demo is False
    assert args.values is None
    assert args.unit_id == 1
    assert args.strict is False  # courtesy mode (RegisterMap default) is the default
    assert args.connect_retry == 300.0
    assert args.retry_interval == 2.0


def test_serve_requires_host_and_port():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["serve"])


def test_sniff_emulate_strict_flag_defaults_off():
    parser = build_parser()
    args = parser.parse_args(["sniff", "--host", "127.0.0.1", "--port", "12345", "--emulate"])
    assert args.strict is False
    assert args.unit_id == 1


def test_serve_failsafe_defaults():
    parser = build_parser()
    args = parser.parse_args(["serve", "--host", "192.168.200.7", "--port", "12345"])
    assert args.failsafe_timeout == 60.0
    assert args.failsafe_import_limit == 11000.0


def test_serve_failsafe_overrides():
    parser = build_parser()
    args = parser.parse_args(
        ["serve", "--host", "192.168.200.7", "--port", "12345", "--failsafe-timeout", "0", "--failsafe-import-limit", "7000"]
    )
    assert args.failsafe_timeout == 0.0
    assert args.failsafe_import_limit == 7000.0


async def test_serve_dials_out_and_answers():
    # serve now dials out too (like sniff --emulate), since every gateway
    # this project has actually seen is itself a TCP server -- see
    # server.py's module docstring.
    body = bytes([1, 0x03, 0x00, 0x00, 0x00, 0x02])
    request = body + codec.crc16_bytes(body)
    received: list = []

    async def _handler(reader, writer):
        writer.write(request)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(64), timeout=2)
        received.append(response)
        writer.close()

    gateway = await asyncio.start_server(_handler, "127.0.0.1", 0)
    port = gateway.sockets[0].getsockname()[1]
    try:
        args = build_parser().parse_args(
            [
                "serve", "--host", "127.0.0.1", "--port", str(port), "--no-mqtt",
                "--connect-retry", "2", "--retry-interval", "0.1",
            ]
        )
        task = asyncio.create_task(_run_serve(args))
        try:
            for _ in range(100):
                if received:
                    break
                await asyncio.sleep(0.02)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        gateway.close()
        await gateway.wait_closed()

    assert len(received) >= 1
    response = received[0]
    assert response[0] == 1 and response[1] == 0x03
    raw = int.from_bytes(response[5:7], "big") << 16 | int.from_bytes(response[3:5], "big")
    assert raw == 2300  # MeterState() defaults l1.voltage to 230.0V


def test_view_readings_parses_mqtt_overrides():
    parser = build_parser()
    args = parser.parse_args(["view-readings", "--mqtt-host", "10.0.0.5", "--mqtt-topic", "custom/topic"])
    assert args.mqtt_host == "10.0.0.5"
    assert args.mqtt_topic == "custom/topic"


def test_sniff_requires_host_and_port():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["sniff"])
    args = parser.parse_args(["sniff", "--host", "192.168.200.7", "--port", "12345"])
    assert args.host == "192.168.200.7"
    assert args.port == 12345
    assert args.duration is None
    assert args.connect_retry == 300.0
    assert args.retry_interval == 2.0


async def test_sniff_decodes_a_valid_rtu_frame(capsys):
    body = bytes([1, 0x03, 0x00, 0x00, 0x00, 0x02])
    frame = body + codec.crc16_bytes(body)

    async def _handler(reader, writer):
        writer.write(frame)
        await writer.drain()
        await asyncio.sleep(0.05)  # let the client read before we close
        writer.close()

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        args = build_parser().parse_args(["sniff", "--host", "127.0.0.1", "--port", str(port), "--duration", "0.3"])
        await _run_sniff(args)
    finally:
        server.close()
        await server.wait_closed()

    out = capsys.readouterr().out
    assert "valid Modbus RTU frame: unit=1 FC03 read addr=0x0000 count=2" in out
    assert f"{len(frame)} bytes" in out
    assert "1 valid Modbus RTU frame(s)" in out


async def _free_port() -> int:
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    return port


async def test_sniff_retries_until_gateway_becomes_available(capsys):
    port = await _free_port()  # nothing listening here yet
    body = bytes([1, 0x03, 0x00, 0x00, 0x00, 0x02])
    frame = body + codec.crc16_bytes(body)

    async def _handler(reader, writer):
        writer.write(frame)
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()

    async def _start_server_late():
        await asyncio.sleep(0.3)  # simulate the gateway powering up after a delay
        return await asyncio.start_server(_handler, "127.0.0.1", port)

    server_task = asyncio.create_task(_start_server_late())
    try:
        args = build_parser().parse_args(
            [
                "sniff", "--host", "127.0.0.1", "--port", str(port),
                "--duration", "0.3", "--connect-retry", "5", "--retry-interval", "0.1",
            ]
        )
        await _run_sniff(args)
    finally:
        server = await server_task
        server.close()
        await server.wait_closed()

    out = capsys.readouterr().out
    assert "attempt 1 failed" in out
    assert "valid Modbus RTU frame: unit=1 FC03 read addr=0x0000 count=2" in out


async def test_sniff_gives_up_after_connect_retry_expires():
    port = await _free_port()  # nothing ever listens here
    args = build_parser().parse_args(
        ["sniff", "--host", "127.0.0.1", "--port", str(port), "--connect-retry", "0.3", "--retry-interval", "0.1"]
    )
    with pytest.raises(OSError):
        await _run_sniff(args)


async def test_sniff_reports_no_traffic(capsys):
    async def _handler(reader, writer):
        await asyncio.sleep(1)  # never sends anything within the sniff duration
        writer.close()

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        args = build_parser().parse_args(["sniff", "--host", "127.0.0.1", "--port", str(port), "--duration", "0.2"])
        await _run_sniff(args)
    finally:
        server.close()
        await server.wait_closed()

    out = capsys.readouterr().out
    assert "0 bytes, 0 valid Modbus RTU frame(s)" in out
    assert "No traffic at all" in out


async def test_sniff_emulate_answers_requests(capsys):
    body = bytes([1, 0x03, 0x00, 0x00, 0x00, 0x02])  # read V L1-N
    request = body + codec.crc16_bytes(body)
    received_response: list = []

    async def _handler(reader, writer):
        writer.write(request)
        await writer.drain()
        response = await reader.read(64)
        received_response.append(response)
        writer.close()

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        args = build_parser().parse_args(
            ["sniff", "--host", "127.0.0.1", "--port", str(port), "--duration", "0.5", "--emulate", "--unit-id", "1"]
        )
        await _run_sniff(args)
    finally:
        server.close()
        await server.wait_closed()

    assert len(received_response) == 1
    response = received_response[0]
    assert response[0] == 1  # unit id
    assert response[1] == 0x03  # function code
    payload = response[3 : 3 + response[2]]
    raw = int.from_bytes(payload[2:4], "big") << 16 | int.from_bytes(payload[0:2], "big")
    assert raw == 2300  # MeterState() defaults l1.voltage to 230.0V

    out = capsys.readouterr().out
    assert "emulating unit id 1" in out
    assert "answered:" in out


async def test_sniff_emulate_ignores_wrong_unit_id(capsys):
    body = bytes([9, 0x03, 0x00, 0x00, 0x00, 0x02])  # a different unit id than configured
    request = body + codec.crc16_bytes(body)
    got_response: list = []

    async def _handler(reader, writer):
        writer.write(request)
        await writer.drain()
        try:
            data = await asyncio.wait_for(reader.read(64), timeout=0.3)
            got_response.append(data)
        except asyncio.TimeoutError:
            got_response.append(b"")
        writer.close()

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        args = build_parser().parse_args(
            ["sniff", "--host", "127.0.0.1", "--port", str(port), "--duration", "0.5", "--emulate", "--unit-id", "1"]
        )
        await _run_sniff(args)
    finally:
        server.close()
        await server.wait_closed()

    assert got_response == [b""]  # no answer, since unit id 9 != configured 1
