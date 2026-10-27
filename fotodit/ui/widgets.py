"""Small helper components: badges on thumbnails, an elided label, pop-up button, number box and table
header, menus, the empty-page message."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPalette
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QHeaderView,
    QLabel,
    QMenu,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
    QStyleOptionHeaderV2,
    QStyleOptionSpinBox,
    QStylePainter,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..i18n import t
from . import icons, theme


def _badge(painter: QPainter, rect: QRectF, fill: QColor, icon: str, text: str, font: QFont,
           ink: str = "#FFFFFF") -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
    h = rect.height()
    s = h * 0.62
    x = rect.x() + h * 0.32
    icons.paint(painter, QRectF(x, rect.center().y() - s / 2, s, s), icon, ink)
    if text:
        painter.setPen(QColor(ink))
        painter.setFont(font)
        painter.drawText(QRectF(x + s + h * 0.16, rect.y(), rect.width(), h),
                         int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)


def _badge_font(painter: QPainter, compact: bool) -> QFont:
    font = QFont(painter.font())
    font.setPointSizeF(max(7.0, font.pointSizeF() - (2.0 if compact else 1.5)))
    font.setWeight(QFont.Weight.DemiBold)
    return font


def paint_video_badge(painter: QPainter, corner: QPointF, duration: float | None, compact: bool = False) -> None:
    """A "▶ 0:42" capsule at the bottom left of a video thumbnail (`corner`: its bottom-left corner)."""
    from ..core.video.probe import duration_text

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = _badge_font(painter, compact)
    text = duration_text(duration) if duration else ""
    fm = QFontMetricsF(font)
    h = fm.height() + (3 if compact else 5)
    w = h * 0.32 + h * 0.62 + (h * 0.16 + fm.horizontalAdvance(text) + h * 0.38 if text else h * 0.32)
    rect = QRectF(corner.x() + 5, corner.y() - h - 5, w, h)
    _badge(painter, rect, QColor(0, 0, 0, 150), "play.fill", text, font)
    painter.restore()


def paint_edit_badge(painter: QPainter, corner: QPointF, compact: bool = False) -> None:
    """The badge in the bottom-right corner of an edited photo's or video's thumbnail."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = _badge_font(painter, compact)
    text = "" if compact else t("badge.edited")
    fm = QFontMetricsF(font)
    h = fm.height() + (3 if compact else 5)
    w = h * 0.32 + h * 0.62 + (h * 0.16 + fm.horizontalAdvance(text) + h * 0.38 if text else h * 0.32)
    rect = QRectF(corner.x() - w - 5, corner.y() - h - 5, w, h)
    _badge(painter, rect, QColor(255, 255, 255, 235), "pencil", text, font, "#161616")  # light, apart from ▶
    painter.restore()


