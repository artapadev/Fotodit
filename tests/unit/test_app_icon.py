"""The app icon (packaging/make_icon.py draws it from packaging/fotodit-logo.svg): every size Windows asks
for, drawn in the interface's greys, since only warnings and errors keep a color."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops

ICON = Path(__file__).resolve().parents[2] / "fotodit" / "resources" / "fotodit.ico"
SIZES = {16, 20, 24, 32, 40, 48, 64, 128, 256}


def colorfulness(frame: Image.Image) -> int:
    """The largest difference between a pixel's channels, over the pixels at least a quarter opaque (the
    colors of nearly clear edge pixels are rounding noise)."""
    r, g, b, a = frame.convert("RGBA").split()
    high = ImageChops.lighter(ImageChops.lighter(r, g), b)
    low = ImageChops.darker(ImageChops.darker(r, g), b)
    seen = a.point(lambda v: 255 if v >= 64 else 0)
    return Image.composite(ImageChops.subtract(high, low), Image.new("L", frame.size), seen).getextrema()[1]


def test_the_icon_holds_every_size_windows_asks_for():
    with Image.open(ICON) as icon:
        assert {width for width, _height in icon.ico.sizes()} == SIZES


def test_the_icon_is_drawn_in_greys():
    with Image.open(ICON) as icon:
        for size in sorted(icon.ico.sizes()):
            assert colorfulness(icon.ico.getimage(size)) <= 8, size
