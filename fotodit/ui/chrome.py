"""The main window's chrome in the Liquid Glass layout.

The window itself is the content layer. Three things float above it: the sidebar (folders and
file info) and the inspector (the operations and the start button) as large glass panels inset
from the window's edges, and, over the content area, the toolbars as glass capsules: the
"Gözat / Seçilenler" segmented control, the page's own controls next to it and a bar at the
bottom. The content scrolls beneath the capsules (the Browse grid starts below them and passes
under them when scrolled), which is where the glass visibly bends what lies beneath it.

A page shows its chrome by offering `header_widgets()` (capsules placed to the right of the
tabs; one may carry the property "stretch" to take the remaining width), `footer_widget()`
(a capsule centered at the bottom) and `set_insets(top, bottom)` (the room the capsules take,
so the content can start below them).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from . import glass, theme
from .controls import Segmented, Toast
from .motion import Spring

EDGE = 10  # distance of the floating glass panels from the window's edges
GAP = 10  # between capsules
HEADER_Y = 12
BAR = 40


class Tabs(QObject):
    """Switches the pages of a stack with a segmented control, keeping the QTabWidget calls the
    window uses (addTab, setTabText, tabText, currentWidget, setCurrentWidget …)."""

    currentChanged = Signal(int)

    def __init__(self, control: Segmented, stack: QStackedWidget):
        super().__init__(control)
        self.control = control
        self.stack = stack
        control.currentChanged.connect(self._chosen)

    def addTab(self, widget: QWidget, text: str) -> int:  # noqa: N802 - QTabWidget's name
        self.stack.addWidget(widget)
        return self.control.add_segment(text)

    def setTabText(self, index: int, text: str) -> None:  # noqa: N802
        self.control.set_text(index, text)

    def tabText(self, index: int) -> str:  # noqa: N802
        return self.control.text(index)

    def count(self) -> int:
        return self.stack.count()

    def widget(self, index: int) -> QWidget:
        return self.stack.widget(index)

    def indexOf(self, widget: QWidget) -> int:  # noqa: N802
        return self.stack.indexOf(widget)

    def currentIndex(self) -> int:  # noqa: N802
        return self.stack.currentIndex()

    def currentWidget(self) -> QWidget:  # noqa: N802
        return self.stack.currentWidget()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        self.control.set_current_index(index)

    def setCurrentWidget(self, widget: QWidget) -> None:  # noqa: N802
        self.setCurrentIndex(self.stack.indexOf(widget))

    def _chosen(self, index: int) -> None:
        if self.stack.currentIndex() != index:
            self.stack.setCurrentIndex(index)
            self.currentChanged.emit(index)


class ToolbarBacking(QWidget):
    """iOS 27's uniform toolbar: once the content scrolls beneath the floating bars, a frosted strip
    fades in behind them (white at 85 % in light, black at 55 % in dark, with a hairline below), so
    text never runs under text. The bars above it see it as their backdrop."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._glass = glass.GlassRenderer(self, glass.TOOLBAR, radius=0)
        self._fade = Spring(0.0, response=0.25, parent=self)
        self._fade.changed.connect(lambda _v: self.update())
        self._image: QImage | None = None
        theme.changes().changed.connect(self._glass.invalidate)

    def set_shown(self, shown: bool) -> None:
        self._fade.set_target(1.0 if shown else 0.0)

    def paintEvent(self, _event) -> None:
        t = self._fade.value
        if t <= 0.01:
            return
        p = QPainter(self)
        p.setOpacity(t)
        if glass.capturing():  # the bars above are taking their backdrop: the strip as last drawn
            if self._image is not None:
                p.drawImage(QPointF(0, 0), self._image)
            return
        dpr = self.devicePixelRatioF() or 1.0
        image = QImage(max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)), glass.FMT)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.transparent)
        ip = QPainter(image)
        self._glass.paint(ip, QRectF(self.rect()))
        ip.fillRect(QRectF(0, self.height() - 1, self.width(), 1),
                    QColor(0, 0, 0, 26 if theme.is_dark() else 13))
        ip.end()
        self._image = image
        p.drawImage(QPointF(0, 0), image)


