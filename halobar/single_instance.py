"""Single-instance guard using a named mutex (Windows)."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = wt.HANDLE
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
_kernel32.CloseHandle.argtypes = [wt.HANDLE]

_ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """Create a named mutex; ``already_running`` is True for a second instance."""

    def __init__(self, name: str = "halobar-single-instance") -> None:
        self._handle = _kernel32.CreateMutexW(None, False, name)
        self.already_running = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None