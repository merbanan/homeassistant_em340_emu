"""Modbus CRC16 and Carlo Gavazzi EM/ET300-series register packing.

Per the EM300/ET300 communication protocol (section 2.1): every 16-bit word
is transmitted MSB first, then LSB (standard Modbus big-endian), but 32-bit
and 64-bit values are split into words in LSW->MSW order -- i.e. the word at
the lower register address holds the least-significant 16 bits.
"""
from __future__ import annotations

_CRC16_POLY = 0xA001


def _build_crc16_table() -> tuple[int, ...]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ _CRC16_POLY if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_CRC16_TABLE = _build_crc16_table()


def crc16_update(crc: int, byte: int) -> int:
    """Extend a running CRC16 by one byte (table-based, O(1))."""
    return (crc >> 8) ^ _CRC16_TABLE[(crc ^ byte) & 0xFF]


def crc16_modbus(data: bytes) -> int:
    """Return the Modbus RTU CRC16 (little-endian value) for data."""
    crc = 0xFFFF
    for byte in data:
        crc = crc16_update(crc, byte)
    return crc


def crc16_bytes(data: bytes) -> bytes:
    """CRC16 as the 2 bytes appended to an RTU frame (low byte, high byte)."""
    crc = crc16_modbus(data)
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def pack_words(value: int, n_words: int, signed: bool) -> list[int]:
    """Split an integer into n_words 16-bit words, LSW first."""
    mask = (1 << (16 * n_words)) - 1
    raw = value & mask if signed else value
    words = []
    for i in range(n_words):
        words.append((raw >> (16 * i)) & 0xFFFF)
    return words


def unpack_words(words: list[int], signed: bool) -> int:
    """Combine 16-bit words (LSW first) into an integer."""
    raw = 0
    for i, word in enumerate(words):
        raw |= (word & 0xFFFF) << (16 * i)
    if signed:
        bits = 16 * len(words)
        if raw & (1 << (bits - 1)):
            raw -= 1 << bits
    return raw


def words_to_bytes(words: list[int]) -> bytes:
    """Each word big-endian (MSB, LSB), words concatenated in given order."""
    out = bytearray()
    for word in words:
        out.append((word >> 8) & 0xFF)
        out.append(word & 0xFF)
    return bytes(out)


def bytes_to_words(data: bytes) -> list[int]:
    if len(data) % 2:
        raise ValueError("byte payload must have an even length")
    return [(data[i] << 8) | data[i + 1] for i in range(0, len(data), 2)]
