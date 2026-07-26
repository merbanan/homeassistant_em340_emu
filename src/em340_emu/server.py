"""Asyncio TCP client that emulates an EM340 towards an RS485-to-Ethernet
gateway.

Every gateway actually used with this project has turned out to be a TCP
*server* in its own right (confirmed via a real gateway's own web UI,
which showed "Work Mode: TCP Server") -- so this dials *out* to the
gateway via serve_as_client(), retrying the connection (including
reconnecting if it later drops) rather than listening for the gateway to
dial in. An earlier version also supported a listen mode for the opposite
arrangement; it was removed once real hardware confirmed dial-out is the
only arrangement this project needs.

See framing.py for the two supported wire formats; _handle_client() does
the actual framing/dispatch and only needs a reader/writer pair.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from .framing import MBAPFramer, RTUFramer, looks_like_mbap
from .model import MeterState
from .modbus import ModbusException, build_exception_pdu, handle_pdu
from .registers import RegisterMap

log = logging.getLogger("em340_emu.server")

BROADCAST_UNIT_ID = 0
FC_WRITE_SINGLE = 0x06


async def connect_with_retry(
    host: str,
    port: int,
    max_wait: float,
    retry_interval: float,
    on_retry: Callable[[int, OSError, float], None] | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Keep trying to connect for up to max_wait seconds.

    Some gateways are only reachable once whatever they're wired to has
    itself finished booting (e.g. a Wallbox powering its own RS485-to-
    Ethernet converter). Retrying across a multi-minute window means a
    client-mode connection can be started ahead of time and still catch
    that window instead of needing perfect timing. on_retry, if given, is
    called with (attempt_number, exception, seconds_remaining) before each
    retry sleep, e.g. to log or print progress.
    """
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            return await asyncio.open_connection(host, port)
        except OSError as exc:
            elapsed = time.monotonic() - start
            if elapsed >= max_wait:
                raise
            remaining = max_wait - elapsed
            if on_retry is not None:
                on_retry(attempt, exc, remaining)
            await asyncio.sleep(min(retry_interval, remaining))


