"""Controls in Artapa's look: the quiet buttons of its header pill, its silver, secondary and red
buttons, a row of tabs with a sliding selection, a switch, a slider with a silver thumb, and a toast.

They keep Qt's own APIs (a switch is a QCheckBox, a button a QPushButton, a slider a QSlider),
so keyboard use, screen readers, UI Automation and the code that drives them work as before;
only painting and motion are new. Feedback starts on press (not on release), and every change
of state moves with a spring (see `motion`), so it can be interrupted and reversed midway.
"""

from __future__ import annotations

import math
from collections import OrderedDict

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QWidget,
)

from ..i18n import t
from . import glass, icons, theme
from .motion import Spring

SILVER, SECONDARY, DANGER = "silver", "secondary", "danger"  # the kinds of PillButton


def _qc(token: str) -> QColor:
    return QColor(token) if token.startswith("#") else QColor(getattr(theme, token))


def _alpha(c: QColor, a: float) -> QColor:
    c = QColor(c)
    c.setAlphaF(max(0.0, min(1.0, c.alphaF() * a)))
    return c


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * t, a.greenF() + (b.greenF() - a.greenF()) * t,
                           a.blueF() + (b.blueF() - a.blueF()) * t, a.alphaF() + (b.alphaF() - a.alphaF()) * t)


def paint_focus(painter: QPainter, rect: QRectF, radius: float | None) -> None:
    """Artapa's focus ring: 1 px of the accent at 62 %, 2 px outside the shape (:focus-visible)."""
    outer = rect.adjusted(-2, -2, 2, 2)
    r = min(outer.width(), outer.height()) / 2 if radius is None else radius + 2
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(_alpha(QColor(theme.ACCENT), 0.62), 1.0))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(outer.adjusted(0.5, 0.5, -0.5, -0.5), r - 0.5, r - 0.5)
    painter.restore()


class _FocusRing:
    """Shows the focus ring only after keyboard navigation (Tab, a shortcut), like the web's
    :focus-visible: a click or a sheet opening gives focus without drawing a ring."""

    _ring = False

    def focusInEvent(self, event) -> None:  # noqa: N802 - Qt override
        reasons = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                   Qt.FocusReason.ShortcutFocusReason)
        self._ring = event.reason() in reasons
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._ring = False
        super().focusOutEvent(event)

    def focus_ring(self) -> bool:
        return self.hasFocus() and self._ring


class _Feedback:
    """Hover and press springs for a button; repaints the widget while they move."""

    def __init__(self, widget: QWidget):
        self.hover = Spring(0.0, response=0.22, parent=widget)
        self.press = Spring(0.0, response=0.16, parent=widget)
        self.hover.changed.connect(lambda _v: widget.update())
        self.press.changed.connect(lambda _v: widget.update())


# ------------------------------------------------------------------- bar buttons