def paint_check_badge(painter: QPainter, rect: QRectF, state: str, hover: bool = False) -> None:
    """The selection circle on tiles: "none" (an empty ring), "full" (a check), "partial" (a dash),
    "busy" (three dots, while a folder is scanned)."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    from . import theme

    if state == "none":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 90 if hover else 56))
        painter.drawEllipse(rect)
        pen = painter.pen()
        pen.setStyle(Qt.PenStyle.SolidLine)
        pen.setColor(QColor(255, 255, 255, 255 if hover else 235))
        pen.setWidthF(1.6)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect.adjusted(0.8, 0.8, -0.8, -0.8))
        painter.restore()
        return
    # The ink with a ring of the opposite tone: it stands out on light and dark photos alike.
    ink, on_ink = QColor(theme.ACCENT), QColor(theme.ON_ACCENT)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(on_ink)
    painter.drawEllipse(rect)
    painter.setBrush(ink)
    painter.drawEllipse(rect.adjusted(1.4, 1.4, -1.4, -1.4))
    inner = rect.adjusted(rect.width() * 0.2, rect.height() * 0.2, -rect.width() * 0.2, -rect.height() * 0.2)
    if state == "full":
        icons.paint(painter, inner, "checkmark", on_ink)
    elif state == "partial":
        pen = painter.pen()
        pen.setStyle(Qt.PenStyle.SolidLine)
        pen.setColor(on_ink)
        pen.setWidthF(2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        y = rect.center().y()
        painter.drawLine(QPointF(rect.left() + rect.width() * 0.3, y), QPointF(rect.right() - rect.width() * 0.3, y))
    else:
        painter.setBrush(on_ink)
        for dx in (-0.22, 0.0, 0.22):
            painter.drawEllipse(QPointF(rect.center().x() + rect.width() * dx, rect.center().y()), 1.5, 1.5)
    painter.restore()


def rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def menu(parent: QWidget) -> QMenu:
    """A context menu in the app's style (rounded, with the theme's colors)."""
    m = QMenu(parent)
    m.setObjectName("AppMenu")
    return m


class ElidedLabel(QLabel):
    """Single-line label that elides text that does not fit, using "…".

    A plain QLabel's minimum width is its full text: a long file name or info line can
    push the window off the screen. This label shrinks when needed; when the text fits,
    it is painted normally (and can be selected with the mouse)."""

    def __init__(self, text: str = "", mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight, parent=None):
        super().__init__(text, parent)
        self._mode = mode

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), 24), hint.height())

    def event(self, event) -> bool:
        # A shortened text shows in full when the mouse rests on it (unless the label has its own tip).
        if event.type() == QEvent.Type.ToolTip and not self.toolTip():
            text = self.text()
            if text and self.fontMetrics().horizontalAdvance(text) > self.contentsRect().width():
                QToolTip.showText(event.globalPos(), text, self)
                return True
        return super().event(event)

    def paintEvent(self, event) -> None:
        rect = self.contentsRect()
        text = self.text()
        if not text or self.fontMetrics().horizontalAdvance(text) <= rect.width():
            super().paintEvent(event)
            return
        elided = self.fontMetrics().elidedText(text, self._mode, rect.width())
        painter = QPainter(self)
        self.style().drawItemText(painter, rect, int(self.alignment() | Qt.AlignmentFlag.AlignVCenter),
                                  self.palette(), self.isEnabled(), elided, self.foregroundRole())


def _arrow_color(widget: QWidget, enabled: bool) -> QColor:
    """The arrows of the boxes below: their text color, softened. The style sheet colors that text for
    each surface (the preview's dark glass too)."""
    group = QPalette.ColorGroup.Active if enabled else QPalette.ColorGroup.Disabled
    color = widget.palette().color(group, QPalette.ColorRole.Text)
    if enabled:
        color.setAlphaF(color.alphaF() * 0.6)
    return color


class ElidedComboBox(QComboBox):
    """Pop-up button whose choice ends in "…" when it does not fit, with its arrow.

    Under the style sheet Qt cuts a long choice off mid-letter, and the sheet's drop-down rule hides
    the style's own arrow (the box looked like a text field). The list opens as wide as its longest
    choice, so a narrow box still lists every choice in full."""

    def shown_text(self) -> str:
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        return self._elided(opt)

    def _elided(self, opt: QStyleOptionComboBox) -> str:
        field = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                            QStyle.SubControl.SC_ComboBoxEditField, self)
        return self.fontMetrics().elidedText(opt.currentText, Qt.TextElideMode.ElideRight, field.width())

    def event(self, event) -> bool:
        # A shortened choice shows in full when the mouse rests on it (unless the box has its own tip).
        if event.type() == QEvent.Type.ToolTip and not self.toolTip() and self.shown_text() != self.currentText():
            QToolTip.showText(event.globalPos(), self.currentText(), self)
            return True
        return super().event(event)

    def paintEvent(self, event) -> None:
        painter = QStylePainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.Text))
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        opt.currentText = self._elided(opt)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)
        arrow = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                            QStyle.SubControl.SC_ComboBoxArrow, self)
        theme.paint_chevron(painter, arrow, _arrow_color(self, self.isEnabled()))

    def showPopup(self) -> None:
        view = self.view()
        view.setMinimumWidth(view.sizeHintForColumn(0) + 2 * view.frameWidth() + 16)
        super().showPopup()


