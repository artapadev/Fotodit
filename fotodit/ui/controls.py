"""Controls in the Liquid Glass style: buttons for glass bars, glass buttons, a segmented
control with a sliding selection, a switch, a slider whose thumb turns into a lens while it is
dragged, and a toast.

They keep Qt's own APIs (a switch is a QCheckBox, a button a QPushButton, a slider a QSlider),
so keyboard use, screen readers, UI Automation and the code that drives them work as before;
only painting and motion are new. Feedback starts on press (not on release), and every change
of state moves with a spring (see `motion`), so it can be interrupted and reversed midway.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
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

# Colors of controls on the always-dark media bars (the large preview).
MEDIA_TEXT = "#F2F2F2"
MEDIA_DISABLED = "#636363"


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
    """A button on a glass bar: an icon and/or a text, no background of its own until hovered
    (a capsule highlight), pressed (darker) or checked (accent). `media`: light colors for the
    always-dark bars of the large preview."""

    def __init__(self, text: str = "", icon_name: str | None = None, parent: QWidget | None = None,
                 media: bool = False, tooltip: str = "", accessible: str | None = None):
        super().__init__(text, parent)
        self._icon_name = icon_name
        self.media = media
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

    def _colors(self) -> tuple[QColor, QColor, QColor, QColor]:
        """(text, hover fill, press fill, checked fill)"""
        if self.media:
            text = QColor(MEDIA_TEXT if self.isEnabled() else MEDIA_DISABLED)
            return text, QColor(255, 255, 255, 34), QColor(255, 255, 255, 58), QColor(255, 255, 255, 64)
        text = _qc("TEXT" if self.isEnabled() else "DISABLED")
        return text, _qc("HOVER"), _qc("PRESSED"), _qc("ACCENT_SOFT")

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
        text_color, hover, press, checked = self._colors()
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = r.height() / 2
        if self.isChecked():
            fill = checked if not self.media else checked
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(r, radius, radius)
            if not self.media:
                text_color = _qc("ACCENT_TEXT") if self.isEnabled() else text_color
        if self.isEnabled():
            h, d = self._fb.hover.value, self._fb.press.value
            if h > 0.01 or d > 0.01:
                fill = _blend(_alpha(hover, h), press, d)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(fill)
                p.drawRoundedRect(r, radius, radius)
        if self.focus_ring() and self.focusPolicy() != Qt.FocusPolicy.NoFocus:
            p.setPen(QPen(_qc("ACCENT"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), radius - 1, radius - 1)
        text = self.shown_text()
        icon_size = 18.0
        x = r.center().x() - icon_size / 2 if not text else r.left() + 13
        if self._icon_name:
            scale = 1.0 - 0.08 * self._fb.press.value
            s = icon_size * scale
            icons.paint(p, QRectF(x + (icon_size - s) / 2, r.center().y() - s / 2, s, s), self._icon_name,
                        text_color, motion=self._fb.hover.value if self.isEnabled() else 0.0)
            x += icon_size + 6
        if text:
            p.setPen(text_color)
            p.setFont(self.font())
            if not self._icon_name:
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
            else:
                p.drawText(QRectF(x, r.top(), r.right() - x - 12, r.height()),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


class GlassButton(_FocusRing, QPushButton):
    """A button that is a piece of glass itself: a round or capsule control floating over content,
    or (`kind` PROMINENT) the primary action in the ink (black in light, white in dark). On press it
    swells a little (about 3 %, macOS 27's calm response to a mouse) and lights up where it was
    pressed."""

    def __init__(self, text: str = "", icon_name: str | None = None, parent: QWidget | None = None,
                 kind: str = glass.CONTROL, tooltip: str = "", accessible: str | None = None,
                 text_color: str | None = None):
        super().__init__(text, parent)
        self._icon_name = icon_name
        self._glass = glass.GlassRenderer(self, kind, None)
        self._fb = _Feedback(self)
        self._press_at = QPointF()
        self._text_color = text_color
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoDefault(False)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        if tooltip:
            self.setToolTip(tooltip)
        if accessible or (not text and tooltip):
            self.setAccessibleName(accessible or tooltip.split("(")[0].strip())
        theme.changes().changed.connect(self._glass.invalidate)

    PAD = 3.0  # room for the edge's contour and to swell into when pressed
    SWELL = 1.0  # px per side when pressed

    def set_icon_name(self, name: str | None) -> None:
        self._icon_name = name
        self.update()

    def set_kind(self, kind: str) -> None:
        self._glass.kind = kind
        self._glass.invalidate()
        self.update()

    def sizeHint(self) -> QSize:
        fm = QFontMetricsF(self.font())
        h = 40 if self._glass.kind == glass.PROMINENT else 38
        if not self.text():
            return QSize(h, h)
        w = fm.horizontalAdvance(self.text()) + 40 + (24 if self._icon_name else 0)
        return QSize(int(w + 0.5), h)

    def minimumSizeHint(self) -> QSize:
        hint = self.sizeHint()
        return QSize(min(hint.width(), 120), hint.height())

    def event(self, event) -> bool:
        kind = event.type()
        if kind in (QEvent.Type.HoverEnter, QEvent.Type.Enter):
            self._fb.hover.set_target(1.0)
        elif kind in (QEvent.Type.HoverLeave, QEvent.Type.Leave):
            self._fb.hover.set_target(0.0)
        elif kind == QEvent.Type.HoverMove:
            self._press_at = event.position()
            if self._fb.hover.value > 0:
                self.update()
        return super().event(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_at = event.position()
            self._fb.press.set_target(1.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._fb.press.set_target(0.0)
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        if glass.capturing():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        swell = self.SWELL * self._fb.press.value
        base = QRectF(self.rect()).adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD)
        rect = base.adjusted(-swell, -swell, swell, swell)
        prominent = self._glass.kind == glass.PROMINENT
        extra = None
        if not self.isEnabled():
            extra = QColor(theme.PRIMARY_DISABLED) if prominent else QColor(128, 128, 128, 40)
        glass.paint_contour(p, rect, None, self._glass.material(), self.devicePixelRatioF() or 1.0)
        self._glass.paint(p, rect, extra_tint=extra)
        radius = min(rect.width(), rect.height()) / 2
        path = glass.shape_path(rect, None)
        if self.isEnabled():
            light = 0.10 * self._fb.hover.value + 0.16 * self._fb.press.value
            if light > 0.005:
                glow = QRadialGradient(self._press_at, max(rect.width(), rect.height()) * 0.75)
                glow.setColorAt(0.0, QColor(255, 255, 255, int(255 * light)))
                glow.setColorAt(1.0, QColor(255, 255, 255, 0))
                p.fillPath(path, glow)
        if self.focus_ring():
            p.setPen(QPen(QColor(theme.ACCENT if not prominent else theme.ON_ACCENT), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(1.5, 1.5, -1.5, -1.5), radius - 1.5, radius - 1.5)
        if prominent:
            color = QColor(theme.ON_ACCENT) if self.isEnabled() else QColor(theme.PRIMARY_DISABLED_TEXT)
        elif self._text_color:
            color = _qc(self._text_color)
        else:
            color = _qc("TEXT" if self.isEnabled() else "DISABLED")
        text = self.text()
        icon_size = 20.0 if not text else 18.0
        if text:
            fm = QFontMetricsF(self.font())
            total = fm.horizontalAdvance(text) + (icon_size + 7 if self._icon_name else 0)
            x = rect.center().x() - total / 2
        else:
            x = rect.center().x() - icon_size / 2
        if self._icon_name:
            icons.paint(p, QRectF(x, rect.center().y() - icon_size / 2, icon_size, icon_size), self._icon_name,
                        color, motion=self._fb.hover.value if self.isEnabled() else 0.0)
            x += icon_size + 7
        if text:
            p.setPen(color)
            p.setFont(self.font())
            p.drawText(QRectF(x, rect.top(), rect.right() - x, rect.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


# ---------------------------------------------------------------- segmented control


class Segmented(_FocusRing, glass.GlassSurface):
    """A row of mutually exclusive segments on a glass capsule (the main tabs). The selection is a
    lighter pill that slides to the chosen segment with a spring and stretches a little while it
    moves, like a drop of liquid."""

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
        """A segment is a button: its label is bold (the selected one sits on the glass pill)."""
        font = self.font()
        font.setWeight(QFont.Weight.Bold)
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
        media = self.kind() == glass.MEDIA
        dark = media or theme.is_dark()
        # The selection pill: lighter than the bar, with a soft shadow in light mode and no outline
        # (iOS 27); stretched while it moves.
        if 0 <= self._current < len(rects):
            target = rects[self._current]
            stretch = min(10.0, abs(self._x.velocity) * 0.012)
            pill = QRectF(self._x.value - stretch / 2, target.y(), self._w.value + stretch, target.height())
            radius = pill.height() / 2
            p.setPen(Qt.PenStyle.NoPen)
            if not dark:
                p.setBrush(QColor(0, 0, 0, 22))
                p.drawRoundedRect(pill.translated(0, 1.0), radius, radius)
            p.setBrush(QColor(255, 255, 255, 240) if not dark else QColor(255, 255, 255, 40))
            p.drawRoundedRect(pill, radius, radius)
        for i, (r, text) in enumerate(zip(rects, self._labels, strict=True)):
            selected = i == self._current
            if i == self._hover and not selected:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 30) if dark else QColor(0, 0, 0, 14))
                p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
            if media:
                color = QColor(MEDIA_TEXT)
            else:
                color = _qc("TEXT")
                if not selected:
                    color = _qc("MUTED") if i != self._hover else _qc("TEXT")
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
            p.setPen(QPen(_qc("ACCENT"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            rr = QRectF(self.rect()).adjusted(1, 1, -1, -1)
            p.drawRoundedRect(rr, rr.height() / 2, rr.height() / 2)


# --------------------------------------------------------------------------- switch


class Switch(_FocusRing, QCheckBox):
    """An on/off switch with its label on the left (Apple's settings rows). It is a QCheckBox, so
    `isChecked`, `setChecked` and `toggled` work as usual; the knob slides with a spring and
    widens while pressed."""

    TRACK_W, TRACK_H = 38.0, 22.0

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
        off, on = _qc("SWITCH_OFF"), _qc("ACCENT")
        if not enabled:
            on = _qc("CHECK_ON_OFF")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_blend(off, on, t))
        p.drawRoundedRect(track, self.TRACK_H / 2, self.TRACK_H / 2)
        knob_h = self.TRACK_H - 4
        knob_w = knob_h + 6 * self._press.value
        x = track.left() + 2 + (track.width() - 4 - knob_w) * t
        knob = QRectF(x, track.top() + 2, knob_w, knob_h)
        p.setBrush(QColor(0, 0, 0, 34))
        p.drawRoundedRect(knob.adjusted(-0.6, -0.2, 0.6, 1.2), knob_h / 2 + 0.6, knob_h / 2 + 0.6)
        # The knob stands out from the ink once on (white on black; in dark mode dark on white).
        p.setBrush(_blend(QColor("#FFFFFF"), _qc("ON_ACCENT"), t) if enabled else QColor("#F2F2F2"))
        p.drawRoundedRect(knob, knob_h / 2, knob_h / 2)
        if self.focus_ring():
            p.setPen(QPen(_qc("ACCENT"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track.adjusted(-2.5, -2.5, 2.5, 2.5), self.TRACK_H / 2 + 2.5, self.TRACK_H / 2 + 2.5)
        p.setPen(_qc("TEXT" if enabled else "DISABLED"))
        p.setFont(self.font())
        text_rect = QRectF(r.left(), r.top(), track.left() - 10 - r.left(), r.height())
        text = QFontMetricsF(self.font()).elidedText(self.text(), Qt.TextElideMode.ElideRight, text_rect.width())
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


# --------------------------------------------------------------------------- slider


class GlassSlider(_FocusRing, QSlider):
    """A horizontal slider: a thin track, the filled part in the accent color (from the middle for
    a centered range like exposure), and a round white thumb that turns into a magnifying lens of
    glass while it is dragged. `marks`: parts of the track drawn in other colors (a trimmed video's
    kept parts), as (start, end, color) in slider values. `media`: colors for dark bars."""

    THUMB = 20.0
    LENS = 30.0

    def __init__(self, parent: QWidget | None = None, centered: bool = False, media: bool = False):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.centered = centered
        self.media = media
        self._drag = Spring(0.0, response=0.2, damping=0.9, parent=self)
        self._drag.changed.connect(lambda _v: self.update())
        self.sliderPressed.connect(lambda: self._drag.set_target(1.0))
        self.sliderReleased.connect(lambda: self._drag.set_target(0.0))
        self.setMinimumHeight(int(self.LENS + 4))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._lens: glass.GlassRenderer | None = None

    def _span(self) -> tuple[float, float]:
        pad = self.LENS / 2 + 1
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
        if self.media:
            return QColor(255, 255, 255, 60), QColor("#FFFFFF") if self.isEnabled() else QColor(255, 255, 255, 90)
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
        t = self._drag.value
        size = self.THUMB + (self.LENS - self.THUMB) * t
        thumb = QRectF(x - size / 2, cy - size / 2, size, size)
        if t > 0.02 and not glass.reduce_transparency():
            self._paint_lens(p, thumb, cy, t)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 46))
            p.drawEllipse(thumb.translated(0, 1))
            p.setBrush(QColor("#FFFFFF") if self.isEnabled() else QColor("#E6E6E6"))
            p.drawEllipse(thumb)
            p.setPen(QPen(QColor(0, 0, 0, 22), 0.8))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(thumb.adjusted(0.4, 0.4, -0.4, -0.4))
        if self.focus_ring():
            p.setPen(QPen(_qc("ACCENT"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(thumb.adjusted(-3, -3, 3, 3))

    def _paint_lens(self, p: QPainter, thumb: QRectF, cy: float, t: float) -> None:
        """The thumb as a lens: the track under it, magnified, inside a rim of glass."""
        dpr = self.devicePixelRatioF() or 1.0
        img = QImage(round(self.width() * dpr), round(self.height() * dpr), glass.FMT)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.GlobalColor.transparent)
        tp = QPainter(img)
        tp.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paint_track(tp, cy)
        tp.end()
        path = QPainterPath()
        path.addEllipse(thumb)
        p.save()
        p.setClipPath(path)
        zoom = 1.0 + 0.6 * t
        c = thumb.center()
        p.translate(c)
        p.scale(zoom, zoom)
        p.translate(-c)
        p.drawImage(QPointF(0, 0), img)
        p.restore()
        tint = QColor(255, 255, 255, int(70 * t)) if not self.media else QColor(255, 255, 255, int(40 * t))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tint)
        p.drawEllipse(thumb)
        mat = glass.material(glass.CONTROL, dark=self.media or theme.is_dark())
        glass.paint_contour(p, thumb, None, mat, dpr)
        e = glass.edges(max(1, round(thumb.width() * dpr)), max(1, round(thumb.height() * dpr)),
                        thumb.width() / 2, mat, dpr)
        light = QImage(e.highlight)
        light.setDevicePixelRatio(dpr)
        p.save()
        p.setOpacity(t)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        p.drawImage(thumb.topLeft() - QPointF(e.pad / dpr, e.pad / dpr), light)
        p.restore()


# ---------------------------------------------------------------------------- toast


class Toast(glass.GlassSurface):
    """A short message in a glass capsule that floats at the bottom of its parent and fades away."""

    def __init__(self, parent: QWidget):
        super().__init__(parent, kind=glass.BAR, radius=None)
        self.label = QLabel(self)
        self.label.setObjectName("ToastText")
        self.label.setTextFormat(Qt.TextFormat.PlainText)
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
    """Buttons sharing one glass capsule on a toolbar (Apple groups neighbouring buttons of the same
    kind). In a narrow window the buttons show only their icons."""

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


def separator(media: bool = False) -> QWidget:
    """A short vertical hairline between groups on a bar."""
    line = QWidget()
    line.setFixedSize(1, 18)
    color = "rgba(255,255,255,0.18)" if media else theme._rgba(theme.SEPARATOR)
    line.setStyleSheet(f"background:{color};")
    line.setObjectName("BarSeparator")
    return line
