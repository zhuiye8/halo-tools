"""High-level Halo PixelBar screen API (text, layout, colour, snapshot/restore).

Everything here was verified against firmware 1.1.1 on a Halo PixelBar A160.
The device exposes two independent text slots:

* ``TEXT_SOURCE_CUSTOM`` (0x00) - the "self-created space" text, max 16 chars / 48 bytes.
* ``TEXT_SOURCE_LYRIC``  (0x0E) - the native lyric overlay slot, reports 80 chars
  capacity but still accepts at most one 48-byte packet per write.

All writes are verified by read-back where the firmware supports it.  A failed
write is never retried automatically: an uncertain device state is worse than a
visible error, and the caller decides whether to continue.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import protocol
from .transport import WinHidTransport

log = logging.getLogger(__name__)


class Align(str, Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"

    @property
    def code(self) -> int:
        return {"left": 0, "center": 1, "right": 2, "justify": 3}[self.value]


class Scroll(str, Enum):
    LEFT_TO_RIGHT = "ltr"
    RIGHT_TO_LEFT = "rtl"

    @property
    def code(self) -> int:
        return {"ltr": 0, "rtl": 1}[self.value]


class RenderError(RuntimeError):
    """Raised when the device rejects or fails to confirm a screen write."""


@dataclass(frozen=True)
class ScreenState:
    """The 12-byte ``0xEE`` screen state.

    Layout (verified)::

        [0]     always 0
        [1..3]  active RGB colour
        [4]     combination selector (0 on this firmware)
        [5]     mode   1 = scene, 2 = text layout
        [6]     category / template id
        [7]     option (alignment, scroll direction, ...)
        [8]     template (255 = none)
        [9..11] music-visualiser options (lyric overlay enable / preset)
    """

    raw: bytes

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ScreenState":
        if len(payload) not in (9, 12):
            raise RenderError(f"unexpected screen state length {len(payload)}")
        return cls(raw=bytes(payload))

    @property
    def rgb(self) -> tuple[int, int, int]:
        return self.raw[1], self.raw[2], self.raw[3]

    @property
    def mode(self) -> int:
        return self.raw[5]

    @property
    def category(self) -> int:
        return self.raw[6]

    @property
    def option(self) -> int:
        return self.raw[7]

    @property
    def lyric_overlay(self) -> tuple[int, int, int]:
        tail = self.raw[9:12]
        return (tail[0], tail[1], tail[2]) if len(tail) == 3 else (0, 0, 0)

    def as_restore_payload(self) -> bytes:
        """Build the ``0xEF`` payload that returns to this state."""
        return bytes([2]) + self.raw[1:9]


@dataclass
class DisplaySnapshot:
    """Everything needed to put the screen back the way we found it."""

    state: bytes
    text_custom: bytes
    text_lyric: bytes
    fingerprint: str = ""

    def to_json(self) -> dict:
        return {
            "state": self.state.hex(),
            "text_custom": self.text_custom.hex(),
            "text_lyric": self.text_lyric.hex(),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_json(cls, data: dict) -> "DisplaySnapshot":
        return cls(
            state=bytes.fromhex(data["state"]),
            text_custom=bytes.fromhex(data["text_custom"]),
            text_lyric=bytes.fromhex(data["text_lyric"]),
            fingerprint=data.get("fingerprint", ""),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "DisplaySnapshot | None":
        if not path.exists():
            return None
        try:
            return cls.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, KeyError) as exc:
            log.warning("ignoring corrupt snapshot %s: %s", path, exc)
            return None


def gamma_correct_rgb(
    rgb: tuple[int, int, int],
    gamma: float = 2.2,
    white_balance: tuple[float, float, float] = (1.0, 0.58, 0.62),
) -> tuple[int, int, int]:
    """Map sRGB colour to linear PWM LED drive with white-balance & peak brightness.

    Halo PixelBar's SMD RGB LEDs drive R/G/B linearly and have noticeably higher
    blue/green luminous efficacy than red.  Combining gamma 2.2 with per-channel
    white balance ``(1.0, 0.58, 0.62)`` and preserving the dominant channel's
    brightness keeps colours bright, saturated, and true to hue (e.g.
    ``#F0B4C8`` -> ``(240, 74, 100)``).  Pure greys/white stay untouched for
    maximum brightness.
    """
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    if r == g == b:
        return (r, g, b)
    peak = max(r, g, b)
    lin = [((c / 255.0) ** gamma) * wb for c, wb in zip((r, g, b), white_balance)]
    lin_peak = max(lin)
    if lin_peak <= 0.0:
        return (0, 0, 0)
    scale = peak / lin_peak
    return (
        max(0, min(255, int(round(lin[0] * scale)))),
        max(0, min(255, int(round(lin[1] * scale)))),
        max(0, min(255, int(round(lin[2] * scale)))),
    )


def _encode_text_state(source: int, text: str) -> bytes:
    """Payload to replay a text slot verbatim during restore."""
    raw = text.encode("utf-8")
    return bytes([source, len(raw)]) + raw


class Screen:
    """A connected Halo PixelBar."""

    def __init__(self, transport: WinHidTransport | None = None) -> None:
        owned = transport is None
        self.transport = transport or WinHidTransport()
        self.protocol_version = 0
        self.firmware = ""
        self._lyric_overlay_active = False
        self._active_layout: tuple[object, ...] | None = None
        try:
            self._handshake()
        except Exception:
            if owned:
                self.transport.close()
            raise

    # ---------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "Screen":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _handshake(self) -> None:
        payload = self._request(protocol.CMD_HANDSHAKE)
        if len(payload) != 4 or payload[1] != 0:
            raise RenderError(f"unexpected handshake reply {payload.hex(' ')}")
        self.protocol_version = payload[0]
        version = self._request(protocol.CMD_VERSION)
        self.firmware = ".".join(str(part) for part in version)
        log.info("device protocol=%d firmware=%s", self.protocol_version, self.firmware)

    # ------------------------------------------------------------------- basics
    def _send(self, command: int, payload: bytes = b"") -> None:
        self.transport.write(protocol.build_packet(command, payload))

    def _request(self, command: int, payload: bytes = b"", timeout_ms: int = 3000) -> bytes:
        self._send(command, payload)
        return self.transport.read_until(command, timeout_ms)

    def state(self) -> ScreenState:
        return ScreenState.from_bytes(self._request(protocol.CMD_SCREEN_GET))

    def get_text(self, source: int = protocol.TEXT_SOURCE_CUSTOM) -> str:
        payload = self._request(protocol.CMD_TEXT_GET, bytes([source]))
        _, _, _, text = protocol.decode_text_state(payload)
        return text

    def capabilities(self, source: int = protocol.TEXT_SOURCE_CUSTOM) -> tuple[int, int]:
        """Return (max_chars, max_bytes) reported by the firmware for a text slot."""
        payload = self._request(protocol.CMD_TEXT_GET, bytes([source]))
        _, max_chars, max_bytes, _ = protocol.decode_text_state(payload)
        return max_chars, max_bytes

    # -------------------------------------------------------------------- write
    def set_text(self, text: str, source: int = protocol.TEXT_SOURCE_CUSTOM) -> str:
        """Write one text packet.  Text is truncated on a code-point boundary."""
        safe, used = protocol.utf8_prefix(text, protocol.MAX_TEXT_BYTES)
        raw = safe.encode("utf-8")
        self._send(protocol.CMD_TEXT_SET, bytes([source, len(raw)]) + raw)
        try:
            ack = self._await_text_ack()
        except TimeoutError as exc:
            raise RenderError("text acknowledgement timed out") from exc
        if ack is False:
            raise RenderError("device rejected the text write")
        if safe != text:
            log.debug("text truncated to %d bytes: %r -> %r", used, text, safe)
        return safe

    def _await_text_ack(self, timeout_ms: int = 3000) -> bool | None:
        import time

        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            frame = self.transport.read(min(200, timeout_ms))
            if not frame:
                continue
            verdict = protocol.parse_text_ack(frame)
            if verdict is not None:
                return verdict
        raise TimeoutError("text ack")

    def set_layout(
        self,
        *,
        align: Align | None = None,
        scroll: Scroll | None = None,
        animation: int | None = None,
        color: tuple[int, int, int] | None = None,
    ) -> None:
        """Configure text presentation.

        ``align`` + ``scroll`` drive the static/scrolling text layout; ``animation``
        selects one of the device's five lyric in/out presets (0-4) and enables the
        lyric overlay.  ``None`` leaves that part untouched.
        """
        rgb = color or self.state().rgb
        if animation is not None:
            if not 0 <= animation <= 4:
                raise ValueError("animation must be 0..4")
            payload = bytes([1, *rgb, 0, 0, 1, 0, animation])
        elif scroll is not None:
            payload = bytes([1, *rgb, 0, 2, 1, scroll.code, 0xFF])
        elif align is not None:
            payload = bytes([1, *rgb, 0, 2, 0, align.code, 0xFF])
        else:
            raise ValueError("set_layout needs one of align/scroll/animation")
        self._send(protocol.CMD_SCREEN_SET, payload)

    def disable_lyric_overlay(self, color: tuple[int, int, int] | None = None) -> None:
        rgb = color or self.state().rgb
        self._send(protocol.CMD_SCREEN_SET, bytes([1, *rgb, 0, 0, 0, 0xFF, 0xFF]))

    def set_color(self, rgb: tuple[int, int, int]) -> None:
        """Set pixel screen RGB via ``0xEF`` action 3 (full 9-byte frame).

        Verified on firmware 1.1.1: action 1 (layout) and action 2 (scene) ignore
        RGB bytes, so action 3 is the authoritative colour command.
        """
        r, g, b = (max(0, min(255, int(c))) for c in rgb)
        self._send(protocol.CMD_SCREEN_SET, bytes([3, r, g, b, 0, 0, 0xFF, 0xFF, 0xFF]))

    def show_scene(
        self,
        category: int,
        option: int = 0,
        template: int = 0xFF,
        color: tuple[int, int, int] | None = None,
    ) -> None:
        if self._lyric_overlay_active:
            try:
                self.set_text("", source=protocol.TEXT_SOURCE_LYRIC)
            except RenderError:
                pass
            self.disable_lyric_overlay(color=color)
            self._lyric_overlay_active = False
        self._active_layout = None
        if color is not None:
            self.set_color(color)
            rgb = color
        else:
            rgb = self.state().rgb
        self._send(protocol.CMD_SCREEN_SET, bytes([2, *rgb, 0, 1, category, option, template]))

    def show_text(
        self,
        text: str,
        *,
        source: int | None = None,
        align: Align | None = Align.CENTER,
        scroll: Scroll | None = None,
        animation: int | None = None,
        color: tuple[int, int, int] | None = None,
    ) -> str:
        """Layout + text in one call (the order the firmware expects).

        When ``animation`` (0..4) is selected, the firmware's native lyric
        animations only trigger on ``TEXT_SOURCE_LYRIC`` (0x0E).  Switching into
        the lyric overlay clears custom slot 0x00 and pauses briefly so the
        firmware applies ``0xEF`` before receiving ``0xE8``; unchanged layouts
        skip redundant ``0xEF`` writes.
        """
        import time as _time

        if animation is not None:
            target = protocol.TEXT_SOURCE_LYRIC if source is None else source
            layout_key = ("anim", target, animation)
            if self._active_layout != layout_key:
                if target == protocol.TEXT_SOURCE_LYRIC and not self._lyric_overlay_active:
                    try:
                        self.set_text("", source=protocol.TEXT_SOURCE_CUSTOM)
                    except RenderError:
                        pass
                self.set_layout(animation=animation, color=color)
                self._active_layout = layout_key
                self._lyric_overlay_active = target == protocol.TEXT_SOURCE_LYRIC
                _time.sleep(0.08)
            return self.set_text(text, source=target)

        target = protocol.TEXT_SOURCE_CUSTOM if source is None else source
        layout_key = ("text", target, align, scroll)
        if self._active_layout != layout_key:
            if self._lyric_overlay_active and target == protocol.TEXT_SOURCE_CUSTOM:
                try:
                    self.set_text("", source=protocol.TEXT_SOURCE_LYRIC)
                except RenderError:
                    pass
                self.disable_lyric_overlay(color=color)
                self._lyric_overlay_active = False
                _time.sleep(0.05)
            self.set_layout(align=align, scroll=scroll, color=color)
            self._active_layout = layout_key
        return self.set_text(text, source=target)

    def clear_text(self, source: int = protocol.TEXT_SOURCE_CUSTOM) -> None:
        self.set_text("", source=source)

    # ------------------------------------------------------------------ bitmap
    def upload_bitmap(self, data: bytes, *, category: int, option: int,
                      progress=None, timeout_s: float = 20.0) -> None:
        """Upload a 256x32 bitmap/animation payload and switch to its scene.

        ``data`` is the record stream produced by :mod:`halobar.bitmap`.  The device
        requests 50-byte chunks and acknowledges each one; the final ``[0,5]`` frame
        is the "verified" signal.  A failure is never retried: the caller decides.
        """
        import time as _time

        state = self.state()
        self._send(protocol.CMD_UPLOAD, bytes([0, 0]) + len(data).to_bytes(4, "big"))
        ready = self._wait_upload(lambda payload: payload[:2] == b"\x00\x01", timeout_s)
        chunk_size = int.from_bytes(ready[2:4], "big")
        if not 1 <= chunk_size <= 53:
            raise RenderError(f"device asked for an unsupported chunk size {chunk_size}")

        index = 0
        for offset in range(0, len(data), chunk_size):
            piece = data[offset:offset + chunk_size]
            self._send(protocol.CMD_UPLOAD,
                       bytes([0, 2]) + index.to_bytes(2, "big") + piece)
            index += 1
            if offset + chunk_size < len(data):
                ack = self._wait_upload(
                    lambda payload: payload[:2] == b"\x00\x03", timeout_s,
                    skip_ready=True)
                if ack[:2] != b"\x00\x03":
                    raise RenderError(f"chunk rejected: {ack.hex(' ')}")
            if progress is not None:
                progress(min(offset + chunk_size, len(data)) / len(data))

        self._send(protocol.CMD_UPLOAD, bytes([0, 4]))
        final = self._wait_upload(lambda payload: payload[:2] == b"\x00\x05", timeout_s,
                                  skip_ready=True)
        if final[:2] != b"\x00\x05":
            raise RenderError("device did not confirm the upload")

        selection = bytes([1]) + state.raw[1:5] + bytes([1, category, option, 255])
        self._send(protocol.CMD_SCREEN_SET, selection)
        _time.sleep(0.3)
        current = self.state()
        if current.raw[5:8] != bytes([1, category, option]):
            raise RenderError(
                f"scene switch not confirmed: {current.raw.hex(' ')}")

    def _wait_upload(self, predicate, timeout_s: float, skip_ready: bool = False) -> bytes:
        import time as _time

        deadline = _time.monotonic() + timeout_s
        while _time.monotonic() < deadline:
            frame = self.transport.read(int(min(200, timeout_s * 1000)))
            if not frame:
                continue
            reply = protocol.parse_reply(frame)
            if reply is None or reply.command != protocol.CMD_UPLOAD:
                continue
            payload = reply.payload
            if skip_ready and payload[:2] == b"\x00\x01":
                continue
            if predicate(payload):
                return payload
            if payload[:2] not in (b"\x00\x03", b"\x00\x05"):
                raise RenderError(f"upload rejected: {payload.hex(' ')}")
        raise RenderError("upload timed out")

    # ----------------------------------------------------------------- snapshot
    def snapshot(self) -> DisplaySnapshot:
        state = self._request(protocol.CMD_SCREEN_GET)
        custom = self.get_text(protocol.TEXT_SOURCE_CUSTOM)
        lyric = self.get_text(protocol.TEXT_SOURCE_LYRIC)
        return DisplaySnapshot(
            state=state,
            text_custom=_encode_text_state(protocol.TEXT_SOURCE_CUSTOM, custom),
            text_lyric=_encode_text_state(protocol.TEXT_SOURCE_LYRIC, lyric),
        )

    def restore(self, snapshot: DisplaySnapshot, timeout_s: float = 2.0) -> None:
        """Replay a snapshot and verify the state read-back.

        On firmware 1.1.1 three writes are required:
        1. action 3 to restore RGB (actions 1 & 2 ignore RGB bytes);
        2. action 1 to restore music-visualiser options (bytes 9..11);
        3. action 2 to restore the scene/text mode.
        """
        self._active_layout = None
        self._lyric_overlay_active = False
        for slot in (snapshot.text_custom, snapshot.text_lyric):
            if len(slot) >= 2:
                self._send(protocol.CMD_TEXT_SET, bytes([slot[0], len(slot) - 2]) + slot[2:])
        if len(snapshot.state) >= 4:
            self.set_color((snapshot.state[1], snapshot.state[2], snapshot.state[3]))
        if len(snapshot.state) == 12:
            self._send(protocol.CMD_SCREEN_SET,
                       bytes([1]) + snapshot.state[1:4] + bytes([0, 0]) + snapshot.state[9:12])
        self._send(protocol.CMD_SCREEN_SET, bytes([2]) + snapshot.state[1:9])

        import time

        deadline = time.monotonic() + timeout_s
        last = ""
        while time.monotonic() < deadline:
            current = self._request(protocol.CMD_SCREEN_GET)
            last = current.hex(" ")
            if current[:9] == snapshot.state[:9] and current[9:] == snapshot.state[9:]:
                return
            time.sleep(0.15)
        raise RenderError(
            f"restore not confirmed: want {snapshot.state.hex(' ')}, got {last}")


def save_snapshot(snapshot: DisplaySnapshot, path: Path) -> None:
    snapshot.save(path)


def load_snapshot(path: Path) -> DisplaySnapshot | None:
    return DisplaySnapshot.load(path)