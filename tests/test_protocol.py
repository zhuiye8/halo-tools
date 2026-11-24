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


def test_gamma_correct_rgb_preserves_endpoints_and_deepens_pastels():
    from halobar.device.screen import gamma_correct_rgb

    assert gamma_correct_rgb((0, 0, 0)) == (0, 0, 0)
    assert gamma_correct_rgb((255, 255, 255)) == (255, 255, 255)
    assert gamma_correct_rgb((128, 128, 128)) == (128, 128, 128)
    assert gamma_correct_rgb((255, 0, 0)) == (255, 0, 0)
    # Pastel #F0B4C8 (240, 180, 200): peak brightness preserved (R=240),
    # blue/green attenuated to eliminate cool blue cast while keeping B > G.
    r, g, b = gamma_correct_rgb((240, 180, 200))
    assert r == 240
    assert r > b > g > 0
    assert (r, g, b) == (240, 74, 100)


def test_show_text_routes_animated_to_lyric_slot_and_clears_overlay_for_static():
    from halobar.device.screen import Align, Screen

    sent: list[bytes] = []

    class FakeTransport:
        def __init__(self) -> None:
            self.replies: list[bytes] = [
                bytes([0x02, 0x00, 0x00, 0x40]),  # handshake
                bytes([1, 1, 1]),                  # version
            ]
            ack = bytearray([0x2F, 0xCC, 0xEC, 0xE8, 0x00, 0x01, 0x01])
            ack.append(protocol.checksum(ack[1:]))
            self.ack_frame = bytes(ack).ljust(protocol.REPORT_LENGTH, b"\x00")

        def write(self, packet: bytes) -> None:
            sent.append(packet)

        def read(self, timeout_ms: int = 1000) -> bytes:
            return self.ack_frame

        def read_until(self, command: int, timeout_ms: int = 3000) -> bytes:
            return self.replies.pop(0)

        def close(self) -> None:
            pass

    screen = Screen(FakeTransport())
    sent.clear()
    screen.show_text("动画句", animation=2, color=(240, 74, 100))
    text_writes = [pkt[6:6 + pkt[5]] for pkt in sent if pkt[3] == protocol.CMD_TEXT_SET]
    assert text_writes[0] == bytes([protocol.TEXT_SOURCE_CUSTOM, 0])
    assert text_writes[-1][0] == protocol.TEXT_SOURCE_LYRIC

    # Second animated line with the same preset must skip redundant 0xEF writes.
    sent.clear()
    screen.show_text("动画句2", animation=2, color=(240, 74, 100))
    assert not [pkt for pkt in sent if pkt[3] == protocol.CMD_SCREEN_SET]

    sent.clear()
    screen.show_text("静态句", align=Align.CENTER, color=(240, 74, 100))
    text_writes = [pkt[6:6 + pkt[5]] for pkt in sent if pkt[3] == protocol.CMD_TEXT_SET]
    # Should clear lyric slot 0x0E first, then write static text to 0x00.
    assert text_writes[0] == bytes([protocol.TEXT_SOURCE_LYRIC, 0])
    assert text_writes[-1][0] == protocol.TEXT_SOURCE_CUSTOM


def test_screen_set_color_sends_full_9_byte_action_3_and_restore_restores_rgb():
    from halobar.device.screen import DisplaySnapshot, Screen

    sent: list[bytes] = []

    class FakeTransport:
        def __init__(self) -> None:
            self.replies: list[bytes] = [
                bytes([0x02, 0x00, 0x00, 0x40]),  # handshake
                bytes([1, 1, 1]),                  # version
                bytes([0x00, 0xF0, 0xB4, 0xC8, 0, 2, 0, 1, 0xFF, 1, 0, 2]),  # restore verify
            ]

        def write(self, packet: bytes) -> None:
            sent.append(packet)

        def read_until(self, command: int, timeout_ms: int = 3000) -> bytes:
            return self.replies.pop(0)

        def close(self) -> None:
            pass

    screen = Screen(FakeTransport())
    sent.clear()
    screen.set_color((0xF0, 0xB4, 0xC8))
    assert sent[-1][3] == protocol.CMD_SCREEN_SET
    assert sent[-1][5] == 9
    assert sent[-1][6:15] == bytes([3, 0xF0, 0xB4, 0xC8, 0, 0, 0xFF, 0xFF, 0xFF])

    sent.clear()
    snap = DisplaySnapshot(
        state=bytes([0x00, 0xF0, 0xB4, 0xC8, 0, 2, 0, 1, 0xFF, 1, 0, 2]),
        text_custom=b"\x00\x00",
        text_lyric=b"\x0e\x00",
    )
    screen.restore(snap)
    screen_sets = [pkt[6:6 + pkt[5]] for pkt in sent if pkt[3] == protocol.CMD_SCREEN_SET]
    assert any(p[:4] == bytes([3, 0xF0, 0xB4, 0xC8]) for p in screen_sets)