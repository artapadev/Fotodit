"""Thumbnail grid showing a folder's contents (the Browse tab).

Clicking a photo or video selects/deselects it (the selection is kept when the folder
changes). Video tiles carry a "▶ duration" label. Double-clicking a folder tile opens that
folder (a single click only highlights it); clicking the circle at the tile's top left (or
Ctrl+click) selects the folder together with its subfolders without opening it. Shift+click
selects a range, Ctrl+A selects everything.

The page's toolbars float above the grid as glass (see `chrome`): the back/forward/up buttons
and the path bar (clickable steps; Ctrl+L types a path) at the top, the folder's counts and the
selection buttons at the bottom. The
grid leaves room for them (insets) and its content passes beneath them when scrolled, softened
by a scroll edge effect.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLineEdit,
    QListView,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..core.edits import Edit
from ..core.folderscan import SKIP_PREFIXES, folder_key, is_hidden
from ..core.formats import is_supported_name, is_video_name
from ..core.video.probe import duration_text
from ..i18n import t
from ..winutil import drive_label, explorer_key, reveal
from . import count_text, glass, icons, number, size_text, theme
from .controls import BarButton, IconView, ToolGroup, separator
from .folders import FULL, NONE, PARTIAL, SCANNING, FolderSelection
from .selection import Selection, norm
from .thumbs import LOADING, ThumbnailLoader
from .widgets import (
    ElidedLabel,
    EmptyState,
    menu,
    paint_check_badge,
    paint_edit_badge,
    paint_video_badge,
)

ALL_RESOLVED = 0xFFFFF  # QFont::AllPropertiesResolved (PySide6 has no name for it)
EntryRole = Qt.ItemDataRole.UserRole + 1
StateRole = Qt.ItemDataRole.UserRole + 2
EditRole = Qt.ItemDataRole.UserRole + 3

THUMB_RADIUS = 9.0
EDGE_FADE = 18  # px of the scroll edge effect beyond the bars


@dataclass
class Entry:
    kind: str  # "dir" | "photo" (photo or video)
    path: str
    name: str
    size: int = 0
    mtime: float = 0.0
    key: str = ""  # comparison key (`norm`): a folder's in the folder states, a file's in the selection
    video: bool = False


def listed(photos: list[Entry]) -> list[tuple[str, int, float]]:
    """Photos of the open folder as the selection takes them, with the size and date the folder's listing
    already holds: selecting thousands of them opens none (a file each is slow, more so on a network)."""
    return [(e.path, e.size, e.mtime) for e in photos]


def folder_path(path: str) -> str:
    """A folder as the browser keeps it: absolute and normalized ("" is This PC). A bare drive ("D:")
    means its root: to Windows it would mean the current folder on that drive."""
    if not path:
        return ""
    drive, rest = os.path.splitdrive(path)
    if drive and not rest:
        path = drive + os.sep
    return os.path.abspath(path)


def scan_folder(folder: str) -> list[Entry]:
    """The folder's direct contents, using the same rules as the subfolder scan (folderscan)."""
    dirs: list[Entry] = []
    photos: list[Entry] = []
    with os.scandir(os.path.abspath(folder)) as it:
        for e in it:
            name = e.name
            if name.startswith(SKIP_PREFIXES):
                continue
            try:
                st = e.stat()
                if is_hidden(st):
                    continue
                if e.is_dir():
                    dirs.append(Entry("dir", e.path, name, key=folder_key(e.path)))
                elif e.is_file() and is_supported_name(name):
                    photos.append(Entry("photo", e.path, name, st.st_size, st.st_mtime, key=norm(e.path),
                                        video=is_video_name(name)))
            except OSError:
                continue
    dirs.sort(key=lambda x: explorer_key(x.name))
    photos.sort(key=lambda x: explorer_key(x.name))
    return dirs + photos


class FolderModel(QAbstractListModel):
    def __init__(self, selection: Selection, folders: FolderSelection, loader: ThumbnailLoader, parent=None):
        super().__init__(parent)
        self.selection = selection
        self.folders = folders
        self.loader = loader
        self.entries: list[Entry] = []
        self._rows: dict[str, int] = {}
        loader.ready.connect(self._thumb_ready)
        selection.edits.changed.connect(self._edits_changed)

    def set_entries(self, entries: list[Entry]) -> None:
        self.beginResetModel()
        self.entries = entries
        self._rows = {e.key: i for i, e in enumerate(entries)}
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def state(self, e: Entry) -> int:
        if e.kind == "dir":
            return self.folders.state(e.path, e.key)
        return FULL if self.selection.has_key(e.key) else NONE

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        e = self.entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return e.name
        if role == EntryRole:
            return e
        if role == StateRole:
            return self.state(e)
        if role == EditRole:
            return self.edit(e)
        if role == Qt.ItemDataRole.ToolTipRole:
            if e.kind == "dir":
                return (t("browser.folder_tip", name=e.name, summary=folder_summary(self.folders, e)))
            edit = self.edit(e)
            extra = "\n" + t("browser.edit_line", edit=edit.describe(future=True)) if edit else ""
            if e.video:
                seconds = self.loader.duration(e.path, e.mtime)
                length = f" · {duration_text(seconds)}" if seconds else ""
                return (t("browser.video_tip", name=e.name, size=size_text(e.size), length=length, extra=extra))
            return (t("browser.photo_tip", name=e.name, size=size_text(e.size), extra=extra))
        return None

    def edit(self, e: Entry) -> Edit | None:
        return self.selection.edits.get(e.path, e.mtime) if e.kind == "photo" else None

    def _edits_changed(self, keys: list[str]) -> None:
        for key in keys:
            row = self._rows.get(key)
            if row is not None:
                idx = self.index(row)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def photos(self) -> list[Entry]:
        return [e for e in self.entries if e.kind == "photo"]

    def dirs(self) -> list[Entry]:
        return [e for e in self.entries if e.kind == "dir"]

    def _thumb_ready(self, path: str) -> None:
        row = self._rows.get(norm(path))
        if row is not None:
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])


