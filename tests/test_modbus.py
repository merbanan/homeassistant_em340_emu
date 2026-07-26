import pytest

from em340_emu.model import MeterState
from em340_emu.modbus import (
    EXC_ILLEGAL_DATA_ADDRESS,
    EXC_ILLEGAL_FUNCTION,
    ModbusException,
    build_exception_pdu,
    handle_pdu,
)
from em340_emu.registers import RegisterMap


@pytest.fixture
def state():
    s = MeterState()
    s.l1.voltage = 230.0
    return s


@pytest.fixture
def registers():
    return RegisterMap()


def test_fc03_read_holding(state, registers):
    pdu = bytes([0x03, 0x00, 0x00, 0x00, 0x02])  # V L1-N, 2 words
    response = handle_pdu(pdu, registers, state)
    assert response[0] == 0x03
    assert response[1] == 4  # byte count
    raw = int.from_bytes(response[2:6], "big")
    lo, hi = raw >> 16, raw & 0xFFFF
    value = (hi << 16) | lo
    assert value == 2300


def test_fc04_identical_to_fc03(state, registers):
    pdu3 = bytes([0x03, 0x00, 0x00, 0x00, 0x02])
    pdu4 = bytes([0x04, 0x00, 0x00, 0x00, 0x02])
    r3 = handle_pdu(pdu3, registers, state)
    r4 = handle_pdu(pdu4, registers, state)
    assert r3[1:] == r4[1:]
    assert r3[0] == 0x03 and r4[0] == 0x04


def test_fc06_write_rejected(state, registers):
    pdu = bytes([0x06, 0x00, 0x00, 0x00, 0x01])
    with pytest.raises(ModbusException) as exc_info:
        handle_pdu(pdu, registers, state)
    assert exc_info.value.code == EXC_ILLEGAL_DATA_ADDRESS


def test_fc06_measurement_mode_write_accepted_as_noop(state, registers):
    # 0x1103 = measurement mode selection; the emulator is always PF.B, so
    # a write here is accepted (echoed) rather than rejected.
    pdu = bytes([0x06, 0x11, 0x03, 0x00, 0x01])
    response = handle_pdu(pdu, registers, state)
    assert response == pdu

    words = registers.read(state, 0x1103, 1)
    assert words[0] == 1  # still reports B regardless of what was "written"


def test_fc08_echo(state, registers):
    pdu = bytes([0x08, 0x00, 0x00, 0x12, 0x34])
    response = handle_pdu(pdu, registers, state)
    assert response == pdu


def test_unknown_function_raises_illegal_function(state, registers):
    pdu = bytes([0x2B, 0x00])
    with pytest.raises(ModbusException) as exc_info:
        handle_pdu(pdu, registers, state)
    assert exc_info.value.code == EXC_ILLEGAL_FUNCTION


def test_unimplemented_address_reads_as_zero_by_default(state, registers):
    # Courtesy mode is the default (see registers.RegisterMap docstring);
    # only strict mode raises for an address we haven't implemented.
    pdu = bytes([0x03, 0x99, 0x99, 0x00, 0x01])
    response = handle_pdu(pdu, registers, state)
    assert response == bytes([0x03, 0x02, 0x00, 0x00])


def test_illegal_address_maps_to_exception_in_strict_mode(state):
    strict_registers = RegisterMap(strict=True)
    pdu = bytes([0x03, 0x99, 0x99, 0x00, 0x01])
    with pytest.raises(ModbusException) as exc_info:
        handle_pdu(pdu, strict_registers, state)
    assert exc_info.value.code == EXC_ILLEGAL_DATA_ADDRESS


def test_build_exception_pdu():
    assert build_exception_pdu(0x03, 0x02) == bytes([0x83, 0x02])