class ModbusGatewayServer:
    def __init__(
        self,
        state: MeterState,
        unit_id: int = 1,
        host: str = "0.0.0.0",
        port: int = 502,
        framing: str = "auto",
        registers: RegisterMap | None = None,
    ) -> None:
        if framing not in ("auto", "rtu", "tcp"):
            raise ValueError("framing must be 'auto', 'rtu' or 'tcp'")
        self.state = state
        self.unit_id = unit_id
        self.host = host
        self.port = port
        self.framing = framing
        self.registers = registers or RegisterMap()
        # Simple running totals for observability (e.g. the HA integration's
        # diagnostic sensors): every request seen (any unit id, including
        # ones filtered out below) vs. every one actually answered.
        self.request_count = 0
        self.response_count = 0

    async def serve_as_client(self, connect_retry: float = 300.0, retry_interval: float = 2.0) -> None:
        """Dial out to self.host:self.port and serve requests over that
        connection, for a gateway that is itself a TCP server. Runs
        forever: reconnects (retrying for up to connect_retry seconds each
        time) whenever the connection drops, e.g. after a gateway reboot.
        Meant to be run as a background task and cancelled to stop it --
        cancellation propagates into _handle_client(), whose finally block
        closes the connection, so no separate stop/close call is needed.
        """
        def _log_retry(attempt: int, exc: OSError, remaining: float) -> None:
            log.warning(
                "connect attempt %d to %s:%d failed (%s); retrying (%.0fs left)",
                attempt, self.host, self.port, exc, remaining,
            )

        while True:
            reader, writer = await connect_with_retry(
                self.host, self.port, connect_retry, retry_interval, on_retry=_log_retry
            )
            log.info("connected to gateway %s:%d", self.host, self.port)
            await self._handle_client(reader, writer)
            log.info("disconnected from gateway %s:%d; will reconnect", self.host, self.port)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("gateway connected: %s", peer)
        mode = None if self.framing == "auto" else self.framing
        rtu = RTUFramer()
        tcp = MBAPFramer()
        pending = b""
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break

                if mode is None:
                    pending += chunk
                    mode = self._detect_framing(peer, pending)
                    if mode is None:
                        continue  # not enough evidence yet, keep buffering
                    chunk, pending = pending, b""

                if mode == "tcp":
                    for frame in tcp.feed(chunk):
                        response = self._dispatch(frame.unit_id, frame.pdu)
                        if response is not None:
                            writer.write(MBAPFramer.build_response(frame.transaction_id, frame.unit_id, response))
                else:
                    for frame in rtu.feed(chunk):
                        response = self._dispatch(frame.unit_id, frame.pdu)
                        if response is not None:
                            writer.write(RTUFramer.build_response(frame.unit_id, response))
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            log.info("gateway disconnected: %s", peer)
            writer.close()

    @staticmethod
    def _detect_framing(peer, pending: bytes) -> str | None:
        """Pick 'rtu' or 'tcp' for a connection from its first bytes.

        A valid RTU CRC is checked first (on a disposable probe framer, so
        detection never consumes the bytes the real framer still needs): it
        is a ~1/65536-odds signal, far stronger than the MBAP structural
        heuristic, which is ambiguous for an RTU request reading any
        register below 0x0100 (see framing.looks_like_mbap). Falls back to
        the structural heuristic, and finally to assuming RTU (the more
        common transparent-gateway setup) once enough bytes have
        accumulated without a clear answer. Returns None while there isn't
        yet enough evidence to decide.
        """
        if len(pending) >= 4 and RTUFramer().feed(pending):
            log.debug("%s: detected rtu framing (valid CRC)", peer)
            return "rtu"
        if len(pending) < 6:
            return None
        if looks_like_mbap(pending):
            log.debug("%s: detected tcp framing", peer)
            return "tcp"
        if len(pending) >= 32:
            log.warning("%s: could not confidently detect framing, assuming rtu", peer)
            return "rtu"
        return None

    def _dispatch(self, unit_id: int, pdu: bytes) -> bytes | None:
        self.request_count += 1
        is_broadcast = unit_id == BROADCAST_UNIT_ID
        if not is_broadcast and unit_id != self.unit_id:
            log.debug("ignoring request for unit id %d (configured as %d)", unit_id, self.unit_id)
            return None
        if is_broadcast and (not pdu or pdu[0] != FC_WRITE_SINGLE):
            return None  # broadcast is only meaningful for FC06, and never gets a reply

        try:
            response = handle_pdu(pdu, self.registers, self.state)
        except ModbusException as exc:
            log.debug("unit=%d %s -> exception 0x%02X", unit_id, _describe_pdu(pdu), exc.code)
            if is_broadcast:
                return None
            self.response_count += 1
            return build_exception_pdu(pdu[0] if pdu else 0, exc.code)
        log.debug("unit=%d %s -> ok (request #%d)", unit_id, _describe_pdu(pdu), self.request_count)
        if is_broadcast:
            return None
        self.response_count += 1
        return response


def _describe_pdu(pdu: bytes) -> str:
    """Human-readable one-liner for a request PDU, for --log-level DEBUG.

    Meant for watching exactly what a real master (e.g. a Wallbox during its
    meter setup/test step) actually reads -- which registers, in what order,
    and whether any of them come back as an exception -- rather than
    guessing from documentation alone.
    """
    if not pdu:
        return "<empty>"
    function = pdu[0]
    if function in (0x03, 0x04) and len(pdu) == 5:
        start = (pdu[1] << 8) | pdu[2]
        count = (pdu[3] << 8) | pdu[4]
        return f"FC{function:02X} read addr=0x{start:04X} count={count}"
    if function == 0x06 and len(pdu) == 5:
        addr = (pdu[1] << 8) | pdu[2]
        value = (pdu[3] << 8) | pdu[4]
        return f"FC06 write addr=0x{addr:04X} value=0x{value:04X}"
    if function == 0x08 and len(pdu) >= 3:
        sub_function = (pdu[1] << 8) | pdu[2]
        return f"FC08 diagnostics sub=0x{sub_function:04X} data={pdu[3:].hex()}"
    return f"FC{function:02X} data={pdu[1:].hex()}"
