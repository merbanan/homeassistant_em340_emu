import pytest

from em340_emu.model import MeterState
from em340_emu.registers import (
    EM340_IDENTIFICATION_CODE,
    ENVELOPES,
    MAX_READ_WORDS,
    MEASUREMENT_MODE_B,
    MEASUREMENT_MODE_REGISTER,
    PRODUCTION_YEAR,
    SERIAL_NUMBER,
    IllegalDataAddress,
    IllegalDataValue,
    RegisterMap,
)


@pytest.fixture
def state():
    s = MeterState()
    s.l1.voltage = 231.2
    s.l2.voltage = 229.8
    s.l3.voltage = 230.5
    s.l1.current = 6.321
    s.l1.active_power_import = 1450.0
    s.frequency = 50.0
    s.energy_active_import = 12345.6
    s.energy_active_export = 3.2
    return s


def test_no_overlaps_at_construction():
    # RegisterMap() raises ValueError on overlap; constructing it is the test.
    RegisterMap()


def test_describe_documents_the_identification_code_overlap():
    names_by_address = {d.address: d.name for d in RegisterMap().describe()}
    assert names_by_address[0x000A] == "V L3-L1"
    assert "identification code" in names_by_address[0x000B]


def test_full_envelope_readable_without_exception(state):
    reg = RegisterMap()
    for lo, hi in ENVELOPES:
        length = hi - lo + 1
        for offset in range(0, length, 20):
            count = min(20, length - offset)
            reg.read(state, lo + offset, count)


def test_voltage_l1_encoding(state):
    reg = RegisterMap()
    words = reg.read(state, 0x0000, 2)
    raw = (words[1] << 16) | words[0]
    assert raw == 2312  # 231.2 V * 10


def test_current_l1_encoding(state):
    reg = RegisterMap()
    words = reg.read(state, 0x000C, 2)
    raw = (words[1] << 16) | words[0]
    assert raw == 6321  # 6.321 A * 1000


def test_v_l3_l1_full_pair_read_gets_real_voltage(state):
    # Reading 0x000A+0x000B together (V L3-L1's normal 2-word slot) must
    # not be clobbered by the identification-code overlap at 0x000B.
    reg = RegisterMap()
    words = reg.read(state, 0x000A, 2)
    raw = (words[1] << 16) | words[0]
    assert raw == round(state.voltage_ll_avg * 10)


def test_isolated_read_of_000B_gets_identification_code(state):
    # A real Wallbox charger issues a standalone FC03 read of exactly
    # addr=0x000B count=1 (confirmed via --log-level DEBUG); that specific
    # access pattern must get the identification code, not V L3-L1's word.
    reg = RegisterMap()
    words = reg.read(state, 0x000B, 1)
    assert words[0] == EM340_IDENTIFICATION_CODE == 341


def test_wider_read_spanning_both_still_pairs_as_v_l3_l1(state):
    # 0x0006..0x000B (V L1-L2, V L2-L3, V L3-L1) includes 0x000A, so 0x000B
    # must still resolve as V L3-L1's high word, not the identification code.
    reg = RegisterMap()
    words = reg.read(state, 0x0006, 6)
    raw = (words[5] << 16) | words[4]  # last pair = V L3-L1
    assert raw == round(state.voltage_ll_avg * 10)


def test_max_words_register(state):
    reg = RegisterMap()
    words = reg.read(state, 0x2004, 1)
    assert words[0] == MAX_READ_WORDS == 50


def test_serial_number_block(state):
    # A real Wallbox reads this as one addr=0x5000 count=7 request.
    reg = RegisterMap()
    words = reg.read(state, 0x5000, 7)
    assert bytes(words) == SERIAL_NUMBER.encode("ascii")
    assert "".join(chr(w) for w in words) == SERIAL_NUMBER == "SN00001"


def test_production_year_register(state):
    reg = RegisterMap()
    words = reg.read(state, 0x5010, 1)
    assert words[0] == PRODUCTION_YEAR


