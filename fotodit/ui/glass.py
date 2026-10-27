"""Liquid Glass as Apple draws it in iOS 27 and macOS 27: the material of the controls that float
above the content.

Glass is a lens over the content, not a blurred panel. It belongs to the control layer (toolbars,
sidebars, buttons, a slider's thumb while it is dragged), never to the content, and glass never
sits on glass. Apple publishes no figures for it; the recipe below follows pixel measurements of
iOS 27 on a device (over black, white and grey) and Apple's iOS 27 design kit:

1. Backdrop: what lies beneath the surface (plus a margin) is rendered into an image: the
   parent's own background and the lower siblings the surface floats over (`QWidget.render`,
   or a widget's own `glass_backdrop` image when it has one, like the large preview's photo).
   Other glass is skipped, so glass never shows glass.
2. Frost, two paths: a wide blur (sigma about a quarter of a bar's height) mixed with a faint,
   nearly sharp copy of the backdrop (about 30 %): content behind reads as a soft cloud with a
   trace of its detail, never as recognizable blobs.
3. Lens: the outer half of the corner radius bends like the edge of a plano-convex lens with a
   convex squircle profile (Snell's law, index 1.5). Content near the edge is pulled inward a
   little; through the frost it reads as a soft bend.
4. Veil: a near-white (light) or mid-grey (dark) layer of about half opacity; for glass over
   photos it darkens over bright images so white symbols stay legible.
5. Edge, with no line all around: a specular highlight lit from straight above, as bright on
   the bottom edge as on the top, added (not blended) to whatever lies beneath and fading to
   nothing at the sides; and a thin dark contour just outside the shape, strongest at the sides,
   that darkens the content next to it. Controls cast almost no shadow (1–6 %).

If the backdrop did not change since the last paint (a child button's hover, say), the last
image is reused. "Görünüm" offers Apple's three looks (clear, default, tinted) and "Saydamlığı
azalt" (reduce transparency): a solid surface with the same edge. The setting is the app's own
rather than Windows' "Transparency effects", which many Windows 10 machines have turned off for
the taskbar's sake.
"""

from __future__ import annotations

import math
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, replace

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QRegion,
    QTransform,
)
from PySide6.QtWidgets import QWidget

from ..i18n import t
from . import theme

FMT = QImage.Format.Format_ARGB32_Premultiplied

PANEL = "panel"  # sidebars and inspectors: large, quiet, edge to edge
BAR = "bar"  # toolbars and floating bars over content
CONTROL = "control"  # small floating controls (a round button, a capsule)
PROMINENT = "prominent"  # the primary action: glass in the ink (black in light, white in dark)
MEDIA = "media"  # bars over photos and videos (the large preview): always dark
SHEET = "sheet"  # a card over the dimmed window (confirmations, progress, About)
TOOLBAR = "toolbar"  # the strip behind the toolbars while the content is scrolled (iOS 27)

IOR = 1.5  # refractive index of the glass

# Apple's three looks (iOS 27: Settings > Appearance > Liquid Glass; macOS 27: System Settings).
CLEAR, DEFAULT, TINTED = "clear", "default", "tinted"
LEVELS = (CLEAR, DEFAULT, TINTED)


def level_label(value: str) -> str:
    """A look's name in the Görünüm menu."""
    return t(f"glass.level.{value}")


@dataclass(frozen=True)
class Material:
    tint: tuple[int, int, int, int]  # RGBA veil laid over the frosted backdrop
    frost: float  # width of the wide blur (logical px; its sigma is about half of it)
    sharp: float  # share of the unblurred backdrop mixed into the frost (0–1)
    vivid: float  # saturation multiplier of the backdrop
    bezel: float  # width of the refracting rim (logical px; 0: no refraction)
    lens: float  # thickness of the glass as a fraction of the bezel (how strongly the rim bends)
    highlight: float  # added white in the outermost pixel of the top and bottom edges (0–1)
    contour: tuple[float, float]  # darkening just outside the edge: at the top and bottom, at the sides
    shadow: float  # opacity of the drop shadow (0–1)
    shadow_blur: float  # logical px
    shadow_y: float  # logical px
    solid: tuple[int, int, int] = (250, 250, 250)  # opaque color with "Saydamlığı azalt"
    adaptive: bool = False  # the veil darkens over bright backdrops (glass over photos)


