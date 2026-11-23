"""Regenerate the project icon assets used by the README and the packaged exe."""

from pathlib import Path

from halobar.tray import make_icon_image

docs = Path(__file__).resolve().parent.parent / "docs"
docs.mkdir(exist_ok=True)

large = make_icon_image(256)
large.save(docs / "icon.png")
large.save(docs / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", docs / "icon.png", "and", docs / "icon.ico")