"""EM340 Modbus register map.

Addresses and encodings are taken from Carlo Gavazzi's "EM300 Series and
ET300 Series Communication Protocol" (v2 rev.17, 2021-07-05), physical
(word) addressing, tables 2.4-1 (instantaneous variables and meters grouped
by variable type) and 2.6-1 (the same data grouped by phase, address range
starting at 0x00F6). Both blocks are implemented and kept in sync from the
same MeterState so the emulator answers correctly regardless of which
address range a given Wallbox firmware/meter-profile happens to read.

Register 0x000B is documented in table 2.8-1 as the "Carlo Gavazzi Controls
identification code", which conflicts with table 2.4-1's own use of that
same address as the second word of the 32-bit "V L3-L1" value. A real
Wallbox charger's own request log (captured via `serve --log-level DEBUG`,
see README "Watching what a real master actually reads") confirmed it
issues a standalone FC03 read of exactly `addr=0x000B count=1` -- i.e. it
does probe the identification code in isolation, contradicting the
community EM330/EM340 emulator reference this project leaned on earlier
(docs.smart-stuff.nl/p1-modbus-dongle), which implements no
identification-code register at all.

Register.read() therefore resolves 0x000B contextually: a request that
also includes 0x000A (the low word of V L3-L1, e.g. reading the whole
2-word V L3-L1 field, or a wider block that happens to span both) gets
V L3-L1's real high word; a request for 0x000B on its own (not paired with
0x000A) gets the identification code instead. A real EM340 chip almost
certainly can't do this -- a physical register holds one fixed value
regardless of how it's read -- but since we aren't bound to replicate that
ambiguity, this gives correct answers for both access patterns instead of
one clobbering the other.

The export ("(-)") energy total addresses (0x0050/0x0052) below are also
taken from that same community reference, since the official protocol
doc's OCR left them ambiguous and the community mapping was independently
confirmed against two firmware versions.

Registers documented as "n.a." / "Not available, value = 0" are served as
a constant 0 rather than an illegal-address exception, matching real
device behaviour. Only addresses fully outside the implemented tables
raise an exception, per the protocol doc's own note: "reading values in
addresses not specified in the below tables returns an illegal data
address exception."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import codec
from .model import MeterState

MAX_READ_WORDS = 50  # per table 2.9-7, register 2004h default

# Carlo Gavazzi identification code for "EM340-DIN AV2 3 X S1 X" (table 2.8-2).
EM340_IDENTIFICATION_CODE = 341
IDENTIFICATION_CODE_REGISTER = 0x000B
V_L3_L1_LOW_WORD_REGISTER = 0x000A  # see module docstring for the overlap this resolves

ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03


class IllegalDataAddress(Exception):
    pass


class IllegalDataValue(Exception):
    pass


Getter = Callable[[MeterState], float] | int | float


@dataclass(frozen=True)
class RegisterDef:
    address: int
    length: int  # words
    fmt: str  # "int16" | "uint16" | "int32" | "uint32"
    scale: float
    getter: Getter
    name: str

    @property
    def signed(self) -> bool:
        return self.fmt.startswith("int")

    def raw_value(self, state: MeterState) -> int:
        value = self.getter(state) if callable(self.getter) else self.getter
        return round(value * self.scale)

    def words(self, state: MeterState) -> list[int]:
        n_words = 1 if self.fmt in ("int16", "uint16") else self.length
        return codec.pack_words(self.raw_value(state), n_words, self.signed)


def _reg(address: int, length: int, fmt: str, scale: float, getter: Getter, name: str) -> RegisterDef:
    return RegisterDef(address, length, fmt, scale, getter, name)


def _phase_energy_share(state: MeterState, total_attr: str) -> float:
    """Evenly split a system energy total across phases.

    No per-phase cumulative energy exists in the P1/HAN source data (see
    Bilaga 3 of the Swedish H1-port recommendation), so this is a documented
    approximation rather than a real per-phase measurement.
    """
    return getattr(state, total_attr) / 3


# --- Table 2.4-1: instantaneous variables and meters, grouped by variable type ---
_BLOCK_A: list[RegisterDef] = [
    _reg(0x0000, 2, "int32", 10, lambda s: s.l1.voltage, "V L1-N"),
    _reg(0x0002, 2, "int32", 10, lambda s: s.l2.voltage, "V L2-N"),
    _reg(0x0004, 2, "int32", 10, lambda s: s.l3.voltage, "V L3-N"),
    _reg(0x0006, 2, "int32", 10, lambda s: s.voltage_ll_avg, "V L1-L2"),
    _reg(0x0008, 2, "int32", 10, lambda s: s.voltage_ll_avg, "V L2-L3"),
    _reg(0x000A, 2, "int32", 10, lambda s: s.voltage_ll_avg, "V L3-L1"),
    _reg(0x000C, 2, "int32", 1000, lambda s: s.l1.current, "A L1"),
    _reg(0x000E, 2, "int32", 1000, lambda s: s.l2.current, "A L2"),
    _reg(0x0010, 2, "int32", 1000, lambda s: s.l3.current, "A L3"),
    _reg(0x0012, 2, "int32", 10, lambda s: s.l1.active_power, "W L1"),
    _reg(0x0014, 2, "int32", 10, lambda s: s.l2.active_power, "W L2"),
    _reg(0x0016, 2, "int32", 10, lambda s: s.l3.active_power, "W L3"),
    _reg(0x0018, 2, "int32", 10, lambda s: s.l1.apparent_power, "VA L1"),
    _reg(0x001A, 2, "int32", 10, lambda s: s.l2.apparent_power, "VA L2"),
    _reg(0x001C, 2, "int32", 10, lambda s: s.l3.apparent_power, "VA L3"),
    _reg(0x001E, 2, "int32", 10, lambda s: s.l1.reactive_power, "var L1"),
    _reg(0x0020, 2, "int32", 10, lambda s: s.l2.reactive_power, "var L2"),
    _reg(0x0022, 2, "int32", 10, lambda s: s.l3.reactive_power, "var L3"),
    _reg(0x0024, 2, "int32", 10, lambda s: s.voltage_ln_avg, "V L-N sys"),
    _reg(0x0026, 2, "int32", 10, lambda s: s.voltage_ll_avg, "V L-L sys"),
    _reg(0x0028, 2, "int32", 10, lambda s: s.active_power_total, "W sys"),
    _reg(0x002A, 2, "int32", 10, lambda s: s.apparent_power_total, "VA sys"),
    _reg(0x002C, 2, "int32", 10, lambda s: s.reactive_power_total, "var sys"),
    _reg(0x002E, 1, "int16", 1000, lambda s: s.l1.power_factor, "PF L1"),
    _reg(0x002F, 1, "int16", 1000, lambda s: s.l2.power_factor, "PF L2"),
    _reg(0x0030, 1, "int16", 1000, lambda s: s.l3.power_factor, "PF L3"),
    _reg(0x0031, 1, "int16", 1000, lambda s: s.power_factor_total, "PF sys"),
    _reg(0x0032, 1, "int16", 1, lambda s: s.phase_sequence, "Phase sequence"),
    _reg(0x0033, 1, "int16", 10, lambda s: s.frequency, "Hz"),
    _reg(0x0034, 2, "uint32", 10, lambda s: s.energy_active_import, "kWh(+) TOT"),
    _reg(0x0036, 2, "uint32", 10, lambda s: s.energy_reactive_import, "kvarh(+) TOT"),
    _reg(0x0038, 2, "int32", 10, lambda s: s.active_power_total, "W dmd"),
    _reg(0x003A, 2, "uint32", 10, lambda s: s.sample_demand_peak(), "kW dmd peak"),
    _reg(0x003C, 2, "uint32", 10, lambda s: s.energy_active_import, "kWh(+) PARTIAL"),
    _reg(0x003E, 2, "uint32", 10, lambda s: s.energy_reactive_import, "kvarh(+) PARTIAL"),
    _reg(0x0040, 2, "uint32", 10, lambda s: _phase_energy_share(s, "energy_active_import"), "kWh(+) L1"),
    _reg(0x0042, 2, "uint32", 10, lambda s: _phase_energy_share(s, "energy_active_import"), "kWh(+) L2"),
    _reg(0x0044, 2, "uint32", 10, lambda s: _phase_energy_share(s, "energy_active_import"), "kWh(+) L3"),
    _reg(0x0050, 2, "uint32", 10, lambda s: s.energy_active_export, "kWh(-) TOT"),
    _reg(0x0052, 2, "uint32", 10, lambda s: s.energy_reactive_export, "kvarh(-) TOT"),
]
_BLOCK_A_ENVELOPE = (0x0000, 0x0053)

# --- Table 2.6-1: instantaneous variables and meters, grouped by phase ---
_BLOCK_B: list[RegisterDef] = [
    _reg(0x0102, 2, "int32", 10, lambda s: s.voltage_ln_avg, "V L-N sys"),
    _reg(0x0104, 2, "int32", 10, lambda s: s.voltage_ll_avg, "V L-L sys"),
    _reg(0x0106, 2, "int32", 10, lambda s: s.active_power_total, "W sys"),
    _reg(0x0108, 2, "int32", 10, lambda s: s.apparent_power_total, "VA sys"),
    _reg(0x010A, 2, "int32", 10, lambda s: s.reactive_power_total, "var sys"),
    _reg(0x010C, 2, "int32", 1000, lambda s: s.power_factor_total, "PF sys"),
    _reg(0x010E, 2, "int32", 1, lambda s: s.phase_sequence, "Phase sequence"),
    _reg(0x0110, 2, "int32", 10, lambda s: s.frequency, "Hz"),
    _reg(0x0112, 2, "uint32", 10, lambda s: s.energy_active_import, "kWh(+) TOT"),
    _reg(0x0114, 2, "uint32", 10, lambda s: s.energy_reactive_import, "kvarh(+) TOT"),
    _reg(0x0116, 2, "uint32", 10, lambda s: s.energy_active_export, "kWh(-) TOT"),
    _reg(0x0118, 2, "uint32", 10, lambda s: s.energy_reactive_export, "kvarh(-) TOT"),
    _reg(0x011A, 2, "int32", 10, lambda s: s.active_power_total, "W dmd"),
    _reg(0x011C, 2, "uint32", 10, lambda s: s.sample_demand_peak(), "W dmd peak"),
    _reg(0x011E, 2, "int32", 10, lambda s: s.voltage_ll_avg, "V L1-L2"),
    _reg(0x0120, 2, "int32", 10, lambda s: s.l1.voltage, "V L1-N"),
    _reg(0x0122, 2, "int32", 1000, lambda s: s.l1.current, "A L1"),
    _reg(0x0124, 2, "int32", 10, lambda s: s.l1.active_power, "W L1"),
    _reg(0x0126, 2, "int32", 10, lambda s: s.l1.apparent_power, "VA L1"),
    _reg(0x0128, 2, "int32", 10, lambda s: s.l1.reactive_power, "var L1"),
    _reg(0x012A, 2, "int32", 1000, lambda s: s.l1.power_factor, "PF L1"),
    _reg(0x012C, 2, "int32", 10, lambda s: s.voltage_ll_avg, "V L2-L3"),
    _reg(0x012E, 2, "int32", 10, lambda s: s.l2.voltage, "V L2-N"),
    _reg(0x0130, 2, "int32", 1000, lambda s: s.l2.current, "A L2"),
    _reg(0x0132, 2, "int32", 10, lambda s: s.l2.active_power, "W L2"),
    _reg(0x0134, 2, "int32", 10, lambda s: s.l2.apparent_power, "VA L2"),
    _reg(0x0136, 2, "int32", 10, lambda s: s.l2.reactive_power, "var L2"),
    _reg(0x0138, 2, "int32", 1000, lambda s: s.l2.power_factor, "PF L2"),
    _reg(0x013A, 2, "int32", 10, lambda s: s.voltage_ll_avg, "V L3-L1"),
    _reg(0x013C, 2, "int32", 10, lambda s: s.l3.voltage, "V L3-N"),
    _reg(0x013E, 2, "int32", 1000, lambda s: s.l3.current, "A L3"),
    _reg(0x0140, 2, "int32", 10, lambda s: s.l3.active_power, "W L3"),
    _reg(0x0142, 2, "int32", 10, lambda s: s.l3.apparent_power, "VA L3"),
    _reg(0x0144, 2, "int32", 10, lambda s: s.l3.reactive_power, "var L3"),
    _reg(0x0146, 2, "int32", 1000, lambda s: s.l3.power_factor, "PF L3"),
    _reg(0x0148, 2, "uint32", 10, lambda s: s.energy_active_import, "kWh(+) PARTIAL"),
    _reg(0x014A, 2, "uint32", 10, lambda s: s.energy_reactive_import, "kvarh(+) PARTIAL"),
    _reg(0x014C, 2, "uint32", 10, lambda s: _phase_energy_share(s, "energy_active_import"), "kWh(+) L1"),
    _reg(0x014E, 2, "uint32", 10, lambda s: _phase_energy_share(s, "energy_active_import"), "kWh(+) L2"),
    _reg(0x0150, 2, "uint32", 10, lambda s: _phase_energy_share(s, "energy_active_import"), "kWh(+) L3"),
]
_BLOCK_B_ENVELOPE = (0x00F6, 0x01B5)

# --- Table 2.7-1: firmware version / revision ---
_BLOCK_C: list[RegisterDef] = [
    _reg(0x0302, 1, "uint16", 1, 0, "Version code"),
    _reg(0x0303, 1, "uint16", 1, 0, "Revision code"),
]

# --- Table 2.9-5: measurement mode, fixed to "B" (bidirectional/PF.B) ---
# The emulated device is always the PF.B variant: signed, bidirectional
# active/reactive power and separate import/export energy totalizers (see
# section 2.2 "Geometric representation" of the protocol doc), which is
# exactly what registers 0x0050/0x0052/0x0116/0x0118 (kWh/kvarh "(-)" TOT)
# implement above. This register reports that explicitly, since it's what a
# Wallbox or configuration tool would read to confirm the meter is PF.B.
MEASUREMENT_MODE_B = 1
MEASUREMENT_MODE_REGISTER = 0x1103
_BLOCK_D: list[RegisterDef] = [
    _reg(MEASUREMENT_MODE_REGISTER, 1, "uint16", 1, MEASUREMENT_MODE_B, "Measurement mode selection (B)"),
]

# --- Table 2.9-7: max words readable per request; also confirmed at this
# same address by docs.smart-stuff.nl's EM330/EM340 reference. Genuinely
# true for this emulator too (see MAX_READ_WORDS), not a fabricated value.
_BLOCK_E: list[RegisterDef] = [
    _reg(0x2004, 1, "uint16", 1, MAX_READ_WORDS, "Max number of words readable with a single request"),
]

# --- Table 2.9-9: serial number (one ASCII letter per word, MSB unused) and
# Table 2.9-10: production year. A real Wallbox charger's own request log
# (captured via `sniff --emulate --log-level DEBUG`) showed it reading
# addr=0x5000 count=7 -- the serial number block -- right after successfully
# reading the identification code, as part of its meter-detection sequence;
# without this it got an illegal-address exception at that point and
# apparently gave up rather than proceeding to read live measurements.
SERIAL_NUMBER = "SN00001"  # exactly 7 characters; the specific value shouldn't matter, only that it's present
_BLOCK_F: list[RegisterDef] = [
    _reg(0x5000 + i, 1, "uint16", 1, ord(ch), f"Serial number letter {i + 1}") for i, ch in enumerate(SERIAL_NUMBER)
]
PRODUCTION_YEAR = 2024
_BLOCK_G: list[RegisterDef] = [
    _reg(0x5010, 1, "uint16", 1, PRODUCTION_YEAR, "Production year"),
]

# Registers a real EM340 exposes as read/write where the emulator accepts a
# write (FC06) as a no-op echo rather than rejecting it outright, since the
# emulated value never changes regardless of what's written.
WRITABLE_NOOP_REGISTERS = {MEASUREMENT_MODE_REGISTER}

ALL_DEFS: list[RegisterDef] = [*_BLOCK_A, *_BLOCK_B, *_BLOCK_C, *_BLOCK_D, *_BLOCK_E, *_BLOCK_F, *_BLOCK_G]
ENVELOPES: list[tuple[int, int]] = [_BLOCK_A_ENVELOPE, _BLOCK_B_ENVELOPE]


class RegisterMap:
    """Resolves Modbus word addresses against a MeterState.

    By default this is permissive ("courtesy mode"): any address not
    explicitly implemented reads back as 0 instead of raising an exception.
    This mirrors the proven-working design of a real-world Carlo Gavazzi
    EM1xx/EM3xx emulator (wallbox-powerboost-emulator's EM112 bridge, whose
    own read_register() default case is `*value = 0; return !strict;`) --
    its README states plainly: "Unknown registers return 0 in courtesy
    mode. Strict mode returns exception 0x02 for illegal address."

    The alternative -- raising an exception for every address we haven't
    specifically anticipated -- means discovering, one at a time via
    traffic captures, every register some particular charger's detection
    routine happens to probe (which is exactly how 0x5000 and 0x1103 were
    found here). Defaulting to courtesy mode avoids that whack-a-mole
    entirely for whatever gets probed next. Pass strict=True to restore
    exceptions for genuinely unimplemented addresses, e.g. for protocol
    conformance testing.
    """

    def __init__(
        self,
        defs: list[RegisterDef] = ALL_DEFS,
        envelopes: list[tuple[int, int]] = ENVELOPES,
        strict: bool = False,
    ):
        self.strict = strict
        self._index: dict[int, tuple[RegisterDef, int]] = {}
        for d in defs:
            for i in range(d.length):
                addr = d.address + i
                if addr in self._index:
                    raise ValueError(f"register overlap at 0x{addr:04X} ({d.name})")
                self._index[addr] = (d, i)

        zero = RegisterDef(0, 1, "uint16", 1, 0, "unused")
        for lo, hi in envelopes:
            for addr in range(lo, hi + 1):
                self._index.setdefault(addr, (zero, 0))

    def read(self, state: MeterState, start: int, count: int) -> list[int]:
        if not (1 <= count <= MAX_READ_WORDS):
            raise IllegalDataValue(f"quantity {count} out of range 1..{MAX_READ_WORDS}")
        end = start + count
        if end > 0x10000:
            raise IllegalDataAddress(f"0x{end - 1:04X} out of Modbus address range")
        # 0x000B is contextually either V L3-L1's high word or the
        # identification code -- see module docstring. It's the former only
        # when this same request also covers 0x000A (V L3-L1's low word).
        paired_with_v_l3_l1 = start <= V_L3_L1_LOW_WORD_REGISTER < end
        words: list[int] = []
        for addr in range(start, end):
            if addr == IDENTIFICATION_CODE_REGISTER and not paired_with_v_l3_l1:
                words.append(EM340_IDENTIFICATION_CODE)
                continue
            if addr not in self._index:
                if self.strict:
                    raise IllegalDataAddress(f"0x{addr:04X} not implemented")
                words.append(0)
                continue
            d, i = self._index[addr]
            words.append(d.words(state)[i])
        return words

    def describe(self) -> list[RegisterDef]:
        """Real (non-filler) register definitions, for docs/introspection.

        Includes a synthetic entry for the identification code even though
        it isn't in the address index (it can't be: 0x000B is already
        claimed by V L3-L1 there) -- read() resolves it contextually
        instead. See module docstring.
        """
        defs = {d for d, _ in self._index.values() if d.name != "unused"}
        id_code_doc = RegisterDef(
            IDENTIFICATION_CODE_REGISTER, 1, "uint16", 1, EM340_IDENTIFICATION_CODE,
            "Carlo Gavazzi identification code (only when read in isolation; V L3-L1 high word when read together with 0x000A)",
        )
        defs.add(id_code_doc)
        return sorted(defs, key=lambda d: d.address)