_LIGHT = {
    PANEL: Material((246, 246, 246, 200), 32, 0.15, 1.15, 0, 1.0, 0.0, (0.0, 0.0), 0.0, 24, 6, (246, 246, 246)),
    BAR: Material((243, 243, 243, 140), 22, 0.30, 1.2, 10, 1.0, 0.25, (0.08, 0.31), 0.03, 15, 7),
    CONTROL: Material((243, 243, 243, 140), 16, 0.30, 1.2, 8, 1.0, 0.25, (0.08, 0.31), 0.03, 15, 6),
    PROMINENT: Material((22, 22, 22, 240), 10, 0.0, 1.0, 8, 1.0, 0.25, (0.08, 0.31), 0.05, 15, 6, (22, 22, 22)),
    SHEET: Material((248, 248, 248, 236), 32, 0.10, 1.15, 0, 1.0, 0.25, (0.06, 0.24), 0.20, 44, 8, (248, 248, 248)),
    TOOLBAR: Material((255, 255, 255, 217), 12, 0.0, 1.0, 0, 1.0, 0.0, (0.0, 0.0), 0.0, 0, 0, (250, 250, 250)),
}
_DARK = {
    PANEL: Material((36, 36, 36, 200), 32, 0.15, 1.15, 0, 1.0, 0.0, (0.0, 0.0), 0.0, 24, 6, (36, 36, 36)),
    BAR: Material((77, 77, 77, 107), 22, 0.30, 1.2, 10, 1.0, 0.25, (0.0, 0.61), 0.06, 15, 7, (44, 44, 44)),
    CONTROL: Material((77, 77, 77, 107), 16, 0.30, 1.2, 8, 1.0, 0.25, (0.0, 0.61), 0.06, 15, 6, (44, 44, 44)),
    PROMINENT: Material((242, 242, 242, 240), 10, 0.0, 1.0, 8, 1.0, 0.25, (0.0, 0.61), 0.06, 15, 6,
                        (242, 242, 242)),
    SHEET: Material((44, 44, 44, 232), 32, 0.10, 1.15, 0, 1.0, 0.25, (0.0, 0.45), 0.40, 44, 8, (44, 44, 44)),
    TOOLBAR: Material((0, 0, 0, 140), 12, 0.0, 1.0, 0, 1.0, 0.0, (0.0, 0.0), 0.0, 0, 0, (28, 28, 28)),
}
# Glass over photos and videos: the dark recipe; over bright images the veil grows darker
# (white symbols need a body of about 115 or darker to stay legible).
_MEDIA = Material((60, 60, 60, 107), 18, 0.35, 1.2, 10, 1.0, 0.25, (0.0, 0.61), 0.06, 15, 7, (40, 40, 40),
                  adaptive=True)

_level = DEFAULT


def level() -> str:
    return _level


def set_level(value: str) -> None:
    """Apple's three looks: CLEAR (more of the content shows), DEFAULT, TINTED (nearly opaque frost)."""
    global _level
    value = value if value in LEVELS else DEFAULT
    if value != _level:
        _level = value
        theme.changes().changed.emit()  # glass surfaces drop their images and repaint


def _tuned(mat: Material, kind: str) -> Material:
    if _level == DEFAULT or kind in (PROMINENT, SHEET, TOOLBAR):
        return mat
    r, g, b, a = mat.tint
    if _level == CLEAR:  # iOS 27 measured: the veil about 0.78 as strong, twice the sharp detail
        return replace(mat, tint=(r, g, b, round(a * 0.78)), sharp=min(0.8, mat.sharp * 2.0), frost=mat.frost * 0.7)
    target = 0.74 if kind != PANEL else 0.9  # tinted: a near-opaque frost, no sharp detail
    return replace(mat, tint=(r, g, b, max(a, round(255 * target))), sharp=0.0, frost=mat.frost * 1.4)


def material(kind: str, dark: bool | None = None) -> Material:
    """The material of a kind of surface in the current (or given) appearance and look."""
    if kind == MEDIA:
        return _tuned(_MEDIA, kind)
    dark = theme.is_dark() if dark is None else dark
    base = (_DARK if dark else _LIGHT)[kind]
    if kind == PROMINENT:  # the ink of the current theme
        ink = QColor(theme.ACCENT)
        base = replace(base, tint=(ink.red(), ink.green(), ink.blue(), base.tint[3]),
                       solid=(ink.red(), ink.green(), ink.blue()))
    return _tuned(base, kind)


# ------------------------------------------------------------ reduce transparency


_reduced = False


def reduce_transparency() -> bool:
    """Solid glass ("Saydamlığı azalt"); FOTODIT_SOLID_GLASS=1 forces it."""
    return _reduced or os.environ.get("FOTODIT_SOLID_GLASS") == "1"


