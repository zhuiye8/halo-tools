"""Read-only HID probe for Halo PixelBar (2D99:A160, usage page FF14).

Only sends read-only query commands (0x65 handshake, 0xC6 version, 0xE7 text, 0xEE screen state).
No write command is issued, so the speaker display content is not modified.
Requires: Windows + ctypes only (no third-party packages).
"""
import ctypes
from ctypes import wintypes as w

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
DIGCF_PRESENT = 0x2
DIGCF_DEVICEINTERFACE = 0x10
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("InterfaceClassGuid", GUID), ("Flags", w.DWORD), ("Reserved", ctypes.c_void_p)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", w.ULONG), ("VendorID", w.USHORT), ("ProductID", w.USHORT), ("VersionNumber", w.USHORT)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", w.USHORT), ("UsagePage", w.USHORT),
        ("InputReportByteLength", w.USHORT), ("OutputReportByteLength", w.USHORT),
        ("FeatureReportByteLength", w.USHORT),
        ("Reserved", w.USHORT * 17),
        ("NumberLinkCollectionNodes", w.USHORT), ("NumberInputButtonCaps", w.USHORT),
        ("NumberInputValueCaps", w.USHORT), ("NumberInputDataIndices", w.USHORT),
        ("NumberOutputButtonCaps", w.USHORT), ("NumberOutputValueCaps", w.USHORT),
        ("NumberOutputDataIndices", w.USHORT), ("NumberFeatureButtonCaps", w.USHORT),
        ("NumberFeatureValueCaps", w.USHORT), ("NumberFeatureDataIndices", w.USHORT),
    ]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]


# --- prototypes (64-bit safe) ---
setupapi.SetupDiGetClassDevsW.restype = w.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), w.LPCWSTR, w.HWND, w.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.restype = w.BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [w.HANDLE, ctypes.c_void_p,
                                                 ctypes.POINTER(GUID), w.DWORD,
                                                 ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = w.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [w.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                      ctypes.c_void_p, w.DWORD,
                                                      ctypes.POINTER(w.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.restype = w.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [w.HANDLE]
kernel32.CreateFileW.restype = w.HANDLE
kernel32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
kernel32.CreateEventW.restype = w.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
kernel32.WaitForSingleObject.restype = w.DWORD
kernel32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
kernel32.GetOverlappedResult.restype = w.BOOL
kernel32.GetOverlappedResult.argtypes = [w.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(w.DWORD), w.BOOL]
kernel32.ReadFile.restype = w.BOOL
kernel32.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.WriteFile.restype = w.BOOL
kernel32.WriteFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.CancelIoEx.restype = w.BOOL
kernel32.CancelIoEx.argtypes = [w.HANDLE, ctypes.POINTER(OVERLAPPED)]
hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
hid.HidD_GetPreparsedData.argtypes = [w.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidP_GetCaps.restype = ctypes.c_long
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]


def find_pixelbar():
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if hdev == w.HANDLE(-1).value:
        raise OSError("SetupDiGetClassDevs failed")
    found = []
    all_paths = []
    idx = 0
    while True:
        did = SP_DEVICE_INTERFACE_DATA()
        did.cbSize = ctypes.sizeof(did)
        ok = setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), idx,
                                                  ctypes.byref(did))
        if not ok:
            break
        idx += 1
        need = w.DWORD(0)
        setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), None, 0,
                                                  ctypes.byref(need), None)
        buf = ctypes.create_string_buffer(need.value)
        ctypes.cast(buf, ctypes.POINTER(w.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        if not setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), buf,
                                                         need, ctypes.byref(need), None):
            continue
        path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
        all_paths.append(path)
        if "vid_2d99&pid_a160" in path.lower():
            caps = read_caps(path)
            found.append((path, caps))
    setupapi.SetupDiDestroyDeviceInfoList(hdev)
    print(f"total HID interfaces: {len(all_paths)}")
    for p in all_paths:
        if "2d99" in p.lower():
            print("   candidate:", p)
    return [f for f in found if f[1] is not None]


