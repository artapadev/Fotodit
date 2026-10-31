"""The icon set: every icon the code names exists, the SVG path data reads correctly (arcs, relative
and implicit commands), and icons paint at rest and in motion (offscreen Qt)."""

from __future__ import annotations

import ast
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fotodit.ui import icons  # noqa: E402

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "fotodit", "ui")
# Where the code names an icon: the call and the position of the name among its arguments.
ICON_ARGS = {"icon": 0, "IconView": 0, "IconTile": 0, "EmptyState": 0, "set_icon_name": 0, "BarButton": 1,
             "PillButton": 1, "paint": 2, "_paint_button": 3}
TOKENS = {"MUTED", "WARN", "EDIT", "DANGER"}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _constants(node) -> list[str]:
    """The string constants an expression can give ("a" if x else "b" gives both)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _constants(node.body) + _constants(node.orelse)
    return []


def _named_icons() -> dict[str, str]:
    """{icon name: where} for every icon name written in the interface code."""
    found = {}
    for name in os.listdir(UI):
        if not name.endswith(".py") or name == "icons.py":
            continue
        tree = ast.parse(open(os.path.join(UI, name), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                pos = ICON_ARGS.get(func)
                if pos is not None and pos < len(node.args):
                    for value in _constants(node.args[pos]):
                        if value:
                            found[value] = f"{name}:{node.lineno}"
            # The info column's pieces, (icon, text, color token), and the sidebar's places, (label, path, icon).
            if isinstance(node, ast.Tuple) and len(node.elts) == 3 and set(_constants(node.elts[2])) & TOKENS:
                for value in _constants(node.elts[0]):
                    found[value] = f"{name}:{node.lineno}"
            if isinstance(node, ast.Tuple) and name == "nav.py" and len(node.elts) == 3:
                for value in _constants(node.elts[2]):
                    found[value] = f"{name}:{node.lineno}"
    return found


def test_every_icon_the_code_names_exists():
    named = _named_icons()
    assert len(named) >= 45  # (the scan itself works)
    missing = {name: where for name, where in named.items() if name not in icons.names()}
    assert not missing


def test_unused_icons_are_not_shipped():
    named = set(_named_icons())
    assert set(icons.names()) <= named


def test_svg_path_reads_every_command(app):
    # A 10×10 square drawn with M, h, v, H, Z; then an arc (a semicircle) and implicit line commands.
    square = icons.svg_path("M2 2h10v10H2Z")
    assert square.boundingRect() == QRectF(2, 2, 10, 10)
    half = icons.svg_path("M4 12a8 8 0 0 1 16 0")  # radius 8 around (12, 12), over the top
    box = half.boundingRect()
    assert abs(box.left() - 4) < 0.01 and abs(box.right() - 20) < 0.01 and abs(box.top() - 4) < 0.05
    assert half.pointAtPercent(0.5).y() == pytest.approx(4, abs=0.05)
    packed = icons.svg_path("M0 0a5 5 0 1010 0")  # arc flags without separators: large arc, sweep
    assert packed.boundingRect().height() == pytest.approx(5, abs=0.05)
    implicit = icons.svg_path("M0 0 10 0 10 10")  # after M, pairs are lines
    assert implicit.elementCount() == 3 and implicit.boundingRect() == QRectF(0, 0, 10, 10)
    smooth = icons.svg_path("M0 0C0 5 5 10 10 10S20 5 20 0")  # S mirrors the previous control point
    assert smooth.boundingRect().right() == pytest.approx(20)


def test_icons_paint_at_rest_and_in_motion(app):
    for name in icons.names():
        for motion in (0.0, 0.5, 1.0):
            img = QImage(48, 48, QImage.Format.Format_ARGB32)
            img.fill(0)
            p = QPainter(img)
            icons.paint(p, QRectF(0, 0, 48, 48), name, QColor("#000000"), motion=motion)
            p.end()
            alpha = sum(img.pixelColor(x, y).alpha() > 0 for x in range(48) for y in range(48))
            assert alpha > 20, (name, motion)


def test_a_moving_icon_moves(app):
    def ink_center(motion: float) -> QPointF:
        img = QImage(48, 48, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        icons.paint(p, QRectF(0, 0, 48, 48), "chevron.right", QColor("#000000"), motion=motion)
        p.end()
        xs = [x for x in range(48) for y in range(48) if img.pixelColor(x, y).alpha() > 128]
        return QPointF(sum(xs) / len(xs), 0)

    if icons._moving():  # Windows' animation effects on
        assert ink_center(1.0).x() - ink_center(0.0).x() == pytest.approx(3.0, abs=0.6)  # 1.5 grid units