def set_reduce_transparency(value: bool) -> None:
    """Turns solid glass on or off (the theme menu's "Saydamlığı azalt")."""
    global _reduced
    if bool(value) != _reduced:
        _reduced = bool(value)
        theme.changes().changed.emit()  # glass surfaces drop their images and repaint


# ------------------------------------------------------------------ image helpers


def pixels(img: QImage) -> np.ndarray:
    """A writable (h, w, 4) view of a 32-bit image's pixels (BGRA in memory)."""
    h, w, stride = img.height(), img.width(), img.bytesPerLine()
    arr = np.frombuffer(img.bits(), np.uint8, count=stride * h).reshape(h, stride)
    return arr[:, : w * 4].reshape(h, w, 4)


def _image(arr: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, w * 4, FMT).copy()


def _box(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    """Box blur of radius `r` along an axis (running sums; edges repeat the border pixel)."""
    if r <= 0 or a.shape[axis] < 2:
        return a
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float32)
    n = a.shape[axis]
    hi = [slice(None)] * a.ndim
    lo = [slice(None)] * a.ndim
    hi[axis] = slice(2 * r + 1, 2 * r + 1 + n)
    lo[axis] = slice(0, n)
    return (c[tuple(hi)] - c[tuple(lo)]) / np.float32(2 * r + 1)


def blur(a: np.ndarray, r: int, passes: int = 3) -> np.ndarray:
    """Gaussian-like blur: a few box passes in both directions."""
    out = a.astype(np.float32, copy=False)
    for _ in range(passes):
        out = _box(_box(out, r, 1), r, 0)
    return out


