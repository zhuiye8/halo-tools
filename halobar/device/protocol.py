"""Halo PixelBar wire protocol: packet framing, checksums and command ids.

Wire format (verified on firmware 1.1.1, VID 2D99 / PID A160):

    host -> device : 2E AA EC <cmd> <len_hi> <len_lo> <payload...> <checksum> <00 padding to 64>
    device -> host : 2F BB EC <cmd> <len_hi> <len_lo> <payload...> <checksum> <00 padding>

The checksum is the sum of every byte from 0xAA (index 1) through the last payload
byte, modulo 256.  Native-text acknowledgements use a separate frame
(``2F CC EC E8 00 01 xx <sum>``) whose checksum starts at 0xCC.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

REPORT_LENGTH = 64
MAX_PAYLOAD = REPORT_LENGTH - 7  # 6 header/len bytes + 1 checksum byte

PACKET_START = 0x2E
REPLY_START = 0x2F
HOST_MAGIC = 0xEC
DEVICE_MAGIC = 0xBB
ACK_MAGIC = 0xCC

CMD_HANDSHAKE = 0x65
CMD_VERSION = 0xC6
CMD_VOLUME_GET = 0x66
CMD_VOLUME_SET = 0x67
CMD_LIGHT_GET = 0x6A
CMD_LIGHT_SET = 0x6B
CMD_TEXT_GET = 0xE7
CMD_TEXT_SET = 0xE8
CMD_SCREEN_GET = 0xEE
CMD_SCREEN_SET = 0xEF
CMD_UPLOAD = 0x17

# Text slots exposed by the firmware.
TEXT_SOURCE_CUSTOM = 0x00
TEXT_SOURCE_LYRIC = 0x0E

# Official capability figures from the user manual; the device reports the
# byte budget through GET_TEXT, but we keep a hard safety cap of 48 bytes.
MAX_TEXT_BYTES = 48
MAX_TEXT_CHARS = 16


class ProtocolError(RuntimeError):
    """Raised when the device answers with a malformed or rejected frame."""


@dataclass(frozen=True)
class Reply:
    """A parsed device reply frame."""

    command: int
    payload: bytes
    raw: bytes


def checksum(data: bytes) -> int:
    """Sum of ``data`` modulo 256 (callers pass the slice starting at 0xAA)."""
    return sum(data) & 0xFF


def build_packet(command: int, payload: bytes = b"") -> bytes:
    """Build a 64-byte host packet for ``command``."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload is {len(payload)} bytes, max {MAX_PAYLOAD}")
    frame = bytearray([PACKET_START, 0xAA, HOST_MAGIC, command, 0x00, len(payload)])
    frame += payload
    frame.append(checksum(frame[1:]))
    frame += b"\x00" * (REPORT_LENGTH - len(frame))
    return bytes(frame)


def parse_reply(data: bytes) -> Reply | None:
    """Parse a device reply, returning ``None`` when the frame is not a reply."""
    if len(data) < 7 or data[0] != REPLY_START or data[1] != DEVICE_MAGIC:
        # Native-text acknowledgements use 2F CC and are handled by parse_text_ack.
        return None
    if data[2] not in (HOST_MAGIC, 0xED):
        return None
    length = int.from_bytes(data[4:6], "big")
    if length > MAX_PAYLOAD or len(data) < 7 + length:
        return None
    if checksum(data[1:6 + length]) != data[6 + length]:
        raise ProtocolError("reply checksum mismatch")
    return Reply(command=data[3], payload=bytes(data[6:6 + length]), raw=bytes(data))


def parse_text_ack(data: bytes) -> bool | None:
    """Validate an ``E8`` acknowledgement; ``None`` when the frame is not an ack."""
    if len(data) < 8 or data[:4] != b"\x2f\xcc\xec\xe8":
        return None
    if checksum(data[1:7]) != data[7]:
        raise ProtocolError("text ack checksum mismatch")
    return data[6] == 1


def decode_text_state(payload: bytes) -> tuple[int, int, int, str]:
    """Decode a ``GET_TEXT`` payload into (source, max chars, max bytes, text)."""
    if len(payload) < 4:
        raise ProtocolError("text state payload too short")
    source, max_chars, max_bytes, length = payload[0], payload[1], payload[2], payload[3]
    if length > len(payload) - 4:
        raise ProtocolError("text state length exceeds payload")
    text = payload[4:4 + length].decode("utf-8", "replace")
    return source, max_chars, max_bytes, text


def utf8_prefix(text: str, max_bytes: int = MAX_TEXT_BYTES) -> tuple[str, int]:
    """Return the longest prefix of ``text`` whose UTF-8 form fits ``max_bytes``.

    Never splits a code point (the firmware would otherwise store invalid UTF-8).
    """
    used = 0
    out: list[str] = []
    for char in text:
        size = len(char.encode("utf-8"))
        if used + size > max_bytes:
            break
        used += size
        out.append(char)
    return "".join(out), used


def sign_volume(volume: int) -> bytes:
    """Payload for ``CMD_VOLUME_SET`` (device accepts 0..16)."""
    return struct.pack("B", max(0, min(16, volume)))