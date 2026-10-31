"""The Selected tab: photos and videos gathered from different folders, with a live
preview of the names they will get after processing.

Its toolbars float above it as glass (see `chrome`): the preview, duplicates and removal
buttons, rotation, and (when ordering by hand) moving rows. The "Bilgi" (info) column tells
in a few words what will happen to each file, with small drawn icons.
"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QObject,
    QPoint,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetricsF,
    QKeySequence,
    QPainter,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..core.formats import DISPLAY_NAME
from ..core.photoinfo import DATE_MTIME, DATE_SOURCE_LABELS, DATE_VIDEO
from ..core.planner import PlanEntry
from ..core.processor import RAW_EDIT_NEEDS_JPEG, RAW_NEEDS_JPEG, RAW_RESIZE_NEEDS_JPEG
from ..core.video.process import VIDEO_EDIT_NEEDS_MP4
from ..i18n import date_time, t
from ..winutil import reveal
from . import icons, number, theme
from .controls import BarButton, IconView, ToolGroup
from .selection import moved
from .thumbs import LOADING, ThumbnailLoader
from .widgets import ElidedHeader, ElidedLabel, EmptyState, menu

_MIME = "application/x-fotodit-rows"

UNDATED_TIP = "selected.undated_tip"

COL_THUMB, COL_POS, COL_NAME, COL_TARGET, COL_DATE, COL_NOTE = range(6)
THUMB_W, THUMB_H = 60, 44
HEADERS = ["", "selected.col.order", "selected.col.current", "selected.col.target", "selected.col.date",
           "selected.col.info"]
NoteRole = Qt.ItemDataRole.UserRole + 10  # the info column as (icon, text, color token) pieces

_DISPLAY = int(Qt.ItemDataRole.DisplayRole)
_DECORATION = int(Qt.ItemDataRole.DecorationRole)
_TOOLTIP = int(Qt.ItemDataRole.ToolTipRole)
_FOREGROUND = int(Qt.ItemDataRole.ForegroundRole)
_FONT = int(Qt.ItemDataRole.FontRole)
_ALIGN = int(Qt.ItemDataRole.TextAlignmentRole)
_NOTE = int(NoteRole)


def _date_tip(e: PlanEntry) -> str:
    """Where the date comes from; for videos also how the local time was determined (UTC, file name …)."""
    if e.date_source == DATE_MTIME:
        return t(UNDATED_TIP)
    video = getattr(e.photo.info, "video", None)
    note = getattr(video, "taken_note", "") if e.date_source == DATE_VIDEO else ""
    return t("selected.date_source", source=note or t(DATE_SOURCE_LABELS.get(e.date_source, e.date_source)))


class PlanModel(QAbstractTableModel):
    rowsDropped = Signal(list, int)  # moved rows, target row

    def __init__(self, loader: ThumbnailLoader, parent=None):
        super().__init__(parent)
        self.loader = loader
        self.plan: list[PlanEntry] = []
        self.in_place = False
        self.base = ""
        self.manual = False
        self.multi_folder = False
        self.source_base: str | None = None  # common parent folder when selected from several folders
        self.copies: dict[str, str] = {}  # normcase path → the name of the file it duplicates ("Kopyaları bul")
        self._rows: dict[str, int] = {}
        self._scaled: dict[tuple[str, float], object] = {}
        self._cell_cache: dict[int, dict[tuple[int, int], object]] = {}
        self._muted = QColor(theme.MUTED)
        self._bold = QFont()
        self._bold.setWeight(QFont.Weight.Medium)
        self._digits = theme.tabular(QFont())  # positions and dates line up down the column
        loader.ready.connect(self._thumb_ready)
        theme.changes().changed.connect(self._theme_changed)

    def _theme_changed(self) -> None:
        """The cached cell colors belong to the old theme."""
        self._muted = QColor(theme.MUTED)
        self._cell_cache.clear()
        if self.plan:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.plan) - 1, len(HEADERS) - 1))

    def set_plan(self, plan: list[PlanEntry], in_place: bool, base: str, manual: bool) -> None:
        self.beginResetModel()
        self._cell_cache.clear()
        self.plan, self.in_place, self.base, self.manual = plan, in_place, base, manual
        self._rows = {os.path.normcase(e.photo.path): i for i, e in enumerate(plan)}
        folders = {e.photo.folder_key: e.photo.folder for e in plan}
        self.multi_folder = len(folders) > 1
        self.source_base = None
        if self.multi_folder:
            try:
                self.source_base = os.path.commonpath(list(folders.values()))
            except ValueError:  # different drives: the full path is shown
                self.source_base = None
        self.endResetModel()

    def set_copies(self, copies: dict[str, str]) -> None:
        """Marks the duplicates found by "Kopyaları bul" (normcase path → the original's name)."""
        self.copies = copies
        self._cell_cache.clear()
        if self.plan:
            self.dataChanged.emit(self.index(0, COL_NOTE), self.index(len(self.plan) - 1, COL_NOTE))

    def source_text(self, e: PlanEntry) -> str:
        """Current file: its name if selected from a single folder, otherwise its path
        relative to the common parent folder (e.g. 2019\\Holiday\\IMG_0001.jpg)."""
        if not self.multi_folder:
            return e.photo.name
        if self.source_base is None:
            return e.photo.path
        return os.path.relpath(e.photo.path, self.source_base)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.plan)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return t(HEADERS[section]) if HEADERS[section] else ""
        if orientation == Qt.Orientation.Horizontal and role == _ALIGN and section == COL_POS:
            return int(Qt.AlignmentFlag.AlignCenter)  # over the centered numbers
        return None

    def _thumb(self, e: PlanEntry):
        key = (e.photo.path, e.photo.mtime, e.edit)
        pm = self._scaled.get(key)
        if pm is None:
            src = self.loader.get(e.photo.path, e.photo.mtime, e.edit)
            if src is LOADING or src is None:
                return None
            dpr = src.devicePixelRatio() or 1.0
            pm = src.scaled(int(THUMB_W * dpr), int(THUMB_H * dpr), Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
            pm.setDevicePixelRatio(dpr)
            if len(self._scaled) > 3000:
                self._scaled.clear()
            self._scaled[key] = pm
        return pm

    def row_of(self, path: str) -> int | None:
        """The row of a file in the plan, or None if it is not in it."""
        return self._rows.get(os.path.normcase(path))

    def describe(self, path: str) -> tuple[str, str | None] | None:
        """(Name/location after processing, skip reason) if the path is in the plan; for the preview's info bar."""
        row = self.row_of(path)
        if row is None:
            return None
        e = self.plan[row]
        return self.target_text(e), e.skip_reason

    def target_text(self, e: PlanEntry) -> str:
        if e.skip_reason:
            return t("selected.skipped")
        if self.in_place:
            if not e.moves:
                return t("selected.name_kept")
            rel = os.path.relpath(e.target_path, e.photo.folder)
            return rel if not rel.startswith("..") else e.target_path
        try:
            return os.path.relpath(e.target_path, self.base)
        except ValueError:
            return e.target_path

    def _note(self, e: PlanEntry) -> tuple[list[tuple[str | None, str, str]], str, str | None]:
        """(pieces as (icon, text, color token), explanation, color token of the whole note)"""
        info = e.photo.info
        if e.skip_reason and e.skip_reason in (t(RAW_NEEDS_JPEG), t(RAW_EDIT_NEEDS_JPEG), t(RAW_RESIZE_NEEDS_JPEG)):
            return [("error", t("selected.raw_needs_jpg"), "DANGER")], e.skip_reason, "DANGER"
        if e.skip_reason and e.skip_reason == t(VIDEO_EDIT_NEEDS_MP4):
            return [("error", t("selected.video_needs_mp4"), "DANGER")], e.skip_reason, "DANGER"
        if e.skip_reason:
            return [("error", t("folder.unreadable"), "DANGER")], e.skip_reason, "DANGER"
        pieces: list[tuple[str | None, str, str]] = []
        long: list[str] = []
        color = None
        video = getattr(info, "video", None)
        if video is not None:
            pieces.append(("film", video.short(), "MUTED"))
            long.append(t("selected.note.video", info=video.short()))
        copy_of = self.copies.get(os.path.normcase(e.photo.path))
        if copy_of:
            pieces.append(("duplicate", t("selected.copy"), "WARN"))
            long.append(t("selected.note.copy", name=copy_of))
            color = "WARN"
        if e.edit is not None:
            pieces.append(("pencil", e.edit.short(), "EDIT"))
            long.append(t("selected.note.edit", edit=e.edit.describe(future=True)))
            color = color or "EDIT"
        if info and info.has_gps:
            pieces.append(("location", "GPS", "WARN"))
            long.append(t("selected.note.gps_video" if video is not None else "selected.note.gps_photo"))
            color = "WARN"
        leak = getattr(e, "name_leak", None)
        if leak:  # (counted on the page's hint line; here only explained)
            long.append(t("selected.note.name_leak", leak=t(leak)))
        src = info.fmt if info else None
        plan = e.video_plan
        if plan is not None:
            if plan.encode_video:
                target = "selected.note.to_hevc" if plan.codec == "hevc" else "selected.note.to_h264"
                pieces.append((None, t(target), "MUTED"))
                how = t("selected.note.gpu", encoder=plan.encoder.label) if plan.encoder else t("selected.note.slow")
                long.append(t("selected.note.reencode", reasons=", ".join(plan.reasons), how=how))
            elif e.out_fmt and src and e.out_fmt != src:
                pieces.append((None, f"{DISPLAY_NAME[src]} → {DISPLAY_NAME[e.out_fmt]}", "MUTED"))
                long.append(t("selected.note.remux", fmt=DISPLAY_NAME[e.out_fmt]))
            if plan.tonemap:
                long.append(t("selected.note.tonemap", hdr=plan.tonemap))
            elif plan.encode_video and plan.codec == "hevc" and video.hdr:
                long.append(t("selected.note.hdr_kept", hdr=video.hdr))
            if plan.audio_encode:
                long.append(t("selected.note.audio_aac"))
        elif e.out_fmt and src and e.out_fmt != src:
            pieces.append((None, f"{DISPLAY_NAME[src]} → {DISPLAY_NAME[e.out_fmt]}", "MUTED"))
            long.append(t("selected.note.convert", src=DISPLAY_NAME[src], dst=DISPLAY_NAME[e.out_fmt]))
        if e.renamed_for_conflict:
            pieces.append(("warning", t("selected.clash"), "WARN"))
            long.append(t("selected.note.clash"))
            color = "WARN"
        return pieces, "\n".join(long), color

    def _cells(self, row: int) -> dict[tuple[int, int], object]:
        """The row's display values, (column, role) → value. Computed once: Qt calls
        data() many times per cell on every paint; these calls must be cheap so that
        painting stays smooth with large selections and while info is read in the background."""
        cells = self._cell_cache.get(row)
        if cells is not None:
            return cells
        e = self.plan[row]
        pieces, long, color = self._note(e)
        muted = self._muted
        cells = {
            (COL_POS, _DISPLAY): str(e.position),
            (COL_NAME, _DISPLAY): self.source_text(e),
            (COL_TARGET, _DISPLAY): self.target_text(e),
            (COL_DATE, _DISPLAY): date_time(e.date),
            (COL_NOTE, _DISPLAY): " · ".join(text for _icon, text, _c in pieces),
            (COL_NOTE, _NOTE): pieces,
            (COL_NAME, _TOOLTIP): e.photo.path,
            (COL_TARGET, _TOOLTIP): e.target_path,
            (COL_DATE, _TOOLTIP): _date_tip(e),
            (COL_NOTE, _TOOLTIP): long or None,
            (COL_POS, _FOREGROUND): muted,
            (COL_DATE, _FOREGROUND): muted if e.date_source == DATE_MTIME else None,
            (COL_NOTE, _FOREGROUND): QColor(getattr(theme, color)) if color else muted,
            (COL_TARGET, _FONT): self._bold if (e.moves or not self.in_place) else None,
            (COL_POS, _FONT): self._digits,
            (COL_DATE, _FONT): self._digits,
            (COL_POS, _ALIGN): int(Qt.AlignmentFlag.AlignCenter),
        }
        if len(self._cell_cache) > 5000:
            self._cell_cache.clear()
        self._cell_cache[row] = cells
        return cells

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        col = index.column()
        role = int(role)
        if role == _DECORATION:
            return self._thumb(self.plan[index.row()]) if col == COL_THUMB else None
        return self._cells(index.row()).get((col, role))

    def paths(self, rows) -> list[str]:
        return [self.plan[r].photo.path for r in rows if 0 <= r < len(self.plan)]

    def _thumb_ready(self, path: str) -> None:
        row = self._rows.get(os.path.normcase(path))
        if row is not None:
            idx = self.index(row, COL_THUMB)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    # ---------------------------------------------- drag and drop (manual order)

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        if self.manual:
            if index.isValid():
                return base | Qt.ItemFlag.ItemIsDragEnabled
            return base | Qt.ItemFlag.ItemIsDropEnabled
        return base

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    def mimeTypes(self):
        return [_MIME]

    def mimeData(self, indexes):
        # The dragged files, not their rows: the list can be planned anew while they are being dragged.
        rows = sorted({i.row() for i in indexes})
        mime = QMimeData()
        mime.setData(_MIME, "\n".join(self.paths(rows)).encode("utf-8"))
        return mime

    def dropMimeData(self, data, action, row, column, parent):
        if not data.hasFormat(_MIME):
            return False
        found = (self.row_of(p) for p in bytes(data.data(_MIME)).decode("utf-8").split("\n") if p)
        rows = sorted(r for r in found if r is not None)
        target = row if row >= 0 else (parent.row() if parent.isValid() else len(self.plan))
        if rows:
            self.rowsDropped.emit(rows, target)
        return False  # keep the view from removing the rows itself; a new plan will follow


class RowDelegate(QStyledItemDelegate):
    """Rows with a rounded highlight across the whole row, thumbnails with rounded corners and the info
    column's drawn icons. A selected row is tinted, with a peach edge at its start, as the current row
    of Artapa's admin tables."""

    def paint(self, painter: QPainter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        hover = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        col = index.column()
        last = index.model().columnCount() - 1
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if selected or hover:
            r = QRectF(opt.rect).adjusted(0, 2, 0, -2)
            if col == 0:
                r.adjust(6, 0, 0, 0)
            if col == last:
                r.adjust(0, 0, -6, 0)
            color = QColor(255, 255, 255, 0x12) if selected else QColor(theme.ITEM_HOVER)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            radius = float(theme.RADIUS_WELL)
            # Only the row's outer corners are rounded: the cells in between are plain rectangles.
            path_rect = QRectF(r)
            if col != 0:
                path_rect.adjust(-radius, 0, 0, 0)
            if col != last:
                path_rect.adjust(0, 0, radius, 0)
            painter.setClipRect(r)
            painter.drawRoundedRect(path_rect, radius, radius)
            if selected and col == 0:
                painter.setBrush(QColor(theme.PEACH))
                painter.drawRoundedRect(QRectF(r.left() + 4.5, r.top() + 9, 2.0, r.height() - 18), 1.0, 1.0)
            painter.setClipping(False)
        opt.state &= ~QStyle.StateFlag.State_Selected
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        if col == COL_THUMB:
            pm = index.data(Qt.ItemDataRole.DecorationRole)
            if pm is not None:
                dpr = pm.devicePixelRatio() or 1.0
                w, h = pm.width() / dpr, pm.height() / dpr
                target = QRectF(opt.rect.center().x() - w / 2 + 1, opt.rect.center().y() - h / 2, w, h)
                from .widgets import rounded

                painter.setClipPath(rounded(target, 5))
                painter.drawPixmap(target, pm, QRectF(pm.rect()))
                painter.setClipping(False)
            painter.restore()
            return
        if col == COL_NOTE:
            self._paint_note(painter, opt, index)
            painter.restore()
            return
        painter.restore()
        opt.backgroundBrush = QColor(0, 0, 0, 0)
        super().paint(painter, opt, index)

    def _paint_note(self, painter: QPainter, opt, index) -> None:
        pieces = index.data(NoteRole) or []
        rect = QRectF(opt.rect).adjusted(8, 0, -8, 0)
        fm = QFontMetricsF(opt.font)
        painter.setFont(opt.font)
        x = rect.left()
        for i, (icon, text, color) in enumerate(pieces):
            c = QColor(getattr(theme, color, theme.MUTED))
            if i:
                sep = "  ·  "
                painter.setPen(QColor(theme.MUTED))
                width = fm.horizontalAdvance(sep)
                if x + width > rect.right():
                    break
                painter.drawText(QRectF(x, rect.top(), width, rect.height()), Qt.AlignmentFlag.AlignVCenter, sep)
                x += width
            if icon:
                if x + 16 > rect.right():
                    break
                icons.paint(painter, QRectF(x, rect.center().y() - 8, 16, 16), icon, c)
                x += 20
            available = rect.right() - x
            if available <= 8:
                break
            shown = fm.elidedText(text, Qt.TextElideMode.ElideRight, available)
            painter.setPen(c)
            painter.drawText(QRectF(x, rect.top(), available, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, shown)
            x += fm.horizontalAdvance(shown)
            if shown != text:
                break


class _DuplicateSearch(QObject):
    """Finds duplicates in the background ("Kopyaları bul")."""

    done = Signal(object)  # list of DuplicateGroup, or an Exception

    def __init__(self, paths: list[str], parent: QObject | None = None):
        super().__init__(parent)
        self.paths = paths
        self.cancel = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="duplicates", daemon=True).start()

    def _run(self) -> None:
        try:
            from ..core.duplicates import find_duplicates

            result = find_duplicates(self.paths, cancelled=self.cancel.is_set)
        except Exception as exc:  # noqa: BLE001 - reported in the UI
            result = exc
        try:
            self.done.emit(result)
        except RuntimeError:  # the window closed meanwhile
            pass


class SelectedPage(QWidget):
    removeRequested = Signal(list)
    removeFolderRequested = Signal(str)  # remove the photos and videos directly in this folder
    previewRequested = Signal(int)  # large preview, starting row
    cropRequested = Signal(int)  # large preview in crop mode, row
    rotateRequested = Signal(list, int)  # paths, 90° step (+1 right, -1 left)
    flipRequested = Signal(list)  # paths, flipped left to right
    resetEditsRequested = Signal(list)  # paths: remove their edits
    clearRequested = Signal()
    reorderRequested = Signal(list)  # new order of all paths
    photoFocused = Signal(str)
    message = Signal(str)  # a short message for the window (duplicates found …)

    def __init__(self, loader: ThumbnailLoader, parent=None):
        super().__init__(parent)
        self.model = PlanModel(loader, self)
        self.model.rowsDropped.connect(self._rows_dropped)
        self._search: _DuplicateSearch | None = None

        self.preview_btn = BarButton(t("selected.large_preview"), "eye")
        self.preview_btn.setToolTip(t("selected.large_preview_tip"))
        self.preview_btn.clicked.connect(self._preview_current)
        self.duplicates_btn = BarButton(t("selected.find_dupes"), "duplicate")
        self.duplicates_btn.setToolTip(t("selected.find_dupes_tip"))
        self.duplicates_btn.clicked.connect(self.find_duplicates)
        self.remove_btn = BarButton(t("selected.remove"), "minus.circle")
        self.remove_btn.setToolTip(t("selected.remove_tip"))
        self.remove_btn.clicked.connect(self._remove_selected)
        self.clear_btn = BarButton(t("selected.clear"), "xmark")
        self.clear_btn.setToolTip(t("selected.clear_tip"))
        self.clear_btn.clicked.connect(self.clearRequested)
        self.left_btn = BarButton("", "rotate.left", tooltip=t("selected.left_tip"),
                                  accessible=t("common.rotate_left"))
        self.left_btn.clicked.connect(lambda: self._rotate(-1))
        self.right_btn = BarButton("", "rotate.right", tooltip=t("selected.right_tip"),
                                   accessible=t("common.rotate_right"))
        self.right_btn.clicked.connect(lambda: self._rotate(1))
        self.flip_btn = BarButton("", "flip", tooltip=t("selected.flip_tip"),
                                  accessible=t("common.flip"))
        self.flip_btn.clicked.connect(lambda: self._flip())
        self.up_btn = BarButton("", "arrow.up", tooltip=t("selected.up_tip"), accessible=t("selected.up"))
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn = BarButton("", "arrow.down", tooltip=t("selected.down_tip"), accessible=t("selected.down"))
        self.down_btn.clicked.connect(lambda: self._move(1))
        self.actions_bar = ToolGroup(self.preview_btn, self.duplicates_btn, self.remove_btn, self.clear_btn,
                                     parent=self)
        self.rotate_bar = ToolGroup(self.left_btn, self.right_btn, self.flip_btn, parent=self)
        self.order_bar = ToolGroup(self.up_btn, self.down_btn, parent=self)
        self.hint = ElidedLabel()
        self.hint.setObjectName("Muted")
        self.leak_icon = IconView("warning", "WARN", 16)
        self.leak_hint = ElidedLabel()
        self.leak_hint.setObjectName("Warn")
        self.hint_bar = QWidget(self)
        lay = QHBoxLayout(self.hint_bar)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(6)
        lay.addWidget(self.hint)
        lay.addSpacing(10)
        lay.addWidget(self.leak_icon)
        lay.addWidget(self.leak_hint)
        for w in (self.actions_bar, self.rotate_bar, self.order_bar, self.hint_bar):
            w.hide()

        self.table = QTableView()
        self.table.setHorizontalHeader(ElidedHeader(self.table))  # a narrow column's title ends in "…"
        self.table.setModel(self.model)
        self.table.setItemDelegate(RowDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setWordWrap(False)
        self.table.setMouseTracking(True)
        self.table.setFrameShape(QTableView.Shape.NoFrame)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)  # keep the file name visible on long paths
        self.table.setIconSize(QSize(THUMB_W, THUMB_H))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(THUMB_H + 12)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(COL_THUMB, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_POS, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_DATE, QHeaderView.ResizeMode.Fixed)
        for column in (COL_NAME, COL_TARGET, COL_NOTE):  # the three texts share the width
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(40)  # (the text columns keep TEXT_MIN: see _fit_columns)
        self.table.setColumnWidth(COL_THUMB, THUMB_W + 18)
        self.table.setColumnWidth(COL_POS, 52)
        # The dates in equal-width digits ("12.05.2024 14:30", "2024-05-12 14:30") and the cell's padding.
        date_w = QFontMetricsF(theme.tabular(self.table.font())).horizontalAdvance("00.00.0000 00:00")
        self.table.setColumnWidth(COL_DATE, max(124, int(date_w + 0.99) + 24))
        self.table.setDragDropOverwriteMode(False)
        self.table.selectionModel().currentRowChanged.connect(self._current_changed)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.table, activated=self._remove_selected)
        for keys, delta in (("Ctrl+R", 1), ("Ctrl+L", -1)):  # only while the table has focus
            QShortcut(QKeySequence(keys), self.table, activated=lambda d=delta: self._rotate(d),
                      context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Ctrl+H"), self.table, activated=lambda: self._flip(),
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.table.installEventFilter(self)  # Space: large preview
        self.table.doubleClicked.connect(lambda index: self.previewRequested.emit(index.row()))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)

        self.empty = EmptyState(
            "photo", t("selected.empty_title"),
            t("selected.empty_text"))

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 0, 12, 0)
        self._layout.setSpacing(0)
        self._layout.addWidget(self.table, 1)
        self._layout.addWidget(self.empty, 1)
        self.set_plan([], False, "", False, "")

    # ------------------------------------------------------------------ chrome

    def header_widgets(self) -> list[QWidget]:
        return [self.actions_bar, self.rotate_bar, self.order_bar]

    def scroll_bar(self):
        """The bar whose position shows whether the content runs under the toolbars."""
        return self.table.verticalScrollBar()

    def footer_widget(self) -> QWidget:
        return self.hint_bar

    def set_insets(self, top: int, bottom: int) -> None:
        self._layout.setContentsMargins(12, top - 4, 12, bottom)

    # ------------------------------------------------------------------ plan

    def set_plan(self, plan: list[PlanEntry], in_place: bool, base: str, manual: bool, sort_label: str,
                 undated: int = 0) -> None:
        """`undated`: number of files put at the end of a sort by date because they have no capture date."""
        selected = set(self.model.paths(r.row() for r in self.table.selectionModel().selectedRows()))
        if self.model.copies:
            present = {os.path.normcase(e.photo.path) for e in plan}
            self.model.copies = {k: v for k, v in self.model.copies.items() if k in present}
        self.model.set_plan(plan, in_place, base, manual)
        self.table.setDragEnabled(manual)
        self.table.setAcceptDrops(manual)
        self.table.setDropIndicatorShown(manual)
        self.table.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove if manual else QAbstractItemView.DragDropMode.NoDragDrop
        )
        if selected:
            self._select_rows([row for row, e in enumerate(plan) if e.photo.path in selected])
        has = bool(plan)
        self.table.setVisible(has)
        self.empty.setVisible(not has)
        tip = ""
        if manual:
            self.hint.setText(t("selected.manual_hint"))
        elif has and undated:
            self.hint.setText(t("selected.order_undated", sort=sort_label, n=undated))
            tip = (t("selected.order_undated_tip"))
        elif has and sort_label:
            self.hint.setText(t("selected.order_hint", sort=sort_label))
        else:
            self.hint.setText("")
        self.hint.setToolTip(tip)
        leaks = sum(1 for e in plan if getattr(e, "name_leak", None))
        self.leak_hint.setText(t("selected.leaks", n=leaks) if leaks else "")
        self.leak_hint.setToolTip(t("selected.leaks_tip") if leaks else "")
        self.leak_icon.setVisible(bool(leaks))
        self.leak_hint.setVisible(bool(leaks))
        self.hint_bar.adjustSize()
        self._update_buttons()

    def show_loading(self) -> None:
        # Fixed text: if a count changed every second, the layout and table would be redrawn each time.
        self.hint.setText(t("selected.loading"))

    def _selected_rows(self) -> list[int]:
        return sorted(i.row() for i in self.table.selectionModel().selectedRows())

    def _select_rows(self, rows: list[int], clear: bool = False) -> None:
        """Selects the rows (in ascending order) in one step: one range per run of neighbouring rows and a
        single "selection changed". Row by row, each step would recount every range selected so far, and
        restoring a few thousand selected rows after a replan would hold the window for minutes."""
        selection = QItemSelection()
        last = self.model.columnCount() - 1
        start = end = None
        for row in [*rows, None]:
            if start is not None and row == end + 1:
                end = row
                continue
            if start is not None:
                selection.select(self.model.index(start, 0), self.model.index(end, last))
            start = end = row
        flags = QItemSelectionModel.SelectionFlag.Rows | (
            QItemSelectionModel.SelectionFlag.ClearAndSelect if clear else QItemSelectionModel.SelectionFlag.Select)
        self.table.selectionModel().select(selection, flags)

    def _update_buttons(self) -> None:
        rows = self._selected_rows()
        self.remove_btn.setEnabled(bool(rows))
        self.left_btn.setEnabled(bool(rows))
        self.right_btn.setEnabled(bool(rows))
        self.flip_btn.setEnabled(bool(rows))
        self.clear_btn.setEnabled(bool(self.model.plan))
        self.preview_btn.setEnabled(bool(self.model.plan))
        self.duplicates_btn.setEnabled(len(self.model.plan) > 1 and self._search is None)
        manual = self.model.manual
        self.order_bar.setProperty("hidden_by_page", not manual)
        if self.actions_bar.isVisible() or self.rotate_bar.isVisible():
            self.order_bar.setVisible(manual)
        self.up_btn.setEnabled(bool(rows) and rows[0] > 0)
        self.down_btn.setEnabled(bool(rows) and rows[-1] < len(self.model.plan) - 1)
        parent = self.hint_bar.parentWidget()
        if parent is not self and hasattr(parent, "relayout"):
            parent.relayout()

    def eventFilter(self, obj, event) -> bool:
        if (obj is self.table and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Space
                and not event.modifiers()):
            self._preview_current()
            return True
        if obj is self.table and event.type() == QEvent.Type.Resize:
            self._fit_columns()
        return super().eventFilter(obj, event)

    TEXT_MIN = 90  # the narrowest a text column gets

    def _fit_columns(self) -> None:
        """The three text columns share the width; when that would leave less than TEXT_MIN each, they
        keep TEXT_MIN and the table scrolls sideways instead of showing "I…g"."""
        header = self.table.horizontalHeader()
        fixed = sum(header.sectionSize(c) for c in (COL_THUMB, COL_POS, COL_DATE))
        narrow = self.table.viewport().width() - fixed < 3 * self.TEXT_MIN
        mode = QHeaderView.ResizeMode.Interactive if narrow else QHeaderView.ResizeMode.Stretch
        for column in (COL_NAME, COL_TARGET, COL_NOTE):
            if header.sectionResizeMode(column) != mode:
                header.setSectionResizeMode(column, mode)
                if narrow:
                    header.resizeSection(column, self.TEXT_MIN)

    def _preview_current(self) -> None:
        """Starts at the selected row (if several are selected, at the focused one or else the first)."""
        if self.model.plan:
            current = self.table.currentIndex()
            rows = self._selected_rows()
            if current.isValid() and (not rows or current.row() in rows):
                row = current.row()
            else:
                row = rows[0] if rows else 0
            self.previewRequested.emit(row)

    def _remove_selected(self) -> None:
        paths = self.model.paths(self._selected_rows())
        if paths:
            self.removeRequested.emit(paths)

    def _rotate(self, delta: int, paths: list[str] | None = None) -> None:
        paths = self.model.paths(self._selected_rows()) if paths is None else paths
        if paths:
            self.rotateRequested.emit(paths, delta)

    def _flip(self, paths: list[str] | None = None) -> None:
        paths = self.model.paths(self._selected_rows()) if paths is None else paths
        if paths:
            self.flipRequested.emit(paths)

    def _move(self, delta: int) -> None:
        """Moves the highlighted rows one place up or down in the list as it is shown. (Files that will be
        skipped are listed last whatever their place in the selection, so a step taken in the selection's
        order could pass one of them and change nothing on screen.)"""
        rows = self._selected_rows()
        if rows:
            order = [e.photo.path for e in self.model.plan]
            self.reorderRequested.emit(moved(order, set(self.model.paths(rows)), delta))

    def _rows_dropped(self, rows: list[int], target: int) -> None:
        order = [e.photo.path for e in self.model.plan]
        moving = [order[r] for r in rows]
        chosen = set(rows)
        rest = [p for i, p in enumerate(order) if i not in chosen]
        insert_at = target - sum(1 for r in rows if r < target)
        insert_at = max(0, min(insert_at, len(rest)))
        self.reorderRequested.emit(rest[:insert_at] + moving + rest[insert_at:])

    # ------------------------------------------------------------------ duplicates

    def find_duplicates(self) -> None:
        """Looks for duplicates in the background; the copies (all but the first of each group) are
        marked in the info column and selected, so "Listeden çıkar" removes them."""
        paths = [e.photo.path for e in self.model.plan]
        if len(paths) < 2 or self._search is not None:
            return
        search = self._search = _DuplicateSearch(paths, self)
        search.done.connect(lambda result: self._duplicates_found(search, result))
        self.duplicates_btn.setEnabled(False)
        self.message.emit(t("selected.dupes_searching"))
        search.start()

    def cancel_duplicates(self) -> None:
        """Stops a search for duplicates that is still running (a job starts, the list is emptied, the
        window closes): it would keep opening the files, and its result would no longer fit the list."""
        if self._search is not None:
            self._search.cancel.set()
            self._search = None
            self._update_buttons()

    def _duplicates_found(self, search: _DuplicateSearch, result) -> None:
        if search is not self._search:  # (a search that was cancelled)
            return
        self._search = None
        self._update_buttons()
        if isinstance(result, Exception):
            self.message.emit(t("selected.dupes_failed", error=result))
            return
        # Files taken off the list while the search ran are left out of its groups.
        present = {os.path.normcase(e.photo.path) for e in self.model.plan}
        copies: dict[str, str] = {}
        for group in result:
            listed = [p for p in group.paths if os.path.normcase(p) in present]
            for path in listed[1:]:
                copies[os.path.normcase(path)] = os.path.basename(listed[0])
        self.model.set_copies(copies)
        if not copies:
            self.message.emit(t("selected.dupes_none"))
            return
        rows = [row for row, e in enumerate(self.model.plan) if os.path.normcase(e.photo.path) in copies]
        self._select_rows(rows, clear=True)
        self.table.scrollTo(self.model.index(rows[0], 0))
        self.message.emit(t("selected.dupes_found", n=len(copies)))

    # ------------------------------------------------------------------ menu

    def _context_menu(self, pos: QPoint) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid() or index.row() >= len(self.model.plan):
            return
        e = self.model.plan[index.row()]
        rows = self._selected_rows()
        if index.row() not in rows:
            rows = [index.row()]
        # The menu acts on these files: while it is open the list can be planned anew (info arriving,
        # a scan adding files), and the same rows may then hold other files.
        paths = self.model.paths(rows)
        m = menu(self)
        count = f" ({number(len(rows))})" if len(rows) > 1 else ""
        right = QAction(icons.icon("rotate.right"), t("common.rotate_right") + count, m)
        right.triggered.connect(lambda: self._rotate(1, paths))
        left = QAction(icons.icon("rotate.left"), t("common.rotate_left") + count, m)
        left.triggered.connect(lambda: self._rotate(-1, paths))
        flip = QAction(icons.icon("flip"), t("common.flip") + count, m)
        flip.triggered.connect(lambda: self._flip(paths))
        crop = QAction(icons.icon("crop"), t("menu.crop"), m)
        crop.triggered.connect(lambda: self._crop(e.photo.path))
        m.addActions([right, left, flip, crop])
        edited = [self.model.plan[r].photo.path for r in rows if self.model.plan[r].edit is not None]
        if edited:
            reset = QAction(t("menu.remove_edits") + (f" ({number(len(edited))})" if len(edited) > 1 else ""), m)
            reset.triggered.connect(lambda: self.resetEditsRequested.emit(edited))
            m.addAction(reset)
        m.addSeparator()
        remove = QAction(t("menu.remove_from_list") + count, m)
        remove.triggered.connect(lambda: self.removeRequested.emit(paths))
        name = os.path.basename(e.photo.folder) or e.photo.folder
        folder = QAction(t("menu.remove_folder_items", name=name), m)
        folder.triggered.connect(lambda: self.removeFolderRequested.emit(e.photo.folder))
        show = QAction(t("menu.open_location"), m)
        show.triggered.connect(lambda: reveal(e.photo.path))
        m.addActions([remove, folder])
        m.addSeparator()
        m.addAction(show)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _crop(self, path: str) -> None:
        """Opens the large preview in crop mode at this file, at whichever row it stands now."""
        row = self.model.row_of(path)
        if row is not None:
            self.cropRequested.emit(row)

    def _current_changed(self, current: QModelIndex, _previous) -> None:
        if current.isValid() and current.row() < len(self.model.plan):
            self.photoFocused.emit(self.model.plan[current.row()].photo.path)
