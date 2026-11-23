"""Protocol level tests (no hardware required)."""

from __future__ import annotations

import pytest

from halobar.device import protocol


def test_checksum_is_sum_from_aa():
    frame = bytes([0x2E, 0xAA, 0xEC, 0x65, 0x00, 0x00])
    assert protocol.checksum(frame[1:]) == (0xAA + 0xEC + 0x65) & 0xFF


def test_build_packet_pads_and_checksums():
    packet = protocol.build_packet(protocol.CMD_TEXT_SET, bytes([0, 3]) + b"abc")
    assert len(packet) == protocol.REPORT_LENGTH
    assert packet[:4] == bytes([0x2E, 0xAA, 0xEC, 0xE8])
    assert packet[4:6] == bytes([0x00, 0x05])
    assert packet[6:11] == bytes([0, 3]) + b"abc"
    assert packet[11] == protocol.checksum(packet[1:11])
    assert packet[12:] == b"\x00" * (protocol.REPORT_LENGTH - 12)


def test_build_packet_rejects_oversized_payload():
    with pytest.raises(ValueError):
        protocol.build_packet(protocol.CMD_TEXT_SET, b"x" * (protocol.MAX_PAYLOAD + 1))


def test_parse_reply_roundtrip():
    payload = b"\x02\x00\x00\x40"
    frame = bytearray([0x2F, 0xBB, 0xEC, 0x65, 0x00, len(payload)]) + payload
    frame.append(protocol.checksum(frame[1:]))
    frame += b"\x00" * (protocol.REPORT_LENGTH - len(frame))
    reply = protocol.parse_reply(bytes(frame))
    assert reply is not None
    assert reply.command == protocol.CMD_HANDSHAKE
    assert reply.payload == payload


def test_parse_reply_detects_bad_checksum():
    frame = bytearray([0x2F, 0xBB, 0xEC, 0x65, 0x00, 0x01, 0x99])
    frame += b"\x00" * (protocol.REPORT_LENGTH - len(frame))
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_reply(bytes(frame))


def test_parse_text_ack():
    frame = bytearray([0x2F, 0xCC, 0xEC, 0xE8, 0x00, 0x01, 0x01])
    frame.append(protocol.checksum(frame[1:]))
    frame += b"\x00" * (protocol.REPORT_LENGTH - len(frame))
    assert protocol.parse_text_ack(bytes(frame)) is True


def test_utf8_prefix_never_splits_a_codepoint():
    text = "一二三四五六七八九十一二三四五六七"  # 17 chars == 51 bytes
    prefix, used = protocol.utf8_prefix(text, 48)
    assert prefix == text[:16]
    assert used == 48
    # The truncated packet must still decode as valid UTF-8.
    assert prefix.encode("utf-8").decode("utf-8") == prefix


def test_decode_text_state():
    payload = bytes([0x0E, 16, 50, 6]) + "测试".encode("utf-8")
    source, max_chars, max_bytes, text = protocol.decode_text_state(payload)
    assert (source, max_chars, max_bytes) == (0x0E, 16, 50)
    assert text == "测试"