"""Transport framing for a Modbus TCP-facing listener.

An RS485-to-Ethernet gateway is typically configured in one of two ways:

* "transparent" mode, which forwards raw RTU bytes (address, function,
  data, 2-byte CRC) straight over the TCP socket -- referred to here as
  "rtu" framing.
* Modbus-TCP gateway mode, which strips the RTU address/CRC and wraps the
  PDU in a 7-byte MBAP header -- referred to here as "tcp" framing.

Since a TCP stream carries no inherent message boundaries, both framers work
by scanning a growing receive buffer for the first byte offset at which a
complete, self-consistent frame ends.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from . import codec

log = logging.getLogger("em340_emu.framing")

MAX_RTU_FRAME = 256
MAX_TCP_FRAME = 260


@dataclass
class RTUFrame:
    unit_id: int
    pdu: bytes
    raw: bytes  # full frame including address byte and CRC, for logging


class RTUFramer:
    """Extracts complete RTU frames from a byte stream via CRC scanning.

    Rather than hard-coding an expected length per function code, this scans
    candidate (start offset, length) windows and accepts the first one whose
    trailing 2 bytes are a valid CRC16 over the rest -- robust to any
    function code without a lookup table, and to leading line noise, at
    negligible cost for low-rate RS485 traffic.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[RTUFrame]:
        self._buf.extend(data)
        frames: list[RTUFrame] = []
        while True:
            before = len(self._buf)
            frame = self._try_extract()
            if frame is not None:
                frames.append(frame)
                continue
            if len(self._buf) != before:
                continue  # a confirmed-garbage prefix was dropped; rescan
            break
        return frames

    def _try_extract(self) -> RTUFrame | None:
        buf = self._buf
        n = len(buf)
        if n < 4:
            return None
        max_start = min(n, MAX_RTU_FRAME)
        for start in range(max_start):
            remaining = n - start
            if remaining < 4:
                break
            max_len = min(remaining, MAX_RTU_FRAME)
            # Extend the running CRC one message byte at a time (table-based,
            # O(1) per byte) instead of recomputing it from scratch for every
            # candidate length -- turns the start*length scan from O(n^3)
            # bit-loops into O(n^2) table lookups, which matters once a line
            # noise burst fills the buffer with bytes to search through.
            crc = 0xFFFF
            covered = 0
            for length in range(4, max_len + 1):
                needed = length - 2
                while covered < needed:
                    crc = codec.crc16_update(crc, buf[start + covered])
                    covered += 1
                if buf[start + length - 2] == (crc & 0xFF) and buf[start + length - 1] == (crc >> 8) & 0xFF:
                    end = start + length
                    candidate = bytes(buf[start:end])
                    if start > 0:
                        # Bytes before a frame we DID manage to decode are
                        # never a valid frame themselves (every start/length
                        # combination up to here was already tried and
                        # failed) -- e.g. line noise, a corrupted response,
                        # or a gateway/bus artifact unrelated to Modbus.
                        # Harmless to the emulator either way: just skipped,
                        # never raised as an error.
                        skipped = bytes(buf[:start])
                        log.debug("ignored %d unrecognized byte(s) before a valid frame: %s", len(skipped), skipped.hex())
                    del buf[:end]
                    return RTUFrame(unit_id=candidate[0], pdu=candidate[1:-2], raw=candidate)
        # No valid frame starts anywhere in the first max_start bytes, so
        # they are confirmed garbage (every possible length was tried at
        # every possible start). Drop them but keep a full frame's worth of
        # tail in case a valid frame starts within it and just needs more
        # bytes appended by the next feed() call.
        if n > MAX_RTU_FRAME:
            dropped = bytes(buf[: n - MAX_RTU_FRAME])
            del buf[: n - MAX_RTU_FRAME]
            log.debug("ignored %d unrecognized byte(s) (buffer full, no valid frame found): %s", len(dropped), dropped.hex())
        return None

    @staticmethod
    def build_response(unit_id: int, pdu: bytes) -> bytes:
        body = bytes([unit_id]) + pdu
        return body + codec.crc16_bytes(body)


@dataclass
class TCPFrame:
    transaction_id: int
    unit_id: int
    pdu: bytes


class MBAPFramer:
    """Standard Modbus TCP framing (MBAP header, no CRC)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[TCPFrame]:
        self._buf.extend(data)
        frames: list[TCPFrame] = []
        while True:
            frame = self._try_extract()
            if frame is None:
                break
            frames.append(frame)
        return frames

    def _try_extract(self) -> TCPFrame | None:
        buf = self._buf
        if len(buf) < 7:
            return None
        length = (buf[4] << 8) | buf[5]
        total = 6 + length
        if length < 1 or total > MAX_TCP_FRAME:
            # Not a plausible MBAP header; let the caller fall back to RTU.
            return None
        if len(buf) < total:
            return None
        transaction_id = (buf[0] << 8) | buf[1]
        unit_id = buf[6]
        pdu = bytes(buf[7:total])
        del buf[:total]
        return TCPFrame(transaction_id=transaction_id, unit_id=unit_id, pdu=pdu)

    @staticmethod
    def build_response(transaction_id: int, unit_id: int, pdu: bytes) -> bytes:
        length = 1 + len(pdu)
        header = bytes([(transaction_id >> 8) & 0xFF, transaction_id & 0xFF, 0, 0, (length >> 8) & 0xFF, length & 0xFF, unit_id])
        return header + pdu


def looks_like_mbap(data: bytes) -> bool:
    """Cheap structural heuristic: could this be an MBAP header?

    This is ambiguous by construction: an RTU request reading a register
    below 0x0100 (address hi-byte 0, address lo-byte 0, e.g. our own
    register 0x0000) also happens to satisfy it. Callers that need a
    reliable decision should try parsing as RTU (CRC is a much stronger
    signal) first and only fall back to this heuristic when that fails.
    """
    if len(data) < 6:
        return False
    if data[2] != 0 or data[3] != 0:
        return False
    length = (data[4] << 8) | data[5]
    return 2 <= length <= 253
