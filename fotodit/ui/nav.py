"""Left panel (the sidebar): quick-access folders and the folder tree.

Every folder in the tree has a check box next to it: checking it selects the folder
together with its subfolders without opening it; clicking its name opens the folder.
The box also shows the folder's selection state (all / some / none). The rows look like the
links of Artapa's admin sidebar: names in the secondary text color, the open folder lit.
"""

from __future__ import annotations

import os

from PySide6.QtCore import (
    QDir,
    QFileInfo,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QRectF,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAbstractFileIconProvider, QCursor, QPainter
from PySide6.QtWidgets import (
    QFileSystemModel,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyleOptionViewItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..core.folderscan import folder_key
from ..i18n import t
from . import count_text, icons
from .folders import FULL, NONE, PARTIAL, SCANNING, FolderSelection
from .widgets import BAR_RIGHT, SidebarDelegate, paint_row_highlight

PathRole = Qt.ItemDataRole.UserRole + 1

_CHECK = {
    NONE: Qt.CheckState.Unchecked,
    PARTIAL: Qt.CheckState.PartiallyChecked,
    FULL: Qt.CheckState.Checked,
    SCANNING: Qt.CheckState.Checked,
}


def known_folders() -> list[tuple[str, str, str]]:
    """(label, path, icon) of the quick-access places that exist on this computer."""
    loc = QStandardPaths.StandardLocation
    items = [
        (t("places.desktop"), QStandardPaths.writableLocation(loc.DesktopLocation), "desktop"),
        (t("places.pictures"), QStandardPaths.writableLocation(loc.PicturesLocation), "photo"),
        (t("places.videos"), QStandardPaths.writableLocation(loc.MoviesLocation), "film"),
        (t("places.downloads"), QStandardPaths.writableLocation(loc.DownloadLocation), "download"),
        (t("places.documents"), QStandardPaths.writableLocation(loc.DocumentsLocation), "doc"),
    ]
    onedrive = os.environ.get("OneDrive")
    if onedrive:
        for sub in ("Resimler", "Pictures"):
            p = os.path.join(onedrive, sub)
            if os.path.isdir(p) and os.path.normcase(p) != os.path.normcase(items[1][1]):
                items.append((t("places.onedrive_pictures"), p, "cloud"))
                break
    return [(label, os.path.normpath(path), icon) for label, path, icon in items if path and os.path.isdir(path)]


class _Icons(QAbstractFileIconProvider):
    """The tree's icons: drawn folders and drives instead of the shell's."""

    def __init__(self):
        super().__init__()
        self._folder = icons.icon("folder", "MUTED")
        self._drive = icons.icon("drive", "MUTED")

    def icon(self, what):  # noqa: D401 - Qt's overloads: an IconType or a QFileInfo
        if isinstance(what, QFileInfo) and what.isRoot():
            return self._drive
        if isinstance(what, QAbstractFileIconProvider.IconType) and what == QAbstractFileIconProvider.IconType.Drive:
            return self._drive
        return self._folder


class FolderTreeModel(QFileSystemModel):
    """Folder tree plus selection check boxes (except on drive roots: scanning a whole
    disk is almost never wanted)."""

    checkToggled = Signal(str)

    def __init__(self, folders: FolderSelection | None, parent=None):
        super().__init__(parent)
        self.folders = folders
        self.toggled = False  # whether the last click was on the check box (if so, the folder is not opened)

    def _checkable(self, index: QModelIndex) -> bool:
        return self.folders is not None and index.isValid() and index.column() == 0 and index.parent().isValid()

    def flags(self, index: QModelIndex):
        flags = super().flags(index)
        if self._checkable(index):
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.CheckStateRole:
            if not self._checkable(index):
                return None
            return _CHECK[self.folders.state(os.path.normpath(self.filePath(index)))]
        if role == Qt.ItemDataRole.ToolTipRole and self._checkable(index):
            path = os.path.normpath(self.filePath(index))
            count = self.folders.counter.peek(folder_key(path))
            lines = [path]
            if count is not None and count.readable:
                what = (count_text(count.photos - count.videos, count.videos) if count.complete
                        else t("count.files_plus", n=count.photos))
                lines.append(t("tree.with_subfolders", what=what))
            lines.append(t("tree.tip"))
            return "\n".join(lines)
        return super().data(index, role)

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if role == Qt.ItemDataRole.CheckStateRole and self._checkable(index):
            self.toggled = True
            self.checkToggled.emit(os.path.normpath(self.filePath(index)))
            return True
        return super().setData(index, value, role)


def _title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    return label


class FolderTree(QTreeView):
    """The folder tree with a full-width rounded highlight on the open and the hovered folder,
    as on Artapa's admin sidebar (the style would only tint the name, leaving the indent bare).

    Deep folders make the tree's steps smaller, down to MIN_STEP, so the deepest names on show keep
    about ten characters even in the narrowest sidebar. (Explorer scrolls its tree sideways instead,
    which cuts the upper folders off at the left; Finder keeps its sidebar to two levels.)"""

    STEP = 14  # px per level while the names fit
    MIN_STEP = 6  # deeper still, names end in "…" (the tooltip gives the whole path)
    NAME_CHARS = 10  # characters (of average width) kept in view of the deepest names

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setIndentation(self.STEP)
        self._open: set[QPersistentModelIndex] = set()  # opened folders, some perhaps inside a closed one
        self.expanded.connect(self._opened)
        self.collapsed.connect(self._closed)
        self._kept: QPersistentModelIndex | None = None  # the folder held in view (keep_in_view)
        self._keep = QTimer(self)  # once per batch of rows the model lists
        self._keep.setSingleShot(True)
        self._keep.setInterval(0)
        self._keep.timeout.connect(self._scroll_to_kept)
        self.verticalScrollBar().actionTriggered.connect(self._let_go)  # the user scrolls (wheel or bar)

    def setModel(self, model) -> None:  # noqa: N802 - Qt override
        super().setModel(model)
        for moved in (model.rowsInserted, model.rowsRemoved, model.layoutChanged):
            moved.connect(self._rows_moved)

    def keep_in_view(self, index: QModelIndex) -> None:
        """Shows the open folder in the middle and holds it there while the folders above it are
        listed (Qt lists them a moment later, and they would push it out of view), until the user
        scrolls, clicks or types in the tree."""
        self._kept = QPersistentModelIndex(index)
        self._scroll_to_kept()

    def _rows_moved(self, *_args) -> None:
        if self._kept is not None:
            self._keep.start()

    def _scroll_to_kept(self) -> None:
        index = QModelIndex(self._kept) if self._kept is not None else QModelIndex()
        if not index.isValid() or self._shown_level(index) is None:  # gone, or a folder above it was closed
            self._kept = None  # (scrolling to it would open them again)
            return
        self.scrollTo(index, QTreeView.ScrollHint.PositionAtCenter)
        self.horizontalScrollBar().setValue(0)

    def _let_go(self, *_args) -> None:
        self._kept = None

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._let_go()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._let_go()
        super().keyPressEvent(event)

    def _opened(self, index: QModelIndex) -> None:
        self._open.add(QPersistentModelIndex(index))
        self._fit_steps()

    def _closed(self, index: QModelIndex) -> None:
        self._open.discard(QPersistentModelIndex(index))
        self._fit_steps()

    def _shown_level(self, index: QModelIndex) -> int | None:
        """The level of `index` (the drives are level 0) if every folder above it is open, else None."""
        level, parent = 0, index.parent()
        while parent.isValid():
            if not self.isExpanded(parent):
                return None
            level, parent = level + 1, parent.parent()
        return level

    def _deepest_level(self) -> int:
        """The level of the deepest folders on show."""
        deepest = 0
        for opened in list(self._open):
            if not opened.isValid():  # deleted, or the drives changed
                self._open.discard(opened)
                continue
            index = QModelIndex(opened)
            level = self._shown_level(index)
            if level is not None and self.isExpanded(index):  # open and on show: its subfolders show
                deepest = max(deepest, level + 1)
        return deepest

    def _fit_steps(self) -> None:
        """Smaller steps (down to MIN_STEP) while the deepest names would keep fewer than
        NAME_CHARS characters, the full step again once they fit."""
        room = self.viewport().width() - self._row_parts() - self.NAME_CHARS * self.fontMetrics().averageCharWidth()
        step = room // (self._deepest_level() + 1)  # + 1: the drives' own step, which holds their arrows
        self.setIndentation(max(self.MIN_STEP, min(self.STEP, step)))

    def _row_parts(self) -> int:
        """The width of a row besides its name: padding, check box, icon and the gaps between them."""
        opt = QStyleOptionViewItem()
        self.initViewItemOption(opt)
        has = QStyleOptionViewItem.ViewItemFeature
        opt.features |= has.HasCheckIndicator | has.HasDecoration | has.HasDisplay
        opt.rect = QRect(0, 0, 1000, 24)
        style = self.style()
        name = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, self)
        margin = style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, None, self) + 1  # Qt's, around text
        return opt.rect.width() - name.width() + 2 * margin

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit_steps()  # the sidebar was resized, or the scroll bar came or went

    def drawRow(self, painter: QPainter, option, index: QModelIndex) -> None:  # noqa: N802 - Qt override
        selected = self.selectionModel() is not None and self.selectionModel().isSelected(index)
        hovered = False
        if not selected and self.viewport().underMouse():
            hovered = self.indexAt(self.viewport().mapFromGlobal(QCursor.pos())) == index
        rect = QRectF(option.rect).adjusted(1, 1, -1, -1)
        paint_row_highlight(painter, rect, selected, hovered, bar=self._bar_fits(index, rect))
        super().drawRow(painter, option, index)

    def _bar_fits(self, index: QModelIndex, rect: QRectF) -> bool:
        """Whether the open folder's bar keeps clear of the row's arrow, which stands in the indent
        step just left of the folder (about 4 px either side of its middle). A folder near the tree's
        top, a drive, has its arrow where the bar would be: it goes without the bar."""
        step = self.indentation()
        arrow = self.visualRect(index).left() - step / 2 - 4.5  # the arrow's left edge
        return arrow - rect.left() >= BAR_RIGHT + 5