def read_caps(path):
    h = kernel32.CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                             OPEN_EXISTING, 0, None)
    if h == w.HANDLE(-1).value:
        return None
    pp = ctypes.c_void_p()
    caps = None
    if hid.HidD_GetPreparsedData(h, ctypes.byref(pp)):
        c = HIDP_CAPS()
        if hid.HidP_GetCaps(pp, ctypes.byref(c)) >= 0:
            caps = c
        hid.HidD_FreePreparsedData(pp)
    kernel32.CloseHandle(h)
    return caps


class Device:
    def __init__(self, path):
        self.h = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                                      FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                      OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if self.h == w.HANDLE(-1).value:
            raise OSError(ctypes.get_last_error(), "CreateFile failed (exit TempoHub?)")

    def close(self):
        kernel32.CloseHandle(self.h)

    def _transfer(self, data, read, timeout_ms):
        ev = kernel32.CreateEventW(None, 1, 0, None)
        ov = OVERLAPPED()
        ov.hEvent = ev
        n = w.DWORD(0)
        if read:
            ok = kernel32.ReadFile(self.h, data, len(data), ctypes.byref(n), ctypes.byref(ov))
        else:
            ok = kernel32.WriteFile(self.h, data, len(data), ctypes.byref(n), ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != 997:  # ERROR_IO_PENDING
                kernel32.CloseHandle(ev)
                raise OSError(err, "ReadFile/WriteFile failed")
            rc = kernel32.WaitForSingleObject(ev, timeout_ms)
            if rc != WAIT_OBJECT_0:
                kernel32.CancelIoEx(self.h, ctypes.byref(ov))
                kernel32.CloseHandle(ev)
                return 0
            if not kernel32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(n), False):
                kernel32.CloseHandle(ev)
                return 0
        kernel32.CloseHandle(ev)
        return n.value

    def send(self, command, payload=b""):
        frame = bytes([0x2E, 0xAA, 0xEC, command]) + len(payload).to_bytes(2, "big") + payload
        frame += bytes([sum(frame[1:]) & 0xFF])
        return self._transfer(frame.ljust(64, b"\x00"), False, 5000)

    def receive(self, command, timeout_ms=5000):
        import time
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            buf = ctypes.create_string_buffer(64)
            n = self._transfer(buf, True, 200)
            if n == 0:
                continue
            data = buf.raw[:n]
            if len(data) < 7 or data[:3] != b"\x2F\xBB\xEC":
                continue
            length = int.from_bytes(data[4:6], "big")
            if length > 57 or len(data) < 7 + length:
                continue
            if (sum(data[1:6 + length]) & 0xFF) != data[6 + length]:
                continue
            if data[3] == command:
                return data[6:6 + length]
        return None

    def request(self, command, payload=b"", timeout_ms=5000):
        self.send(command, payload)
        return self.receive(command, timeout_ms)


def main():
    devices = find_pixelbar()
    print(f"HID interfaces with VID_2D99&PID_A160: {len(devices)}")
    target = None
    for path, caps in devices:
        print(f"  usage_page=0x{caps.UsagePage:04X} usage=0x{caps.Usage:04X} "
              f"in={caps.InputReportByteLength} out={caps.OutputReportByteLength} {path[-60:]}")
        if caps.UsagePage == 0xFF14 and caps.InputReportByteLength == 64:
            target = path
    if not target:
        print("No vendor interface (usage page FF14) found.")
        return
    d = Device(target)
    try:
        base = d.request(0x65)
        print("0x65 handshake:", base.hex(" ") if base is not None else "TIMEOUT")
        ver = d.request(0xC6)
        print("0xC6 version  :", ver.hex(" ") if ver is not None else "TIMEOUT")
        text = d.request(0xE7, b"\x00")
        print("0xE7 text     :", text.hex(" ") if text is not None else "TIMEOUT")
        if text and len(text) >= 4:
            print(f"     max_len={text[1]} package_max={text[2]} current={text[4:].decode('utf-8', 'replace')!r}")
        state = d.request(0xEE)
        print("0xEE state    :", state.hex(" ") if state is not None else "TIMEOUT")
    finally:
        d.close()


if __name__ == "__main__":
    main()