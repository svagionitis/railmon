"""Zero-dependency Protocol Buffers (proto2) binary wire encoder and decoder.

Implements iterative LEB128 varint encoding (including 64-bit two's-complement
signed int32/int64), IEEE-754 little-endian float/double serialization, and
length-delimited framing conforming strictly to the Protobuf wire specification
for GTFS-Realtime v2.0.
"""

from enum import IntEnum
import math
import struct
from typing import Iterator, Tuple, Union


class WireType(IntEnum):
    """Protocol Buffers wire types required by GTFS-Realtime v2.0."""

    VARINT = 0
    FIXED64 = 1
    LEN = 2
    FIXED32 = 5


class WireDecodeError(ValueError):
    """Raised when a binary protobuf payload is malformed or truncated."""


MAX_VARINT_BYTES: int = 10
UINT32_MAX: int = (1 << 32) - 1
UINT64_MASK: int = (1 << 64) - 1
INT32_MIN: int = -(1 << 31)
INT32_MAX: int = (1 << 31) - 1
INT64_MIN: int = -(1 << 63)
INT64_MAX: int = (1 << 63) - 1
SIGN_BIT_64: int = 1 << 63
SIGN_BIT_32: int = 1 << 31


def encode_varint(value: int) -> bytes:
    """Encode an unsigned or 64-bit two's-complement integer as LEB128 varint."""
    if isinstance(value, bool):
        n = 1 if value else 0
    elif value < 0:
        if value < INT64_MIN:
            raise ValueError(f"Signed varint below int64 minimum: {value}")
        n = value & UINT64_MASK
    else:
        if value > UINT64_MASK:
            raise ValueError(f"Varint exceeds uint64 maximum: {value}")
        n = value

    out = bytearray()
    for _ in range(MAX_VARINT_BYTES):
        chunk = n & 0x7F
        n >>= 7
        if n:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)
    raise ValueError(f"Varint overflow encoding value: {value}")


def decode_varint(buf: bytes, offset: int) -> Tuple[int, int]:
    """Decode a LEB128 varint from buf at offset, returning (uint64_val, next_offset)."""
    result = 0
    shift = 0
    buf_len = len(buf)
    if offset < 0 or offset >= buf_len:
        raise WireDecodeError(f"Varint offset out of range: {offset} (len={buf_len})")

    for idx in range(MAX_VARINT_BYTES):
        pos = offset + idx
        if pos >= buf_len:
            raise WireDecodeError(f"Truncated varint at byte offset {offset}")
        byte_val = buf[pos]
        result |= (byte_val & 0x7F) << shift
        if not (byte_val & 0x80):
            return result & UINT64_MASK, pos + 1
        shift += 7
    raise WireDecodeError(f"Varint exceeds {MAX_VARINT_BYTES} bytes at offset {offset}")


def to_signed_int32(raw: int) -> int:
    """Interpret a decoded 64-bit varint as a signed 32-bit integer."""
    val = raw & UINT32_MAX
    return val - (1 << 32) if val >= SIGN_BIT_32 else val


def to_signed_int64(raw: int) -> int:
    """Interpret a decoded 64-bit varint as a signed 64-bit integer."""
    val = raw & UINT64_MASK
    return val - (1 << 64) if val >= SIGN_BIT_64 else val


def encode_tag(field_no: int, wire_type: WireType) -> bytes:
    """Encode a protobuf field tag ((field_no << 3) | wire_type) as a varint."""
    if field_no <= 0 or field_no > 536_870_911:
        raise ValueError(f"Protobuf field number out of valid range [1, 2^29-1]: {field_no}")
    return encode_varint((field_no << 3) | int(wire_type))


def encode_uint32(field_no: int, value: int) -> bytes:
    """Encode an unsigned 32-bit integer varint field."""
    if value < 0 or value > UINT32_MAX:
        raise ValueError(f"uint32 out of range for field {field_no}: {value}")
    return encode_tag(field_no, WireType.VARINT) + encode_varint(value)


def encode_uint64(field_no: int, value: int) -> bytes:
    """Encode an unsigned 64-bit integer varint field."""
    if value < 0 or value > UINT64_MASK:
        raise ValueError(f"uint64 out of range for field {field_no}: {value}")
    return encode_tag(field_no, WireType.VARINT) + encode_varint(value)


def encode_int32(field_no: int, value: int) -> bytes:
    """Encode a signed 32-bit integer field (negative values use 10-byte 64-bit varint)."""
    if value < INT32_MIN or value > INT32_MAX:
        raise ValueError(f"int32 out of range for field {field_no}: {value}")
    return encode_tag(field_no, WireType.VARINT) + encode_varint(value)


