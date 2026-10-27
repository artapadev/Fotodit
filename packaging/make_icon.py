"""Generates the Fotodit icon (fotodit/resources/fotodit.ico) from packaging/fotodit-logo.svg.

Every size is drawn from the vector on its own, so the small ones stay sharp.
"""

from __future__ import annotations

import io
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no window needed, also on a build machine

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "fotodit-logo.svg")
OUT = os.path.join(os.path.dirname(HERE), "fotodit", "resources")
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def draw(renderer: QSvgRenderer, size: int) -> Image.Image:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return Image.open(io.BytesIO(buffer.data().data())).convert("RGBA")


def main() -> None:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841  (Qt paints only with one)
    renderer = QSvgRenderer(SOURCE)
    if not renderer.isValid():
        raise SystemExit(f"cannot read {SOURCE}")
    images = [draw(renderer, size) for size in SIZES]
    os.makedirs(OUT, exist_ok=True)
    images[-1].save(os.path.join(OUT, "fotodit.ico"), sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    print("icon written:", OUT)


if __name__ == "__main__":
    main()