def folder_summaries(folders: FolderSelection, e: Entry) -> list[str]:
    """The short info on a folder tile, from the most detailed to the shortest ("124 fotoğraf, 6 video",
    "130 dosya" …): the tile shows the first one that fits, the tooltip the first one."""
    state = folders.state(e.path, e.key)
    if state == SCANNING:
        return [t("folder.scanning")]
    count = folders.count(e.path)
    chosen = folders.selected_in(e.path)
    if count is None:
        return [t("folder.chosen", n=chosen) if chosen else t("folder.counting")]
    if not count.readable:
        return [t("folder.unreadable")]
    if not count.complete:  # counting hit the limit: splitting by type would be misleading
        total = f"{number(count.photos)}+"
        if state == PARTIAL:
            return [t("folder.chosen_of", n=chosen, total=total)]
        return [t("folder.files_total", total=total)]
    if state == PARTIAL:
        return [t("folder.chosen_of", n=chosen, total=number(count.photos))]
    what = [count_text(count.photos - count.videos, count.videos), t("count.files.plain", n=count.photos)]
    if state == FULL:
        return [t("folder.all_chosen_with", what=w) for w in what] + [t("folder.all_chosen")]
    return what if count.photos else [t("folder.empty")]


def folder_summary(folders: FolderSelection, e: Entry) -> str:
    return folder_summaries(folders, e)[0]


_BADGE_STATE = {NONE: "none", FULL: "full", PARTIAL: "partial", SCANNING: "busy"}


def paint_folder(painter: QPainter, rect: QRectF) -> None:
    """A folder in the theme's folder colors (a back panel with its tab, and a lighter front)."""
    w, h = rect.width(), rect.height()
    x, y = rect.x(), rect.y()
    back = QPainterPath()
    tab_w, tab_h = w * 0.36, h * 0.12
    r = min(w, h) * 0.07
    back.moveTo(x + r, y)
    back.lineTo(x + tab_w - r * 0.5, y)
    back.quadTo(x + tab_w, y, x + tab_w + r * 0.7, y + tab_h * 0.6)
    back.lineTo(x + tab_w + r * 1.2, y + tab_h)
    back.lineTo(x + w - r, y + tab_h)
    back.quadTo(x + w, y + tab_h, x + w, y + tab_h + r)
    back.lineTo(x + w, y + h - r)
    back.quadTo(x + w, y + h, x + w - r, y + h)
    back.lineTo(x + r, y + h)
    back.quadTo(x, y + h, x, y + h - r)
    back.lineTo(x, y + r)
    back.quadTo(x, y, x + r, y)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.fillPath(back, QColor(theme.FOLDER_TAB))
    front = QPainterPath()
    front.addRoundedRect(QRectF(x, y + h * 0.24, w, h * 0.76), r, r)
    grad = QLinearGradient(QPointF(x, y + h * 0.24), QPointF(x, y + h))
    top = QColor(theme.FOLDER)
    grad.setColorAt(0.0, top.lighter(112))
    grad.setColorAt(1.0, top)
    painter.fillPath(front, grad)
    painter.setPen(QPen(QColor(255, 255, 255, 70), 1))
    painter.drawLine(QPointF(x + r, y + h * 0.24 + 0.5), QPointF(x + w - r, y + h * 0.24 + 0.5))
    painter.restore()