def frost(img: QImage, radius: float, vivid: float, part: QRect | None = None) -> QImage:
    """Blurs a (premultiplied) image by about `radius` device pixels and boosts its saturation;
    `part`: the only rectangle of the result that is wanted (a surface inside its backdrop's margin).
    The work happens on a reduced copy; Qt averages while reducing and interpolates when enlarging."""
    w, h = img.width(), img.height()
    part = QRect(0, 0, w, h) if part is None else part
    factor = max(1, int(radius / 2.2))
    sw, sh = max(2, -(-w // factor)), max(2, -(-h // factor))
    small = img.scaled(sw, sh, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    small = small.convertToFormat(FMT)
    a = blur(pixels(small), 1, passes=2)
    if vivid != 1.0:
        lum = a[..., :3] @ np.array([0.0722, 0.7152, 0.2126], np.float32)  # B, G, R
        a[..., :3] = lum[..., None] + (a[..., :3] - lum[..., None]) * np.float32(vivid)
    a = np.clip(a, 0, 255)
    a[..., :3] = np.minimum(a[..., :3], a[..., 3:4])  # stays premultiplied
    blurred = _image(a.astype(np.uint8))
    if (sw, sh) == (w, h):
        return blurred.copy(part)
    out = QImage(part.width(), part.height(), FMT)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
    kx, ky = sw / w, sh / h
    # A little more than the part is drawn: Qt stops interpolating at the edge of the source rectangle,
    # which would show in the outermost pixels.
    e = min(2 * factor, part.x(), part.y(), w - part.right() - 1, h - part.bottom() - 1)
    grown = QRectF(part).adjusted(-e, -e, e, e)
    p.drawImage(QRectF(-e, -e, grown.width(), grown.height()), blurred,
                QRectF(grown.x() * kx, grown.y() * ky, grown.width() * kx, grown.height() * ky))
    p.end()
    return out


# ------------------------------------------------------------------- the shape's optics


def _rim(inside: np.ndarray, bezel: float, thickness: float) -> np.ndarray:
    """How far inward (px) the content seen at `inside` px from the edge comes from: the rim is a
    convex squircle h(x) = T·(1 − (1 − x)⁴)^¼ (x = inside/bezel); a vertical view ray refracts into
    the glass at the surface normal and travels down through the height h, ending up h·tan(θ) further in."""
    x = np.clip(inside / bezel, 1e-3, 1.0)
    base = 1.0 - (1.0 - x) ** 4
    height = thickness * base ** 0.25
    slope = (thickness / bezel) * (1.0 - x) ** 3 * np.maximum(base, 1e-9) ** -0.75
    slope = np.where(inside >= bezel, 0.0, slope)
    height = np.where(inside >= bezel, thickness, height)
    nz = 1.0 / np.sqrt(1.0 + slope * slope)
    nr = slope * nz  # the normal's tilt toward the outside
    eta = 1.0 / IOR
    k = np.maximum(1.0 - eta * eta * (1.0 - nz * nz), 0.0)
    f = eta * nz - np.sqrt(k)
    rr = f * nr  # (negative: the ray bends inward)
    rz = -eta + f * nz
    return (height * -rr / np.maximum(-rz, 1e-6)).astype(np.float32)


def _field(w: int, h: int, radius: float, pad: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The signed distance to a rounded rectangle's edge (device px, negative inside) and the edge's
    outward normal, on a grid `pad` px larger than the shape on every side."""
    ys, xs = np.mgrid[-pad:h + pad, -pad:w + pad].astype(np.float32)
    px, py = xs + 0.5 - w / 2, ys + 0.5 - h / 2
    qx, qy = np.abs(px) - (w / 2 - radius), np.abs(py) - (h / 2 - radius)
    d = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - radius
    corner = (qx > 0) & (qy > 0)
    norm = np.maximum(np.hypot(qx, qy), 1e-6)
    nx = np.where(corner, np.sign(px) * qx / norm, np.where(qx >= qy, np.sign(px), 0.0)).astype(np.float32)
    ny = np.where(corner, np.sign(py) * qy / norm, np.where(qx >= qy, 0.0, np.sign(py))).astype(np.float32)
    return d.astype(np.float32), nx, ny


class _Optics:
    """Where each pixel of the refracting rim looks (device px): `yy, xx` the pixels of the band,
    `sy, sx` the backdrop points they show."""

    def __init__(self, w: int, h: int, radius: float, bezel: float, lens: float):
        radius = max(0.0, min(radius, w / 2, h / 2))
        d, nx, ny = _field(w, h, radius)
        inside = np.maximum(-d, 0.0)
        bezel = max(2.0, min(bezel, w / 2, h / 2))
        move = _rim(inside, bezel, bezel * lens)
        band = (d <= 0) & (inside < bezel)
        self.yy, self.xx = np.nonzero(band)
        m = move[self.yy, self.xx]
        self.sy = self.yy - ny[self.yy, self.xx] * m
        self.sx = self.xx - nx[self.yy, self.xx] * m
        # The pixels they take (the content only moves inward, so they lie inside the shape).
        self.syi = np.clip(np.rint(self.sy), 0, h - 1).astype(np.intp)
        self.sxi = np.clip(np.rint(self.sx), 0, w - 1).astype(np.intp)


_OPTICS: OrderedDict[tuple, _Optics] = OrderedDict()


def optics(w: int, h: int, radius: float, mat: Material, dpr: float) -> _Optics:
    key = (w, h, round(radius, 2), mat.bezel, mat.lens, round(dpr, 3))
    hit = _OPTICS.get(key)
    if hit is None:
        hit = _Optics(w, h, radius * dpr, mat.bezel * dpr, mat.lens)
        _OPTICS[key] = hit
        while len(_OPTICS) > 40:
            _OPTICS.popitem(last=False)
    else:
        _OPTICS.move_to_end(key)
    return hit


class _Edges:
    """The edge of a w×h glass shape (device px), on a grid `pad` px larger on every side:
    `highlight`, white to be added (premultiplied), and `contour`, black to be laid over what is
    next to the shape. Measured on iOS 27: the highlight peaks in the outermost pixel (half of it
    0.8 pt in, a tenth 1.8 pt in), comes from straight above and is as bright at the bottom as at
    the top, fading with (1 − θ/90°)^1.33 toward the sides; the contour is a 0.5 pt ring just outside
    the edge whose strength grows from the top and bottom to the sides, (1 − |cos θ|)^0.75."""

    def __init__(self, w: int, h: int, radius: float, highlight: float, contour: tuple[float, float],
                 dpr: float):
        self.pad = pad = max(2, math.ceil(1.6 * dpr))
        radius = max(0.0, min(radius, w / 2, h / 2))
        d, _nx, ny = _field(w, h, radius, pad)
        up = np.abs(ny)  # |cos θ|: 1 at the top and bottom, 0 at the sides
        theta = np.degrees(np.arccos(np.clip(up, 0.0, 1.0)))
        angle = np.clip(1.0 - theta / 90.0, 0.0, 1.0) ** 1.33
        inside_pt = np.maximum(-d, 0.0) / dpr
        profile = 0.84 * np.exp(-(inside_pt / 0.85) ** 2.4) + 0.16 * np.exp(-inside_pt / 2.8)
        cover = np.clip(0.5 - d, 0.0, 1.0)
        light = np.clip(highlight * angle * profile * cover, 0.0, 1.0) * 255.0
        self.highlight = _image(np.stack([light, light, light, light], axis=-1).astype(np.uint8))
        floor, peak = contour
        strength = floor + (peak - floor) * (1.0 - up) ** 0.75
        outside_pt = d / dpr
        ring = np.exp(-((outside_pt - 0.1) / 0.32) ** 2)
        dark = np.clip(strength * ring, 0.0, 1.0) * 255.0
        zero = np.zeros_like(dark)
        self.contour = _image(np.stack([zero, zero, zero, dark], axis=-1).astype(np.uint8))


_EDGES: OrderedDict[tuple, _Edges] = OrderedDict()


def edges(w: int, h: int, radius: float, mat: Material, dpr: float) -> _Edges:
    """The edge images of a w×h (device px) shape with corner radius `radius` (logical px)."""
    key = (w, h, round(radius, 2), mat.highlight, mat.contour, round(dpr, 3))
    hit = _EDGES.get(key)
    if hit is None:
        hit = _Edges(w, h, radius * dpr, mat.highlight, mat.contour, dpr)
        _EDGES[key] = hit
        while len(_EDGES) > 60:
            _EDGES.popitem(last=False)
    else:
        _EDGES.move_to_end(key)
    return hit


def shape_path(rect: QRectF, radius: float | None) -> QPainterPath:
    """A rounded rectangle; `radius` None: a capsule (fully rounded ends)."""
    path = QPainterPath()
    r = min(rect.width(), rect.height()) / 2 if radius is None else min(radius, rect.width() / 2, rect.height() / 2)
    path.addRoundedRect(rect, r, r)
    return path


# --------------------------------------------------------------------------- backdrop

_capturing = 0  # > 0 while a backdrop is being rendered: glass inside it does not paint itself


def capturing() -> bool:
    return _capturing > 0


def is_glass_layer(widget: QWidget) -> bool:
    return bool(getattr(widget, "glass_layer", False))


def capture_backdrop(widget: QWidget, rect: QRect, dpr: float, base: QColor | None = None) -> QImage:
    """What lies beneath `rect` (in `widget`'s coordinates): the way the window paints itself, from
    the window down to `widget`'s parent, each ancestor's own background followed by its children
    that lie below the path to `widget`; glass is left out. Device pixels, premultiplied."""
    global _capturing
    img = QImage(max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr)), FMT)
    img.setDevicePixelRatio(dpr)
    img.fill(base if base is not None else QColor(theme.WINDOW))
    levels: list[tuple[QWidget, QWidget]] = []
    child = widget
    while not child.isWindow() and child.parentWidget() is not None:
        levels.append((child.parentWidget(), child))
        child = child.parentWidget()
    if not levels:
        return img
    painter = QPainter(img)
    _capturing += 1
    try:
        for parent, upper in reversed(levels):
            origin = widget.mapTo(parent, rect.topLeft())
            area = QRect(origin, rect.size())  # in the parent's coordinates
            parent.render(painter, QPoint(0, 0), QRegion(area), QWidget.RenderFlag.DrawWindowBackground)
            for sibling in parent.children():
                if sibling is upper:
                    break
                if not isinstance(sibling, QWidget) or sibling.isWindow() or not sibling.isVisible():
                    continue
                if is_glass_layer(sibling):
                    continue
                geometry = sibling.geometry()
                part = geometry.intersected(area)
                if part.isEmpty():
                    continue
                target = part.topLeft() - area.topLeft()
                source = part.translated(-geometry.topLeft())
                own = getattr(sibling, "glass_backdrop", None)
                image = own(source, dpr) if own is not None else None
                if image is not None:
                    painter.drawImage(QRectF(QPointF(target), source.size().toSizeF()), image)
                else:
                    sibling.render(painter, target, QRegion(source))
    finally:
        _capturing -= 1
        painter.end()
    return img


class SharedBackdrop:
    """Renders `widget` (the content beneath the floating bars) once per paint pass for all the
    glass above it: the first request renders a full-width band around itself, later requests of
    the same pass are cut from it. Without it every bar would render the same thumbnails again.
    Set as the widget's `glass_backdrop`, which `capture_backdrop` asks first."""

    SLACK = 24  # px above and below a band, so the neighbouring bars' requests fall inside it
    LIFE = 0.1  # s: a band belongs to one paint pass, which ends when the event loop turns (or after this)

    def __init__(self, widget: QWidget):
        self.widget = widget
        self._bands: list[tuple[QRect, float, QImage]] = []
        self._born = 0.0
        self._clear_queued = False
        widget.glass_backdrop = self.image

    def _clear(self) -> None:
        self._bands.clear()
        self._clear_queued = False

    def image(self, source: QRect, dpr: float) -> QImage | None:
        now = time.perf_counter()
        if self._bands and now - self._born > self.LIFE:
            self._bands.clear()
        for band, band_dpr, image in self._bands:
            if band_dpr == dpr and band.contains(source):
                return self._cut(image, band, source, dpr)
        band = QRect(0, source.top() - self.SLACK, self.widget.width(), source.height() + 2 * self.SLACK)
        band = band.intersected(self.widget.rect()).united(source)
        image = QImage(max(1, round(band.width() * dpr)), max(1, round(band.height() * dpr)), FMT)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        self.widget.render(painter, QPoint(0, 0), QRegion(band))
        painter.end()
        if not self._bands:
            self._born = now
        self._bands.append((band, dpr, image))
        if not self._clear_queued:
            self._clear_queued = True
            QTimer.singleShot(0, self._clear)
        return self._cut(image, band, source, dpr)

    @staticmethod
    def _cut(image: QImage, band: QRect, source: QRect, dpr: float) -> QImage:
        part = image.copy(round((source.x() - band.x()) * dpr), round((source.y() - band.y()) * dpr),
                          round(source.width() * dpr), round(source.height() * dpr))
        part.setDevicePixelRatio(dpr)
        return part


# ----------------------------------------------------------------------------- glass


def render_glass(backdrop: QImage, margin: int, w: int, h: int, radius_px: float, mat: Material,
                 dpr: float) -> tuple[QImage, float]:
    """The glass body (device px, without the veil and the edge) from a backdrop that is `margin`
    device pixels larger than the surface on every side, and the backdrop's mean luminance (0–1)."""
    source = backdrop.convertToFormat(FMT)
    source.setDevicePixelRatio(1.0)  # (device pixels throughout)
    inner = QRect(margin, margin, w, h)  # the surface; the margin only feeds the blur
    out = frost(source, mat.frost * dpr, mat.vivid, inner)
    if mat.sharp > 0:  # the faint, nearly sharp copy over the wide cloud
        p = QPainter(out)
        p.setOpacity(mat.sharp)
        p.drawImage(QRectF(0, 0, w, h), source, QRectF(inner))
        p.end()
    if mat.bezel > 0:
        opt = optics(w, h, radius_px / dpr, mat, dpr)
        if opt.yy.size:
            a = pixels(out)
            a[opt.yy, opt.xx] = a[opt.syi, opt.sxi]  # (fancy indexing reads a copy first)
    luminance = 0.0
    if mat.adaptive:
        small = pixels(out)[:: max(1, h // 24), :: max(1, w // 48), :3].astype(np.float32)
        luminance = float((small @ np.array([0.0722, 0.7152, 0.2126], np.float32)).mean() / 255.0)
    return out, luminance


def veil(mat: Material, luminance: float) -> QColor:
    """The veil over the frosted backdrop; adaptive glass grows darker over bright backdrops (up to
    72 % over white, which keeps white symbols at about 4.5:1)."""
    r, g, b, a = mat.tint
    if mat.adaptive:
        t = min(1.0, max(0.0, (luminance - 0.2) / 0.5))
        t = t * t * (3 - 2 * t)
        return QColor(r, g, b, int(a + (184 - a) * t))
    return QColor(r, g, b, a)


def paint_glass(painter: QPainter, rect: QRectF, image: QImage | None, mat: Material, radius: float | None,
                dpr: float, extra_tint: QColor | None = None, luminance: float = 0.0,
                clip: QRectF | None = None) -> None:
    """Draws a glass surface: the refracted backdrop (`image`, device px, or None for solid glass),
    the veil, an optional extra tint (hover, press) and the highlight of the edge. The contour lies
    outside the shape: `paint_contour` draws it, beneath. `clip`: the part of the surface that is
    visible (panels that run past the window's edge show no edge there)."""
    path = shape_path(rect, radius)
    painter.save()
    if clip is not None:
        painter.setClipRect(clip)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    if image is not None:
        brush = QBrush(image)
        brush.setTransform(QTransform.fromScale(1 / dpr, 1 / dpr) * QTransform.fromTranslate(rect.x(), rect.y()))
        painter.setBrush(brush)
        painter.drawPath(path)
        painter.fillPath(path, veil(mat, luminance))
    else:
        r, g, b = mat.solid
        painter.fillPath(path, QColor(r, g, b))  # opaque, so text on it keeps ClearType
    if extra_tint is not None:
        painter.fillPath(path, extra_tint)
    if mat.highlight > 0:
        e = _edges_for(rect, radius, mat, dpr)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        painter.drawImage(rect.topLeft() - QPointF(e.pad / dpr, e.pad / dpr), _with_dpr(e.highlight, dpr))
    painter.restore()


def paint_contour(painter: QPainter, rect: QRectF, radius: float | None, mat: Material, dpr: float) -> None:
    """The thin dark contour just outside a glass shape (draw it before, beneath, the glass)."""
    if max(mat.contour) <= 0:
        return
    e = _edges_for(rect, radius, mat, dpr)
    painter.drawImage(rect.topLeft() - QPointF(e.pad / dpr, e.pad / dpr), _with_dpr(e.contour, dpr))


def _edges_for(rect: QRectF, radius: float | None, mat: Material, dpr: float) -> _Edges:
    w, h = max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr))
    r = min(rect.width(), rect.height()) / 2 if radius is None else radius
    return edges(w, h, r, mat, dpr)


def _with_dpr(image: QImage, dpr: float) -> QImage:
    out = QImage(image)
    out.setDevicePixelRatio(dpr)
    return out


class GlassRenderer:
    """Draws the glass of one widget, reusing the last image while the backdrop stays the same.
    `bleed` (left, top, right, bottom): how far the surface runs past the widget's edges, for
    panels that reach the window's edge (no edge there, as in macOS 27's sidebars)."""

    def __init__(self, widget: QWidget, kind: str, radius: float | None = None,
                 bleed: tuple[int, int, int, int] = (0, 0, 0, 0)):
        self.widget = widget
        self.kind = kind
        self.radius = radius  # None: capsule
        self.bleed = bleed
        self._last_backdrop: QImage | None = None
        self._last: QImage | None = None
        self._luminance = 0.0
        self._key: tuple | None = None

    def invalidate(self) -> None:
        self._last_backdrop = self._last = self._key = None

    def material(self) -> Material:
        return material(self.kind)

    def margin(self, mat: Material) -> int:
        return int(mat.frost * 1.5 + 2)

    def luminance(self) -> float:
        """The mean luminance of the backdrop at the last paint (adaptive materials only)."""
        return self._luminance

    def paint(self, painter: QPainter, rect: QRectF | None = None, extra_tint: QColor | None = None) -> None:
        widget = self.widget
        rect = QRectF(widget.rect()) if rect is None else rect
        if rect.width() < 2 or rect.height() < 2:
            return
        clip = None
        if any(self.bleed):
            left, top, right, bottom = self.bleed
            clip = QRectF(rect)
            rect = rect.adjusted(-left, -top, right, bottom)
        mat = self.material()
        dpr = widget.devicePixelRatioF() or 1.0
        if reduce_transparency():
            paint_glass(painter, rect, None, mat, self.radius, dpr, extra_tint, clip=clip)
            return
        image = self._image(rect, mat, dpr)
        paint_glass(painter, rect, image, mat, self.radius, dpr, extra_tint, self._luminance, clip)

    def _image(self, rect: QRectF, mat: Material, dpr: float) -> QImage:
        w, h = max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr))
        m = self.margin(mat)
        area = rect.toAlignedRect().adjusted(-m, -m, m, m)
        radius = (min(rect.width(), rect.height()) / 2 if self.radius is None else self.radius) * dpr
        key = (w, h, round(radius, 2), self.kind, theme.is_dark(), _level, round(dpr, 3))
        backdrop = capture_backdrop(self.widget, area, dpr)
        if self._last is not None and key == self._key and self._last_backdrop == backdrop:
            return self._last
        self._last, self._luminance = render_glass(backdrop, round(m * dpr), w, h, radius, mat, dpr)
        self._last_backdrop = backdrop
        self._key = key
        return self._last


# ------------------------------------------------------------------------ shadow


_SHADOWS: OrderedDict[tuple, QImage] = OrderedDict()


def shadow_image(w: float, h: float, radius: float | None, mat: Material, dpr: float) -> tuple[QImage, int]:
    """A soft shadow for a w×h surface (logical px) and its margin (logical px)."""
    blur_px = mat.shadow_blur
    margin = int(blur_px + abs(mat.shadow_y) + 2)
    key = (round(w), round(h), None if radius is None else round(radius, 1), blur_px, mat.shadow_y, mat.shadow,
           round(dpr, 3))
    hit = _SHADOWS.get(key)
    if hit is not None:
        _SHADOWS.move_to_end(key)
        return hit, margin
    scale = 0.5  # shadows are soft: computed at half resolution
    sw, sh = max(4, round((w + 2 * margin) * dpr * scale)), max(4, round((h + 2 * margin) * dpr * scale))
    img = QImage(sw, sh, FMT)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(dpr * scale, dpr * scale)
    p.fillPath(shape_path(QRectF(margin, margin + mat.shadow_y, w, h), radius), QColor(0, 0, 0, 255))
    p.end()
    a = pixels(img)[..., 3].astype(np.float32)
    a = blur(a, max(1, round(blur_px * dpr * scale / 2.4)))
    alpha = np.clip(a * mat.shadow, 0, 255).astype(np.uint8)
    out = np.zeros((sh, sw, 4), np.uint8)
    out[..., 3] = alpha
    image = _image(out).scaled(round((w + 2 * margin) * dpr), round((h + 2 * margin) * dpr),
                               Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    image.setDevicePixelRatio(dpr)
    _SHADOWS[key] = image
    while len(_SHADOWS) > 30:
        _SHADOWS.popitem(last=False)
    return image, margin


class ShadowLayer(QWidget):
    """What a glass surface casts around itself: its faint shadow and the dark contour of its edge.
    A sibling just beneath the surface that takes no mouse input."""

    glass_layer = True

    def __init__(self, surface: QWidget):
        super().__init__(surface.parentWidget())
        self.surface = surface
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._margin = 0

    def _needed(self, mat: Material) -> int:
        """The margin around the surface this layer needs, or 0 if it draws nothing."""
        dpr = self.surface.devicePixelRatioF() or 1.0
        margin = 0
        if mat.shadow > 0:
            margin = shadow_image(self.surface.width(), self.surface.height(), self.surface._glass.radius, mat,
                                  dpr)[1]
        if max(mat.contour) > 0:
            margin = max(margin, math.ceil(2 / dpr) + 1)
        return margin

    def sync(self) -> None:
        surface = self.surface
        if self.parentWidget() is not surface.parentWidget():
            self.setParent(surface.parentWidget())
        mat = surface._glass.material()
        margin = self._needed(mat) if surface.casts_shadow else 0
        if not surface.isVisible() or margin <= 0:
            self.hide()
            return
        self._margin = margin
        self.setGeometry(surface.geometry().adjusted(-margin, -margin, margin, margin))
        self.stackUnder(surface)
        self.show()
        self.update()

    def paintEvent(self, _event) -> None:
        if capturing():
            return
        surface = self.surface
        renderer: GlassRenderer = surface._glass
        mat = renderer.material()
        dpr = surface.devicePixelRatioF() or 1.0
        p = QPainter(self)
        if mat.shadow > 0:
            image, margin = shadow_image(surface.width(), surface.height(), renderer.radius, mat, dpr)
            p.drawImage(QPointF(self._margin - margin, self._margin - margin), image)
        rect = QRectF(self._margin, self._margin, surface.width(), surface.height())
        paint_contour(p, rect, renderer.radius, mat, dpr)


class GlassSurface(QWidget):
    """A glass panel or bar; its children (labels, buttons, lists) are drawn on top of the glass.
    Children should not paint a background of their own."""

    def __init__(self, parent: QWidget | None = None, kind: str = BAR, radius: float | None = None,
                 shadow: bool = True, bleed: tuple[int, int, int, int] = (0, 0, 0, 0)):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        self._glass = GlassRenderer(self, kind, radius, bleed)
        self.casts_shadow = shadow
        self._shadow: ShadowLayer | None = None
        self._syncing = False
        theme.changes().changed.connect(self._theme_changed)

    # The glass of other surfaces skips this one, and this one paints nothing while it is being captured.
    glass_layer = True

    def set_kind(self, kind: str) -> None:
        self._glass.kind = kind
        self._glass.invalidate()
        self._sync_shadow()
        self.update()

    def kind(self) -> str:
        return self._glass.kind

    def radius(self) -> float | None:
        return self._glass.radius

    def _theme_changed(self) -> None:
        self._glass.invalidate()
        self._sync_shadow()
        self.update()

    def _sync_shadow(self) -> None:
        if self.parentWidget() is None or self._syncing:
            return
        if self._shadow is None:
            if not self.casts_shadow:
                return
            self._shadow = ShadowLayer(self)
        self._syncing = True
        try:
            self._shadow.sync()
        finally:
            self._syncing = False

    def event(self, event) -> bool:
        kind = event.type()
        if kind in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide,
                    QEvent.Type.ParentChange, QEvent.Type.ZOrderChange):
            self._sync_shadow()
        return super().event(event)

    def paintEvent(self, _event) -> None:
        if capturing():
            return
        p = QPainter(self)
        self._glass.paint(p)
