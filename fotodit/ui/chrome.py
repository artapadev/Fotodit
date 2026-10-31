"""The main window's chrome in Artapa's layout (its admin panel).

The window itself is the calm stage (`Stage`). On it lie the sidebar (folders and file info) and the
inspector (the operations and the start button) as cards inset from the window's edges, and, over the
content area, the toolbars as the site's header pills: the "Gözat / Seçilenler" tabs, the page's own
controls next to them and a bar at the bottom. The content scrolls beneath the pills (the Browse grid
starts below them and passes under them when scrolled), which is where they frost what lies beneath.

A page shows its chrome by offering `header_widgets()` (pills placed to the right of the tabs; one
may carry the property "stretch" to take the remaining width), `footer_widget()` (a pill centered at
the bottom) and `set_insets(top, bottom)` (the room the pills take, so the content can start below
them).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from . import glass, theme
from .controls import Segmented, Toast
from .motion import Spring

EDGE = 10  # distance of the cards from the window's edges
GAP = 10  # between pills
HEADER_Y = 12
BAR = 40


class Stage(QWidget):
    """The window's background: Artapa's calm stage (see `glass.paint_stage`). Everything on it is
    transparent, so the stage shows between the cards and behind the content."""

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setClipRegion(event.region())
        glass.paint_stage(p, QRectF(self.rect()), self.devicePixelRatioF() or 1.0)


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
    """Artapa's header bar while the page is scrolled: once the content runs beneath the floating
    pills, a strip fades in behind them, the content blurred and darkened toward the page's color at
    the top (#060606 at 34 %), both fading out toward its foot, so text never runs under text. The pills
    above it see it as their backdrop."""

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
        if glass.capturing():  # the pills above are taking their backdrop: the strip as last drawn
            if self._image is not None:
                p.drawImage(QPointF(0, 0), self._image)
            return
        dpr = self.devicePixelRatioF() or 1.0
        image = QImage(max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)), glass.FMT)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.transparent)
        ip = QPainter(image)
        rect = QRectF(self.rect())
        self._glass.paint(ip, rect)
        tint = QLinearGradient(QPointF(0, 0), QPointF(0, rect.height()))
        for at, alpha in ((0.0, 0.34), (0.55, 0.16), (1.0, 0.0)):
            tint.setColorAt(at, QColor(6, 6, 6, round(255 * alpha)))
        ip.fillRect(rect, tint)
        fade = QLinearGradient(QPointF(0, 0), QPointF(0, rect.height()))  # the blur gives way toward the foot
        fade.setColorAt(0.0, QColor(0, 0, 0, 255))
        fade.setColorAt(0.6, QColor(0, 0, 0, 200))
        fade.setColorAt(1.0, QColor(0, 0, 0, 0))
        ip.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        ip.fillRect(rect, fade)
        ip.end()
        self._image = image
        p.drawImage(QPointF(0, 0), image)


class ContentArea(QWidget):
    """The pages with their floating toolbars and a toast for short messages."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.stack = QStackedWidget(self)
        self._content = glass.SharedBackdrop(self.stack)  # the pills above take their backdrop from one rendering
        self.backing = ToolbarBacking(self)
        self.segmented = Segmented(self)
        self.tabs = Tabs(self.segmented, self.stack)
        self.tabs.currentChanged.connect(self._page_changed)
        self.activity: QWidget | None = None  # a pill shown above the footer while something runs
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
        self.backing.setGeometry(0, 0, w, HEADER_Y + BAR + GAP + 12)
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
    """A splitter cell holding a card inset from the window's edges."""

    def __init__(self, panel: QWidget, left: int, right: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.panel = panel
        lay = QVBoxLayout(self)
        lay.setContentsMargins(left, EDGE, right, EDGE)
        lay.addWidget(panel)

    def minimumSizeHint(self) -> QSize:
        hint = self.panel.minimumSizeHint()
        m = self.layout().contentsMargins()
        return QSize(hint.width() + m.left() + m.right(), hint.height() + m.top() + m.bottom())


def panel() -> glass.GlassSurface:
    """A card on the stage (the sidebar, the inspector), as the sidebar of Artapa's admin panel."""
    surface = glass.GlassSurface(kind=glass.PANEL, radius=theme.RADIUS_STAGE, shadow=False)
    surface.setObjectName("Glass")
    return surface


__all__ = ["BAR", "EDGE", "GAP", "ContentArea", "PanelCell", "QRect", "Stage", "Tabs", "panel"]