class BarButton(_FocusRing, QPushButton):
    """A button on a bar, as the links of Artapa's header pill: an icon and/or a text in the
    secondary text color, with no background until the pointer comes (white at 6 % and the primary
    color); pressed or checked, white at 10 %."""

    def __init__(self, text: str = "", icon_name: str | None = None, parent: QWidget | None = None,
                 tooltip: str = "", accessible: str | None = None):
        super().__init__(text, parent)
        self._icon_name = icon_name
        self._compact = False  # only the icon is shown (narrow windows); the text stays the button's name
        self._fb = _Feedback(self)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAutoDefault(False)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if tooltip:
            self.setToolTip(tooltip)
        if accessible or (not text and tooltip):
            self.setAccessibleName(accessible or tooltip.split("(")[0].strip())
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def set_icon_name(self, name: str | None) -> None:
        self._icon_name = name
        self.updateGeometry()
        self.update()

    def icon_name(self) -> str | None:
        return self._icon_name

    def set_compact(self, compact: bool) -> None:
        """Shows only the icon (when the window is narrow); the text becomes the tooltip."""
        compact = compact and bool(self._icon_name)
        if compact != self._compact:
            self._compact = compact
            if compact and not self.toolTip():
                self.setToolTip(self.text())
            if not self.accessibleName():
                self.setAccessibleName(self.text())
            self.updateGeometry()
            self.update()

    def shown_text(self) -> str:
        return "" if self._compact else self.text()

    def sizeHint(self) -> QSize:
        fm = QFontMetricsF(self.font())
        h = 32
        text = self.shown_text()
        if not text:
            return QSize(h, h)
        w = fm.horizontalAdvance(text) + 26
        if self._icon_name:
            w += 18 + 6
        return QSize(int(w + 0.5), h)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def event(self, event) -> bool:
        kind = event.type()
        if kind in (QEvent.Type.HoverEnter, QEvent.Type.Enter):
            self._fb.hover.set_target(1.0)
        elif kind in (QEvent.Type.HoverLeave, QEvent.Type.Leave):
            self._fb.hover.set_target(0.0)
        return super().event(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._fb.press.set_target(1.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._fb.press.set_target(0.0)
        super().mouseReleaseEvent(event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self._fb.hover.jump(0.0)
            self._fb.press.jump(0.0)
        super().changeEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = r.height() / 2
        enabled = self.isEnabled()
        h, d = (self._fb.hover.value, self._fb.press.value) if enabled else (0.0, 0.0)
        lit = self.isChecked() and enabled
        # White at 6 % under the pointer, 10 % pressed or checked (Artapa's nav-link and nav-link-active).
        fill = max(0.10 if lit else 0.0, 0.06 * h, 0.10 * d)
        if fill > 0.004:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, round(255 * fill)))
            p.drawRoundedRect(r, radius, radius)
        if not enabled:
            color = _qc("DISABLED")
        else:
            color = _blend(_qc("MUTED"), _qc("TEXT"), max(h, d, 1.0 if lit else 0.0))
        if self.focus_ring() and self.focusPolicy() != Qt.FocusPolicy.NoFocus:
            paint_focus(p, r.adjusted(2, 2, -2, -2), None)
        text = self.shown_text()
        icon_size = 18.0
        x = r.center().x() - icon_size / 2 if not text else r.left() + 13
        if self._icon_name:
            scale = 1.0 - 0.08 * d
            s = icon_size * scale
            icons.paint(p, QRectF(x + (icon_size - s) / 2, r.center().y() - s / 2, s, s), self._icon_name,
                        color, motion=h)
            x += icon_size + 6
        if text:
            p.setPen(color)
            p.setFont(self.font())
            if not self._icon_name:
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
            else:
                p.drawText(QRectF(x, r.top(), r.right() - x - 12, r.height()),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


# ------------------------------------------------------------------- pill buttons


_GLOWS: OrderedDict[tuple, QImage] = OrderedDict()


def _inset_glow(w: float, h: float, radius: float, dy: float, blur: float, spread: float, rgb: tuple[int, int, int],
                alpha: float, dpr: float) -> QImage:
    """CSS's `box-shadow: inset 0 dy blur spread color` of a w×h shape with corner radius `radius`:
    the light that falls inside the shape past its edge moved by `dy` and grown by −`spread`. Kept for
    reuse (the hover steps are rounded)."""
    key = (round(w), round(h), round(radius, 1), round(dy, 1), round(blur, 1), spread, rgb, round(alpha, 3),
           round(dpr, 3))
    hit = _GLOWS.get(key)
    if hit is not None:
        _GLOWS.move_to_end(key)
        return hit
    pw, ph = max(1, round(w * dpr)), max(1, round(h * dpr))
    sigma = max(1, round(blur / 2 * dpr))  # CSS: a Gaussian of half the blur radius (three box passes of it)
    pad = 3 * sigma + 2  # the hole is drawn on a larger canvas, so its edges beyond the shape blur in too
    hole = QImage(pw + 2 * pad, ph + 2 * pad, glass.FMT)
    hole.fill(Qt.GlobalColor.transparent)
    p = QPainter(hole)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.translate(pad, pad)
    p.scale(dpr, dpr)
    rect = QRectF(0, dy, w, h).adjusted(spread, spread, -spread, -spread)
    p.fillPath(glass.shape_path(rect, max(0.0, radius - spread)), QColor(0, 0, 0, 255))
    p.end()
    a = glass.pixels(hole)[..., 3].astype(np.float32) / 255.0
    a = glass.blur(a, sigma)[pad:pad + ph, pad:pad + pw]
    light = np.clip(1.0 - a, 0.0, 1.0)
    shape = QImage(pw, ph, glass.FMT)
    shape.fill(Qt.GlobalColor.transparent)
    p = QPainter(shape)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(dpr, dpr)
    p.fillPath(glass.shape_path(QRectF(0, 0, w, h), radius), QColor(0, 0, 0, 255))
    p.end()
    light *= glass.pixels(shape)[..., 3].astype(np.float32) / 255.0
    out = np.zeros((ph, pw, 4), np.float32)
    level = light * alpha * 255.0
    out[..., 0] = level * rgb[2] / 255.0  # B, G, R, A in memory, premultiplied
    out[..., 1] = level * rgb[1] / 255.0
    out[..., 2] = level * rgb[0] / 255.0
    out[..., 3] = level
    image = glass._image(np.clip(out, 0, 255).astype(np.uint8))
    image.setDevicePixelRatio(dpr)
    _GLOWS[key] = image
    while len(_GLOWS) > 80:
        _GLOWS.popitem(last=False)
    return image


def _edge_gradient(rect: QRectF, alpha: float) -> QLinearGradient:
    """The silver button's edge: linear-gradient(140deg, white .62, .06 at 38 %, 0 at 60 %, .34), at `alpha`."""
    angle = math.radians(140)
    dx, dy = math.sin(angle), -math.cos(angle)
    length = abs(rect.width() * dx) + abs(rect.height() * dy)
    c = rect.center()
    gradient = QLinearGradient(QPointF(c.x() - dx * length / 2, c.y() - dy * length / 2),
                               QPointF(c.x() + dx * length / 2, c.y() + dy * length / 2))
    for at, level in ((0.0, 0.62), (0.38, 0.06), (0.60, 0.0), (1.0, 0.34)):
        gradient.setColorAt(at, QColor(255, 255, 255, round(255 * level * alpha)))
    return gradient


def paint_silver_circle(painter: QPainter, c: QPointF, r: float, icon: str | None = None,
                        icon_size: float = 0.0) -> None:
    """Artapa's silver icon circle: #fff → #e6e7ee (55 %) → #c9ccd6, a light inner top edge, a dark
    inner foot and a soft white glow under it, holding a dark icon."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    for i in range(4):  # the glow: 0 4px 14px -8px white at 34 %
        k = (4 - i) / 4
        painter.setBrush(QColor(255, 255, 255, round(255 * 0.34 * 0.18 * k)))
        painter.drawEllipse(QPointF(c.x(), c.y() + 4), r - 6 + 4 * i, r - 6 + 4 * i)
    rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
    painter.setBrush(theme.silver_gradient(rect.top(), rect.bottom()))
    painter.drawEllipse(rect)
    path = glass.shape_path(rect, None)
    painter.fillPath(path.subtracted(path.translated(0, 1)), QColor(255, 255, 255, 242))
    painter.fillPath(path.subtracted(path.translated(0, -1)), QColor(0, 0, 0, 20))
    painter.restore()
    if icon:
        s = icon_size or r * 0.95
        icons.paint(painter, QRectF(c.x() - s / 2, c.y() - s / 2, s, s), icon, QColor(theme.ON_ACCENT))


class PillButton(_FocusRing, QPushButton):
    """Artapa's buttons, as its `Button.astro` draws them. SILVER, the primary one: white at 5.5 % over
    what is behind it, a line of light inside the top edge, a glow rising inside its foot and an edge a
    pixel wide that is bright at the top left and again, fainter, at the bottom right; its icon sits in
    the silver circle at its end. SECONDARY: a ring round a quiet wash, a cool glow inside its foot under
    the pointer. DANGER: Artapa's red tint in a red ring, for what ends something. Under the pointer each
    lifts (more light, a brighter edge); pressed, it shrinks to 97 %; disabled, it is at half strength."""

    PAD = 3.0  # room for the focus ring

    def __init__(self, text: str = "", icon_name: str | None = None, parent: QWidget | None = None,
                 kind: str = SILVER, tooltip: str = "", accessible: str | None = None):
        super().__init__(text, parent)
        self._icon_name = icon_name
        self._kind = kind
        self._fb = _Feedback(self)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoDefault(False)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        if tooltip:
            self.setToolTip(tooltip)
        if accessible or (not text and tooltip):
            self.setAccessibleName(accessible or tooltip.split("(")[0].strip())

    def set_icon_name(self, name: str | None) -> None:
        self._icon_name = name
        self.updateGeometry()
        self.update()

    def kind(self) -> str:
        return self._kind

    def _body_height(self) -> float:
        return 40.0 if self._kind == SILVER else 34.0

    def sizeHint(self) -> QSize:
        fm = QFontMetricsF(self.font())
        h = self._body_height() + 2 * self.PAD
        text = fm.horizontalAdvance(self.text()) if self.text() else 0.0
        if self._kind == SILVER:
            w = 24 + text + (12 + 30 + 5 if self._icon_name else 24)
        else:
            w = 16 + text + 16 + (16 + 7 if self._icon_name else 0)
        if not self.text():
            w = h
        return QSize(int(w + 2 * self.PAD + 0.5), int(h + 0.5))

    def minimumSizeHint(self) -> QSize:
        hint = self.sizeHint()
        return QSize(min(hint.width(), 120), hint.height())

    def event(self, event) -> bool:
        kind = event.type()
        if kind in (QEvent.Type.HoverEnter, QEvent.Type.Enter):
            self._fb.hover.set_target(1.0)
        elif kind in (QEvent.Type.HoverLeave, QEvent.Type.Leave):
            self._fb.hover.set_target(0.0)
        return super().event(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._fb.press.set_target(1.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._fb.press.set_target(0.0)
        super().mouseReleaseEvent(event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self._fb.hover.jump(0.0)
            self._fb.press.jump(0.0)
        super().changeEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()
        h = self._fb.hover.value if enabled else 0.0
        d = self._fb.press.value if enabled else 0.0
        h = max(h, 1.0 if self.focus_ring() else 0.0)  # Artapa lifts a button that has the keyboard
        full = QRectF(self.rect()).adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD)
        body_h = min(full.height(), self._body_height())
        rect = QRectF(full.x(), full.center().y() - body_h / 2, full.width(), body_h)
        if not enabled:
            p.setOpacity(0.5)
        scale = 1.0 - 0.03 * d
        if scale != 1.0:
            c = rect.center()
            p.translate(c)
            p.scale(scale, scale)
            p.translate(-c)
        radius = rect.height() / 2
        path = glass.shape_path(rect, None)
        dpr = self.devicePixelRatioF() or 1.0
        p.setPen(Qt.PenStyle.NoPen)
        if self._kind == SILVER:
            p.fillPath(path, QColor(255, 255, 255, round(255 * (0.055 + 0.07 * h))))
            hq = round(h * 4) / 4  # the glow is drawn in a few steps of the hover (and kept)
            glow = _inset_glow(rect.width(), rect.height(), radius, -(14 + 7 * hq), 24 + 22 * hq, -16,
                               (255, 255, 255), 0.16, dpr)
            p.drawImage(rect.topLeft(), glow)
            glass.paint_rim(p, rect, None, 0x29)
            glass.paint_ring(p, rect, None, _edge_gradient(rect, 0.58 + 0.42 * h))
            text_color = QColor(theme.TEXT)
        elif self._kind == DANGER:
            tint = QColor(theme.STOP_TINT)
            p.fillPath(path, _alpha(tint, 1.0 + h))
            ring = QColor(theme.STOP_RING)
            glass.paint_ring(p, rect, None, _alpha(ring, 1.0 + 0.6 * h))
            text_color = QColor(theme.STOP)
        else:
            p.fillPath(path, QColor(255, 255, 255, round(255 * (0.05 + 0.04 * h))))
            if h > 0.01:
                hq = round(h * 4) / 4
                glow = _inset_glow(rect.width(), rect.height(), radius, -12, 28, -16, (150, 170, 180), 0.42 * hq, dpr)
                p.drawImage(rect.topLeft(), glow)
            glass.paint_rim(p, rect, None, round(255 * (0.08 + 0.06 * h)))
            glass.paint_ring(p, rect, None, _blend(_qc("BORDER"), _qc("BORDER_HOVER"), h))
            text_color = QColor(theme.TEXT)
        if self.focus_ring():
            paint_focus(p, rect, None)
        text = self.text()
        fm = QFontMetricsF(self.font())
        p.setFont(self.font())
        if self._kind == SILVER:
            if self._icon_name:
                cr = 15.0
                center = QPointF(rect.right() - 5 - cr, rect.center().y())
                if text:
                    room = QRectF(rect.left() + 24, rect.top(), center.x() - cr - 12 - rect.left() - 24, rect.height())
                    shown = fm.elidedText(text, Qt.TextElideMode.ElideRight, room.width())
                    p.setPen(text_color)
                    p.drawText(room, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter, shown)
                else:
                    center = rect.center()
                paint_silver_circle(p, center, cr, self._icon_name, 15.0)
            elif text:
                p.setPen(text_color)
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                           fm.elidedText(text, Qt.TextElideMode.ElideRight, rect.width() - 32))
            return
        icon_size = 16.0
        total = (fm.horizontalAdvance(text) if text else 0.0) + (icon_size + (7 if text else 0) if self._icon_name else 0)
        x = rect.center().x() - min(total, rect.width() - 24) / 2
        if self._icon_name:
            icons.paint(p, QRectF(x, rect.center().y() - icon_size / 2, icon_size, icon_size), self._icon_name,
                        text_color, motion=h)
            x += icon_size + 7
        if text:
            p.setPen(text_color)
            room = QRectF(x, rect.top(), rect.right() - 12 - x, rect.height())
            p.drawText(room, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       fm.elidedText(text, Qt.TextElideMode.ElideRight, room.width()))


# ---------------------------------------------------------------- segmented control


class Segmented(_FocusRing, glass.GlassSurface):
    """A row of mutually exclusive segments on a pill (the main tabs), as the links of Artapa's header
    pill: the chosen one on white at 10 %, which slides to it with a spring and stretches a little
    while it moves."""

    currentChanged = Signal(int)

    PAD = 4.0

    def __init__(self, parent: QWidget | None = None, kind: str = glass.BAR):
        super().__init__(parent, kind=kind, radius=None)
        self._labels: list[str] = []
        self._icons: list[str | None] = []
        self._current = -1
        self._x = Spring(0.0, response=0.34, damping=0.86, parent=self)
        self._w = Spring(0.0, response=0.34, damping=0.86, parent=self)
        self._x.changed.connect(lambda _v: self.update())
        self._w.changed.connect(lambda _v: self.update())
        self._hover = -1
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(t("tabs.name"))

    def add_segment(self, text: str, icon_name: str | None = None) -> int:
        self._labels.append(text)
        self._icons.append(icon_name)
        if self._current < 0:
            self._current = 0
        self.updateGeometry()
        self._place(immediate=True)
        return len(self._labels) - 1

    def set_text(self, index: int, text: str) -> None:
        if self._labels[index] != text:
            self._labels[index] = text
            self.updateGeometry()
            self.adjustSize()
            self._place(immediate=not self._x.moving())
            self.update()

    def text(self, index: int) -> str:
        return self._labels[index]

    def count(self) -> int:
        return len(self._labels)

    def set_current_index(self, index: int) -> None:
        if index == self._current or not 0 <= index < len(self._labels):
            return
        self._current = index
        self._place()
        self.update()
        self.currentChanged.emit(index)

    def _label_font(self) -> QFont:
        font = self.font()
        font.setWeight(QFont.Weight.Medium)
        return font

    def _segment_widths(self) -> list[float]:
        fm = QFontMetricsF(self._label_font())
        widths = []
        for text, icon in zip(self._labels, self._icons, strict=True):
            w = fm.horizontalAdvance(text) + 32 + (24 if icon else 0)
            widths.append(max(w, 72.0))
        return widths

    def _segment_rects(self) -> list[QRectF]:
        widths = self._segment_widths()
        rects, x = [], self.PAD
        h = self.height() - 2 * self.PAD
        for w in widths:
            rects.append(QRectF(x, self.PAD, w, h))
            x += w
        return rects

    def sizeHint(self) -> QSize:
        return QSize(int(sum(self._segment_widths()) + 2 * self.PAD + 0.5), 40)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _place(self, immediate: bool = False) -> None:
        rects = self._segment_rects()
        if not 0 <= self._current < len(rects):
            return
        r = rects[self._current]
        self._x.set_target(r.x(), immediate=immediate)
        self._w.set_target(r.width(), immediate=immediate)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place(immediate=True)

    def _index_at(self, pos: QPointF) -> int:
        for i, r in enumerate(self._segment_rects()):
            if r.contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:
        hover = self._index_at(event.position())
        if hover != self._hover:
            self._hover = hover
            self.update()

    def leaveEvent(self, _event) -> None:
        self._hover = -1
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            i = self._index_at(event.position())
            if i >= 0:
                self.set_current_index(i)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = -1 if event.key() == Qt.Key.Key_Left else 1
            self.set_current_index(max(0, min(len(self._labels) - 1, self._current + step)))
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        if glass.capturing():
            return
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rects = self._segment_rects()
        if 0 <= self._current < len(rects):
            target = rects[self._current]
            stretch = min(10.0, abs(self._x.velocity) * 0.012)
            pill = QRectF(self._x.value - stretch / 2, target.y(), self._w.value + stretch, target.height())
            radius = pill.height() / 2
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.ACCENT_SOFT))
            p.drawRoundedRect(pill, radius, radius)
        for i, (r, text) in enumerate(zip(rects, self._labels, strict=True)):
            selected = i == self._current
            if i == self._hover and not selected:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(theme.HOVER))
                p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
            color = _qc("TEXT") if selected or i == self._hover else _qc("MUTED")
            font = self._label_font()
            p.setFont(font)
            fm = QFontMetricsF(font)
            icon = self._icons[i]
            total = fm.horizontalAdvance(text) + (24 if icon else 0)
            x = r.center().x() - total / 2
            if icon:
                icons.paint(p, QRectF(x, r.center().y() - 9, 18, 18), icon, color)
                x += 24
            p.setPen(color)
            p.drawText(QRectF(x, r.top(), r.right() - x, r.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        if self.focus_ring():
            paint_focus(p, QRectF(self.rect()).adjusted(2, 2, -2, -2), None)


# --------------------------------------------------------------------------- switch


class Switch(_FocusRing, QCheckBox):
    """An on/off switch with its label on the left. It is a QCheckBox, so `isChecked`, `setChecked`
    and `toggled` work as usual. Off, a well in a ring with a grey knob; on, Artapa's switch: the
    premium gradient with a light inner top edge and a knob in the page's color. The knob slides with
    a spring and widens while pressed."""

    TRACK_W, TRACK_H = 40.0, 22.0

    def __init__(self, text: str = "", parent: QWidget | None = None, subtitle: str = ""):
        super().__init__(text, parent)
        self._pos = Spring(0.0, response=0.26, damping=0.82, parent=self)
        self._press = Spring(0.0, response=0.16, parent=self)
        self._pos.changed.connect(lambda _v: self.update())
        self._press.changed.connect(lambda _v: self.update())
        self.toggled.connect(lambda on: self._pos.set_target(1.0 if on else 0.0))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.subtitle = subtitle

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt's name
        super().setChecked(checked)
        if not self.isVisible():
            self._pos.jump(1.0 if checked else 0.0)

    def showEvent(self, event) -> None:
        self._pos.jump(1.0 if self.isChecked() else 0.0)
        super().showEvent(event)

    def sizeHint(self) -> QSize:
        fm = QFontMetricsF(self.font())
        return QSize(int(fm.horizontalAdvance(self.text()) + self.TRACK_W + 16), 30)

    def minimumSizeHint(self) -> QSize:
        return QSize(int(self.TRACK_W + 40), 30)

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press.set_target(1.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._press.set_target(0.0)
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()
        r = QRectF(self.rect())
        track = QRectF(r.right() - self.TRACK_W - 1, r.center().y() - self.TRACK_H / 2, self.TRACK_W, self.TRACK_H)
        t = self._pos.value
        radius = self.TRACK_H / 2
        p.save()
        if not enabled:
            p.setOpacity(0.45)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc("SWITCH_OFF"))
        p.drawRoundedRect(track, radius, radius)
        if t > 0.004:  # the premium gradient fades in as the knob slides over
            p.setOpacity(p.opacity() * min(1.0, t))
            p.setBrush(theme.premium_gradient(track.topLeft(), track.topRight()))
            p.drawRoundedRect(track, radius, radius)
            glass.paint_rim(p, track, None, 0x80)
            p.setOpacity(1.0 if enabled else 0.45)
        if t < 0.996:
            glass.paint_ring(p, track, None, _alpha(_qc("BORDER"), 1.0 - t))
        knob_h = self.TRACK_H - 6
        knob_w = knob_h + 4 * self._press.value
        x = track.left() + 3 + (track.width() - 6 - knob_w) * t
        knob = QRectF(x, track.top() + 3, knob_w, knob_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_blend(QColor("#B4B4B8"), _qc("PAGE"), t))
        p.drawRoundedRect(knob, knob_h / 2, knob_h / 2)
        p.restore()
        if self.focus_ring():
            paint_focus(p, track, None)
        p.setPen(_qc("TEXT" if enabled else "DISABLED"))
        p.setFont(self.font())
        text_rect = QRectF(r.left(), r.top(), track.left() - 10 - r.left(), r.height())
        text = QFontMetricsF(self.font()).elidedText(self.text(), Qt.TextElideMode.ElideRight, text_rect.width())
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


# --------------------------------------------------------------------------- slider


class Slider(_FocusRing, QSlider):
    """A horizontal slider: a thin track, the filled part in the accent (from the middle for a centered
    range like exposure) and Artapa's silver circle for a thumb, which grows a little while it is
    dragged. Subclasses draw marks of their own on the track (a trimmed video's kept parts)."""

    THUMB = 18.0
    DRAGGED = 22.0

    def __init__(self, parent: QWidget | None = None, centered: bool = False):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.centered = centered
        self._drag = Spring(0.0, response=0.2, damping=0.9, parent=self)
        self._drag.changed.connect(lambda _v: self.update())
        self.sliderPressed.connect(lambda: self._drag.set_target(1.0))
        self.sliderReleased.connect(lambda: self._drag.set_target(0.0))
        self.setMinimumHeight(int(self.DRAGGED + 8))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def _span(self) -> tuple[float, float]:
        pad = self.DRAGGED / 2 + 1
        return pad, max(1.0, self.width() - 2 * pad)

    def _x_of(self, value: float) -> float:
        left, span = self._span()
        lo, hi = self.minimum(), self.maximum()
        frac = 0.0 if hi == lo else (value - lo) / (hi - lo)
        return left + span * max(0.0, min(1.0, frac))

    def _value_at(self, x: float) -> int:
        left, span = self._span()
        frac = max(0.0, min(1.0, (x - left) / span))
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(frac * 10000), 10000)

    # The handle is ours: clicks anywhere on the track jump there and start a drag.
    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setSliderDown(True)
        self.setValue(self._value_at(event.position().x()))
        self._drag.set_target(1.0)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self.isSliderDown():
            self.setValue(self._value_at(event.position().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self.isSliderDown():
            self.setSliderDown(False)
            self._drag.set_target(0.0)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def track_colors(self) -> tuple[QColor, QColor]:
        return _qc("TRACK"), _qc("ACCENT") if self.isEnabled() else _qc("CHECK_ON_OFF")

    def paint_track(self, p: QPainter, cy: float) -> None:
        """The track and its filled part (subclasses add their marks)."""
        track, fill = self.track_colors()
        left, span = self._span()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(left, cy - 2, span, 4), 2, 2)
        x = self._x_of(self.value())
        start = self._x_of((self.minimum() + self.maximum()) / 2) if self.centered else left
        a, b = sorted((start, x))
        if b - a > 0.5:
            p.setBrush(fill)
            p.drawRoundedRect(QRectF(a, cy - 2, b - a, 4), 2, 2)
        if self.centered:
            p.setBrush(track if abs(x - start) > 3 else fill)
            p.drawRoundedRect(QRectF(start - 1, cy - 5, 2, 10), 1, 1)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cy = self.height() / 2
        self.paint_track(p, cy)
        x = self._x_of(self.value())
        size = self.THUMB + (self.DRAGGED - self.THUMB) * self._drag.value
        if not self.isEnabled():
            p.setOpacity(0.5)
        paint_silver_circle(p, QPointF(x, cy), size / 2)
        p.setOpacity(1.0)
        if self.focus_ring():
            paint_focus(p, QRectF(x - size / 2, cy - size / 2, size, size), None)


# ---------------------------------------------------------------------------- toast


class Toast(glass.GlassSurface):
    """A short message on Artapa's toast, a pill that floats at the bottom of its parent and fades away."""

    def __init__(self, parent: QWidget):
        super().__init__(parent, kind=glass.TOAST, radius=None)
        self.label = QLabel(self)
        self.label.setObjectName("ToastText")
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        font = self.label.font()
        font.setWeight(QFont.Weight.Medium)
        self.label.setFont(font)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 8, 18, 8)
        lay.addWidget(self.label)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, text: str, msec: int = 6000) -> None:
        self.label.setText(text)
        self.label.setStyleSheet(f"color:{theme.TEXT}")
        self.adjustSize()
        self.place()
        self.show()
        self.raise_()
        self._timer.start(msec if msec > 0 else 6000)

    def current_message(self) -> str:
        return self.label.text() if self.isVisible() else ""

    def clear(self) -> None:
        self._timer.stop()
        self.hide()

    def place(self, bottom: int = 18) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        width = min(self.sizeHint().width(), parent.width() - 40)
        self.resize(width, self.sizeHint().height())
        self.move((parent.width() - width) // 2, parent.height() - self.height() - bottom)


class ToolGroup(glass.GlassSurface):
    """Buttons sharing one pill on a toolbar. In a narrow window the buttons show only their icons."""

    def __init__(self, *buttons: QWidget, parent: QWidget | None = None, kind: str = glass.BAR,
                 margins: tuple[int, int, int, int] = (4, 4, 4, 4)):
        super().__init__(parent, kind=kind)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(*margins)
        lay.setSpacing(0)
        self.buttons = list(buttons)
        for b in buttons:
            lay.addWidget(b)
        self._compact = False

    def set_compact(self, level: int | bool) -> None:
        """0: every button shows its text; 1: only the first one; 2: none (icons only)."""
        level = int(level)
        if level != self._compact:
            self._compact = level
            texts = [b for b in self.buttons if isinstance(b, BarButton) and b.text()]
            for i, b in enumerate(texts):
                b.set_compact(level >= 2 or (level == 1 and i > 0))
            self.layout().invalidate()
            self.adjustSize()


class IconView(QWidget):
    """A drawn icon as a widget (it follows the theme's colors)."""

    def __init__(self, name: str, color: str = "MUTED", size: int = 18, parent: QWidget | None = None):
        super().__init__(parent)
        self.name, self.color = name, color
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_icon(self, name: str, color: str | None = None) -> None:
        self.name = name
        if color is not None:
            self.color = color
        self.update()

    def paintEvent(self, _event) -> None:
        if glass.capturing():
            return
        p = QPainter(self)
        icons.paint(p, QRectF(self.rect()), self.name, self.color)


def separator() -> QWidget:
    """A short vertical hairline between groups on a bar."""
    line = QWidget()
    line.setFixedSize(1, 18)
    line.setStyleSheet(f"background:{theme._rgba(theme.BORDER_HOVER)};")
    line.setObjectName("BarSeparator")
    return line
