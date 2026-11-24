"""Safe, reversible visual test on Halo PixelBar: bright LED white-balance & 5 native lyric animations.

Captures the current screen snapshot on start and restores it (including RGB via
action 3) in ``finally``.  Stop the halobar tray app before running so only one
process owns the HID interface.

Usage::

    python probe/color_slot_test.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halobar.device import protocol  # noqa: E402
from halobar.device.screen import Align, Screen, gamma_correct_rgb  # noqa: E402


def main() -> int:
    target = (0xF0, 0xB4, 0xC8)  # #F0B4C8
    v1_gamma = (223, 119, 149)   # previous pure gamma 2.2
    v2_balanced = gamma_correct_rgb(target)  # new white-balanced + peak-preserving

    with Screen() as screen:
        snap = screen.snapshot()
        print(f"Captured snapshot: {snap.state.hex(' ')}")
        try:
            print(f"\n[1/3] 旧版纯 Gamma 2.2 {v1_gamma}")
            screen.set_color(v1_gamma)
            screen.show_text("旧校正 偏暗偏蓝", align=Align.CENTER, color=v1_gamma)
            time.sleep(3.5)

            print(f"\n[2/3] 新版白平衡保亮校正 {v2_balanced}")
            screen.set_color(v2_balanced)
            screen.show_text("新校正 樱花粉色", align=Align.CENTER, color=v2_balanced)
            time.sleep(3.5)

            print("\n[3/3] 原生 0x0E 歌词动画 0..4 轮播预览")
            for preset in range(5):
                print(f"  animation preset {preset}")
                screen.show_text(f"原生动画 预设 {preset + 1}", animation=preset, color=v2_balanced)
                time.sleep(2.2)
        finally:
            screen.set_text("", source=protocol.TEXT_SOURCE_LYRIC)
            screen.restore(snap)
            print("\nRestored cleanly:", screen.state().raw.hex(" "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
