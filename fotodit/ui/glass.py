"""Artapa's materials (artapa.com), drawn the way the site's CSS draws them.

- Stage (`paint_stage`): the window itself, as Artapa's admin panel lies on its page: #070707 with a
  faint light from the top left and a fainter one from the bottom right, and a grain too fine to see
  that keeps the light from banding.
- Card (PANEL): the sidebar and the inspector, as the admin panel's sidebar: surface-1 under the
  aurora (a warm light from the top left and a cool one from the bottom right, about 4 %), a ring a
  pixel wide and a line of light inside the top edge. Opaque: nothing behind it is read.
- Pill (BAR): a toolbar floating over the content, as the site's header pill: what lies beneath it
  blurred (CSS blur(18px)) and more saturated (1.4), under rgb(30 30 30 / .5), which darkens over bright
  content (up to .9) so that its words stay legible (the secondary ones at 5.1:1 over white); a ring.
- Floating pill (MEDIA): a bar over photos and videos, as the site's floating pill: blur(34px)
  saturate(1.45) under #0A0A0A at 74 %, a ring, a line of light at the top and a soft shadow.
- Sheet (SHEET): a dialog card, as the site's sign-in card: blur(26px) saturate(1.35) under #0C0C0C at
  50 %, the sheen, the aurora, a ring, a line of light and a 1 px glint along the top; a large, soft
  shadow.
- Toast (TOAST): a short message, as the site's toast: the blur under rgb(30 30 30 / .86) and a ring.
- Round (ROUND): the large preview's round buttons, as the site's round play button: blur(14px) under
  rgb(12 12 12 / .42), a ring of white at 17 % and a line of light.

How a frosted surface is drawn: what lies beneath it (plus a margin) is rendered into an image (the
parent's own background and the lower siblings the surface floats over, through `QWidget.render`, or
a widget's own `glass_backdrop` image when it has one, like the large preview's stage), blurred on a
reduced copy and drawn under the veil. Other frosted surfaces are skipped, so glass never shows
glass. If the backdrop did not change since the last paint (a child button's hover, say), the last
image is reused. "Saydamlığı azalt" (reduce transparency) draws an almost opaque fill instead of the
blur, as the site does where a browser has no backdrop filter.
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
    QRadialGradient,
    QRegion,
    QTransform,
)
from PySide6.QtWidgets import QWidget

from . import theme

FMT = QImage.Format.Format_ARGB32_Premultiplied

PANEL = "panel"  # the sidebar and the inspector: opaque cards on the stage
BAR = "bar"  # toolbars floating over the content
MEDIA = "media"  # bars over photos and videos (the large preview)
SHEET = "sheet"  # a card over the dimmed window (confirmations, progress, About)
TOAST = "toast"  # a short message
TOOLBAR = "toolbar"  # the strip behind the toolbars while the content is scrolled
ROUND = "round"  # a round button over the large preview's stage (the edge arrows, play)


@dataclass(frozen=True)
class Material:
    tint: tuple[int, int, int, int]  # the veil over the frosted backdrop (RGBA); opaque: a solid card
    blur: float = 0.0  # CSS blur radius (logical px): the standard deviation of the backdrop's blur
    saturate: float = 1.0  # CSS saturate()
    ring: int = 0  # alpha of the white ring a pixel wide inside the edge
    rim: int = 0  # alpha of the white line of light inside the top edge
    sheen: int = 0  # alpha of the white sheen from the top, gone by 45 % of the height
    aurora: float = 0.0  # Artapa's aurora: 1 its usual strength, about 1.55 its strong one
    glint: bool = False  # the line of warm and cool light along the top edge (the sign-in card)
    shadow: float = 0.0  # opacity of the drop shadow (black)
    shadow_blur: float = 0.0  # CSS blur radius of the shadow (logical px)
    shadow_y: float = 0.0
    shadow_spread: float = 0.0  # CSS spread (negative: the shadow is smaller than the surface)
    solid: tuple[int, int, int] = (13, 13, 13)  # the fill with "Saydamlığı azalt"
    adaptive: int = 0  # > 0: over bright backdrops the veil's alpha grows to this (its words stay legible)


_MATERIALS = {
    PANEL: Material((13, 13, 13, 255), ring=20, rim=15, aurora=1.0),
    BAR: Material((30, 30, 30, 128), blur=18, saturate=1.4, ring=15, shadow=0.25, shadow_blur=24, shadow_y=8,
                  shadow_spread=-18, solid=(24, 24, 24), adaptive=230),
    MEDIA: Material((10, 10, 10, 189), blur=34, saturate=1.45, ring=19, rim=15, shadow=0.7, shadow_blur=40,
                    shadow_y=16, shadow_spread=-18, solid=(16, 16, 16)),
    SHEET: Material((12, 12, 12, 128), blur=26, saturate=1.35, ring=20, rim=15, sheen=9, aurora=1.0, glint=True,
                    shadow=0.9, shadow_blur=70, shadow_y=30, shadow_spread=-40, solid=(13, 13, 13)),
    TOAST: Material((30, 30, 30, 219), blur=18, saturate=1.4, ring=20, shadow=0.35, shadow_blur=24, shadow_y=8,
                    shadow_spread=-12, solid=(30, 30, 30)),
    TOOLBAR: Material((6, 6, 6, 0), blur=10, saturate=1.4, solid=(10, 10, 10)),
    ROUND: Material((12, 12, 12, 107), blur=14, ring=0x2B, rim=15, solid=(24, 24, 24)),
}


def material(kind: str) -> Material:
    """The material of a kind of surface."""
    return _MATERIALS[kind]


# ------------------------------------------------------------ reduce transparency


_reduced = False


def reduce_transparency() -> bool:
    """Solid surfaces ("Saydamlığı azalt"); FOTODIT_SOLID_GLASS=1 forces it."""
    return _reduced or os.environ.get("FOTODIT_SOLID_GLASS") == "1"


def set_reduce_transparency(value: bool) -> None:
    """Turns solid surfaces on or off (the Görünüm menu's "Saydamlığı azalt")."""
    global _reduced
    if bool(value) != _reduced:
        _reduced = bool(value)
        theme.changes().changed.emit()  # frosted surfaces drop their images and repaint


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
    """Gaussian-like blur: a few box passes in both directions (three of radius r: a sigma of about r)."""
    out = a.astype(np.float32, copy=False)
    for _ in range(passes):
        out = _box(_box(out, r, 1), r, 0)
    return out


def frost(img: QImage, sigma: float, saturate: float, part: QRect | None = None) -> QImage:
    """Blurs a (premultiplied) image with a standard deviation of about `sigma` device pixels and sets
    its saturation as CSS's saturate() does; `part`: the only rectangle of the result that is wanted (a
    surface inside its backdrop's margin). The work happens on a reduced copy; Qt averages while
    reducing and interpolates when enlarging."""
    w, h = img.width(), img.height()
    part = QRect(0, 0, w, h) if part is None else part
    factor = max(1, int(sigma / 1.25))
    sw, sh = max(2, -(-w // factor)), max(2, -(-h // factor))
    small = img.scaled(sw, sh, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    small = small.convertToFormat(FMT)
    a = blur(pixels(small), 1, passes=2)
    if saturate != 1.0:
        lum = a[..., :3] @ np.array([0.0722, 0.7152, 0.2126], np.float32)  # B, G, R
        a[..., :3] = lum[..., None] + (a[..., :3] - lum[..., None]) * np.float32(saturate)
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


def shape_path(rect: QRectF, radius: float | None) -> QPainterPath:
    """A rounded rectangle; `radius` None: a capsule (fully rounded ends)."""
    path = QPainterPath()
    r = min(rect.width(), rect.height()) / 2 if radius is None else min(radius, rect.width() / 2, rect.height() / 2)
    path.addRoundedRect(rect, r, r)
    return path


def _radius(rect: QRectF, radius: float | None) -> float:
    return min(rect.width(), rect.height()) / 2 if radius is None else min(radius, rect.width() / 2,
                                                                            rect.height() / 2)


# ------------------------------------------------------------------- the stage


_STAGES: OrderedDict[tuple, QPixmap] = OrderedDict()
_GRAIN: QImage | None = None


def _grain() -> QImage:
    """A tile of grain a level of grey high on every other pixel, added to the stage so the edges
    between the levels of its faint light do not show as bands (a browser dithers its gradients)."""
    global _GRAIN
    if _GRAIN is None:
        on = np.random.default_rng(7).random((128, 128)) < 0.5
        a = np.zeros((128, 128, 4), np.uint8)
        a[on] = 1  # premultiplied white at 1/255: adds one level
        _GRAIN = _image(a)
    return _GRAIN


def paint_stage(painter: QPainter, rect: QRectF, dpr: float, base: str | None = None) -> None:
    """Artapa's calm stage over `rect`: radial-gradient(60% 55% at 0% 0%, #d8d8db1c, transparent 70%) and
    radial-gradient(55% 55% at 100% 100%, #e2e0dd12, transparent 70%) on #070707 (or `base`)."""
    w, h = max(1, round(rect.width())), max(1, round(rect.height()))
    key = (w, h, round(dpr, 3), base)
    pm = _STAGES.get(key)
    if pm is None:
        pm = QPixmap(max(1, round(w * dpr)), max(1, round(h * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(QColor(base or theme.WINDOW))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(0, 0, w, h)
        for corner, (rx, ry), color, alpha in ((QPointF(0, 0), (0.60, 0.55), "#D8D8DB", 0x1C),
                                               (QPointF(w, h), (0.55, 0.55), "#E2E0DD", 0x12)):
            gradient = QRadialGradient(QPointF(0, 0), 1.0)
            c = QColor(color)
            c.setAlpha(alpha)
            gradient.setColorAt(0.0, c)
            c.setAlpha(0)
            gradient.setColorAt(0.7, c)
            brush = QBrush(gradient)
            brush.setTransform(QTransform.fromScale(rx * w, ry * h) * QTransform.fromTranslate(corner.x(), corner.y()))
            p.fillRect(area, brush)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        grain = QImage(_grain())
        grain.setDevicePixelRatio(dpr)
        p.fillRect(area, QBrush(grain))
        p.end()
        _STAGES[key] = pm
        while len(_STAGES) > 6:
            _STAGES.popitem(last=False)
    else:
        _STAGES.move_to_end(key)
    painter.drawPixmap(rect.topLeft(), pm)


# ------------------------------------------------------------------- Artapa's layers


def paint_aurora(painter: QPainter, rect: QRectF, path: QPainterPath, strength: float = 1.0) -> None:
    """Artapa's aurora on a shape: radial-gradient(120% 90% at 0% 0%, #feeae10b, transparent 55%) and
    radial-gradient(120% 90% at 100% 100%, #c0cef20a, transparent 60%), `strength` times as strong."""
    for corner, token, alpha, end in ((rect.topLeft(), theme.AURORA_WARM, 0x0B, 0.55),
                                      (rect.bottomRight(), theme.AURORA_COOL, 0x0A, 0.60)):
        gradient = QRadialGradient(QPointF(0, 0), 1.0)
        c = QColor(token)
        c.setAlpha(min(255, round(alpha * strength)))
        gradient.setColorAt(0.0, c)
        c.setAlpha(0)
        gradient.setColorAt(end, c)
        brush = QBrush(gradient)
        brush.setTransform(QTransform.fromScale(1.2 * rect.width(), 0.9 * rect.height())
                           * QTransform.fromTranslate(corner.x(), corner.y()))
        painter.fillPath(path, brush)


def paint_sheen(painter: QPainter, rect: QRectF, path: QPainterPath, alpha: int) -> None:
    """linear-gradient(180deg, white at `alpha`, transparent 45%)."""
    gradient = QLinearGradient(QPointF(0, rect.top()), QPointF(0, rect.top() + rect.height() * 0.45))
    gradient.setColorAt(0.0, QColor(255, 255, 255, alpha))
    gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.fillPath(path, QBrush(gradient))


def paint_ring(painter: QPainter, rect: QRectF, radius: float | None, color: QColor | QBrush,
               width: float = 1.0) -> None:
    """A ring `width` px wide inside the edge (CSS: a border, or inset 0 0 0 1px)."""
    outer = shape_path(rect, radius)
    r = _radius(rect, radius)
    inner = shape_path(rect.adjusted(width, width, -width, -width), max(0.0, r - width))
    painter.fillPath(outer.subtracted(inner), color)


def paint_rim(painter: QPainter, rect: QRectF, radius: float | None, alpha: int) -> None:
    """A line of light inside the top edge (CSS: inset 0 1px 0 0 white at `alpha`)."""
    path = shape_path(rect, radius)
    painter.fillPath(path.subtracted(path.translated(0, 1)), QColor(255, 255, 255, alpha))


def paint_glint(painter: QPainter, rect: QRectF) -> None:
    """The sign-in card's glint: a line a pixel high along the top, from 14 % to 86 % of the width,
    clear at its ends, warm then cool at 42 %."""
    left, right = rect.left() + rect.width() * 0.14, rect.right() - rect.width() * 0.14
    gradient = QLinearGradient(QPointF(left, 0), QPointF(right, 0))
    for at, token, alpha in ((0.0, theme.AURORA_WARM, 0), (0.35, theme.AURORA_WARM, 0x6B),
                             (0.65, theme.AURORA_COOL, 0x6B), (1.0, theme.AURORA_COOL, 0)):
        c = QColor(token)
        c.setAlpha(alpha)
        gradient.setColorAt(at, c)
    painter.fillRect(QRectF(left, rect.top(), right - left, 1.0), QBrush(gradient))


def paint_card(painter: QPainter, rect: QRectF, radius: float | None, fill: QColor | None = None,
               aurora: float = 1.0, ring: int = 20, rim: int = 15, sheen: int = 0) -> None:
    """A card drawn without a backdrop: an optional fill, the aurora, the sheen, the ring and the rim
    (Artapa's glyph cards, the groups of the inspector)."""
    path = shape_path(rect, radius)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    if fill is not None:
        painter.fillPath(path, fill)
    if aurora > 0:
        paint_aurora(painter, rect, path, aurora)
    if sheen > 0:
        paint_sheen(painter, rect, path, sheen)
    if ring > 0:
        paint_ring(painter, rect, radius, QColor(255, 255, 255, ring))
    if rim > 0:
        paint_rim(painter, rect, radius, rim)
    painter.restore()


# --------------------------------------------------------------------------- backdrop

_capturing = 0  # > 0 while a backdrop is being rendered: frosted surfaces inside it do not paint themselves


def capturing() -> bool:
    return _capturing > 0


def is_glass_layer(widget: QWidget) -> bool:
    return bool(getattr(widget, "glass_layer", False))


def capture_backdrop(widget: QWidget, rect: QRect, dpr: float, base: QColor | None = None) -> QImage:
    """What lies beneath `rect` (in `widget`'s coordinates): the way the window paints itself, from
    the window down to `widget`'s parent, each ancestor's own background followed by its children
    that lie below the path to `widget`; frosted surfaces are left out. Device pixels, premultiplied."""
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
    frosted surfaces above it: the first request renders a full-width band around itself, later
    requests of the same pass are cut from it. Without it every bar would render the same thumbnails
    again. Set as the widget's `glass_backdrop`, which `capture_backdrop` asks first."""

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


def margin_of(mat: Material) -> int:
    """How far around a surface its backdrop is read (logical px): two standard deviations of the
    blur, which hold 95 % of its weight (three cost Gözat's scrolling a fifth more, measured)."""
    return int(2 * mat.blur + 2)


def render_glass(backdrop: QImage, margin: int, w: int, h: int, mat: Material,
                 dpr: float) -> tuple[QImage, float]:
    """The frosted backdrop of a w×h (device px) surface, from a backdrop that is `margin` device pixels
    larger on every side, and the backdrop's mean luminance (0–1, for adaptive materials)."""
    source = backdrop.convertToFormat(FMT)
    source.setDevicePixelRatio(1.0)  # (device pixels throughout)
    out = frost(source, mat.blur * dpr, mat.saturate, QRect(margin, margin, w, h))
    luminance = 0.0
    if mat.adaptive:
        small = pixels(out)[:: max(1, h // 24), :: max(1, w // 48), :3].astype(np.float32)
        luminance = float((small @ np.array([0.0722, 0.7152, 0.2126], np.float32)).mean() / 255.0)
    return out, luminance


def veil(mat: Material, luminance: float) -> QColor:
    """The veil over the frosted backdrop; adaptive materials grow darker over bright backdrops."""
    r, g, b, a = mat.tint
    if mat.adaptive:
        t = min(1.0, max(0.0, (luminance - 0.15) / 0.45))
        t = t * t * (3 - 2 * t)
        return QColor(r, g, b, int(a + (mat.adaptive - a) * t))
    return QColor(r, g, b, a)


def paint_glass(painter: QPainter, rect: QRectF, image: QImage | None, mat: Material, radius: float | None,
                dpr: float, extra_tint: QColor | None = None, luminance: float = 0.0) -> None:
    """Draws a surface: the frosted backdrop (`image`, device px) under its veil, or a solid fill (an
    opaque material, or `image` None), then the aurora, the sheen, an optional extra tint (hover), the
    ring, the rim and the glint."""
    path = shape_path(rect, radius)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    if image is not None:
        brush = QBrush(image)
        brush.setTransform(QTransform.fromScale(1 / dpr, 1 / dpr) * QTransform.fromTranslate(rect.x(), rect.y()))
        painter.setBrush(brush)
        painter.drawPath(path)
        painter.fillPath(path, veil(mat, luminance))
    else:
        r, g, b, a = mat.tint
        fill = QColor(r, g, b) if a == 255 and not reduce_transparency() else QColor(*mat.solid)
        painter.fillPath(path, fill)  # opaque, so text on it keeps ClearType
    if mat.aurora > 0:
        paint_aurora(painter, rect, path, mat.aurora)
    if mat.sheen > 0:
        paint_sheen(painter, rect, path, mat.sheen)
    if extra_tint is not None:
        painter.fillPath(path, extra_tint)
    if mat.ring > 0:
        paint_ring(painter, rect, radius, QColor(255, 255, 255, mat.ring))
    if mat.rim > 0:
        paint_rim(painter, rect, radius, mat.rim)
    if mat.glint:
        paint_glint(painter, rect)
    painter.restore()


class GlassRenderer:
    """Draws the surface of one widget, reusing the last frosted image while the backdrop stays the same."""

    def __init__(self, widget: QWidget, kind: str, radius: float | None = None):
        self.widget = widget
        self.kind = kind
        self.radius = radius  # None: capsule
        self._last_backdrop: QImage | None = None
        self._last: QImage | None = None
        self._luminance = 0.0
        self._key: tuple | None = None

    def invalidate(self) -> None:
        self._last_backdrop = self._last = self._key = None

    def material(self) -> Material:
        return material(self.kind)

    def margin(self, mat: Material) -> int:
        return margin_of(mat)

    def luminance(self) -> float:
        """The mean luminance of the backdrop at the last paint (adaptive materials only)."""
        return self._luminance

    def paint(self, painter: QPainter, rect: QRectF | None = None, extra_tint: QColor | None = None) -> None:
        widget = self.widget
        rect = QRectF(widget.rect()) if rect is None else rect
        if rect.width() < 2 or rect.height() < 2:
            return
        mat = self.material()
        dpr = widget.devicePixelRatioF() or 1.0
        if reduce_transparency() or mat.blur <= 0:
            paint_glass(painter, rect, None, mat, self.radius, dpr, extra_tint)
            return
        image = self._image(rect, mat, dpr)
        paint_glass(painter, rect, image, mat, self.radius, dpr, extra_tint, self._luminance)

    def _image(self, rect: QRectF, mat: Material, dpr: float) -> QImage:
        w, h = max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr))
        m = self.margin(mat)
        area = rect.toAlignedRect().adjusted(-m, -m, m, m)
        key = (w, h, self.kind, round(dpr, 3))
        backdrop = capture_backdrop(self.widget, area, dpr)
        if self._last is not None and key == self._key and self._last_backdrop == backdrop:
            return self._last
        self._last, self._luminance = render_glass(backdrop, round(m * dpr), w, h, mat, dpr)
        self._last_backdrop = backdrop
        self._key = key
        return self._last


# ------------------------------------------------------------------------ shadow


_SHADOWS: OrderedDict[tuple, QImage] = OrderedDict()


def shadow_image(w: float, h: float, radius: float | None, mat: Material, dpr: float) -> tuple[QImage, int]:
    """The drop shadow of a w×h surface (logical px), as CSS's box-shadow draws it (offset, blur radius,
    spread), and its margin (logical px)."""
    blur_px, spread = mat.shadow_blur, mat.shadow_spread
    margin = int(blur_px + abs(mat.shadow_y) + max(0.0, spread) + 2)
    key = (round(w), round(h), None if radius is None else round(radius, 1), blur_px, mat.shadow_y, spread,
           mat.shadow, round(dpr, 3))
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
    shape = QRectF(margin, margin + mat.shadow_y, w, h).adjusted(-spread, -spread, spread, spread)
    r = None if radius is None else max(0.0, radius + spread)
    if shape.width() > 0 and shape.height() > 0:
        p.fillPath(shape_path(shape, r), QColor(0, 0, 0, 255))
    p.end()
    a = pixels(img)[..., 3].astype(np.float32)
    a = blur(a, max(1, round(blur_px / 2 * dpr * scale)))  # CSS: a Gaussian of half the blur radius
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
    """The shadow a surface casts: a sibling just beneath the surface that takes no mouse input."""

    glass_layer = True

    def __init__(self, surface: QWidget):
        super().__init__(surface.parentWidget())
        self.surface = surface
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._margin = 0

    def sync(self) -> None:
        surface = self.surface
        if self.parentWidget() is not surface.parentWidget():
            self.setParent(surface.parentWidget())
        mat = surface._glass.material()
        margin = 0
        if surface.casts_shadow and mat.shadow > 0:
            dpr = surface.devicePixelRatioF() or 1.0
            margin = shadow_image(surface.width(), surface.height(), surface._glass.radius, mat, dpr)[1]
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
        image, margin = shadow_image(surface.width(), surface.height(), renderer.radius, mat, dpr)
        p = QPainter(self)
        p.drawImage(QPointF(self._margin - margin, self._margin - margin), image)


class GlassSurface(QWidget):
    """A card or a bar; its children (labels, buttons, lists) are drawn on top of it. Children should
    not paint a background of their own."""

    def __init__(self, parent: QWidget | None = None, kind: str = BAR, radius: float | None = None,
                 shadow: bool = True):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        self._glass = GlassRenderer(self, kind, radius)
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
            if not self.casts_shadow or self._glass.material().shadow <= 0:
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
