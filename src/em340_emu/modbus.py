"""Modbus PDU (protocol data unit) handling, transport-independent.

Implements the subset of the protocol the EM300/ET300 doc documents:
FC03 (read holding registers), FC04 (read input registers, treated
identically to FC03 per the doc), FC06 (write single holding register) and
FC08 sub-function 0000h (diagnostics: return query data).
"""
from __future__ import annotations

from . import codec
from .model import MeterState
from .registers import WRITABLE_NOOP_REGISTERS, IllegalDataAddress, IllegalDataValue, RegisterMap

FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_SINGLE = 0x06
FC_DIAGNOSTICS = 0x08

EXC_ILLEGAL_FUNCTION = 0x01
EXC_ILLEGAL_DATA_ADDRESS = 0x02
EXC_ILLEGAL_DATA_VALUE = 0x03


class ModbusException(Exception):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"Modbus exception 0x{code:02X}")


def handle_pdu(pdu: bytes, registers: RegisterMap, state: MeterState) -> bytes:
    """Process a request PDU (function code + data, no address/CRC) and
    return the response PDU. Raises ModbusException for exception responses.
    """
    if not pdu:
        raise ModbusException(EXC_ILLEGAL_FUNCTION)

    function = pdu[0]

    if function in (FC_READ_HOLDING, FC_READ_INPUT):
        if len(pdu) != 5:
            raise ModbusException(EXC_ILLEGAL_DATA_VALUE)
        start = (pdu[1] << 8) | pdu[2]
        count = (pdu[3] << 8) | pdu[4]
        try:
            words = registers.read(state, start, count)
        except IllegalDataValue as exc:
            raise ModbusException(EXC_ILLEGAL_DATA_VALUE) from exc
        except IllegalDataAddress as exc:
            raise ModbusException(EXC_ILLEGAL_DATA_ADDRESS) from exc
        payload = codec.words_to_bytes(words)
        return bytes([function, len(payload)]) + payload

    if function == FC_WRITE_SINGLE:
        if len(pdu) != 5:
            raise ModbusException(EXC_ILLEGAL_DATA_VALUE)
        address = (pdu[1] << 8) | pdu[2]
        if address in WRITABLE_NOOP_REGISTERS:
            # e.g. a setup tool writing the measurement-mode register to
            # force bidirectional/PF.B: accepted (echoed back per the
            # protocol doc's FC06 semantics) but never actually changes
            # anything, since the emulator is always PF.B.
            return pdu
        # Every other register is effectively read-only, same as a real
        # meter's measurement registers.
        raise ModbusException(EXC_ILLEGAL_DATA_ADDRESS)

    if function == FC_DIAGNOSTICS:
        if len(pdu) < 4:
            raise ModbusException(EXC_ILLEGAL_DATA_VALUE)
        sub_function = (pdu[1] << 8) | pdu[2]
        if sub_function != 0x0000:
            raise ModbusException(EXC_ILLEGAL_FUNCTION)
        return pdu  # "Return Query Data": echo the request verbatim

    raise ModbusException(EXC_ILLEGAL_FUNCTION)


def build_exception_pdu(function: int, code: int) -> bytes:
    return bytes([function | 0x80, code])
