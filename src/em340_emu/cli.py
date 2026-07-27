from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import math
import sys
import time
from pathlib import Path

from .failsafe import FailSafeConfig, FailSafeMonitor
from .framing import RTUFramer
from .model import MeterState
from .mqtt_source import DEFAULT_PORT as DEFAULT_MQTT_PORT
from .mqtt_source import MqttSource
from .parameters import PARAMETERS
from .registers import RegisterMap
from .server import ModbusGatewayServer, _describe_pdu, connect_with_retry
from .sources import apply_values

log = logging.getLogger("em340_emu.cli")

# The RS485-to-Ethernet gateway setup this was built for has a P1/HAN
# reader publishing parsed readings to this broker/topic; used as `serve`'s
# default live-value source and by `view-readings`. Override with
# --mqtt-host/--mqtt-topic for a different setup.
DEFAULT_MQTT_HOST = "192.168.200.142"
DEFAULT_MQTT_TOPIC = "energy-meter/#"

DEFAULT_FAILSAFE_TIMEOUT = 60.0
DEFAULT_FAILSAFE_IMPORT_LIMIT_W = 11000.0  # a common Swedish 16A/phase 3-phase main fuse; adjust to your own


def _add_mqtt_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mqtt-host", default=DEFAULT_MQTT_HOST, help=f"MQTT broker host (default: {DEFAULT_MQTT_HOST})")
    parser.add_argument("--mqtt-port", type=int, default=DEFAULT_MQTT_PORT, help=f"MQTT broker port (default: {DEFAULT_MQTT_PORT})")
    parser.add_argument("--mqtt-topic", default=DEFAULT_MQTT_TOPIC, help=f"MQTT topic to subscribe to (default: {DEFAULT_MQTT_TOPIC!r})")
    parser.add_argument("--mqtt-username", default=None)
    parser.add_argument("--mqtt-password", default=None)


async def _watch_values_file(state: MeterState, path: Path, interval: float, monitor: FailSafeMonitor | None = None) -> None:
    last_mtime: float | None = None
    while True:
        try:
            mtime = path.stat().st_mtime
            if mtime != last_mtime:
                data = json.loads(path.read_text())
                apply_values(state, data)
                last_mtime = mtime
                log.info("reloaded values from %s", path)
                if monitor is not None:
                    monitor.touch()
        except FileNotFoundError:
            log.warning("values file %s not found yet, waiting", path)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("failed to read values file %s: %s", path, exc)
        await asyncio.sleep(interval)


async def _demo_simulation(state: MeterState, interval: float, monitor: FailSafeMonitor | None = None) -> None:
    """A small deterministic load pattern so `serve --demo` is useful with no
    external data source: L1 ramps like a single-phase EV charge session.
    """
    start = time.monotonic()
    for phase in state.phases():
        phase.voltage = 230.0
    while True:
        t = time.monotonic() - start
        charge_amps = max(0.0, 6.0 + 5.0 * math.sin(t / 30.0))
        state.l1.current = charge_amps
        state.l1.active_power_import = 230.0 * charge_amps
        state.energy_active_import += (state.active_power_total * interval) / 3_600_000
        if monitor is not None:
            monitor.touch()
        await asyncio.sleep(interval)