class ContentArea(QWidget):
    """The pages with their floating glass toolbars and a toast for short messages."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.stack = QStackedWidget(self)
        self._content = glass.SharedBackdrop(self.stack)  # the bars above take their backdrop from one rendering
        self.backing = ToolbarBacking(self)
        self.segmented = Segmented(self)
        self.tabs = Tabs(self.segmented, self.stack)
        self.tabs.currentChanged.connect(self._page_changed)
        self.activity: QWidget | None = None  # a capsule shown above the footer while something runs
        self.toast = Toast(self)
        self._pages: list[QWidget] = []

    def add_page(self, page: QWidget, title: str) -> int:
        index = self.tabs.addTab(page, title)
        self._pages.append(page)
        bar = page.scroll_bar() if hasattr(page, "scroll_bar") else None
        if bar is not None:
            bar.valueChanged.connect(lambda _v: self._scrolled())
        for w in self._chrome(page):
            w.setParent(self)
        self._page_changed(self.tabs.currentIndex())
        return index

    def set_activity(self, widget: QWidget) -> None:
        widget.setParent(self)
        self.activity = widget
        widget.hide()

    @staticmethod
    def _chrome(page: QWidget) -> list[QWidget]:
        widgets = list(page.header_widgets())
        footer = page.footer_widget()
        if footer is not None:
            widgets.append(footer)
        return widgets

    def _page_changed(self, index: int) -> None:
        current = self.stack.widget(index)
        for page in self._pages:
            for w in self._chrome(page):
                w.setVisible(page is current and not w.property("hidden_by_page"))
        self.relayout()
        self._scrolled()

    def _scrolled(self) -> None:
        page = self.stack.currentWidget()
        bar = page.scroll_bar() if page is not None and hasattr(page, "scroll_bar") else None
        self.backing.set_shown(bar is not None and bar.value() > bar.minimum())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.relayout()

    def relayout(self) -> None:
        w, h = self.width(), self.height()
        self.stack.setGeometry(0, 0, w, h)
        page = self.stack.currentWidget()
        if page is None:
            return
        self.backing.setGeometry(0, 0, w, HEADER_Y + BAR + GAP // 2 + 4)
        self.backing.raise_()
        x = EDGE + 4
        self.segmented.adjustSize()
        self.segmented.setGeometry(x, HEADER_Y, self.segmented.sizeHint().width(), BAR)
        x += self.segmented.width() + GAP
        header = [wd for wd in page.header_widgets() if not wd.property("hidden_by_page")]
        right = w - EDGE - 4
        stretch = [wd for wd in header if wd.property("stretch")]
        least = 200 * len(stretch)  # the narrowest a stretching field (the path) may get

        def free() -> int:
            fixed = sum(wd.sizeHint().width() for wd in header if not wd.property("stretch"))
            return right - x - fixed - GAP * max(0, len(header) - 1)

        # Text buttons show only their icons when the bar does not fit otherwise (the first button of a
        # group keeps its text as long as possible).
        for level in (0, 1, 2):
            for wd in header:
                if hasattr(wd, "set_compact"):
                    wd.set_compact(level)
            if free() >= least:
                break
        # Still too wide: the last groups step aside (their commands stay in the context menus).
        fixed = [wd for wd in header if not wd.property("stretch")]
        while len(fixed) > 1 and free() < least:
            header.remove(fixed.pop())
        # A field (the path) too short to read steps aside as well; the tree shows where one is.
        if stretch and len(header) > len(stretch) and free() < 120 * len(stretch):
            for wd in stretch:
                header.remove(wd)
            stretch = []
        for wd in page.header_widgets():
            if not wd.property("hidden_by_page"):
                wd.setVisible(wd in header)
        room = free()
        for wd in header:
            if wd.property("stretch"):
                width = max(120, room // max(1, len(stretch)))
            else:
                width = wd.sizeHint().width()
            width = max(40, min(width, right - x))
            wd.setGeometry(x, HEADER_Y, width, BAR)
            wd.raise_()
            x += width + GAP
        top = HEADER_Y + BAR + GAP
        bottom = 0
        footer = page.footer_widget()
        if footer is not None and footer.isVisible():
            room = w - 2 * (EDGE + 4)
            for level in (0, 1, 2):
                if hasattr(footer, "set_compact"):
                    footer.set_compact(level)
                if footer.sizeHint().width() <= room:
                    break
            fw = min(footer.sizeHint().width(), room)
            footer.setGeometry((w - fw) // 2, h - BAR - 16, fw, BAR)
            footer.raise_()
            bottom = BAR + 16 + GAP
        if self.activity is not None and self.activity.isVisible():
            aw = min(self.activity.sizeHint().width(), w - 2 * (EDGE + 4))
            ay = h - bottom - BAR - 6 if bottom else h - BAR - 16
            self.activity.setGeometry((w - aw) // 2, ay, aw, BAR)
            self.activity.raise_()
        self.segmented.raise_()
        page.set_insets(top, bottom)
        if self.toast.isVisible():
            self.toast.place(bottom + 6 if bottom else 18)
            self.toast.raise_()

    def show_message(self, text: str, msec: int = 6000) -> None:
        footer = self.stack.currentWidget().footer_widget() if self.stack.currentWidget() else None
        lift = BAR + 16 + 6 if footer is not None and footer.isVisible() else 18
        if self.activity is not None and self.activity.isVisible():
            lift += BAR + 6
        self.toast.show_message(text, msec)
        self.toast.place(lift)
        self.toast.raise_()


class PanelCell(QWidget):
    """A splitter cell holding a glass panel inset from the window's edges (room for its shadow)."""

    def __init__(self, panel: glass.GlassSurface, left: int, right: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.panel = panel
        lay = QVBoxLayout(self)
        lay.setContentsMargins(left, EDGE, right, EDGE + 2)
        lay.addWidget(panel)

    def minimumSizeHint(self) -> QSize:
        hint = self.panel.minimumSizeHint()
        m = self.layout().contentsMargins()
        return QSize(hint.width() + m.left() + m.right(), hint.height() + m.top() + m.bottom())


def panel(kind: str = glass.PANEL) -> glass.GlassSurface:
    """A large glass panel (sidebar, inspector)."""
    surface = glass.GlassSurface(kind=kind, radius=theme.RADIUS_PANEL)
    surface.setObjectName("Glass")
    return surface


__all__ = ["BAR", "EDGE", "GAP", "ContentArea", "PanelCell", "QRect", "Tabs", "panel"]
