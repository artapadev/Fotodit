"""The app's icons: Hugeicons' free "stroke rounded" set (MIT, © 2025 Hugeicons; `licenses/Hugeicons.txt`).

Each icon is the set's own vector data on a 24×24 grid (`resources/icons/hugeicons.json`, taken
unchanged from the npm package @hugeicons/core-free-icons 4.3.5), drawn as Qt paths: one family of
shapes and strokes everywhere, crisp at any size and display scale. An icon's color is a theme token
("TEXT", "MUTED", "ACCENT_TEXT" …) or a fixed color ("#FFFFFF"), looked up when the icon is painted,
so icons follow a theme change without being created again. A disabled button paints its icon in the
DISABLED token.

An icon on a button moves a little while the pointer is over it (`MOTIONS`): an arrow nudges the way
it points, a rotate arrow turns, a plus spins a quarter, a magnifier grows … The button's hover spring
drives it, so the motion follows the pointer and turns back midway. The motions are our own:
animate-ui's animated icons are MIT + Commons Clause, which does not fit Fotodit's GPL.
"""

from __future__ import annotations

import functools
import json
import math
import os
import re

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QIconEngine,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from . import theme
from .motion import animations_enabled

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "icons",
                    "hugeicons.json")
GRID = 24.0

# How an icon moves at full hover: (dx, dy) on the 24 grid, degrees clockwise, scale. Icons not
# listed grow a little. A mirrored scale (-1) flips the icon over, through zero width.
MOTIONS: dict[str, tuple[float, float, float, float]] = {
    "chevron.left": (-1.5, 0, 0, 1), "chevron.right": (1.5, 0, 0, 1),
    "arrow.up": (0, -1.5, 0, 1), "arrow.down": (0, 1.5, 0, 1),
    "trim.start": (-1.5, 0, 0, 1), "trim.end": (1.5, 0, 0, 1),
    "download": (0, 1.5, 0, 1), "location": (0, -1.5, 0, 1), "info": (0, -1, 0, 1),
    "duplicate": (-1, -1, 0, 1),
    "arrow.clockwise": (0, 0, 180, 1), "theme": (0, 0, 180, 1), "sun": (0, 0, 45, 1),
    "rotate.right": (0, 0, 20, 1), "rotate.left": (0, 0, -20, 1), "undo": (0, 0, -25, 1),
    "plus": (0, 0, 90, 1), "plus.circle": (0, 0, 90, 1), "xmark": (0, 0, 90, 1), "xmark.circle": (0, 0, 90, 1),
    "scissors": (0, 0, -15, 1), "pencil": (0, 0, -12, 1),
    "zoom.in": (0, 0, 0, 1.14), "zoom.out": (0, 0, 0, 0.88),
    "fullscreen": (0, 0, 0, 1.14), "fullscreen.exit": (0, 0, 0, 0.88),
    "play.fill": (0, 0, 0, 1.12), "pause.fill": (0, 0, 0, 1.1),
}
DEFAULT_MOTION = (0.0, 0.0, 0.0, 1.08)
FLIPS = {"flip"}  # turns over horizontally instead
FILLED = {"play.fill", "pause.fill"}  # drawn filled as well, like SF Symbols' ".fill" glyphs

_CAPS = {"round": Qt.PenCapStyle.RoundCap, "square": Qt.PenCapStyle.SquareCap}
_JOINS = {"round": Qt.PenJoinStyle.RoundJoin, "bevel": Qt.PenJoinStyle.BevelJoin}
_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


# ----------------------------------------------------------------- SVG path data


def _numbers(d: str, i: int, count: int, flags: tuple[int, ...] = ()) -> tuple[list[float], int]:
    """`count` numbers from position `i` of path data; the arguments at `flags` are arc flags, a
    single 0 or 1 that may be written without a separator ("a1 1 0 011 1")."""
    out: list[float] = []
    for k in range(count):
        while i < len(d) and d[i] in " \t\r\n,":
            i += 1
        if k in flags:
            out.append(float(d[i]))
            i += 1
            continue
        m = _TOKEN.match(d, i)
        if m is None or m.group().isalpha():
            raise ValueError(f"bad path data at {i}: {d[i:i + 12]!r}")
        out.append(float(m.group()))
        i = m.end()
    return out, i


