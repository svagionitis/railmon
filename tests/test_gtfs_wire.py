"""Unit tests for the zero-dependency Proto2 binary wire codec (gtfs_rt.wire)."""

import math
import struct
import unittest

from gtfs_rt.wire import (
    WireDecodeError,
    WireType,
    decode_double,
    decode_float,
    decode_utf8,
    decode_varint,
    encode_bool,
    encode_bytes,
    encode_double,
    encode_enum,
    encode_float,
    encode_int32,
    encode_int64,
    encode_string,
    encode_submsg,
    encode_tag,
    encode_uint32,
    encode_uint64,
    encode_varint,
    iter_wire_fields,
    to_signed_int32,
    to_signed_int64,
)


class TestGtfsWireCodec(unittest.TestCase):
    """Verify exact Protobuf proto2 wire encoding, decoding, and boundary guards."""

    def test_varint_golden_vectors(self):
        """TC-GTFS-WIRE-01: Standard unsigned and multi-byte varints match Protobuf spec."""
        vectors = [
            (0, b"\x00"),
            (1, b"\x01"),
            (127, b"\x7f"),
            (128, b"\x80\x01"),
            (150, b"\x96\x01"),
            (300, b"\xac\x02"),
            (1795469559, b"\xf7\xe1\x92\xd8\x06"),
        ]
        for val, expected in vectors:
            encoded = encode_varint(val)
            self.assertEqual(encoded, expected)
            decoded, next_off = decode_varint(encoded, 0)
            self.assertEqual(decoded, val)
            self.assertEqual(next_off, len(encoded))

    def test_negative_int32_ten_byte_twos_complement(self):
        """TC-GTFS-WIRE-02: Negative signed int32/int64 encode as 10-byte 64-bit two's complement."""
        neg_one = encode_varint(-1)
        self.assertEqual(neg_one, b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01")
        self.assertEqual(len(neg_one), 10)

        neg_120 = encode_varint(-120)
        self.assertEqual(neg_120, b"\x88\xff\xff\xff\xff\xff\xff\xff\xff\x01")
        raw, off = decode_varint(neg_120, 0)
        self.assertEqual(off, 10)
        self.assertEqual(to_signed_int32(raw), -120)
        self.assertEqual(to_signed_int64(raw), -120)

    def test_float_double_ieee754_little_endian(self):
        """TC-GTFS-WIRE-03: Float (FIXED32) and Double (FIXED64) encode little-endian IEEE-754."""
        f_bytes = encode_float(1, 37.9926)
        self.assertEqual(f_bytes[0], (1 << 3) | 5)
        self.assertEqual(f_bytes[1:], struct.pack("<f", 37.9926))
        self.assertAlmostEqual(decode_float(f_bytes[1:]), 37.9926, places=4)

        d_bytes = encode_double(4, 365600.5)
        self.assertEqual(d_bytes[0], (4 << 3) | 1)
        self.assertEqual(d_bytes[1:], struct.pack("<d", 365600.5))
        self.assertAlmostEqual(decode_double(d_bytes[1:]), 365600.5, places=6)

    def test_non_finite_float_and_double_rejected(self):
        """TC-GTFS-WIRE-04: NaN and +/-Inf are rejected by encode and decode guards."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                encode_float(1, bad)
            with self.assertRaises(ValueError):
                encode_double(4, bad)

        with self.assertRaises(WireDecodeError):
            decode_float(struct.pack("<f", math.nan))
        with self.assertRaises(WireDecodeError):
            decode_double(struct.pack("<d", math.inf))

    def test_utf8_greek_string_and_submessage_framing(self):
        """TC-GTFS-WIRE-05: Greek UTF-8 strings and nested submessages round-trip cleanly."""
        text = "Αθήνα (Σ.Σ. Λαρίσης) → Θεσσαλονίκη"
        raw = encode_string(1, text)
        fields = list(iter_wire_fields(raw))
        self.assertEqual(len(fields), 1)
        fno, wt, payload = fields[0]
        self.assertEqual(fno, 1)
        self.assertEqual(wt, WireType.LEN)
        self.assertEqual(decode_utf8(payload), text)

        outer = encode_submsg(2, raw)
        outer_fields = list(iter_wire_fields(outer))
        self.assertEqual(outer_fields[0][0], 2)
        self.assertEqual(outer_fields[0][2], raw)

    def test_scalar_field_helpers_and_bounds(self):
        """TC-GTFS-WIRE-06: uint32/uint64/int32/int64/bool/enum helpers enforce bounds."""
        buf = (
            encode_uint32(1, 42)
            + encode_uint64(2, 1795469559)
            + encode_int32(3, -180)
            + encode_int64(4, -9999999999)
            + encode_bool(5, True)
            + encode_enum(6, 3)
            + encode_bytes(7, b"\xde\xad\xbe\xef")
        )
        parsed = list(iter_wire_fields(buf))
        self.assertEqual(len(parsed), 7)
        self.assertEqual(parsed[0][2], 42)
        self.assertEqual(parsed[1][2], 1795469559)
        self.assertEqual(to_signed_int32(parsed[2][2]), -180)
        self.assertEqual(to_signed_int64(parsed[3][2]), -9999999999)
        self.assertEqual(parsed[4][2], 1)
        self.assertEqual(parsed[5][2], 3)
        self.assertEqual(parsed[6][2], b"\xde\xad\xbe\xef")

        with self.assertRaises(ValueError):
            encode_tag(0, WireType.VARINT)
        with self.assertRaises(ValueError):
            encode_uint32(1, -1)
        with self.assertRaises(ValueError):
            encode_uint64(1, -1)
        with self.assertRaises(ValueError):
            encode_int32(1, 1 << 31)

    def test_malformed_and_truncated_wire_buffers_raise(self):
        """TC-GTFS-WIRE-07: Truncated varints, lengths, fixed values, and bad UTF-8 raise WireDecodeError."""
        # Truncated varint (MSB continuation set with no following byte)
        with self.assertRaises(WireDecodeError):
            list(iter_wire_fields(b"\x08\x80"))

        # Overlong varint (> 10 continuation bytes)
        with self.assertRaises(WireDecodeError):
            decode_varint(b"\x80" * 11, 0)

        # Truncated LEN field (says length 10, only 2 bytes present)
        with self.assertRaises(WireDecodeError):
            list(iter_wire_fields(b"\x0a\x0a\x41\x42"))

        # Truncated FIXED32 and FIXED64
        with self.assertRaises(WireDecodeError):
            list(iter_wire_fields(b"\x0d\x00\x00"))
        with self.assertRaises(WireDecodeError):
            list(iter_wire_fields(b"\x09\x00\x00\x00"))

        # Unsupported wire type 3 (StartGroup) / 6
        with self.assertRaises(WireDecodeError):
            list(iter_wire_fields(b"\x0b"))
        with self.assertRaises(WireDecodeError):
            list(iter_wire_fields(b"\x0e"))

        # Invalid UTF-8 bytes
        with self.assertRaises(WireDecodeError):
            decode_utf8(b"\xff\xfe")


if __name__ == "__main__":
    unittest.main()