async def _run_serve(args: argparse.Namespace) -> None:
    state = MeterState()
    server = ModbusGatewayServer(
        state=state,
        unit_id=args.unit_id,
        host=args.host,
        port=args.port,
        framing=args.framing,
        registers=RegisterMap(strict=args.strict),
    )
    retry_desc = "retrying forever" if args.connect_retry is None else f"retrying up to {args.connect_retry:.0f}s"
    log.info(
        "connecting to gateway %s:%d (unit id %d, framing=%s, %s if needed, every %.0fs)",
        args.host, args.port, args.unit_id, args.framing, retry_desc, args.retry_interval,
    )

    monitor = FailSafeMonitor(
        state,
        FailSafeConfig(timeout=args.failsafe_timeout, import_limit_w=args.failsafe_import_limit),
    )
    if monitor.enabled:
        log.info(
            "fail-safe armed: %.0fs without an update ramps active power import to %.0fW (export to 0) over %.0fs",
            args.failsafe_timeout, args.failsafe_import_limit, monitor.config.ramp_seconds,
        )
    else:
        log.info("fail-safe disabled (--failsafe-timeout <= 0)")

    tasks = [
        asyncio.create_task(server.serve_as_client(connect_retry=args.connect_retry, retry_interval=args.retry_interval)),
        asyncio.create_task(monitor.run_forever()),
    ]
    mqtt_source: MqttSource | None = None
    if args.demo:
        tasks.append(asyncio.create_task(_demo_simulation(state, args.poll_interval, monitor)))
    elif args.values:
        tasks.append(asyncio.create_task(_watch_values_file(state, Path(args.values), args.poll_interval, monitor)))
    elif not args.no_mqtt:
        def _on_values(values: dict) -> None:
            apply_values(state, values)
            monitor.touch()

        mqtt_source = MqttSource(
            host=args.mqtt_host,
            port=args.mqtt_port,
            topic=args.mqtt_topic,
            username=args.mqtt_username,
            password=args.mqtt_password,
        )
        mqtt_source.start(_on_values)
        log.info("live values from mqtt://%s:%d/%s", args.mqtt_host, args.mqtt_port, args.mqtt_topic)
    else:
        log.warning("no live value source configured (--demo/--values/mqtt all off); fail-safe will engage after the timeout if enabled")

    try:
        await asyncio.gather(*tasks)
    finally:
        if mqtt_source is not None:
            mqtt_source.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _cmd_serve(args: argparse.Namespace) -> None:
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(_run_serve(args))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        # Only reachable with an explicit finite --connect-retry (the
        # default is to retry forever, see serve_as_client's docstring).
        log.error("could not connect to %s:%d: %s", args.host, args.port, exc)


def _cmd_registers(args: argparse.Namespace) -> None:
    for d in RegisterMap().describe():
        print(f"0x{d.address:04X}\t{d.length}w\t{d.fmt}\tx{d.scale}\t{d.name}")


def _format_readings_table(seen: dict, unknown_keys: set, last_message_at: float | None) -> str:
    lines = [
        "EM340 P1/HAN readings, live over MQTT (Ctrl+C to quit)",
        "",
        f"{'Parameter':<38}{'Value':>12}  {'Unit':<6}OBIS",
        "-" * 72,
    ]
    for p in PARAMETERS:
        value = seen.get(p.key)
        value_str = f"{value:g}" if value is not None else "—"
        lines.append(f"{p.label:<38}{value_str:>12}  {p.unit:<6}{p.obis or '-'}")
    if unknown_keys:
        lines.append("")
        lines.append(f"Unrecognized keys seen on the topic: {', '.join(sorted(unknown_keys))}")
    lines.append("")
    if last_message_at is None:
        lines.append("Waiting for the first message...")
    else:
        age = time.monotonic() - last_message_at
        lines.append(f"Last message: {age:.1f}s ago ({datetime.datetime.now().strftime('%H:%M:%S')})")
    return "\n".join(lines)


async def _run_view_readings(args: argparse.Namespace) -> None:
    known_keys = {p.key for p in PARAMETERS}
    seen: dict = {}
    unknown_keys: set = set()
    last_message_at: dict = {"t": None}

    def _redraw() -> None:
        text = _format_readings_table(seen, unknown_keys, last_message_at["t"])
        if sys.stdout.isatty():
            print("\033[2J\033[H" + text, flush=True)
        else:
            print(text + "\n---", flush=True)

    def _on_values(values: dict) -> None:
        for key, value in values.items():
            if key in known_keys:
                seen[key] = value
            else:
                unknown_keys.add(key)
        last_message_at["t"] = time.monotonic()
        _redraw()

    source = MqttSource(
        host=args.mqtt_host, port=args.mqtt_port, topic=args.mqtt_topic,
        username=args.mqtt_username, password=args.mqtt_password,
    )
    source.start(_on_values)
    log.info("subscribed to mqtt://%s:%d/%s", args.mqtt_host, args.mqtt_port, args.mqtt_topic)
    _redraw()
    try:
        while True:
            await asyncio.sleep(1)
            _redraw()  # keeps "last message Ns ago" ticking even when idle
    finally:
        source.stop()


