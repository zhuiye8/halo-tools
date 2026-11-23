"""Inspect the NetEase 'DesktopLyrics' window: geometry, child controls, and a screenshot."""
import ctypes
import ctypes.wintypes as wt
import subprocess

from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def cloudmusic_pids():
    out = subprocess.check_output(
        ["tasklist", "/fi", "imagename eq cloudmusic.exe", "/fo", "csv"],
        text=True, encoding="gbk", errors="ignore")
    pids = set()
    for line in out.splitlines()[1:]:
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1:
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def main():
    pids = cloudmusic_pids()
    targets = []

    def cb(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value == "DesktopLyrics":
                targets.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    print(f"DesktopLyrics windows: {len(targets)}")
    for hwnd in targets:
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        print(f"  hwnd={hwnd} visible={bool(user32.IsWindowVisible(hwnd))} "
              f"rect=({r.left},{r.top},{r.right},{r.bottom}) size={r.right-r.left}x{r.bottom-r.top}")

        kids = []

        def kcb(child, _):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child, cls, 256)
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(child, buf, 512)
            length = user32.SendMessageW(child, 0x000E, 0, 0)  # WM_GETTEXTLENGTH
            txt = ctypes.create_unicode_buffer(length + 2)
            user32.SendMessageW(child, 0x000D, length + 1, ctypes.byref(txt))  # WM_GETTEXT
            kids.append((cls.value, buf.value, txt.value[:80], length))
            return True

        user32.EnumChildWindows(hwnd, WNDENUMPROC(kcb), 0)
        for cls, title, txt, length in kids:
            print(f"    child class={cls!r} title={title!r} text={txt!r} len={length}")

        # capture with PrintWindow(PW_RENDERFULLCONTENT=2)
        w, h = r.right - r.left, r.bottom - r.top
        if w > 0 and h > 0:
            hdc = user32.GetWindowDC(hwnd)
            mdc = gdi32.CreateCompatibleDC(hdc)
            bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
            gdi32.SelectObject(mdc, bmp)
            ok = user32.PrintWindow(hwnd, mdc, 2)
            class BMIH(ctypes.Structure):
                _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                            ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]
            bi = BMIH()
            bi.biSize = ctypes.sizeof(BMIH)
            bi.biWidth = w
            bi.biHeight = -h
            bi.biPlanes = 1
            bi.biBitCount = 32
            bi.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
            img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
            out = "lyrics_window.png"
            img.convert("RGB").save(out)
            print(f"  PrintWindow ok={ok} -> {out}")
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
            user32.ReleaseDC(hwnd, hdc)


if __name__ == "__main__":
    main()