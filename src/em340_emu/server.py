"""Asyncio TCP listener that emulates an EM340 towards an RS485-to-Ethernet
gateway.

The gateway is expected to dial *into* this listener (its "TCP client"
mode) forwarding whatever the Wallbox charger sends as the Modbus master on
the RS485 bus. See framing.py for the two supported wire formats.
"""
from __future__ import annotations

import asyncio
import logging

from .framing import MBAPFramer, RTUFramer, looks_like_mbap
from .model import MeterState
from .modbus import ModbusException, build_exception_pdu, handle_pdu
from .registers import RegisterMap

log = logging.getLogger("em340_emu.server")

BROADCAST_UNIT_ID = 0
FC_WRITE_SINGLE = 0x06


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
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def sockets(self):
        return self._server.sockets if self._server else None

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
            return build_exception_pdu(pdu[0] if pdu else 0, exc.code)
        log.debug("unit=%d %s -> ok", unit_id, _describe_pdu(pdu))
        return None if is_broadcast else response


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
