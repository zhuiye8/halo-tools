"""Device layer: wire protocol, transport and the high-level screen API."""

from .protocol import (
    MAX_TEXT_BYTES,
    MAX_TEXT_CHARS,
    TEXT_SOURCE_CUSTOM,
    TEXT_SOURCE_LYRIC,
    ProtocolError,
    build_packet,
    decode_text_state,
    parse_reply,
    parse_text_ack,
    utf8_prefix,
)
from .screen import (
    Align,
    DisplaySnapshot,
    RenderError,
    Screen,
    ScreenState,
    Scroll,
)
from .transport import DeviceNotFound, WinHidTransport, find_vendor_path

__all__ = [
    "MAX_TEXT_BYTES",
    "MAX_TEXT_CHARS",
    "TEXT_SOURCE_CUSTOM",
    "TEXT_SOURCE_LYRIC",
    "Align",
    "DeviceNotFound",
    "DisplaySnapshot",
    "ProtocolError",
    "RenderError",
    "Screen",
    "ScreenState",
    "Scroll",
    "WinHidTransport",
    "build_packet",
    "decode_text_state",
    "find_vendor_path",
    "parse_reply",
    "parse_text_ack",
    "utf8_prefix",
]