def encode_int64(field_no: int, value: int) -> bytes:
    """Encode a signed 64-bit integer varint field."""
    if value < INT64_MIN or value > INT64_MAX:
        raise ValueError(f"int64 out of range for field {field_no}: {value}")
    return encode_tag(field_no, WireType.VARINT) + encode_varint(value)


def encode_bool(field_no: int, value: bool) -> bytes:
    """Encode a boolean varint field."""
    return encode_tag(field_no, WireType.VARINT) + encode_varint(1 if value else 0)


def encode_enum(field_no: int, value: int) -> bytes:
    """Encode a protobuf enum integer value as a varint field."""
    return encode_int32(field_no, int(value))


def encode_float(field_no: int, value: float) -> bytes:
    """Encode a finite IEEE-754 32-bit little-endian float field."""
    fval = float(value)
    if not math.isfinite(fval):
        raise ValueError(f"Non-finite float rejected for field {field_no}: {value}")
    return encode_tag(field_no, WireType.FIXED32) + struct.pack("<f", fval)


def encode_double(field_no: int, value: float) -> bytes:
    """Encode a finite IEEE-754 64-bit little-endian double field."""
    fval = float(value)
    if not math.isfinite(fval):
        raise ValueError(f"Non-finite double rejected for field {field_no}: {value}")
    return encode_tag(field_no, WireType.FIXED64) + struct.pack("<d", fval)


def decode_float(raw: bytes) -> float:
    """Decode a 4-byte IEEE-754 little-endian float and guard against NaN/Inf."""
    if len(raw) != 4:
        raise WireDecodeError(f"Expected 4 bytes for FIXED32 float, got {len(raw)}")
    val = float(struct.unpack("<f", raw)[0])
    if not math.isfinite(val):
        raise WireDecodeError(f"Decoded non-finite float value: {val}")
    return val


def decode_double(raw: bytes) -> float:
    """Decode an 8-byte IEEE-754 little-endian double and guard against NaN/Inf."""
    if len(raw) != 8:
        raise WireDecodeError(f"Expected 8 bytes for FIXED64 double, got {len(raw)}")
    val = float(struct.unpack("<d", raw)[0])
    if not math.isfinite(val):
        raise WireDecodeError(f"Decoded non-finite double value: {val}")
    return val


def encode_bytes(field_no: int, raw: bytes) -> bytes:
    """Encode raw byte payload as a length-delimited field."""
    return encode_tag(field_no, WireType.LEN) + encode_varint(len(raw)) + raw


def encode_string(field_no: int, text: str) -> bytes:
    """Encode UTF-8 text as a length-delimited field."""
    return encode_bytes(field_no, text.encode("utf-8"))


def encode_submsg(field_no: int, payload: bytes) -> bytes:
    """Encode a serialized nested protobuf message as a length-delimited field."""
    return encode_bytes(field_no, payload)


def decode_utf8(raw: bytes) -> str:
    """Decode length-delimited UTF-8 bytes into a string, raising WireDecodeError on failure."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WireDecodeError(f"Invalid UTF-8 string in protobuf payload: {exc}") from exc


def iter_wire_fields(buf: bytes) -> Iterator[Tuple[int, WireType, Union[int, bytes]]]:
    """Iteratively yield (field_no, wire_type, raw_value) tuples from a protobuf message.

    Bounds-checks every fixed and length-delimited slice without recursion.
    """
    offset = 0
    end = len(buf)
    while offset < end:
        tag, offset = decode_varint(buf, offset)
        field_no = tag >> 3
        raw_wt = tag & 0x07
        if field_no <= 0:
            raise WireDecodeError(f"Invalid protobuf field number 0 at offset {offset}")

        if raw_wt == WireType.VARINT:
            val, offset = decode_varint(buf, offset)
            yield field_no, WireType.VARINT, val
        elif raw_wt == WireType.FIXED64:
            if offset + 8 > end:
                raise WireDecodeError(f"Truncated FIXED64 field {field_no} at offset {offset}")
            yield field_no, WireType.FIXED64, buf[offset : offset + 8]
            offset += 8
        elif raw_wt == WireType.LEN:
            length, offset = decode_varint(buf, offset)
            if offset + length > end:
                raise WireDecodeError(
                    f"Truncated LEN field {field_no} (need {length} bytes at offset {offset})"
                )
            yield field_no, WireType.LEN, buf[offset : offset + length]
            offset += length
        elif raw_wt == WireType.FIXED32:
            if offset + 4 > end:
                raise WireDecodeError(f"Truncated FIXED32 field {field_no} at offset {offset}")
            yield field_no, WireType.FIXED32, buf[offset : offset + 4]
            offset += 4
        else:
            raise WireDecodeError(f"Unsupported protobuf wire type {raw_wt} for field {field_no}")