def _arc(path: QPainterPath, p0: QPointF, rx: float, ry: float, angle: float, large: bool, sweep: bool,
         p1: QPointF) -> None:
    """An SVG elliptical arc as cubic curves (the endpoint-to-center conversion of SVG 1.1, F.6.5)."""
    if p0 == p1:
        return
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        path.lineTo(p1)
        return
    phi = math.radians(angle % 360)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx, dy = (p0.x() - p1.x()) / 2, (p0.y() - p1.y()) / 2
    x1 = cos_p * dx + sin_p * dy
    y1 = -sin_p * dx + cos_p * dy
    scale = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry)
    if scale > 1:  # the radii are too small for the endpoints: scaled up, as SVG says
        rx, ry = rx * math.sqrt(scale), ry * math.sqrt(scale)
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cx1, cy1 = coef * rx * y1 / ry, -coef * ry * x1 / rx
    cx = cos_p * cx1 - sin_p * cy1 + (p0.x() + p1.x()) / 2
    cy = sin_p * cx1 + cos_p * cy1 + (p0.y() + p1.y()) / 2

    def angle_of(ux: float, uy: float, vx: float, vy: float) -> float:
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    theta = angle_of(1, 0, (x1 - cx1) / rx, (y1 - cy1) / ry)
    delta = angle_of((x1 - cx1) / rx, (y1 - cy1) / ry, (-x1 - cx1) / rx, (-y1 - cy1) / ry)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    parts = max(1, math.ceil(abs(delta) / (math.pi / 2)))
    step = delta / parts
    k = 4 / 3 * math.tan(step / 4)

    def point(a: float) -> tuple[float, float]:
        x, y = rx * math.cos(a), ry * math.sin(a)
        return cx + cos_p * x - sin_p * y, cy + sin_p * x + cos_p * y

    def tangent(a: float) -> tuple[float, float]:
        x, y = -rx * math.sin(a), ry * math.cos(a)
        return cos_p * x - sin_p * y, sin_p * x + cos_p * y

    a = theta
    for _ in range(parts):
        b = a + step
        (ax, ay), (bx, by) = point(a), point(b)
        (tax, tay), (tbx, tby) = tangent(a), tangent(b)
        path.cubicTo(QPointF(ax + k * tax, ay + k * tay), QPointF(bx - k * tbx, by - k * tby), QPointF(bx, by))
        a = b


def svg_path(d: str) -> QPainterPath:
    """SVG path data (every command, absolute and relative) as a QPainterPath."""
    path = QPainterPath()
    cur = start = QPointF()
    last_control: QPointF | None = None  # for S and T: the previous curve's last control point
    last = ""
    i = 0
    while i < len(d):
        while i < len(d) and d[i] in " \t\r\n,":
            i += 1
        if i >= len(d):
            break
        if d[i].isalpha():
            cmd = d[i]
            i += 1
        else:  # arguments repeat the last command (after M they mean L)
            cmd = {"M": "L", "m": "l"}.get(last, last)
        rel = cmd.islower()
        c = cmd.upper()
        base = cur if rel else QPointF()
        if c == "Z":
            path.closeSubpath()
            cur = start
            last_control = None
        elif c in "ML":
            (x, y), i = _numbers(d, i, 2)
            cur = base + QPointF(x, y)
            if c == "M":
                path.moveTo(cur)
                start = cur
            else:
                path.lineTo(cur)
            last_control = None
        elif c == "H":
            (x,), i = _numbers(d, i, 1)
            cur = QPointF(cur.x() + x if rel else x, cur.y())
            path.lineTo(cur)
            last_control = None
        elif c == "V":
            (y,), i = _numbers(d, i, 1)
            cur = QPointF(cur.x(), cur.y() + y if rel else y)
            path.lineTo(cur)
            last_control = None
        elif c in "CS":
            if c == "C":
                (x1, y1, x2, y2, x, y), i = _numbers(d, i, 6)
                c1 = base + QPointF(x1, y1)
            else:
                (x2, y2, x, y), i = _numbers(d, i, 4)
                c1 = cur * 2 - last_control if last.upper() in "CS" and last_control is not None else cur
            c2, end = base + QPointF(x2, y2), base + QPointF(x, y)
            path.cubicTo(c1, c2, end)
            last_control, cur = c2, end
        elif c in "QT":
            if c == "Q":
                (x1, y1, x, y), i = _numbers(d, i, 4)
                c1 = base + QPointF(x1, y1)
            else:
                (x, y), i = _numbers(d, i, 2)
                c1 = cur * 2 - last_control if last.upper() in "QT" and last_control is not None else cur
            end = base + QPointF(x, y)
            path.quadTo(c1, end)
            last_control, cur = c1, end
        elif c == "A":
            (rx, ry, rot, large, sweep, x, y), i = _numbers(d, i, 7, flags=(3, 4))
            end = base + QPointF(x, y)
            _arc(path, cur, rx, ry, rot, bool(large), bool(sweep), end)
            cur = end
            last_control = None
        else:
            raise ValueError(f"unknown path command {cmd!r}")
        last = cmd
    return path