class NumberBox(QSpinBox):
    """Number field with its step arrows drawn (the style sheet's button rules hide the style's own:
    they could be clicked without being seen). It takes the mouse wheel only while it has the focus,
    so scrolling a panel over it scrolls the panel instead of changing the number."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # the wheel does not give it the focus

    def wheelEvent(self, event) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()  # on to the panel

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        opt = QStyleOptionSpinBox()
        self.initStyleOption(opt)
        painter = QPainter(self)
        steps = self.stepEnabled()
        flags = QAbstractSpinBox.StepEnabledFlag
        for part, flag, up in ((QStyle.SubControl.SC_SpinBoxUp, flags.StepUpEnabled, True),
                               (QStyle.SubControl.SC_SpinBoxDown, flags.StepDownEnabled, False)):
            rect = self.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, opt, part, self)
            theme.paint_chevron(painter, rect, _arrow_color(self, self.isEnabled() and bool(steps & flag)), up)


class ElidedHeader(QHeaderView):
    """Table header whose titles end in "…" when a column is narrower than its title.

    Under the style sheet Qt cuts them off instead (it loses the header's elide mode). The titles are
    measured with the header's own font, which the style sheet sets to theirs (see `theme`)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(Qt.Orientation.Horizontal, parent)

    def shown_title(self, logical: int) -> str:
        return self._option(logical, QRect(0, 0, self.sectionSize(logical), self.height())).text

    def _option(self, logical: int, rect: QRect) -> QStyleOptionHeaderV2:
        opt = QStyleOptionHeaderV2()
        self.initStyleOption(opt)
        self.initStyleOptionForIndex(opt, logical)
        opt.rect = rect
        label = self.style().subElementRect(QStyle.SubElement.SE_HeaderLabel, opt, self)
        opt.text = self.fontMetrics().elidedText(opt.text, Qt.TextElideMode.ElideRight, label.width())
        return opt

    def paintSection(self, painter, rect, logical) -> None:
        if rect.isValid():
            self.style().drawControl(QStyle.ControlElement.CE_Header, self._option(logical, rect), painter, self)

    def viewportEvent(self, event) -> bool:
        # A shortened title shows in full when the mouse rests on it.
        if event.type() == QEvent.Type.ToolTip and self.model() is not None:
            logical = self.logicalIndexAt(event.pos())
            full = str(self.model().headerData(logical, self.orientation()) or "") if logical >= 0 else ""
            if full and self.shown_title(logical) != full:
                QToolTip.showText(event.globalPos(), full, self)
                return True
        return super().viewportEvent(event)


class EmptyState(QWidget):
    """What an empty page says: a large icon, a title and a short explanation, centered."""

    def __init__(self, icon: str = "photo", title: str = "", text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        from .controls import IconView

        self.icon = IconView(icon, "MUTED", 46)
        self.title = QLabel(title)
        self.title.setObjectName("EmptyTitle")
        self.text = QLabel(text)
        self.text.setObjectName("Empty")
        for label in (self.title, self.text):
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(0)
        layout.addStretch(1)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(14)
        layout.addWidget(self.title, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(6)
        layout.addWidget(self.text, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        self.text.setVisible(bool(text))

    def set_content(self, icon: str, title: str, text: str = "") -> None:
        self.icon.set_icon(icon)
        self.title.setText(title)
        self.text.setText(text)
        self.text.setVisible(bool(text))
        self._fit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        # Wrapped text is measured at the width it really gets: a centered label would otherwise be
        # given the height of the full page width and lose its last lines.
        width = max(120, min(560, self.width() - 48))
        for label in (self.title, self.text):
            label.setFixedWidth(width)
            label.setFixedHeight(label.heightForWidth(width) if label.text() else 0)