def _cmd_view_readings(args: argparse.Namespace) -> None:
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(_run_view_readings(args))
    except KeyboardInterrupt:
        pass


async def _connect_with_retry(host: str, port: int, max_wait: float, retry_interval: float):
    """`sniff`'s thin wrapper around server.connect_with_retry, printing
    progress instead of logging it (this is a foreground diagnostic tool,
    not a background service). See that function's docstring for why
    retrying across a multi-minute window matters."""
    def _print_retry(attempt: int, exc: OSError, remaining: float) -> None:
        print(f"  attempt {attempt} failed ({exc}); retrying (up to {remaining:.0f}s left)...", flush=True)

    return await connect_with_retry(host, port, max_wait, retry_interval, on_retry=_print_retry)


async def _run_sniff(args: argparse.Namespace) -> None:
    """Connect to a gateway and log everything it sends.

    In plain (default) mode this is purely passive -- it answers nothing --
    for checking whether an RS485-to-Ethernet gateway is actually
    forwarding bus traffic at all. With --emulate, it additionally answers
    as an EM340 over that same outbound connection: some gateways (like
    this project's own real one, discovered via its web UI) are configured
    as a TCP *server* that the RS485 master's traffic is only reachable
    through by dialing in, the opposite of `serve`'s assumption that the
    gateway dials out to us -- so emulating here means connecting out,
    the same way sniffing already does, rather than listening.
    """
    print(f"Connecting to {args.host}:{args.port} (retrying for up to {args.connect_retry:.0f}s if needed) ...", flush=True)
    reader, writer = await _connect_with_retry(args.host, args.port, args.connect_retry, args.retry_interval)
    duration_note = f", or up to {args.duration:.0f}s" if args.duration else ""
    mode_note = f", emulating unit id {args.unit_id}" if args.emulate else ""
    print(f"Connected. Listening for traffic (Ctrl+C to stop{duration_note}){mode_note} ...", flush=True)

    rtu = RTUFramer()
    stats = {"bytes": 0, "frames": 0}
    start = time.monotonic()

    dispatcher = None
    demo_task = None
    if args.emulate:
        state = MeterState()
        dispatcher = ModbusGatewayServer(state=state, unit_id=args.unit_id, registers=RegisterMap(strict=args.strict))
        if args.demo:
            demo_task = asyncio.create_task(_demo_simulation(state, 1.0))

    async def _read_loop() -> None:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                print("Connection closed by remote end.", flush=True)
                return
            stats["bytes"] += len(chunk)
            timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[{timestamp}] {len(chunk)} bytes: {chunk.hex()}", flush=True)
            for frame in rtu.feed(chunk):
                stats["frames"] += 1
                print(f"    -> valid Modbus RTU frame: unit={frame.unit_id} {_describe_pdu(frame.pdu)}", flush=True)
                if dispatcher is not None:
                    response = dispatcher._dispatch(frame.unit_id, frame.pdu)
                    if response is not None:
                        writer.write(RTUFramer.build_response(frame.unit_id, response))
                        await writer.drain()
                        exception_note = " (exception)" if response[0] & 0x80 else ""
                        print(f"    <- answered{exception_note}: {response.hex()}", flush=True)

    try:
        if args.duration:
            try:
                await asyncio.wait_for(_read_loop(), timeout=args.duration)
            except asyncio.TimeoutError:
                pass
        else:
            await _read_loop()
    finally:
        if demo_task is not None:
            demo_task.cancel()
        writer.close()
        elapsed = time.monotonic() - start
        print(flush=True)
        print(f"--- {stats['bytes']} bytes, {stats['frames']} valid Modbus RTU frame(s) over {elapsed:.1f}s ---", flush=True)
        if stats["bytes"] == 0:
            print("No traffic at all. Check the gateway's power/wiring/config, and that the Wallbox is actually configured to poll a meter on this bus.", flush=True)
        elif stats["frames"] == 0:
            print("Bytes arrived but none formed a valid Modbus RTU frame (CRC never matched). Possible baud/parity/stop-bit mismatch between the gateway and the Wallbox, an A/B wiring swap, or non-Modbus traffic on this line.", flush=True)


