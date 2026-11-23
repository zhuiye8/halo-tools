"""Win32 HID transport for the Halo PixelBar (ctypes only, no third-party deps).

Only Windows is supported.  The API mirrors a tiny file-like object so it can be
swapped for a fake transport in tests.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from dataclasses import dataclass
from typing import Iterator

from . import protocol

VENDOR_ID = 0x2D99
PRODUCT_ID = 0xA160
USAGE_PAGE = 0xFF14

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_SHARE_ALL = 0x1 | 0x2
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 0x40000000
_DIGCF_PRESENT = 0x02
_DIGCF_DEVICEINTERFACE = 0x10
_ERROR_IO_PENDING = 997
_WAIT_OBJECT_0 = 0


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", _GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_void_p)]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wt.USHORT), ("UsagePage", wt.USHORT),
        ("InputReportByteLength", wt.USHORT), ("OutputReportByteLength", wt.USHORT),
        ("FeatureReportByteLength", wt.USHORT),
        ("Reserved", wt.USHORT * 17),
        ("NumberLinkCollectionNodes", wt.USHORT), ("NumberInputButtonCaps", wt.USHORT),
        ("NumberInputValueCaps", wt.USHORT), ("NumberInputDataIndices", wt.USHORT),
        ("NumberOutputButtonCaps", wt.USHORT), ("NumberOutputValueCaps", wt.USHORT),
        ("NumberOutputDataIndices", wt.USHORT), ("NumberFeatureButtonCaps", wt.USHORT),
        ("NumberFeatureValueCaps", wt.USHORT), ("NumberFeatureDataIndices", wt.USHORT),
    ]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_hid = ctypes.WinDLL("hid", use_last_error=True)

_setupapi.SetupDiGetClassDevsW.restype = wt.HANDLE
_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wt.LPCWSTR, wt.HWND, wt.DWORD]
_setupapi.SetupDiDestroyDeviceInfoList.restype = wt.BOOL
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
_setupapi.SetupDiEnumDeviceInterfaces.restype = wt.BOOL
_setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(_GUID), wt.DWORD,
    ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA)]
_setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wt.BOOL
_setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wt.HANDLE, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
    wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_kernel32.CreateFileW.restype = wt.HANDLE
_kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                  wt.DWORD, wt.DWORD, wt.HANDLE]
_kernel32.CreateEventW.restype = wt.HANDLE
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
_kernel32.WaitForSingleObject.restype = wt.DWORD
_kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
_kernel32.GetOverlappedResult.restype = wt.BOOL
_kernel32.GetOverlappedResult.argtypes = [
    wt.HANDLE, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(wt.DWORD), wt.BOOL]
_kernel32.ReadFile.restype = wt.BOOL
_kernel32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                               ctypes.POINTER(wt.DWORD), ctypes.POINTER(_OVERLAPPED)]
_kernel32.WriteFile.restype = wt.BOOL
_kernel32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                                ctypes.POINTER(wt.DWORD), ctypes.POINTER(_OVERLAPPED)]
_kernel32.CancelIoEx.restype = wt.BOOL
_kernel32.CancelIoEx.argtypes = [wt.HANDLE, ctypes.POINTER(_OVERLAPPED)]
_hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(_GUID)]
_hid.HidD_GetPreparsedData.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
_hid.HidP_GetCaps.restype = ctypes.c_long
_hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(_HIDP_CAPS)]
_hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]


class DeviceNotFound(RuntimeError):
    """No matching vendor interface is currently attached."""


def _read_caps(path: str) -> _HIDP_CAPS | None:
    handle = _kernel32.CreateFileW(path, 0, _SHARE_ALL, None, _OPEN_EXISTING, 0, None)
    if handle == wt.HANDLE(-1).value:
        return None
    preparsed = ctypes.c_void_p()
    caps = None
    try:
        if _hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            buffer = _HIDP_CAPS()
            if _hid.HidP_GetCaps(preparsed, ctypes.byref(buffer)) >= 0:
                caps = buffer
    finally:
        if preparsed:
            _hid.HidD_FreePreparsedData(preparsed)
        _kernel32.CloseHandle(handle)
    return caps


def iter_vendor_interfaces() -> Iterator[tuple[str, _HIDP_CAPS]]:
    """Yield (device path, caps) for every Halo PixelBar vendor HID collection."""
    guid = _GUID()
    _hid.HidD_GetHidGuid(ctypes.byref(guid))
    handle = _setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE)
    if handle == wt.HANDLE(-1).value:
        raise DeviceNotFound("SetupDiGetClassDevs failed")
    try:
        index = 0
        while True:
            info = _SP_DEVICE_INTERFACE_DATA()
            info.cbSize = ctypes.sizeof(info)
            if not _setupapi.SetupDiEnumDeviceInterfaces(
                    handle, None, ctypes.byref(guid), index, ctypes.byref(info)):
                break
            index += 1
            needed = wt.DWORD(0)
            _setupapi.SetupDiGetDeviceInterfaceDetailW(
                handle, ctypes.byref(info), None, 0, ctypes.byref(needed), None)
            if not needed.value:
                continue
            buffer = ctypes.create_string_buffer(needed.value)
            ctypes.cast(buffer, ctypes.POINTER(wt.DWORD))[0] = 8
            if not _setupapi.SetupDiGetDeviceInterfaceDetailW(
                    handle, ctypes.byref(info), buffer, needed, ctypes.byref(needed), None):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buffer) + 4)
            lowered = path.lower()
            if f"vid_{VENDOR_ID:04x}&pid_{PRODUCT_ID:04x}" not in lowered:
                continue
            caps = _read_caps(path)
            if caps is not None:
                yield path, caps
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(handle)


def find_vendor_path() -> str:
    """Return the device path of the 64-byte vendor collection (usage page 0xFF14)."""
    for path, caps in iter_vendor_interfaces():
        if caps.UsagePage == USAGE_PAGE and caps.InputReportByteLength == protocol.REPORT_LENGTH:
            return path
    raise DeviceNotFound(
        "Halo PixelBar vendor interface (usage page 0xFF14) not found; "
        "connect the speaker over USB and switch it to sound-card input (red LED).")


class WinHidTransport:
    """Blocking HID transport with per-operation timeouts."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or find_vendor_path()
        self._handle = _kernel32.CreateFileW(
            self.path, _GENERIC_READ | _GENERIC_WRITE, _SHARE_ALL, None,
            _OPEN_EXISTING, _FILE_FLAG_OVERLAPPED, None)
        if self._handle == wt.HANDLE(-1).value:
            error = ctypes.get_last_error()
            raise OSError(error, f"cannot open {self.path} (is another app using it?)")

    def close(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = 0

    def __enter__(self) -> "WinHidTransport":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _transfer(self, buffer: ctypes.Array, size: int, read: bool, timeout_ms: int) -> int:
        event = _kernel32.CreateEventW(None, 1, 0, None)
        if not event:
            raise OSError(ctypes.get_last_error(), "CreateEvent failed")
        overlapped = _OVERLAPPED()
        overlapped.hEvent = event
        written = wt.DWORD(0)
        try:
            if read:
                ok = _kernel32.ReadFile(self._handle, buffer, size,
                                        ctypes.byref(written), ctypes.byref(overlapped))
            else:
                ok = _kernel32.WriteFile(self._handle, buffer, size,
                                         ctypes.byref(written), ctypes.byref(overlapped))
            if not ok:
                error = ctypes.get_last_error()
                if error != _ERROR_IO_PENDING:
                    raise OSError(error, "HID transfer failed")
                if _kernel32.WaitForSingleObject(event, timeout_ms) != _WAIT_OBJECT_0:
                    _kernel32.CancelIoEx(self._handle, ctypes.byref(overlapped))
                    return 0
                if not _kernel32.GetOverlappedResult(
                        self._handle, ctypes.byref(overlapped), ctypes.byref(written), False):
                    return 0
            return written.value
        finally:
            _kernel32.CloseHandle(event)

    def write(self, packet: bytes, timeout_ms: int = 5000) -> None:
        buffer = ctypes.create_string_buffer(packet, len(packet))
        sent = self._transfer(buffer, len(packet), False, timeout_ms)
        if sent != len(packet):
            raise OSError(f"short HID write: {sent}/{len(packet)}")

    def read(self, timeout_ms: int = 1000) -> bytes:
        buffer = ctypes.create_string_buffer(protocol.REPORT_LENGTH)
        got = self._transfer(buffer, protocol.REPORT_LENGTH, True, timeout_ms)
        return buffer.raw[:got] if got else b""

    def read_until(self, command: int, timeout_ms: int = 3000) -> bytes:
        """Read frames until ``command`` answers, returning its payload."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            frame = self.read(min(200, timeout_ms))
            if not frame:
                continue
            reply = protocol.parse_reply(frame)
            if reply is not None and reply.command == command:
                return reply.payload
        raise TimeoutError(f"device did not answer command 0x{command:02X}")