"""Small helper components: badges on thumbnails, an elided label, pop-up button, number box and table
header, menus, an icon on a tile, the empty-page message."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QHeaderView,
    QLabel,
    QMenu,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionComboBox,
    QStyleOptionHeaderV2,
    QStyleOptionSpinBox,
    QStylePainter,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..i18n import t
from . import glass, icons, theme


def _badge(painter: QPainter, rect: QRectF, fill: QColor, icon: str, text: str, font: QFont,
           ink: str = "#FFFFFF", border: QBrush | QColor | None = None) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
    if border is not None:
        glass.paint_ring(painter, rect, None, border)
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
    # Artapa's dark glass round button, made a pill: dark, with a faint ring.
    _badge(painter, rect, QColor(12, 12, 12, 170), "play.fill", text, font, theme.TEXT, QColor(255, 255, 255, 0x2B))
    painter.restore()


def paint_edit_badge(painter: QPainter, corner: QPointF, compact: bool = False) -> None:
    """The badge in the bottom-right corner of an edited photo's or video's thumbnail: Artapa's badge
    ("Yeni", "Yakında"), surface-1 in a ring of the premium gradient."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = _badge_font(painter, compact)
    text = "" if compact else t("badge.edited")
    fm = QFontMetricsF(font)
    h = fm.height() + (3 if compact else 5)
    w = h * 0.32 + h * 0.62 + (h * 0.16 + fm.horizontalAdvance(text) + h * 0.38 if text else h * 0.32)
    rect = QRectF(corner.x() - w - 5, corner.y() - h - 5, w, h)
    ring = QBrush(theme.premium_gradient(rect.topLeft(), rect.topRight()))
    _badge(painter, rect, QColor(theme.PANEL), "pencil", text, font, theme.TEXT, ring)
    painter.restore()


def paint_check_badge(painter: QPainter, rect: QRectF, state: str, hover: bool = False) -> None:
    """The selection circle on tiles: "none" (an empty ring), "full" (a check), "partial" (a dash),
    "busy" (three dots, while a folder is scanned). Chosen, it is Artapa's silver circle in a dark ring,
    which stands out on light and dark photos alike."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

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
    on_ink = QColor(theme.ON_ACCENT)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 110))
    painter.drawEllipse(rect)
    silver = rect.adjusted(1.4, 1.4, -1.4, -1.4)
    painter.setBrush(theme.silver_gradient(silver.top(), silver.bottom()))
    painter.drawEllipse(silver)
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


BAR_RIGHT = 6.5  # the open row's bar ends this far from the row's left edge (it starts 4.5 in)


def paint_row_highlight(painter: QPainter, rect: QRectF, selected: bool, hover: bool, bar: bool = True) -> None:
    """A sidebar row, as the links of Artapa's admin sidebar: under the pointer white at 6 %; the open
    one white at 5 % over the strong aurora, with a line of light inside its top and, at its left, a bar
    2 px wide of the premium gradient (`bar` False: none, where a tree's arrow would meet it)."""
    if not (selected or hover):
        return
    radius = float(theme.RADIUS_WELL)
    path = glass.shape_path(rect, radius)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    if selected:
        painter.fillPath(path, QColor(255, 255, 255, 0x0D))
        glass.paint_aurora(painter, rect, path, 1.55)
        glass.paint_rim(painter, rect, radius, 0x0F)
        if bar:
            inset = min(8.0, rect.height() * 0.25)
            line = QRectF(rect.left() + BAR_RIGHT - 2.0, rect.top() + inset, 2.0, rect.height() - 2 * inset)
            gradient = QLinearGradient(QPointF(0, line.top()), QPointF(0, line.bottom()))
            for at, token in ((0.0, theme.PREMIUM_START), (0.5, theme.AURORA_WARM), (1.0, theme.AURORA_COOL)):
                gradient.setColorAt(at, QColor(token))
            painter.setBrush(gradient)
            painter.drawRoundedRect(line, 1.0, 1.0)
    else:
        painter.fillPath(path, QColor(theme.HOVER))
    painter.restore()


class SidebarDelegate(QStyledItemDelegate):
    """The rows of a sidebar list: their names in the secondary text color, in the primary one when
    open or under the pointer, as the links of Artapa's admin sidebar. `highlight`: the delegate draws
    the row's highlight too (a tree draws its own across the indent)."""

    def __init__(self, parent: QWidget | None = None, highlight: bool = False):
        super().__init__(parent)
        self.highlight = highlight

    def initStyleOption(self, option, index) -> None:  # noqa: N802 - Qt override
        super().initStyleOption(option, index)
        on = bool(option.state & (QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver))
        color = QColor(theme.TEXT if on else theme.MUTED)
        for role in (QPalette.ColorRole.Text, QPalette.ColorRole.HighlightedText):
            option.palette.setColor(role, color)

    def paint(self, painter: QPainter, option, index) -> None:
        if self.highlight:
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
            paint_row_highlight(painter, QRectF(option.rect).adjusted(1, 1, -1, -1), selected, hover)
        super().paint(painter, option, index)


def menu(parent: QWidget) -> QMenu:
    """A context menu in Artapa's look: the panel of its dropdowns, rounded, in a faint ring. The
    window is see-through outside the rounded panel, so Windows' square shadow is left off."""
    m = QMenu(parent)
    m.setObjectName("AppMenu")
    m.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    m.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    m.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
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


class IconTile(QWidget):
    """An icon on Artapa's tile (the mark of its sign-in card): white from 9 % at the top to 2.4 % at
    the foot over the strong aurora, a faint ring and a line of light."""

    def __init__(self, icon: str, color: str = "TEXT", size: int = 44, parent: QWidget | None = None):
        super().__init__(parent)
        self.name, self.color = icon, color
        self.setFixedSize(QSize(size, size))
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
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.width() * 0.29
        path = glass.shape_path(rect, radius)
        fill = QLinearGradient(QPointF(0, rect.top()), QPointF(0, rect.bottom()))
        fill.setColorAt(0.0, QColor(255, 255, 255, 0x17))
        fill.setColorAt(1.0, QColor(255, 255, 255, 0x06))
        p.fillPath(path, fill)
        glass.paint_aurora(p, rect, path, 1.55)
        glass.paint_ring(p, rect, radius, QColor(255, 255, 255, 0x0D))
        glass.paint_rim(p, rect, radius, 0x0F)
        s = rect.width() * 0.5
        icons.paint(p, QRectF(rect.center().x() - s / 2, rect.center().y() - s / 2, s, s), self.name, self.color)


class EmptyState(QWidget):
    """What an empty page says: an icon on a tile, a title and a short explanation, centered."""

    def __init__(self, icon: str = "photo", title: str = "", text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.icon = IconTile(icon, "MUTED", 72)
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
