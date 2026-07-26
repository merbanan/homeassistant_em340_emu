from em340_emu import codec


def test_crc16_known_vector():
    # Slave 1, FC03, read 10 registers starting at 0 -- a textbook Modbus example.
    msg = bytes.fromhex("0103000000 0A".replace(" ", ""))
    assert codec.crc16_modbus(msg) == 0xCDC5
    assert codec.crc16_bytes(msg) == bytes.fromhex("C5CD")


def test_crc16_residue_property():
    msg = bytes.fromhex("1101001300 25".replace(" ", ""))
    framed = msg + codec.crc16_bytes(msg)
    assert codec.crc16_modbus(framed) == 0x0000


def test_pack_unpack_int32_roundtrip():
    for value in (0, 1, -1, 12345, -12345, 2**31 - 1, -(2**31)):
        words = codec.pack_words(value, 2, signed=True)
        assert len(words) == 2
        assert codec.unpack_words(words, signed=True) == value


def test_pack_unpack_uint32_roundtrip():
    for value in (0, 1, 2**32 - 1, 123456789):
        words = codec.pack_words(value, 2, signed=False)
        assert codec.unpack_words(words, signed=False) == value


def test_word_order_is_lsw_first():
    # 0x00010002 -> low word 0x0002 at index 0, high word 0x0001 at index 1
    words = codec.pack_words(0x00010002, 2, signed=False)
    assert words == [0x0002, 0x0001]


def test_words_to_bytes_is_big_endian_per_word():
    assert codec.words_to_bytes([0x1234, 0x0002]) == bytes.fromhex("12340002")
    assert codec.bytes_to_words(bytes.fromhex("12340002")) == [0x1234, 0x0002]