def _cmd_sniff(args: argparse.Namespace) -> None:
    try:
        asyncio.run(_run_sniff(args))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"Could not connect to {args.host}:{args.port} after retrying for {args.connect_retry:.0f}s: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="em340-emu", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="connect to the RS485-to-Ethernet gateway and serve as an EM340")
    serve.add_argument("--host", required=True, help="gateway host/IP to connect to")
    serve.add_argument("--port", type=int, required=True, help="gateway TCP port")
    serve.add_argument("--unit-id", type=int, default=1, help="Modbus slave/unit id to answer as (default: 1)")
    serve.add_argument("--framing", choices=["auto", "rtu", "tcp"], default="auto", help="wire framing used by the gateway (default: auto-detect per connection)")
    serve.add_argument("--strict", action="store_true", help="raise a Modbus exception for any unimplemented register instead of the default courtesy-mode 0 (see RegisterMap docstring)")
    serve.add_argument("--connect-retry", type=float, default=None, help="give up and exit after this many seconds of failed connection attempts, instead of the default of retrying forever (recommended for a persistent service)")
    serve.add_argument("--retry-interval", type=float, default=15.0, help="seconds between connection attempts (default: 15.0)")
    serve.add_argument("--values", help="path to a JSON file with live readings, hot-reloaded on change")
    serve.add_argument("--poll-interval", type=float, default=1.0, help="seconds between checks of --values / demo ticks (default: 1.0)")
    serve.add_argument("--demo", action="store_true", help="ignore --values/mqtt and run a self-contained demo load pattern")
    serve.add_argument("--no-mqtt", action="store_true", help="disable the default MQTT live-value source (readings stay at 0 unless --values/--demo is given)")
    _add_mqtt_arguments(serve)
    serve.add_argument("--failsafe-timeout", type=float, default=DEFAULT_FAILSAFE_TIMEOUT, help=f"seconds without an update before fail-safe engages; <= 0 disables it (default: {DEFAULT_FAILSAFE_TIMEOUT:.0f})")
    serve.add_argument("--failsafe-import-limit", type=float, default=DEFAULT_FAILSAFE_IMPORT_LIMIT_W, help=f"total active power import (W) to ramp towards, split across phases, when fail-safe engages (default: {DEFAULT_FAILSAFE_IMPORT_LIMIT_W:.0f})")
    serve.add_argument("--log-level", default="INFO")
    serve.set_defaults(func=_cmd_serve)

    registers = sub.add_parser("registers", help="print the implemented register map")
    registers.set_defaults(func=_cmd_registers)

    view_readings = sub.add_parser("view-readings", help="live-print all P1/HAN parameters as they're broadcast over MQTT")
    _add_mqtt_arguments(view_readings)
    view_readings.add_argument("--log-level", default="WARNING")
    view_readings.set_defaults(func=_cmd_view_readings)

    sniff = sub.add_parser("sniff", help="connect to a gateway and log/decode its traffic; optionally answer as an EM340 (diagnostic)")
    sniff.add_argument("--host", required=True, help="gateway host/IP to connect to")
    sniff.add_argument("--port", type=int, required=True, help="gateway TCP port")
    sniff.add_argument("--duration", type=float, default=None, help="stop listening after this many seconds once connected (default: run until Ctrl+C)")
    sniff.add_argument("--connect-retry", type=float, default=300.0, help="keep retrying the connection for up to this many seconds, e.g. to catch a gateway that only powers up when a charger starts (default: 300 = 5 minutes)")
    sniff.add_argument("--retry-interval", type=float, default=2.0, help="seconds between connection attempts (default: 2.0)")
    sniff.add_argument("--emulate", action="store_true", help="answer requests as an EM340 over this same outbound connection, instead of only logging passively (for a gateway configured as TCP server, the opposite of what `serve` assumes)")
    sniff.add_argument("--unit-id", type=int, default=1, help="Modbus slave/unit id to answer as when --emulate is given (default: 1)")
    sniff.add_argument("--demo", action="store_true", help="with --emulate, run the self-contained demo load pattern instead of all-default/zero values")
    sniff.add_argument("--strict", action="store_true", help="with --emulate, raise a Modbus exception for any unimplemented register instead of the default courtesy-mode 0")
    sniff.set_defaults(func=_cmd_sniff)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