def test_measurement_mode_is_always_b(state):
    reg = RegisterMap()
    words = reg.read(state, MEASUREMENT_MODE_REGISTER, 1)
    assert words[0] == MEASUREMENT_MODE_B == 1


def test_energy_totals(state):
    reg = RegisterMap()
    words = reg.read(state, 0x0034, 2)
    raw = (words[1] << 16) | words[0]
    assert raw == 123456  # 12345.6 kWh * 10

    words = reg.read(state, 0x004E, 2)
    raw = (words[1] << 16) | words[0]
    assert raw == 32  # 3.2 kWh * 10


def test_kwh_export_total_at_official_table_2_4_1_address(state):
    # Table 2.4-1 places "kWh (-) TOT" at physical address 0x004E -- an
    # earlier community-reference-based mapping had this at 0x0050 instead
    # (see module docstring). Confirmed directly: a real Wallbox polls
    # exactly addr=0x004E count=2 every single cycle; under the old wrong
    # mapping this silently read back 0, hiding exported/solar energy.
    reg = RegisterMap()
    words = reg.read(state, 0x004E, 2)
    raw = (words[1] << 16) | words[0]
    assert raw == 32  # 3.2 kWh * 10


def test_kvarh_export_total_at_official_table_2_4_1_address(state):
    state.energy_reactive_export = 1.5
    reg = RegisterMap()
    words = reg.read(state, 0x0050, 2)
    raw = (words[1] << 16) | words[0]
    assert raw == 15  # 1.5 kvarh * 10


def test_block_a_and_block_b_agree(state):
    reg = RegisterMap()
    a = reg.read(state, 0x0012, 2)  # W L1, block A
    b = reg.read(state, 0x0124, 2)  # W L1, block B
    assert a == b


def test_negative_active_power_signed(state):
    state.l1.active_power_export = 500.0  # net = 1450 - 500 = 950
    reg = RegisterMap()
    words = reg.read(state, 0x0012, 2)
    raw = (words[1] << 16) | words[0]
    assert raw == 9500

    state.l1.active_power_export = 2000.0  # net negative
    words = reg.read(state, 0x0012, 2)
    raw = (words[1] << 16) | words[0]
    if raw >= 2**31:
        raw -= 2**32
    assert raw == round((1450 - 2000) * 10)


def test_courtesy_mode_returns_zero_for_unimplemented_address_by_default(state):
    # Default behavior mirrors wallbox-powerboost-emulator's EM112 bridge:
    # unimplemented addresses read back as 0 rather than raising, so we
    # don't need to have specifically anticipated every register some
    # charger's detection routine happens to probe.
    reg = RegisterMap()
    assert reg.read(state, 0x9999, 1) == [0]


def test_courtesy_mode_fills_gaps_past_the_envelope_too(state):
    reg = RegisterMap()
    words = reg.read(state, 0x0050, 5)  # runs off the end of block A's envelope
    assert words[-1] == 0  # 0x0054, past the envelope, reads as 0 instead of raising


def test_strict_mode_raises_for_unimplemented_address(state):
    reg = RegisterMap(strict=True)
    with pytest.raises(IllegalDataAddress):
        reg.read(state, 0x9999, 1)


def test_strict_mode_raises_spanning_gap(state):
    reg = RegisterMap(strict=True)
    with pytest.raises(IllegalDataAddress):
        reg.read(state, 0x0050, 5)  # runs off the end of block A's envelope


def test_illegal_quantity(state):
    reg = RegisterMap()
    with pytest.raises(IllegalDataValue):
        reg.read(state, 0x0000, 0)
    with pytest.raises(IllegalDataValue):
        reg.read(state, 0x0000, 200)


def test_demand_peak_tracks_running_max():
    s = MeterState()
    reg = RegisterMap()
    s.l1.active_power_import = 1000.0
    reg.read(s, 0x003A, 2)
    s.l1.active_power_import = 500.0
    words = reg.read(s, 0x003A, 2)
    raw = (words[1] << 16) | words[0]
    assert raw == 10000  # peak stayed at 1000 W * 10, not the lower current value