class TileDelegate(QStyledItemDelegate):
    NAME_H = 24
    INFO_H = 18  # the folder's photo count under its icon
    BADGE = 22
    CACHE_BYTES = 48 << 20  # drawn tiles kept (about 200 at 125 % display scaling)

    def __init__(self, loader: ThumbnailLoader, folders: FolderSelection, tile: QSize, parent=None):
        super().__init__(parent)
        self.loader = loader
        self.folders = folders
        self.tile = tile
        self._tiles: OrderedDict[tuple, QPixmap] = OrderedDict()  # drawn tiles, by everything they show
        theme.changes().changed.connect(self._forget)

    def _forget(self) -> None:
        self._tiles.clear()

    def sizeHint(self, option, index) -> QSize:
        return self.tile

    @classmethod
    def image_rect(cls, item_rect: QRect) -> QRectF:
        """The area of the thumbnail (or the folder icon and its count) above the name."""
        r = QRectF(item_rect.adjusted(4, 4, -4, -4))
        return QRectF(r.x() + 9, r.y() + 9, r.width() - 18, r.height() - 18 - cls.NAME_H)

    @classmethod
    def folder_rect(cls, item_rect: QRect) -> QRectF:
        area = cls.image_rect(item_rect).adjusted(0, 0, 0, -cls.INFO_H)
        s = min(area.width() * 0.72, area.height() * 0.86)
        return QRectF(area.center().x() - s / 2, area.center().y() - s * 0.4, s, s * 0.8)

    @classmethod
    def badge_rect(cls, item_rect: QRect) -> QRectF:
        """A folder's selection circle: on the folder's front, top left, as on the photos."""
        folder = cls.folder_rect(item_rect)
        return QRectF(folder.x() + 8, folder.y() + folder.height() * 0.24 + 8, cls.BADGE, cls.BADGE)

    @classmethod
    def badge_hit(cls, item_rect: QRect, pos: QPoint) -> bool:
        return cls.badge_rect(item_rect).adjusted(-8, -8, 8, 8).contains(QPointF(pos))

    def _key(self, painter: QPainter, option, index, e: Entry, dpr: float) -> tuple:
        """Everything a tile shows: a drawn tile is reused while all of it stays the same."""
        if e.kind == "dir":
            look: tuple = tuple(folder_summaries(self.folders, e))
        else:
            edit = index.data(EditRole)
            pm = self.loader.get(e.path, e.mtime, edit)
            look = (edit, pm.cacheKey() if isinstance(pm, QPixmap) else id(pm),
                    self.loader.duration(e.path, e.mtime) if e.video else None)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focus = bool(option.state & QStyle.StateFlag.State_HasFocus)
        return (e.path, e.mtime, e.kind, e.name, int(index.data(StateRole) or NONE), hover, focus, look,
                option.rect.width(), option.rect.height(), round(dpr, 3), theme.is_dark(),
                painter.font().key(), option.font.key())

    def paint(self, painter: QPainter, option, index) -> None:
        """Copies the tile from the drawn ones, drawing it first if anything on it changed."""
        e: Entry = index.data(EntryRole)
        if e is None:
            return
        device = painter.device()
        dpr = device.devicePixelRatioF() if device is not None else 1.0
        key = self._key(painter, option, index, e, dpr)
        tile = self._tiles.get(key)
        if tile is None:
            size = option.rect.size()
            tile = QPixmap(max(1, round(size.width() * dpr)), max(1, round(size.height() * dpr)))
            tile.setDevicePixelRatio(dpr)
            tile.fill(Qt.GlobalColor.transparent)
            p = QPainter(tile)
            font = QFont(painter.font())
            font.setResolveMask(ALL_RESOLVED)  # the view's font as it is, not filled in from the pixmap's
            p.setFont(font)
            local = QStyleOptionViewItem(option)
            local.rect = QRect(QPoint(0, 0), size)
            self._draw(p, local, index, e)
            p.end()
            self._tiles[key] = tile
            limit = max(40, self.CACHE_BYTES // max(1, tile.width() * tile.height() * 4))
            while len(self._tiles) > limit:
                self._tiles.popitem(last=False)
        else:
            self._tiles.move_to_end(key)
        painter.drawPixmap(option.rect.topLeft(), tile)

    def _draw(self, painter: QPainter, option, index, e: Entry) -> None:
        state = int(index.data(StateRole) or NONE)
        marked = state != NONE
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focus = bool(option.state & QStyle.StateFlag.State_HasFocus)
        r = QRectF(option.rect.adjusted(4, 4, -4, -4))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        if hover or focus:
            frame = QPainterPath()
            frame.addRoundedRect(r, 12, 12)
            painter.fillPath(frame, QColor(theme.ITEM_HOVER))
        img = self.image_rect(option.rect)
        target = img
        if e.kind == "dir":
            folder = self.folder_rect(option.rect)
            paint_folder(painter, folder)
            target = folder
            info = QRectF(img.x(), img.bottom() - self.INFO_H, img.width(), self.INFO_H)
            font = QFont(painter.font())
            font.setPointSizeF(max(7.5, font.pointSizeF() - 1.5))
            painter.setFont(font)
            painter.setPen(QColor(theme.ACCENT_TEXT if marked else theme.MUTED))
            fm = QFontMetrics(font)
            texts = folder_summaries(self.folders, e)
            text = next((t for t in texts if fm.horizontalAdvance(t) <= info.width()), None)
            if text is None:
                text = fm.elidedText(texts[-1], Qt.TextElideMode.ElideRight, int(info.width()))
            painter.drawText(info, Qt.AlignmentFlag.AlignCenter, text)
            painter.setFont(option.font)
        else:
            edit = index.data(EditRole)
            pm = self.loader.get(e.path, e.mtime, edit)
            if pm is LOADING or pm is None:
                empty = img.adjusted(8, 8, -8, -8)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(theme.TILE_EMPTY))
                painter.drawRoundedRect(empty, THUMB_RADIUS, THUMB_RADIUS)
                if pm is None:
                    icons.paint(painter, QRectF(empty.center().x() - 14, empty.center().y() - 22, 28, 28),
                                "film" if e.video else "photo", "MUTED")
                    painter.setPen(QColor(theme.MUTED))
                    painter.drawText(empty.adjusted(0, 18, 0, 0), Qt.AlignmentFlag.AlignCenter, t("tile.no_preview"))
                target = empty
            else:
                dpr = pm.devicePixelRatio() or 1.0
                w, h = pm.width() / dpr, pm.height() / dpr
                scale = min(img.width() / w, img.height() / h, 1.0)
                tw, th = w * scale, h * scale
                # Standing on the name (as in Finder), so wide photos do not float far above it.
                target = QRectF(img.center().x() - tw / 2, img.bottom() - th, tw, th)
                clip = QPainterPath()
                radius = min(THUMB_RADIUS, tw / 4, th / 4)
                clip.addRoundedRect(target, radius, radius)
                painter.save()
                painter.setClipPath(clip)
                painter.drawPixmap(target, pm, QRectF(pm.rect()))
                painter.restore()
                painter.setPen(QPen(QColor(0, 0, 0, 28 if not theme.is_dark() else 60), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(target.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
                if e.video:
                    paint_video_badge(painter, QPointF(target.left(), target.bottom()),
                                      self.loader.duration(e.path, e.mtime))
            if e.video and (pm is LOADING or pm is None):
                paint_video_badge(painter, QPointF(target.left(), target.bottom()), None)
            if edit is not None:
                paint_edit_badge(painter, QPointF(target.right(), target.bottom()))
        if marked:
            ring = target.adjusted(-4, -4, 4, 4)
            pen = QPen(QColor(theme.ACCENT), 2.5)
            if state == PARTIAL:  # some of the folder: a lighter, dashed ring
                pen = QPen(QColor(theme.MUTED), 2.0, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(ring, THUMB_RADIUS + 4, THUMB_RADIUS + 4)

        # The name keeps its weight when selected (the ring and the check show it), so it never
        # jumps or gets shortened on a click.
        text_rect = QRectF(r.x() + 6, r.bottom() - self.NAME_H - 2, r.width() - 12, self.NAME_H)
        fm = QFontMetrics(painter.font())
        name = fm.elidedText(e.name, Qt.TextElideMode.ElideMiddle, int(text_rect.width()))
        painter.setPen(QColor(theme.ACCENT_TEXT if marked else theme.TEXT))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, name)

        if e.kind == "dir":
            # Folders: the circle is a button of its own (selecting without opening), always shown.
            paint_check_badge(painter, self.badge_rect(option.rect), _BADGE_STATE[state], hover)
        elif marked or hover:
            s = self.BADGE
            inset = 7.0 if target.width() > 3 * s and target.height() > 3 * s else -s / 2
            badge = QRectF(target.left() + inset, target.top() + inset, s, s)
            paint_check_badge(painter, badge, _BADGE_STATE[state], hover)
        painter.restore()


class PhotoGrid(QListView):
    folderActivated = Signal(str)  # open the folder
    photoFocused = Signal(str)
    selectAllRequested = Signal()
    previewRequested = Signal(str)  # large preview
    cropRequested = Signal(str)  # large preview in crop mode
    edited = Signal(str)  # rotated / edit removed (info in the status bar)

    def __init__(self, model: FolderModel, selection: Selection, folders: FolderSelection,
                 delegate: TileDelegate, parent=None):
        super().__init__(parent)
        self.selection = selection
        self.folders = folders
        self._top = 0  # room for the floating bars (the content scrolls beneath them)
        self._bottom = 0
        self.setModel(model)
        self.setItemDelegate(delegate)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setGridSize(delegate.tile)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(30)
        self.setMouseTracking(True)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self._anchor: int | None = None
        # Last pressed item: what did the first click of a double-click do? ("photo"/"dir"/"range", path)
        self._pressed: tuple[str, str | None] = ("", None)
        theme.changes().changed.connect(self.viewport().update)  # the tiles are painted with the theme's colors

    def model(self) -> FolderModel:  # type: ignore[override]
        return super().model()

    # ------------------------------------------------------------ insets

    def set_insets(self, top: int, bottom: int) -> None:
        """Room for the bars floating above the grid: the first row starts below the top bar and the
        last one can be scrolled above the bottom bar."""
        if (top, bottom) != (self._top, self._bottom):
            self._top, self._bottom = top, bottom
            self.updateGeometries()
            self.viewport().update()

    def verticalOffset(self) -> int:  # noqa: N802 - Qt's name
        return super().verticalOffset() - self._top

    def updateGeometries(self) -> None:  # noqa: N802
        super().updateGeometries()
        bar = self.verticalScrollBar()
        total = self.contentsSize().height() + self._top + self._bottom
        bar.setRange(0, max(0, total - self.viewport().height()))
        bar.setPageStep(max(1, self.viewport().height() - self._top - self._bottom))

    def scrollTo(self, index: QModelIndex, hint=QAbstractItemView.ScrollHint.EnsureVisible) -> None:  # noqa: N802
        """Like Qt's, but "visible" means between the bars."""
        rect = self.visualRect(index)
        if not rect.isValid():
            return
        top, bottom = self._top, self.viewport().height() - self._bottom
        bar = self.verticalScrollBar()
        if hint == QAbstractItemView.ScrollHint.PositionAtCenter:
            bar.setValue(bar.value() + rect.center().y() - (top + bottom) // 2)
        elif hint == QAbstractItemView.ScrollHint.PositionAtTop or rect.top() < top:
            bar.setValue(bar.value() - (top - rect.top()))
        elif rect.bottom() > bottom:
            bar.setValue(bar.value() + rect.bottom() - bottom)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        # Scroll edge effect: where the content passes beneath the bars it fades into the window's
        # color, so the glass above it stays legible (Apple's soft scroll edge).
        bar = self.verticalScrollBar()
        p = QPainter(self.viewport())
        base = QColor(theme.WINDOW)
        w = self.viewport().width()
        if self._top and bar.value() > 0:
            strength = min(1.0, bar.value() / 40)
            h = self._top + EDGE_FADE
            grad = QLinearGradient(0, 0, 0, h)
            for stop, alpha in ((0.0, 0.86), (self._top / h * 0.75, 0.62), (1.0, 0.0)):
                c = QColor(base)
                c.setAlphaF(alpha * strength)
                grad.setColorAt(stop, c)
            p.fillRect(QRect(0, 0, w, h), grad)
        if self._bottom and bar.value() < bar.maximum():
            strength = min(1.0, (bar.maximum() - bar.value()) / 40)
            h = self._bottom + EDGE_FADE
            y0 = self.viewport().height() - h
            grad = QLinearGradient(0, y0, 0, y0 + h)
            for stop, alpha in ((0.0, 0.0), (EDGE_FADE / h, 0.55), (1.0, 0.86)):
                c = QColor(base)
                c.setAlphaF(alpha * strength)
                grad.setColorAt(stop, c)
            p.fillRect(QRect(0, y0, w, h), grad)

    # ------------------------------------------------------------ mouse and keys

    def reset_anchor(self) -> None:
        self._anchor = None
        self._pressed = ("", None)

    def _entry(self, index: QModelIndex) -> Entry | None:
        return index.data(EntryRole) if index.isValid() else None

    def toggle(self, e: Entry) -> None:
        if e.kind == "dir":
            self.folders.toggle(e.path)
        else:
            self.selection.toggle(e.path)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        e = self._entry(index)
        self._pressed = ("", None)
        if e is None:
            super().mousePressEvent(event)
            return
        self.setCurrentIndex(index)
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ShiftModifier and self._anchor is not None:
            self._pressed = ("range", None)
            self._select_range(index.row(), self.model().state(e) != FULL)
            return
        if e.kind == "dir":
            # A single click only highlights a folder; its circle (or Ctrl+click) selects it and a
            # double-click opens it, so a click meant for a photo never lands you in another folder.
            if mods & Qt.KeyboardModifier.ControlModifier or TileDelegate.badge_hit(self.visualRect(index), pos):
                self._pressed = ("badge", e.path)
                self.folders.toggle(e.path)
                self._anchor = index.row()
            else:
                self._pressed = ("dir", e.path)
            return
        self.selection.toggle(e.path)
        self._anchor = index.row()
        self._pressed = ("photo", e.path)
        self.photoFocused.emit(e.path)

    def mouseDoubleClickEvent(self, event) -> None:
        """Double-clicking a folder opens it; double-clicking a photo opens the large preview (the
        first click of the double-click has already changed the selection; that change is undone,
        so the selection stays the same). Two quick clicks on a folder's circle select it and take
        it back, like two clicks."""
        event.accept()
        kind, pressed = self._pressed
        self._pressed = ("", None)
        if event.button() != Qt.MouseButton.LeftButton or kind == "range":
            return
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        e = self._entry(index)
        if e is None:
            return
        if e.kind == "dir":
            if kind == "badge" and pressed == e.path:
                self.folders.toggle(e.path)
            elif kind == "dir" and pressed == e.path:
                self.folderActivated.emit(e.path)
            return
        if e.kind != "photo" or kind in ("dir", "badge"):
            return
        if kind == "photo":
            if pressed != e.path:
                return
            self.selection.toggle(e.path)
        self.previewRequested.emit(e.path)

    def _select_range(self, row: int, state: bool) -> None:
        lo, hi = sorted((self._anchor, row))
        entries = self.model().entries[lo:hi + 1]
        photos = [e for e in entries if e.kind == "photo"]
        if state:
            self.selection.add_entries(listed(photos))
        else:
            self.selection.remove([e.path for e in photos])
        dirs = [e.path for e in entries if e.kind == "dir"]
        if dirs:
            self.folders.set_selected(dirs, state)

    def keyPressEvent(self, event) -> None:
        index = self.currentIndex()
        e = self._entry(index)
        key = event.key()
        if key == Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.selectAllRequested.emit()
            return
        if key == Qt.Key.Key_Space and e is not None:
            self.toggle(e)
            self._anchor = index.row()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and e is not None:
            if e.kind == "dir":
                self.folderActivated.emit(e.path)
            else:
                self.selection.toggle(e.path)
                self._anchor = index.row()
            return
        super().keyPressEvent(event)

    def currentChanged(self, current: QModelIndex, previous: QModelIndex) -> None:
        super().currentChanged(current, previous)
        e = self._entry(current)
        if e is not None and e.kind == "photo":
            self.photoFocused.emit(e.path)

    def _context_menu(self, pos: QPoint) -> None:
        e = self._entry(self.indexAt(pos))
        if e is None:
            return
        m = menu(self)
        if e.kind == "dir":
            selected = self.model().state(e) in (FULL, SCANNING)
            toggle = QAction(t("menu.folder_deselect") if selected else t("menu.folder_select"), m)
            toggle.triggered.connect(lambda: self.folders.set_selected([e.path], not selected))
            enter = QAction(t("menu.folder_open"), m)
            enter.triggered.connect(lambda: self.folderActivated.emit(e.path))
            show = QAction(t("menu.show_in_explorer"), m)
            show.triggered.connect(lambda: reveal(e.path))
            m.addActions([toggle, enter, show])
        else:
            selected = e.path in self.selection
            preview = QAction(icons.icon("play.fill" if e.video else "eye"), t("menu.play") if e.video else t("menu.large_preview"),
                              m)
            preview.triggered.connect(lambda: self.previewRequested.emit(e.path))
            toggle = QAction(t("menu.deselect") if selected else t("menu.select"), m)
            toggle.triggered.connect(lambda: self.selection.toggle(e.path))
            right = QAction(icons.icon("rotate.right"), t("common.rotate_right"), m)
            right.triggered.connect(lambda: self.rotate(e, 1))
            left = QAction(icons.icon("rotate.left"), t("common.rotate_left"), m)
            left.triggered.connect(lambda: self.rotate(e, -1))
            flip = QAction(icons.icon("flip"), t("common.flip"), m)
            flip.triggered.connect(lambda: self.flip(e))
            crop = QAction(icons.icon("crop"), t("menu.crop"), m)
            crop.triggered.connect(lambda: self.cropRequested.emit(e.path))
            view = QAction(t("menu.open_default"), m)
            view.triggered.connect(lambda: os.startfile(e.path))  # noqa: S606
            show = QAction(t("menu.open_location"), m)
            show.triggered.connect(lambda: reveal(e.path))
            m.addActions([preview, toggle])
            m.addSeparator()
            m.addActions([right, left, flip, crop])
            if self.model().edit(e) is not None:
                reset = QAction(t("menu.remove_edits"), m)
                reset.triggered.connect(lambda: self._reset_edit(e))
                m.addAction(reset)
            m.addSeparator()
            m.addActions([view, show])
            m.setDefaultAction(preview)
        m.exec(self.viewport().mapToGlobal(pos))

    def rotate(self, e: Entry, delta: int) -> None:
        """Rotates the photo by 90° (applied when the run starts); selects it if it is not selected."""
        self.selection.edits.update([(e.path, e.mtime)], lambda edit: edit.rotated(delta))
        if e.path not in self.selection:
            self.selection.add([e.path])
        self.edited.emit(e.path)

    def flip(self, e: Entry) -> None:
        """Flips the photo left to right (applied when the run starts); selects it if it is not selected."""
        self.selection.edits.update([(e.path, e.mtime)], lambda edit: edit.mirrored())
        if e.path not in self.selection:
            self.selection.add([e.path])
        self.edited.emit(e.path)

    def _reset_edit(self, e: Entry) -> None:
        self.selection.edits.clear([e.path])
        self.edited.emit(e.path)


def crumbs(path: str) -> list[tuple[str, str]]:
    """The steps (label, path) from the top down to `path`. A known place stands for its folder, as
    in Explorer's address bar (Resimler › 2024 rather than C: › Users › … › Pictures › 2024);
    elsewhere the steps start at the drive, by the name Explorer gives it."""
    if not path:
        return []
    from .nav import known_folders

    here = os.path.normcase(path)
    best: tuple[str, str] | None = None
    for label, place, _icon in known_folders():
        key = os.path.normcase(place)
        inside = here == key or here.startswith(key.rstrip("\\/") + os.sep)
        if inside and (best is None or len(place) > len(best[1])):
            best = (label, place)
    if best is not None:
        steps, rest = [best], os.path.relpath(path, best[1])
    else:
        drive, rest = os.path.splitdrive(path)
        top = drive + os.sep if drive else os.sep
        steps, rest = [(drive_label(top) if drive else os.sep, top)], rest.strip("\\/")
    current = steps[0][1]
    for name in [] if rest in ("", ".") else rest.split(os.sep):
        current = os.path.join(current, name)
        steps.append((name, current))
    return steps


class Breadcrumb(QWidget):
    """The open folder as clickable steps, Resimler › 2024 › Tatil. A step opens its folder; a click
    on the empty part or on the last step turns the bar into a field to type a path into. Steps that
    do not fit give way to "…" at the start."""

    pathClicked = Signal(str)
    editRequested = Signal()

    PAD = 6.0  # around each step's name: its hover capsule and click area
    SEP = 14.0  # the chevron between steps

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._steps: list[tuple[str, str]] = []
        self._shown: list[tuple[QRectF, str, str]] = []  # (where, label, path), as last laid out
        self._hover = -1
        self._pressed = -1
        self.setMouseTracking(True)
        self.setMinimumWidth(60)
        self.setAccessibleName(t("path.name"))

    def set_path(self, path: str) -> None:
        self._steps = crumbs(path)
        self._hover = self._pressed = -1
        self.setAccessibleDescription(path)
        self._relayout()

    def sizeHint(self) -> QSize:
        return QSize(240, round(QFontMetricsF(self.font()).height()) + 10)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def _strong(self) -> QFont:
        f = QFont(self.font())
        f.setWeight(QFont.Weight.DemiBold)
        return f

    def _relayout(self) -> None:
        """Places the steps; while they do not fit, the first ones give way to "…"."""
        fm = QFontMetricsF(self.font())
        widths = [fm.horizontalAdvance(label) + 2 * self.PAD for label, _ in self._steps]
        if widths:
            widths[-1] = QFontMetricsF(self._strong()).horizontalAdvance(self._steps[-1][0]) + 2 * self.PAD
        dots = fm.horizontalAdvance("…") + 2 * self.PAD

        def needed(first: int) -> float:
            return sum(widths[first:]) + self.SEP * (len(widths) - first - 1) + ((dots + self.SEP) if first else 0)

        first = 0
        while first < len(widths) - 1 and needed(first) > self.width():
            first += 1
        shown: list[tuple[QRectF, str, str]] = []
        x, h = 0.0, float(self.height())
        if first:
            shown.append((QRectF(x, 0, dots, h), "…", self._steps[first - 1][1]))
            x += dots + self.SEP
        for i in range(first, len(widths)):
            label, path = self._steps[i]
            shown.append((QRectF(x, 0, min(widths[i], max(0.0, self.width() - x)), h), label, path))
            x += widths[i] + self.SEP
        self._shown = shown
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self._shown:
            p.setPen(theme.color(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       " " + t("path.placeholder"))
            return
        last = len(self._shown) - 1
        for i, (rect, label, _path) in enumerate(self._shown):
            if i in (self._hover, self._pressed) and i != last:
                pill = QRectF(rect.x(), rect.center().y() - 12, rect.width(), 24)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(theme.color(theme.ACCENT_SOFT if i == self._pressed else theme.HOVER))
                p.drawRoundedRect(pill, 12, 12)
            font = self._strong() if i == last else self.font()
            p.setFont(font)
            p.setPen(theme.color(theme.TEXT if i in (last, self._hover) else theme.MUTED))
            inner = rect.adjusted(self.PAD, 0, -self.PAD, 0)
            text = QFontMetricsF(font).elidedText(label, Qt.TextElideMode.ElideMiddle, max(0.0, inner.width()))
            p.drawText(inner, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
            if i != last:
                icons.paint(p, QRectF(rect.right(), rect.center().y() - 7, self.SEP, 14), "chevron.right", "MUTED")

    def _at(self, pos: QPointF) -> int:
        for i, (rect, _label, _path) in enumerate(self._shown):
            if rect.contains(pos):
                return i
        return -1

    def _set_hover(self, i: int) -> None:
        hover = -1 if i == len(self._shown) - 1 else i  # the last step is where you are: no link
        if hover != self._hover:
            self._hover = hover
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover >= 0 else Qt.CursorShape.IBeamCursor)
            self.update()

    def mouseMoveEvent(self, event) -> None:
        self._set_hover(self._at(event.position()))

    def leaveEvent(self, _event) -> None:
        self._set_hover(-1)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        i = self._at(event.position())
        if 0 <= i < len(self._shown) - 1:
            self._pressed = i  # the step opens on release, unless the pointer has left it by then
            self.update()
        else:
            self.editRequested.emit()

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._pressed = self._pressed, -1
        self.update()
        if pressed >= 0 and event.button() == Qt.MouseButton.LeftButton and self._at(event.position()) == pressed:
            self.pathClicked.emit(self._shown[pressed][2])

    def event(self, e) -> bool:
        if e.type() == QEvent.Type.ToolTip:
            i = self._at(QPointF(e.pos()))
            QToolTip.showText(e.globalPos(), self._shown[i][2] if i >= 0
                              else t("path.click_tip"), self)
            return True
        return super().event(e)


class PathField(QLineEdit):
    """The path bar while a path is typed; Esc or a click elsewhere gives the steps back."""

    done = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.done.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        # Switching to another window (to copy a path, say) or a context menu keeps the field.
        if event.reason() not in (Qt.FocusReason.ActiveWindowFocusReason, Qt.FocusReason.PopupFocusReason):
            self.done.emit()


class BrowserPage(QWidget):
    folderChanged = Signal(str)
    photoFocused = Signal(str)
    addFilesRequested = Signal()
    previewRequested = Signal(str)  # large preview (of the folder's photos and videos)
    cropRequested = Signal(str)  # large preview, in crop mode

    def __init__(self, selection: Selection, folders: FolderSelection, loader: ThumbnailLoader,
                 tile: QSize, parent=None):
        super().__init__(parent)
        self.selection = selection
        self.folders = folders
        self.loader = loader
        self.folder = ""
        self._history: list[str] = []
        self._pos = -1
        self._not_found = False  # a typed path that does not exist is on show

        # The bars (glass, floating above the grid; see `chrome`).
        self.back_btn = BarButton("", "chevron.left", tooltip=t("nav.back"))
        self.back_btn.clicked.connect(self.go_back)
        self.fwd_btn = BarButton("", "chevron.right", tooltip=t("nav.forward"))
        self.fwd_btn.clicked.connect(self.go_forward)
        self.up_btn = BarButton("", "arrow.up", tooltip=t("nav.up"))
        self.up_btn.clicked.connect(self.go_up)
        self.nav_bar = glass.GlassSurface(self, kind=glass.BAR)
        lay = QHBoxLayout(self.nav_bar)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(0)
        for w in (self.back_btn, self.fwd_btn, self.up_btn):
            lay.addWidget(w)

        self.crumbs = Breadcrumb()
        self.crumbs.pathClicked.connect(self.navigate)
        self.crumbs.editRequested.connect(self.edit_path)
        self.path_edit = PathField()
        self.path_edit.setObjectName("GlassField")
        self.path_edit.setPlaceholderText(t("path.placeholder"))
        self.path_edit.setAccessibleName(t("path.name"))
        self.path_edit.returnPressed.connect(self._path_entered)
        self.path_edit.done.connect(self._end_path_edit)
        self.path_edit.hide()
        for keys in ("Ctrl+L", "Alt+D"):  # Explorer's keys for typing a path (only while Gözat shows)
            QShortcut(QKeySequence(keys), self, activated=self.edit_path)
        self.refresh_btn = BarButton("", "arrow.clockwise", tooltip=t("nav.refresh"))
        self.refresh_btn.clicked.connect(self.refresh)
        self.path_bar = glass.GlassSurface(self, kind=glass.BAR)
        self.path_bar.setProperty("stretch", True)
        lay = QHBoxLayout(self.path_bar)
        lay.setContentsMargins(14, 4, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(IconView("folder", "MUTED", 18))
        lay.addWidget(self.crumbs, 1)
        lay.addWidget(self.path_edit, 1)
        lay.addWidget(self.refresh_btn)

        self.count_label = ElidedLabel()
        self.count_label.setObjectName("BarText")
        self.all_btn = BarButton(t("browser.select_all"), "checkmark.circle")
        self.all_btn.clicked.connect(self.select_all)
        self.none_btn = BarButton(t("browser.deselect_all"), "xmark.circle")
        self.none_btn.clicked.connect(self.select_none)
        self.files_btn = BarButton(t("browser.add_files"), "plus")
        self.files_btn.setToolTip(t("browser.add_files_tip"))
        self.files_btn.clicked.connect(self.addFilesRequested)
        self.footer = ToolGroup(self.count_label, self.all_btn, self.none_btn, self.files_btn, parent=self,
                                margins=(18, 4, 4, 4))
        lay = self.footer.layout()
        lay.setSpacing(2)
        lay.insertSpacing(1, 10)
        lay.insertWidget(2, separator())
        lay.insertSpacing(3, 6)
        for w in (self.nav_bar, self.path_bar, self.footer):
            w.hide()

        self.model = FolderModel(selection, folders, loader, self)
        self.grid = PhotoGrid(self.model, selection, folders, TileDelegate(loader, folders, tile, self), self)
        self.grid.folderActivated.connect(self.navigate)
        self.grid.photoFocused.connect(self.photoFocused)
        self.grid.previewRequested.connect(self.previewRequested)
        self.grid.cropRequested.connect(self.cropRequested)
        self.grid.selectAllRequested.connect(self.select_all)
        self.empty = EmptyState("folder")
        self.empty_holder = QWidget()
        holder = QVBoxLayout(self.empty_holder)
        holder.setContentsMargins(40, 0, 40, 0)
        holder.addWidget(self.empty)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.empty_holder)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.stack, 1)

        # Folder states are read while painting: on a change only the visible tiles are repainted
        # (emitting "data changed" for every row was slow in views with thousands of folders).
        folders.changed.connect(self.grid.viewport().update)
        # The count label computes the state of every folder; it is updated sparingly while scanning.
        self._counts_timer = QTimer(self)
        self._counts_timer.setSingleShot(True)
        self._counts_timer.setInterval(250)
        self._counts_timer.timeout.connect(self._update_counts)
        folders.changed.connect(self._schedule_counts)
        self._update_nav()

    # ------------------------------------------------------------------ chrome

    def header_widgets(self) -> list[QWidget]:
        return [self.nav_bar, self.path_bar]

    def scroll_bar(self):
        """The bar whose position shows whether the content runs under the toolbars."""
        return self.grid.verticalScrollBar()

    def footer_widget(self) -> QWidget:
        return self.footer

    def set_insets(self, top: int, bottom: int) -> None:
        self.grid.set_insets(top, bottom)
        self.empty_holder.layout().setContentsMargins(40, top, 40, bottom)

    # ------------------------------------------------------------------ navigation

    def edit_path(self) -> None:
        """Turns the path bar into a field to type a path into: Enter opens it; Esc or a click
        elsewhere goes back to the steps."""
        self.path_edit.setText(self.folder)
        self.crumbs.hide()
        self.path_edit.show()
        self.path_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.path_edit.selectAll()

    def _path_entered(self) -> None:
        path = os.path.expandvars(self.path_edit.text().strip().strip('"'))
        if path and not os.path.isdir(folder_path(path)):
            # The typed path stays in the field to be corrected, as in Explorer.
            self._show_missing(path)
            self.path_edit.selectAll()
            return
        self._not_found = False  # (the new folder replaces it)
        self._end_path_edit()
        self.navigate(path)

    def _end_path_edit(self) -> None:
        if not self.path_edit.isVisible():
            return
        focused = self.path_edit.hasFocus()
        self.path_edit.hide()  # (its focus-out comes back here and finds it hidden)
        self.crumbs.show()
        if self._not_found:
            self._not_found = False
            self._load()  # the open folder again, instead of "not found"
        if focused:
            self.grid.setFocus(Qt.FocusReason.OtherFocusReason)

    def navigate(self, path: str, record: bool = True) -> bool:
        """Opens a folder ("" is This PC); False if it is not there."""
        path = folder_path(path)
        if path and not os.path.isdir(path):
            self._show_missing(path)
            return False
        self.loader.clear_queue()
        self.folders.counter.clear_queue()
        self.grid.reset_anchor()
        self.folder = path
        if record:
            del self._history[self._pos + 1:]
            if not self._history or os.path.normcase(self._history[-1]) != os.path.normcase(path):
                self._history.append(path)
            self._pos = len(self._history) - 1
        self.crumbs.set_path(path)
        self._load()
        self._update_nav()
        self.folderChanged.emit(path)
        return True

    def _load(self) -> None:
        self._not_found = False
        if not self.folder:
            self.model.set_entries([])
            self._show_empty(t("browser.choose_folder"))
            self._update_counts()
            return
        try:
            entries = scan_folder(self.folder)
        except OSError as exc:
            self.model.set_entries([])
            self._show_empty(t("browser.cannot_open"), str(exc.strerror or exc), "warning")
            self._update_counts()
            return
        self.model.set_entries(entries)
        # The open folder itself is counted too: the tree uses it to show whether all of it is selected.
        self.folders.count(self.folder)
        if entries:
            self.stack.setCurrentWidget(self.grid)
            self.grid.scrollToTop()
        else:
            self._show_empty(
                t("browser.no_media"),
                t("browser.formats"))
        self._update_counts()

    def _show_empty(self, title: str, text: str = "", icon: str = "folder") -> None:
        self.empty.set_content(icon, title, text)
        self.stack.setCurrentWidget(self.empty_holder)

    def _show_missing(self, path: str) -> None:
        """A folder that is not there (any more): the page says so, and the folder shown before is put
        away, so "Select all" and the counts cannot act on files that are out of sight."""
        self._not_found = True
        self.model.set_entries([])
        self._show_empty(t("browser.not_found"), path, "warning")
        self._update_counts()

    def refresh(self) -> None:
        self.folders.invalidate_counts()
        self._load()

    def go_up(self) -> None:
        if self.folder:
            parent = os.path.dirname(self.folder)
            # Above a drive's root there is no folder: "This PC" lists the drives.
            self.navigate("" if os.path.normcase(parent) == os.path.normcase(self.folder) else parent)

    def go_back(self) -> None:
        if self._pos > 0:
            self._pos -= 1
            if not self.navigate(self._history[self._pos], record=False):
                self._pos += 1  # (the folder is gone: the history stays where it was)
                self._update_nav()

    def go_forward(self) -> None:
        if self._pos < len(self._history) - 1:
            self._pos += 1
            if not self.navigate(self._history[self._pos], record=False):
                self._pos -= 1
                self._update_nav()

    def _update_nav(self) -> None:
        self.back_btn.setEnabled(self._pos > 0)
        self.fwd_btn.setEnabled(self._pos < len(self._history) - 1)
        self.up_btn.setEnabled(bool(self.folder))

    # ------------------------------------------------------------------- selection

    def select_all(self) -> None:
        """Selects everything in this folder: photos/videos and subfolders (with their contents)."""
        self.selection.add_entries(listed(self.model.photos()))
        dirs = [e.path for e in self.model.dirs()]
        if dirs:
            self.folders.set_selected(dirs, True)

    def select_none(self) -> None:
        self.selection.remove([e.path for e in self.model.photos()])
        dirs = [e.path for e in self.model.dirs()]
        if dirs:
            self.folders.set_selected(dirs, False)

    def _schedule_counts(self) -> None:
        if not self._counts_timer.isActive():
            self._counts_timer.start()

    def _update_counts(self) -> None:
        photos = self.model.photos()
        dirs = self.model.dirs()
        chosen = sum(1 for e in photos if self.selection.has_key(e.key))  # (after every change: no path work)
        states = self.folders.states(self.folder, dirs)
        dirs_marked = sum(1 for s in states if s != NONE)
        parts = []
        if photos:
            videos = sum(1 for e in photos if e.video)
            kinds = [t(f"count.{kind}.plain", n=n) for n, kind in ((len(photos) - videos, "photos"), (videos, "videos"))
                     if n]
            parts.append(", ".join(kinds) + (" " + t("browser.chosen_paren", n=chosen) if chosen else ""))
        if dirs:
            marked = " " + t("browser.chosen_paren", n=dirs_marked) if dirs_marked else ""
            parts.append(t("count.folders", n=len(dirs)) + marked)
        self.count_label.setText(" · ".join(parts) or t("browser.empty_folder"))
        self.count_label.setVisible(bool(self.folder) and not self._not_found)
        everything = chosen == len(photos) and all(s in (FULL, SCANNING) for s in states)
        self.all_btn.setEnabled(bool(photos or dirs) and not everything)
        self.none_btn.setEnabled(chosen > 0 or dirs_marked > 0)
        if dirs:
            self.all_btn.setToolTip(t("browser.select_all_tip_dirs"))
            self.none_btn.setToolTip(t("browser.deselect_tip_dirs"))
        else:
            self.all_btn.setToolTip(t("browser.select_all_tip"))
            self.none_btn.setToolTip(t("browser.deselect_tip"))
        self.footer.adjustSize()
        parent = self.footer.parentWidget()
        if parent is not self and hasattr(parent, "relayout"):
            parent.relayout()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Backspace and not self.path_edit.hasFocus():
            self.go_up()
            return
        if event.key() == Qt.Key.Key_F5:
            self.refresh()
            return
        super().keyPressEvent(event)
