import logging

from em340_emu import codec
from em340_emu.framing import MBAPFramer, RTUFramer, looks_like_mbap


def _rtu_frame(unit: int, pdu: bytes) -> bytes:
    body = bytes([unit]) + pdu
    return body + codec.crc16_bytes(body)


def test_rtu_framer_single_frame():
    framer = RTUFramer()
    frame_bytes = _rtu_frame(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02]))
    frames = framer.feed(frame_bytes)
    assert len(frames) == 1
    assert frames[0].unit_id == 1
    assert frames[0].pdu == bytes([0x03, 0x00, 0x00, 0x00, 0x02])


def test_rtu_framer_byte_by_byte():
    framer = RTUFramer()
    frame_bytes = _rtu_frame(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02]))
    frames = []
    for b in frame_bytes:
        frames.extend(framer.feed(bytes([b])))
    assert len(frames) == 1
    assert frames[0].pdu == bytes([0x03, 0x00, 0x00, 0x00, 0x02])


def test_rtu_framer_multiple_frames_in_one_chunk():
    framer = RTUFramer()
    f1 = _rtu_frame(1, bytes([0x08, 0x00, 0x00, 0x11, 0x22]))
    f2 = _rtu_frame(1, bytes([0x03, 0x00, 0x0C, 0x00, 0x02]))
    frames = framer.feed(f1 + f2)
    assert len(frames) == 2
    assert frames[0].pdu[0] == 0x08
    assert frames[1].pdu[0] == 0x03


def test_rtu_framer_resyncs_after_garbage():
    framer = RTUFramer()
    good = _rtu_frame(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02]))
    garbage = b"\xff" * 600  # forces the resync threshold
    frames = framer.feed(garbage + good)
    frames.extend(framer.feed(b""))
    assert any(f.pdu == bytes([0x03, 0x00, 0x00, 0x00, 0x02]) for f in frames)


def test_rtu_framer_ignores_and_logs_short_unrecognized_bytes(caplog):
    # e.g. a gateway/bus artifact too short to ever cross the resync
    # threshold on its own (see test_rtu_framer_resyncs_after_garbage for
    # the large-garbage case) -- must not be lost or raised as an error,
    # just skipped, with a DEBUG-level trail for anyone investigating.
    caplog.set_level(logging.DEBUG, logger="em340_emu.framing")
    framer = RTUFramer()
    mystery = bytes.fromhex("8c00fc")
    good = _rtu_frame(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02]))

    frames = framer.feed(mystery)
    assert frames == []  # too short to decide anything yet

    frames = framer.feed(good)
    assert len(frames) == 1
    assert frames[0].pdu == bytes([0x03, 0x00, 0x00, 0x00, 0x02])

    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("3 unrecognized byte(s)" in m and "8c00fc" in m for m in debug_messages)


def test_rtu_framer_logs_large_garbage_drop(caplog):
    caplog.set_level(logging.DEBUG, logger="em340_emu.framing")
    framer = RTUFramer()
    good = _rtu_frame(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02]))
    garbage = b"\xff" * 600
    framer.feed(garbage + good)
    framer.feed(b"")

    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("unrecognized byte(s)" in m for m in debug_messages)


def test_mbap_framer_roundtrip():
    framer = MBAPFramer()
    pdu = bytes([0x03, 0x04, 0x08, 0x00, 0x01, 0x00, 0x02])
    header = bytes([0x00, 0x07, 0x00, 0x00, 0x00, len(pdu) + 1, 0x01])
    frames = framer.feed(header + pdu)
    assert len(frames) == 1
    assert frames[0].transaction_id == 0x0007
    assert frames[0].unit_id == 1
    assert frames[0].pdu == pdu


def test_mbap_framer_split_across_reads():
    framer = MBAPFramer()
    pdu = bytes([0x03, 0x00, 0x00, 0x00, 0x02])
    header = bytes([0x00, 0x01, 0x00, 0x00, 0x00, len(pdu) + 1, 0x01])
    whole = header + pdu
    frames = framer.feed(whole[:4])
    assert frames == []
    frames = framer.feed(whole[4:])
    assert len(frames) == 1
    assert frames[0].pdu == pdu


def test_looks_like_mbap():
    pdu = bytes([0x03, 0x00, 0x00, 0x00, 0x02])
    header = bytes([0x00, 0x01, 0x00, 0x00, 0x00, len(pdu) + 1, 0x01])
    assert looks_like_mbap(header + pdu)

    # An RTU request reading a register at or above 0x0100 has a nonzero
    # address hi-byte, which unambiguously fails the MBAP protocol-id check.
    rtu = _rtu_frame(1, bytes([0x03, 0x01, 0x20, 0x00, 0x02]))
    assert not looks_like_mbap(rtu)


def test_looks_like_mbap_is_ambiguous_for_low_register_reads():
    # Documents the known ambiguity: reading register 0x0000 by RTU also
    # satisfies the MBAP heuristic. Auto-detection must not rely on this
    # function alone -- see ModbusGatewayServer's RTU-first probing.
    rtu = _rtu_frame(1, bytes([0x03, 0x00, 0x00, 0x00, 0x02]))
    assert looks_like_mbap(rtu)


def test_response_builders():
    pdu = bytes([0x03, 0x02, 0x00, 0x01])
    rtu_response = RTUFramer.build_response(1, pdu)
    assert rtu_response[:-2] == bytes([1]) + pdu
    assert codec.crc16_bytes(rtu_response[:-2]) == rtu_response[-2:]

    tcp_response = MBAPFramer.build_response(0x0042, 1, pdu)
    assert tcp_response[:2] == bytes([0x00, 0x42])
    assert tcp_response[2:4] == b"\x00\x00"
    assert tcp_response[6] == 1
    assert tcp_response[7:] == pdu