# --------------------------------------------------------------------- the set


@functools.lru_cache(maxsize=1)
def _data() -> dict[str, list]:
    with open(DATA, encoding="utf-8") as fh:
        return {name: entry["elements"] for name, entry in json.load(fh)["icons"].items()}


def names() -> list[str]:
    return sorted(_data())


@functools.lru_cache(maxsize=None)
def _shapes(name: str) -> tuple:
    """(path, stroke width or 0, cap, join, filled) for each element of icon `name`."""
    shapes = []
    for tag, attrs in _data().get(name, []):
        if tag == "path":
            path = svg_path(attrs["d"])
        elif tag == "circle":
            path = QPainterPath()
            r = float(attrs["r"])
            path.addEllipse(QPointF(float(attrs["cx"]), float(attrs["cy"])), r, r)
        else:
            continue
        stroke = float(attrs.get("strokeWidth", 1.5)) if attrs.get("stroke", "none") != "none" else 0.0
        cap = _CAPS.get(attrs.get("strokeLinecap", ""), Qt.PenCapStyle.FlatCap)
        join = _JOINS.get(attrs.get("strokeLinejoin", ""), Qt.PenJoinStyle.MiterJoin)
        shapes.append((path, stroke, cap, join, attrs.get("fill") == "currentColor" or name in FILLED))
    return tuple(shapes)


@functools.lru_cache(maxsize=1)
def _moving() -> bool:
    """Icons move only with Windows' animation effects on (read once)."""
    return animations_enabled()


def _resolve(color: str) -> QColor:
    if color.startswith("#"):
        return QColor(color)
    return QColor(getattr(theme, color, theme.TEXT))


def paint(painter: QPainter, rect: QRectF, name: str, color: str | QColor = "TEXT", motion: float = 0.0) -> None:
    """Draws icon `name` fitted in `rect` (a square area centered in it). `motion`: how far the
    hover motion has gone (0 at rest, 1 under the pointer)."""
    side = min(rect.width(), rect.height())
    shapes = _shapes(name)
    if side <= 0 or not shapes:
        return
    c = color if isinstance(color, QColor) else _resolve(color)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(rect.center().x() - side / 2, rect.center().y() - side / 2)
    painter.scale(side / GRID, side / GRID)
    if motion > 0.001 and _moving():
        m = min(motion, 1.2)  # (a bouncy spring may pass its target a little)
        mid = GRID / 2
        if name in FLIPS:  # turns over like a card; a sliver stays at the turning point
            turn = math.cos(math.pi * min(m, 1.0))
            painter.translate(mid, mid)
            painter.scale(math.copysign(max(abs(turn), 0.15), turn), 1.0)
            painter.translate(-mid, -mid)
        else:
            dx, dy, degrees, scale = MOTIONS.get(name, DEFAULT_MOTION)
            painter.translate(mid + dx * m, mid + dy * m)
            painter.rotate(degrees * m)
            factor = 1 + (scale - 1) * m
            painter.scale(factor, factor)
            painter.translate(-mid, -mid)
    for path, stroke, cap, join, filled in shapes:
        if stroke:
            pen = QPen(c, stroke)
            pen.setCapStyle(cap)
            pen.setJoinStyle(join)
            painter.setPen(pen)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(c if filled else Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
    painter.restore()


class _Engine(QIconEngine):
    def __init__(self, name: str, color: str, disabled: str, active: str | None):
        super().__init__()
        self.name, self.color, self.disabled, self.active = name, color, disabled, active

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        if mode == QIcon.Mode.Disabled:
            color = self.disabled
        elif mode in (QIcon.Mode.Active, QIcon.Mode.Selected) and self.active:
            color = self.active
        else:
            color = self.color
        paint(painter, QRectF(rect), self.name, color)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        pm = QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        self.paint(p, QRect(0, 0, size.width(), size.height()), mode, state)
        p.end()
        return pm

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:
        pm = self.pixmap(QSize(round(size.width() * scale), round(size.height() * scale)), mode, state)
        pm.setDevicePixelRatio(scale)
        return pm

    def clone(self) -> QIconEngine:
        return _Engine(self.name, self.color, self.disabled, self.active)

    def key(self) -> str:
        return "fotodit-vector"


def icon(name: str, color: str = "TEXT", disabled: str = "DISABLED", active: str | None = None) -> QIcon:
    """A vector icon; `color`/`disabled`/`active`: theme token names or fixed colors ("#FFFFFF")."""
    return QIcon(_Engine(name, color, disabled, active))