class NavPanel(QWidget):
    folderSelected = Signal(str)

    def __init__(self, folders: FolderSelection | None = None, parent=None):
        super().__init__(parent)
        self.folders = folders
        self.setObjectName("Sidebar")

        self.quick = QListWidget()
        self.quick.setObjectName("Sidebar")
        self.quick.setIconSize(QSize(18, 18))
        self.quick.setFrameShape(QListWidget.Shape.NoFrame)
        self.quick.setItemDelegate(SidebarDelegate(self.quick, highlight=True))
        self.quick.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        for label, path, icon in known_folders():
            item = QListWidgetItem(icons.icon(icon, "MUTED", active="TEXT"), label)
            item.setData(PathRole, path)
            item.setToolTip(path)
            self.quick.addItem(item)
        pc = QListWidgetItem(icons.icon("computer", "MUTED", active="TEXT"), t("places.this_pc"))
        pc.setData(PathRole, "")
        self.quick.addItem(pc)
        self.quick.setFixedHeight(self.quick.sizeHintForRow(0) * self.quick.count() + 6)
        self.quick.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.quick.itemClicked.connect(lambda item: self.folderSelected.emit(item.data(PathRole)))

        tree_hint = QLabel(t("tree.hint"))
        tree_hint.setObjectName("Hint")
        tree_hint.setWordWrap(True)
        self.fs = FolderTreeModel(folders, self)
        self._icons = _Icons()
        self.fs.setIconProvider(self._icons)
        self.fs.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)
        self.fs.setOption(QFileSystemModel.Option.DontUseCustomDirectoryIcons, True)
        self.fs.setRootPath("")
        if folders is not None:
            self.fs.checkToggled.connect(folders.toggle)
            folders.changed.connect(self.refresh_checks)
        self.tree = FolderTree()
        self.tree.setObjectName("Sidebar")
        self.tree.setFrameShape(QTreeView.Shape.NoFrame)
        self.tree.setItemDelegate(SidebarDelegate(self.tree))
        self.tree.setModel(self.fs)
        for column in (1, 2, 3):
            self.tree.hideColumn(column)
        self.tree.setHeaderHidden(True)
        self.tree.setIconSize(QSize(17, 17))
        # The column is fitted to the panel: deep folders do not scroll the tree sideways (its steps get
        # smaller instead, see FolderTree), long names are elided.
        self.tree.header().setStretchLastSection(True)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setAnimated(False)
        self.tree.pressed.connect(self._tree_pressed)
        self.tree.clicked.connect(self._tree_clicked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 8, 4)
        layout.setSpacing(4)
        layout.addWidget(_title(t("sidebar.quick_access")))
        layout.addWidget(self.quick)
        layout.addSpacing(8)
        layout.addWidget(_title(t("sidebar.folders")))
        if folders is not None:
            layout.addWidget(tree_hint)
        layout.addWidget(self.tree, 1)

    def refresh_checks(self) -> None:
        self.tree.viewport().update()

    def _tree_pressed(self, _index: QModelIndex) -> None:
        self.fs.toggled = False

    def _tree_clicked(self, index: QModelIndex) -> None:
        path = self.fs.filePath(index)
        if path:
            # A click on the check box does not open the folder. Qt may report the box
            # change before or after the click signal, so the decision is made on the
            # next turn of the event loop.
            QTimer.singleShot(0, lambda: self._open_unless_toggled(path))

    def _open_unless_toggled(self, path: str) -> None:
        if self.fs.toggled:
            self.fs.toggled = False
            return
        self.folderSelected.emit(os.path.normpath(path))

    def sync(self, path: str) -> None:
        """Makes the browsed folder visible and selected in the tree."""
        self.quick.clearSelection()
        for i in range(self.quick.count()):
            item = self.quick.item(i)
            if os.path.normcase(item.data(PathRole) or "") == os.path.normcase(path or ""):
                item.setSelected(True)
        if not path:
            self.tree.clearSelection()
            self.tree.collapseAll()
            return
        index = self.fs.index(QDir.fromNativeSeparators(path))
        if index.isValid():
            self.tree.setCurrentIndex(index)
            parent = index.parent()
            while parent.isValid():
                self.tree.expand(parent)
                parent = parent.parent()
            self.tree.keep_in_view(index)
