"""Large preview ("Büyük önizleme"): shows photos in a full window and plays videos;
rotating and cropping are done here.

Opened from the Selected tab ("Seçilenler") it shows the selected photos (in list order),
opened from the Browse tab ("Gözat") the photos in the folder. Navigate with the arrow
keys, the edge arrows or the thumbnail strip at the bottom; Del removes the photo from
the selection (pressing it again puts it back in its old place). The image is fitted
to the window; double-click or Z switches to the 100% (actual pixels) view, where it
can be dragged with the mouse. F11: full screen, I: metadata panel, Esc: close. Holding O
shows the photo as it is in the file, without its edits (before/after).

The look: the photo sits on a dark stage, and behind it the stage carries a blurred, darkened
copy of the photo (Apple's background extension), so the controls floating above it as clear
glass bend real color. The title and the tools float at the top, the exposure or video
controls and the thumbnail strip at the bottom; the photo is fitted between them, so nothing
covers it. While a video plays, the controls step aside after a few seconds without the mouse.

Editing: R / L rotate by 90°, C opens crop & straighten mode, Ctrl+Z undoes. Edits are
not written to the file right away; they are applied when processing starts (see
`fotodit.core.edits`). In the preview the image itself stays unchanged and the edit is
applied while painting, so rotation and cropping show up instantly. Under a photo an
exposure slider is shown (see `exposure`): with an exposure set, a RAW is shown developed
from its RAW data instead of the camera's embedded JPEG, and other photos are shown adjusted.

Images are prepared at screen size in the background, and the previous and next ones
are preloaded; orientation and colors are corrected as in the JPG conversion
(see `fotodit.core.preview`).

For a video, a frame from its start is shown first; a click (or K / Enter) plays it
(see `player`). The frames are painted in the same image area, so rotation and
cropping are visible during playback too; in crop mode the video pauses.
Shift+← / → seeks 5 s, M mutes, double-click toggles full screen. The video bar's
"Buradan başlasın" / "Burada bitsin" buttons trim the video at the current position,
"Bölüm çıkar" removes a part: pressed at its start, then at its end (Esc drops the mark),
and "Sesi kaldır" removes the video's sound.
"""

from __future__ import annotations

import math
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, fields, replace

import numpy as np
from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QObject,
    QPointF,
    QRect,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QSizePolicy,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ..core.edits import MIN_CUT, Edit
from ..core.formats import DISPLAY_NAME, Fmt, is_raw_name, is_video_name
from ..core.photoinfo import DATE_MTIME, PhotoInfo, best_date, read_info
from ..core.preview import FULL_LIMIT, PreviewError, render
from ..i18n import capitalize, date_time, percent, t
from ..winutil import reveal
from . import glass, icons, number, size_text, theme
from .controls import BarButton, ToolGroup
from .crop import (
    ASPECTS,
    CURSORS,
    CropBar,
    CropSession,
    paint_crop,
    quarter_transform,
    upright_to_canvas,
)
from .edit_store import key_of
from .exposure import (
    ExposureBar,
    RawDeveloper,
    draft_image,
    draft_preview,
    exposed_image,
)
from .info_panel import InfoPanel
from .selection import Selection, norm
from .thumbs import LOADING, ThumbnailLoader, _to_qimage
from .widgets import ElidedLabel, paint_edit_badge, paint_video_badge

VIEW_BG = "#0C0C0C"
LIGHT = "#F2F2F2"
DIM = "#ABABAB"
CACHE_BYTES = 300 * 1024 * 1024  # memory limit for prepared images
MARGIN = 14  # the floating controls' distance from the window's edges
GAP = 10
NAME_MIN = 120  # the file name's narrowest width in the title capsule (the tools beside it show only icons sooner)
CHROME_IDLE_MS = 2600  # a playing video: the controls step aside after this long without the mouse
# The info panel's colors on the dark stage (it uses the theme's elsewhere).
MEDIA_COLORS = {"TEXT": LIGHT, "MUTED": DIM, "WARN": "#FFB340", "SUCCESS": LIGHT, "DANGER": "#FF6961"}

QSS = f"""
QDialog#Preview {{ background: {VIEW_BG}; }}
QDialog#Preview QLabel {{ color: {LIGHT}; }}
QDialog#Preview QLabel#PvMuted {{ color: {DIM}; }}
QDialog#Preview QLabel#PvWarn {{ color: #FFB340; }}
QDialog#Preview QLabel#PvTarget {{ color: {LIGHT}; }}
QDialog#Preview QLabel#PvDanger {{ color: #FF6961; }}
QDialog#Preview QLabel#PvEdit {{ color: {LIGHT}; }}
QDialog#Preview QLabel#PvTitle {{ color: {LIGHT}; font-weight: 700; }}
QDialog#Preview QLabel#PvChipOn {{
    background: {LIGHT}; color: {VIEW_BG}; border-radius: 13px; font-weight: 600;
}}
QDialog#Preview QLabel#PvChipOff {{
    background: rgba(255,255,255,0.16); color: {LIGHT}; border-radius: 13px;
}}
QDialog#Preview QComboBox {{
    background: rgba(255,255,255,0.12); color: {LIGHT}; border: 1px solid rgba(255,255,255,0.18);
    border-radius: 8px; padding: 4px 9px;
}}
QDialog#Preview QComboBox QAbstractItemView {{
    background: #2C2C2C; color: {LIGHT}; selection-background-color: #4D4D4D; border: 1px solid #3A3A3A;
}}
QListView#PvStrip {{ background: transparent; border: none; }}
QListView#PvStrip QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0 14px; }}
QListView#PvStrip QScrollBar::handle:horizontal {{
    background: rgba(255,255,255,0.28); border-radius: 3px; min-width: 40px; margin: 1px 0;
}}
QListView#PvStrip QScrollBar::add-line:horizontal, QListView#PvStrip QScrollBar::sub-line:horizontal {{ width: 0; }}
QDialog#Preview QScrollArea {{ background: transparent; }}
"""

EDIT_TIP = "preview.edit_tip"
MIN_KEPT = 0.3  # s: the shortest part of a video that trimming can keep
PHOTO_HINT = "preview.photo_hint"
VIDEO_HINT = "preview.video_hint"


@dataclass
class PreviewItem:
    path: str
    mtime: float
    name: str


# ------------------------------------------------------------------ loader


class _Emitter(QObject):
    done = Signal(str, object, object, str)  # key, QImage | None, PhotoInfo | None, error


class _Task(QRunnable):
    def __init__(self, key: str, path: str, box: int | None, want_info: bool, emitter: _Emitter):
        super().__init__()
        self.key, self.path, self.box, self.want_info, self.emitter = key, path, box, want_info, emitter

    def run(self) -> None:
        image, info, error = None, None, ""
        try:
            image = _to_qimage(render(self.path, self.box))
        except PreviewError as exc:
            error = str(exc)
        except MemoryError:
            error = t("preview.out_of_memory")
        except Exception as exc:  # noqa: BLE001 - a preview error must not close the window
            error = t("preview.failed", error=exc)
        if self.want_info:
            info = read_info(self.path)
        self.emitter.done.emit(self.key, image, info, error)


class PreviewLoader(QObject):
    """Prepares images in the background; the most recent request comes first, and results
    are cached within a memory limit. A photo's info is read with its image when it is not known
    yet (also for the photos prepared in advance), so it is there when the photo is shown."""

    ready = Signal(str)  # key

    def __init__(self, parent: QObject | None = None, budget: int = CACHE_BYTES):
        super().__init__(parent)
        self.budget = budget
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._cache: OrderedDict[str, tuple[QImage | None, str]] = OrderedDict()
        self._bytes = 0
        self._infos: dict[str, PhotoInfo] = {}
        self._pending: set[str] = set()
        self._priority = 0
        self._emitter = _Emitter()
        self._emitter.done.connect(self._on_done)

    @staticmethod
    def key(path: str, mtime: float, box: int | None) -> str:
        return f"{path}|{mtime}|{box or 0}"

    def get(self, path: str, mtime: float, box: int | None, *,
            urgent: bool = True) -> tuple[QImage | None, str] | None:
        """(image, error) if ready, otherwise None (and preparation starts)."""
        key = self.key(path, mtime, box)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit
        if key not in self._pending:
            self._pending.add(key)
            self._priority += 1
            # Urgent requests (the photo about to be shown) go ahead of preloads.
            self._pool.start(_Task(key, path, box, path not in self._infos, self._emitter),
                             self._priority + (1_000_000 if urgent else 0))
        return None

    def info(self, path: str) -> PhotoInfo | None:
        return self._infos.get(path)

    def remember_info(self, path: str, info: PhotoInfo) -> None:
        """Info the caller already has (the selection's): not read again, and still at hand when the
        photo leaves the selection."""
        self._infos[path] = info

    def clear_queue(self) -> None:
        """Drops preloads that have not started (so stale requests do not hold up fast navigation)."""
        self._pool.clear()
        self._pending.clear()

    @Slot(str, object, object, str)
    def _on_done(self, key: str, image, info, error: str) -> None:
        self._pending.discard(key)
        if info is not None:
            self._infos[key.split("|", 1)[0]] = info
        if key in self._cache:
            return
        self._cache[key] = (image, error)
        self._bytes += image.sizeInBytes() if image is not None else 0
        while self._bytes > self.budget and len(self._cache) > 1:
            _, (old, _) = self._cache.popitem(last=False)
            self._bytes -= old.sizeInBytes() if old is not None else 0
        self.ready.emit(key)

    def shutdown(self) -> None:
        self._pool.clear()
        self._pool.waitForDone(3000)


# ---------------------------------------------------------------- image area


def ambient_of(image: QImage) -> QImage:
    """Apple's background extension for a photo: a tiny, blurred, darkened and slightly calmer copy
    of it, enlarged to fill the stage behind the photo (dark enough not to sway how its colors look)."""
    small = image.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    small = small.convertToFormat(glass.FMT)
    a = glass.blur(glass.pixels(small), 2, passes=3)
    lum = a[..., :3] @ np.array([0.0722, 0.7152, 0.2126], np.float32)
    a[..., :3] = (lum[..., None] + (a[..., :3] - lum[..., None]) * 0.9) * 0.40
    a[..., 3] = 255
    out = np.clip(a, 0, 255).astype(np.uint8)
    h, w = out.shape[:2]
    return QImage(np.ascontiguousarray(out).data, w, h, w * 4, glass.FMT).copy()


class ImageView(QWidget):
    """Shows the image fitted to its safe area (between the floating controls) or at 100%;
    edge arrows and panning.

    The image held is always the unedited (orientation-corrected) photo; rotation,
    straightening and cropping are applied as a transform while painting. In crop mode
    the whole photo is shown together with the crop box.

    The stage is painted into a buffer once per change: the floating glass reads what lies
    beneath it from that buffer (`glass_backdrop`) instead of painting the stage again."""

    prevRequested = Signal()
    nextRequested = Signal()
    zoomRequested = Signal(float, float, QPointF)  # clicked point in the image (0–1), position in the window
    cropChanged = Signal()  # the crop box was dragged
    cropApplyRequested = Signal()  # double-click inside the box
    clicked = Signal()  # single click (play/pause for a video)
    fullscreenRequested = Signal()  # double-click on a video
    moved = Signal()  # the mouse moved over the stage (wakes the controls of a playing video)

    ARROW_R = 22
    CROP_MARGIN = 40
    HANDLE_TOL = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._pixmap: QPixmap | None = None
        self._natural: tuple[float, float] | None = None  # the photo's actual size (logical px)
        self._placeholder = False
        self._message = ""
        self._full = False
        self._offset = QPointF(0, 0)
        self._focus: tuple[float, float, QPointF] | None = None
        self._drag: QPointF | None = None
        self._hover = 0
        self._wheel = 0
        self._edit: Edit | None = None
        self._crop: CropSession | None = None
        self.fine_grid = False  # dense grid while straightening
        self.can_prev = self.can_next = False
        self.arrows_visible = True
        self._frame: QImage | None = None  # frame of the playing video (in its encoded orientation)
        self._frame_rotation = 0  # clockwise rotation of the frame when displayed
        self.video_mode = False
        self.show_play = False  # ▶ in the middle of a paused video
        self._press: QPointF | None = None
        self._click = QTimer(self)
        self._click.setSingleShot(True)
        self._click.timeout.connect(self.clicked)
        self._safe: QRect | None = None  # where the image is fitted (between the floating controls)
        self._buffer: QImage | None = None
        self._dirty = True
        self._ambient: QImage | None = None
        self._ambient_at = 0.0
        self._arrow_glass = glass.GlassRenderer(self, glass.MEDIA, None)

    # ------------------------------------------------------------ state

    def scene_changed(self) -> None:
        """The stage must be painted again (the image, the edit, the crop box …)."""
        self._dirty = True
        self.update()

    def set_safe_rect(self, rect: QRect | None) -> None:
        if rect != self._safe:
            self._safe = rect
            self.scene_changed()

    def area(self) -> QRectF:
        return QRectF(self._safe) if self._safe is not None and self._safe.isValid() else QRectF(self.rect())

    def set_image(self, pixmap: QPixmap | None, message: str = "",
                  natural: tuple[float, float] | None = None, placeholder: bool = False) -> None:
        """`natural`: the photo's actual (unedited) size; the image is laid out as if it had this
        size (an image prepared at screen size is enlarged as needed when the window is larger).
        `placeholder`: a thumbnail shown while preparing; replaced by the sharp image when ready."""
        changed_source = pixmap is not self._pixmap
        self._pixmap = pixmap
        self._frame = None
        self._natural = natural
        self._placeholder = placeholder
        self._message = message
        if changed_source:
            self._ambient = ambient_of(pixmap.toImage()) if pixmap is not None else None
        if self._full and self._focus is not None and pixmap is not None:
            # Keep the double-clicked point under the cursor (the placeholder is drawn at the same size).
            fx, fy, anchor = self._focus
            w, h = self._logical_size()
            center = self.area().center()
            self._offset = QPointF(anchor.x() - fx * w - (center.x() - w / 2),
                                   anchor.y() - fy * h - (center.y() - h / 2))
            if not placeholder:
                self._focus = None
        self.scene_changed()

    def set_edit(self, edit: Edit | None) -> None:
        self._edit = None if edit is None or edit.is_identity() else edit
        self.scene_changed()

    def set_frame(self, image: QImage | None, rotation: int = 0) -> None:
        """A video frame (in its encoded orientation; `rotation`: clockwise rotation when displayed)."""
        self._frame = image
        self._frame_rotation = rotation % 360
        if image is not None:
            self._placeholder = False
            self._message = ""
            now = time.monotonic()
            if self._ambient is None or now - self._ambient_at > 0.5:  # the backdrop follows the video slowly
                self._ambient = ambient_of(image)
                self._ambient_at = now
        self.scene_changed()

    def set_message(self, message: str) -> None:
        self._message = message
        self.scene_changed()

    def has_image(self) -> bool:
        return self._frame is not None or self._pixmap is not None

    def has_frame(self) -> bool:
        return self._frame is not None

    def set_full(self, full: bool, focus: tuple[float, float, QPointF] | None = None) -> None:
        self._full = full
        self._offset = QPointF(0, 0)
        self._focus = focus if full else None
        self.setCursor(Qt.CursorShape.OpenHandCursor if full else Qt.CursorShape.ArrowCursor)
        self.scene_changed()

    def is_full(self) -> bool:
        return self._full

    def current_pixmap(self) -> QPixmap | None:
        return self._pixmap

    def upright_size(self) -> tuple[float, float]:
        """Logical size of the unedited photo (for a video: of the frame with its orientation applied)."""
        if self._natural is not None:
            return self._natural
        if self._frame is not None:
            dpr = self._frame.devicePixelRatio() or 1.0
            w, h = self._frame.width() / dpr, self._frame.height() / dpr
            return (h, w) if self._frame_rotation in (90, 270) else (w, h)
        pm = self._pixmap
        dpr = pm.devicePixelRatio() or 1.0
        return pm.width() / dpr, pm.height() / dpr

    def shown_edit(self) -> Edit:
        if self._crop is not None:
            return self._crop.edit()
        return self._edit or Edit()

    def _logical_size(self) -> tuple[float, float]:
        """Logical size of the displayed (edited) image."""
        w, h = self.upright_size()
        x0, y0, x1, y1 = self.shown_edit().crop_box(w, h)
        return x1 - x0, y1 - y0

    def image_rect(self) -> QRectF:
        area = self.area()
        if not self.has_image():
            return QRectF()
        w, h = self._logical_size()
        if not self._full:
            scale = min(area.width() / w, area.height() / h, 1.0)  # small images are not enlarged
            tw, th = w * scale, h * scale
            return QRectF(area.center().x() - tw / 2, area.center().y() - th / 2, tw, th)
        full = QRectF(self.rect())
        x = full.center().x() - w / 2 + self._offset.x()
        y = full.center().y() - h / 2 + self._offset.y()
        # An image larger than the window cannot be dragged past its edges; a smaller one is centered.
        x = min(max(x, full.right() - w), full.left()) if w > full.width() else full.center().x() - w / 2
        y = min(max(y, full.bottom() - h), full.top()) if h > full.height() else full.center().y() - h / 2
        return QRectF(x, y, w, h)

    def _pixmap_transform(self, rect: QRectF) -> QTransform:
        """Image (logical px) → window: orientation-corrected image → canvas (rotation,
        straightening) → crop box (the whole canvas in crop mode) → `rect`. A video frame is
        first rotated according to its orientation (portrait phone video)."""
        src = self._frame if self._frame is not None else self._pixmap
        dpr = src.devicePixelRatio() or 1.0
        pw, ph = src.width() / dpr, src.height() / dpr
        w, h = self.upright_size()
        edit = self.shown_edit()
        quarter = self._frame_rotation // 90 if self._frame is not None else 0
        uw, uh = (ph, pw) if quarter % 2 else (pw, ph)
        t = quarter_transform(quarter, pw, ph) * QTransform.fromScale(w / uw, h / uh) * upright_to_canvas(edit, w, h)
        if self._crop is not None:
            k = rect.width() / edit.canvas_size(w, h)[0]
            return t * QTransform.fromScale(k, k) * QTransform.fromTranslate(rect.x(), rect.y())
        x0, y0, x1, _ = edit.crop_box(w, h)
        k = rect.width() / (x1 - x0)
        return (t * QTransform.fromTranslate(-x0, -y0) * QTransform.fromScale(k, k)
                * QTransform.fromTranslate(rect.x(), rect.y()))

    # ------------------------------------------------------------ crop mode

    def start_crop(self, session: CropSession) -> None:
        self._crop = session
        self._full = False
        self._offset = QPointF(0, 0)
        self._focus = None
        self._drag = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.scene_changed()

    def stop_crop(self) -> None:
        self._crop = None
        self.fine_grid = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.scene_changed()

    def canvas_rect(self) -> QRectF:
        """In crop mode: where the whole canvas (with margins) sits in the window."""
        cw, ch = self._crop.canvas
        area = self.area().adjusted(self.CROP_MARGIN, self.CROP_MARGIN, -self.CROP_MARGIN, -self.CROP_MARGIN)
        k = min(area.width() / cw, area.height() / ch) if cw and ch else 1.0
        return QRectF(area.center().x() - cw * k / 2, area.center().y() - ch * k / 2, cw * k, ch * k)

    def crop_box_rect(self) -> QRectF:
        rect = self.canvas_rect()
        k = rect.width() / self._crop.canvas[0]
        x0, y0, x1, y1 = self._crop.box
        return QRectF(rect.x() + x0 * k, rect.y() + y0 * k, (x1 - x0) * k, (y1 - y0) * k)

    def _to_canvas(self, pos: QPointF) -> tuple[float, float, float]:
        rect = self.canvas_rect()
        k = rect.width() / self._crop.canvas[0]
        return (pos.x() - rect.x()) / k, (pos.y() - rect.y()) / k, k

    # ------------------------------------------------------------ geometry

    def _arrow_center(self, side: int) -> QPointF:
        r = self.ARROW_R
        area = self.area()
        x = area.left() + 6 + r if side < 0 else area.right() - 6 - r
        return QPointF(x, area.center().y())

    def _arrow_at(self, pos: QPointF) -> int:
        if self._crop is not None or not self.arrows_visible:
            return 0
        for side, allowed in ((-1, self.can_prev), (1, self.can_next)):
            if allowed and (pos - self._arrow_center(side)).manhattanLength() <= self.ARROW_R * 1.6:
                return side
        return 0

    # ------------------------------------------------------------ painting

    def _paint_stage(self, p: QPainter) -> None:
        p.fillRect(self.rect(), QColor(VIEW_BG))
        if self._ambient is not None and self._crop is None:
            # The background extension: the photo, enlarged to cover the stage, blurred and darkened.
            amb = self._ambient
            r = QRectF(self.rect())
            scale = max(r.width() / amb.width(), r.height() / amb.height())
            w, h = amb.width() * scale, amb.height() * scale
            p.save()
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(QRectF(r.center().x() - w / 2, r.center().y() - h / 2, w, h), amb)
            p.restore()
        if self.has_image():
            rect = self.canvas_rect() if self._crop is not None else self.image_rect()
            smooth = not self._full or self._placeholder or bool(self.shown_edit().angle)
            p.save()
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, smooth)
            p.setClipRect(rect)
            p.setTransform(self._pixmap_transform(rect))
            if self._frame is not None:
                p.drawImage(QPointF(0, 0), self._frame)
            else:
                p.drawPixmap(QPointF(0, 0), self._pixmap)
            p.restore()
            if self._crop is not None:
                paint_crop(p, rect, self.crop_box_rect(), self.fine_grid)
        if self._message:
            p.setPen(QColor(DIM if not self.has_image() else LIGHT))
            font = QFont(self.font())
            font.setPointSizeF(font.pointSizeF() + 2)
            p.setFont(font)
            area = self.area()
            rect = area.adjusted(40, 0, -40, 0).toRect()
            if self.has_image():  # a small label over the image
                fm = QFontMetrics(font)
                w = fm.horizontalAdvance(self._message) + 28
                rect = QRect(int(area.center().x() - w / 2), int(area.top()) + 12, w, fm.height() + 12)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                path = QPainterPath()
                path.addRoundedRect(QRectF(rect), rect.height() / 2, rect.height() / 2)
                p.fillPath(path, QColor(0, 0, 0, 150))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._message)

    def _ensure_buffer(self) -> QImage:
        dpr = self.devicePixelRatioF() or 1.0
        size = QSize(max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)))
        if self._buffer is None or self._buffer.size() != size:
            self._buffer = QImage(size, glass.FMT)
            self._buffer.setDevicePixelRatio(dpr)
            self._dirty = True
        if self._dirty:
            p = QPainter(self._buffer)
            self._paint_stage(p)
            p.end()
            self._dirty = False
        return self._buffer

    def glass_backdrop(self, rect: QRect, dpr: float) -> QImage | None:
        """The stage beneath `rect` (this widget's coordinates), for the glass floating above it."""
        buffer = self._ensure_buffer()
        own = buffer.devicePixelRatio() or 1.0
        if abs(own - dpr) > 1e-3:
            return None
        part = buffer.copy(QRect(round(rect.x() * own), round(rect.y() * own),
                                 round(rect.width() * own), round(rect.height() * own)))
        part.setDevicePixelRatio(own)
        return part

    def paintEvent(self, event) -> None:
        if glass.capturing():
            return
        buffer = self._ensure_buffer()
        p = QPainter(self)
        p.drawImage(QPointF(0, 0), buffer)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.has_image() and self._crop is None and self.video_mode and self.show_play:
            self._paint_button(p, self.image_rect().center(), 36, "play.fill", 1.0)
        if self._crop is None and self.arrows_visible:
            for side, allowed in ((-1, self.can_prev), (1, self.can_next)):
                if allowed:
                    hot = self._hover == side
                    self._paint_button(p, self._arrow_center(side), self.ARROW_R,
                                       "chevron.left" if side < 0 else "chevron.right", 1.0 if hot else 0.8)

    def _paint_button(self, p: QPainter, c: QPointF, r: float, icon: str, strength: float) -> None:
        """A round button of clear glass over the stage (the edge arrows, play)."""
        rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        dpr = self.devicePixelRatioF() or 1.0
        mat = glass.material(glass.MEDIA)
        margin = self._arrow_glass.margin(mat)
        area = rect.toAlignedRect().adjusted(-margin, -margin, margin, margin)
        glass.paint_contour(p, rect, None, mat, dpr)
        if glass.reduce_transparency():
            glass.paint_glass(p, rect, None, mat, None, dpr)
        else:
            backdrop = QImage(round(area.width() * dpr), round(area.height() * dpr), glass.FMT)
            backdrop.setDevicePixelRatio(dpr)
            backdrop.fill(QColor(VIEW_BG))
            bp = QPainter(backdrop)
            bp.drawImage(QPointF(-area.x(), -area.y()), self._ensure_buffer())
            bp.end()
            w = h = max(1, round(2 * r * dpr))
            image, luminance = glass.render_glass(backdrop, round(margin * dpr), w, h, r * dpr, mat, dpr)
            glass.paint_glass(p, rect, image, mat, None, dpr,
                              QColor(255, 255, 255, int(28 * (strength - 0.8) * 5)) if strength > 0.8 else None,
                              luminance)
        s = r * 0.9
        icons.paint(p, QRectF(c.x() - s / 2 + (2 if icon == "play.fill" else 0), c.y() - s / 2, s, s), icon,
                    QColor(255, 255, 255, int(255 * min(1.0, 0.75 + strength / 4))))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._dirty = True

    # ------------------------------------------------------------ mouse

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._crop is not None:
            x, y, k = self._to_canvas(event.position())
            handle = self._crop.hit(x, y, self.HANDLE_TOL / k)
            if handle:
                self._crop.begin(handle, x, y)
            return
        side = self._arrow_at(event.position())
        if side:
            (self.prevRequested if side < 0 else self.nextRequested).emit()
            return
        self._press = event.position()
        if self._full:
            self._drag = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        self.moved.emit()
        if self._crop is not None:
            x, y, k = self._to_canvas(pos)
            if self._crop.dragging:
                self._crop.drag_to(x, y)
                self.scene_changed()
                self.cropChanged.emit()
            else:
                handle = self._crop.hit(x, y, self.HANDLE_TOL / k)
                self.setCursor(CURSORS.get(handle, Qt.CursorShape.ArrowCursor))
            return
        if self._drag is not None:
            self._offset += pos - self._drag
            self._drag = pos
            self.scene_changed()
            return
        hover = self._arrow_at(pos)
        if hover != self._hover:
            self._hover = hover
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._crop is not None:
            if self._crop.dragging:
                self._crop.end()
                self.scene_changed()
                self.cropChanged.emit()
            return
        press, self._press = self._press, None
        if self._drag is not None:
            self._drag = None
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._full else Qt.CursorShape.ArrowCursor)
        if self.video_mode and press is not None and (event.position() - press).manhattanLength() < 5:
            # A single click plays/pauses; wait briefly so the first click of a double-click does not count.
            self._click.start(QApplication.doubleClickInterval())

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._crop is not None:
            x, y, k = self._to_canvas(event.position())
            if self._crop.hit(x, y, self.HANDLE_TOL / k) == "move":
                self.cropApplyRequested.emit()
            return
        side = self._arrow_at(event.position())
        if side:  # a quick second click on an arrow arrives as a double-click: it moves on too
            (self.prevRequested if side < 0 else self.nextRequested).emit()
            return
        if self.video_mode:
            self._click.stop()
            self.fullscreenRequested.emit()
            return
        rect = self.image_rect()
        pos = event.position()
        fx = (pos.x() - rect.left()) / rect.width() if rect.width() else 0.5
        fy = (pos.y() - rect.top()) / rect.height() if rect.height() else 0.5
        self.zoomRequested.emit(min(max(fx, 0.0), 1.0), min(max(fy, 0.0), 1.0), pos)

    def leaveEvent(self, _event) -> None:
        if self._hover:
            self._hover = 0
            self.update()

    def wheelEvent(self, event) -> None:
        """The wheel moves between photos (steps are accumulated for high-resolution wheels)."""
        event.accept()
        if self._crop is not None:
            return
        self._wheel += event.angleDelta().y()
        while abs(self._wheel) >= 120:
            (self.prevRequested if self._wheel > 0 else self.nextRequested).emit()
            self._wheel -= 120 if self._wheel > 0 else -120


# -------------------------------------------------------------------- thumbnail strip


class StripModel(QAbstractListModel):
    def __init__(self, items: list[PreviewItem], selection: Selection, thumbs: ThumbnailLoader, parent=None):
        super().__init__(parent)
        self.items = items
        self.selection = selection
        self.thumbs = thumbs
        self.current = 0
        self._rows = {norm(it.path): i for i, it in enumerate(items)}
        thumbs.ready.connect(self._thumb_ready)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            edit = self.edit(item)
            return item.name + ("\n" + t("preview.edit_line", edit=edit.describe(future=True)) if edit else "")
        return None

    def edit(self, item: PreviewItem) -> Edit | None:
        return self.selection.edits.get(item.path, item.mtime)

    def row_of(self, key: str) -> int | None:
        return self._rows.get(key)

    def refresh(self, row: int | None = None) -> None:
        if row is None:
            if self.items:
                self.dataChanged.emit(self.index(0), self.index(len(self.items) - 1))
        else:
            self.dataChanged.emit(self.index(row), self.index(row))

    def _thumb_ready(self, path: str) -> None:
        row = self._rows.get(norm(path))
        if row is not None:
            self.refresh(row)


class StripDelegate(QStyledItemDelegate):
    SIZE = QSize(92, 68)

    def __init__(self, model: StripModel, parent=None):
        super().__init__(parent)
        self.model = model

    def sizeHint(self, option, index) -> QSize:
        return self.SIZE

    def paint(self, painter: QPainter, option, index) -> None:
        from .widgets import rounded

        row = index.row()
        item = self.model.items[row]
        r = QRectF(option.rect.adjusted(4, 5, -4, -5))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        edit = self.model.edit(item)
        pm = self.model.thumbs.get(item.path, item.mtime, edit)
        clip = rounded(r, 7)
        painter.save()
        painter.setClipPath(clip)
        if pm is LOADING or pm is None:
            painter.fillRect(r, QColor(255, 255, 255, 30))
            if pm is None:  # no picture (a broken or unsupported file)
                c = r.center()
                icons.paint(painter, QRectF(c.x() - 11, c.y() - 11, 22, 22), "photo", QColor(255, 255, 255, 150))
        else:
            dpr = pm.devicePixelRatio() or 1.0
            w, h = pm.width() / dpr, pm.height() / dpr
            scale = max(r.width() / w, r.height() / h)  # filled (cropped to the cell), like Apple's filmstrip
            tw, th = w * scale, h * scale
            painter.drawPixmap(QRectF(r.center().x() - tw / 2, r.center().y() - th / 2, tw, th), pm,
                               QRectF(pm.rect()))
        chosen = item.path in self.model.selection
        if not chosen:
            painter.fillRect(r, QColor(0, 0, 0, 120))
        painter.restore()
        if row == self.model.current:
            painter.setPen(QPen(QColor("#FFFFFF"), 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(r.adjusted(-1.5, -1.5, 1.5, 1.5), 8.5, 8.5)
        if is_video_name(item.name):
            paint_video_badge(painter, QPointF(r.left() - 3, r.bottom() + 3),
                              self.model.thumbs.duration(item.path, item.mtime), compact=True)
        if edit is not None:
            paint_edit_badge(painter, QPointF(r.right() + 3, r.bottom() + 3), compact=True)
        painter.restore()


# -------------------------------------------------------------------- window


def _info_text(item: PreviewItem, info: PhotoInfo | None) -> str:
    parts = []
    if info is not None and info.width:
        parts.append(f"{info.width} × {info.height}")
    if info is not None and info.fmt is not None:
        ext = os.path.splitext(item.path)[1].lstrip(".").upper()
        parts.append(f"RAW ({ext})" if info.fmt == Fmt.RAW else DISPLAY_NAME[info.fmt])
        video = getattr(info, "video", None)
        if video is not None:
            parts.append(video.short())
    try:
        parts.append(size_text(os.path.getsize(item.path)))
    except OSError:
        pass
    if info is not None:
        when, source = best_date(info, item.name, item.mtime)
        label = "" if source != DATE_MTIME else " " + t("preview.modified")
        parts.append(date_time(when) + label)
        if info.camera:
            parts.append(info.camera)
    return " · ".join(parts)


def _sentence(text: str) -> str:
    return capitalize(text)


def _edit_has(field: str) -> bool:
    return any(f.name == field for f in fields(Edit))


class PreviewDialog(QDialog):
    def __init__(self, items: list[PreviewItem], index: int, selection: Selection, thumbs: ThumbnailLoader,
                 parent=None, plan=None, crop: bool = False):
        """`plan`: the processing plan of the Selected tab (PlanModel). If given, each photo's
        name/location after processing is shown and updated when the plan changes (e.g. when
        a photo is removed from the selection in the preview and the numbers shift). `crop`:
        go straight to crop mode when the window opens (once the image is ready)."""
        super().__init__(parent)
        self.setObjectName("Preview")
        self.setWindowTitle(t("preview.window_title"))
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setStyleSheet(QSS)
        self.items = items
        self.selection = selection
        self.edits = selection.edits
        self.thumbs = thumbs
        self.plan = plan
        self.loader = PreviewLoader(self)
        self.loader.ready.connect(self._on_ready)
        self._index = -1
        self._removed: dict[str, tuple] = {}  # removed from the selection: put back in their old place when re-added
        self._shown_key: str | None = None
        self._shown_path: str | None = None
        self._shown_box: int | None = None
        self._session: CropSession | None = None
        self._aspect_index = 0
        self._pending_crop = crop
        self._undo: list[tuple[str, float, Edit | None]] = []
        self._player = None  # VideoPlayer: created when the first video is shown
        self.video_bar = None
        self._cut_from: float | None = None  # s: the start of the part being marked for removal
        self._frame_path: str | None = None  # file of the video frame in the image area
        self._comparing = False  # the photo is shown without its edits while O (or the button) is held
        screen = parent.screen() if parent is not None else self.screen()
        size = screen.size() * screen.devicePixelRatio() if screen else QSize(1920, 1080)
        self.box = max(size.width(), size.height())
        self.developer = RawDeveloper(self.box, self)  # RAW developments for the live exposure preview
        self.developer.ready.connect(self._raw_ready)
        self._live_ev: float | None = None  # the exposure slider's value while it is being moved
        self._developed = False  # the image area shows a RAW developed with an exposure
        self._draft: tuple | None = None  # (image or development, its reduced copy) while the slider moves
        self._exposed: tuple | None = None  # (source, exposure, reduced, display scale, the shown pixmap)
        self._exposure_timer = QTimer(self)
        self._exposure_timer.setSingleShot(True)
        self._exposure_timer.setInterval(30)  # at most ~30 updates a second while dragging
        self._exposure_timer.timeout.connect(self._refresh_exposure)
        self._idle = QTimer(self)  # a playing video: the controls step aside when the mouse rests
        self._idle.setSingleShot(True)
        self._idle.setInterval(CHROME_IDLE_MS)
        self._idle.timeout.connect(lambda: self._set_chrome_visible(False))
        self._chrome_shown = True

        # The stage: the image area fills the window; everything else floats above it.
        self.view = ImageView(self)
        self.view.prevRequested.connect(self.prev)
        self.view.nextRequested.connect(self.next)
        self.view.zoomRequested.connect(self._zoom_at)
        self.view.cropChanged.connect(self._update_crop_info)
        self.view.cropApplyRequested.connect(self.apply_crop)
        self.view.clicked.connect(self._view_clicked)
        self.view.fullscreenRequested.connect(self.toggle_fullscreen)
        self.view.moved.connect(self._wake_chrome)

        # Top left: the name, its position in the list and whether it is selected.
        self.title = ElidedLabel(mode=Qt.TextElideMode.ElideMiddle)
        self.title.setObjectName("PvTitle")
        title_font = QFont(self.font())
        title_font.setPointSizeF(title_font.pointSizeF() + 1.5)
        title_font.setWeight(QFont.Weight.Bold)
        self.title.setFont(title_font)
        self.position = QLabel()
        self.position.setObjectName("PvMuted")
        self.position.setFont(theme.tabular(self.position.font()))
        self.chip = QLabel()  # a capsule inside the capsule, inset as much at the side as above and below
        self.chip.setFixedHeight(26)
        self.chip.setContentsMargins(11, 0, 11, 0)
        self.chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.head = glass.GlassSurface(self, kind=glass.MEDIA, radius=None)
        head = QHBoxLayout(self.head)
        head.setContentsMargins(18, 4, 9, 4)
        head.setSpacing(10)
        head.addWidget(self.title, 1)
        head.addWidget(self.position)
        head.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignVCenter)

        # Under it, as plain text on the stage: details, location, edit, the name after processing.
        self.details = ElidedLabel()
        self.details.setObjectName("PvMuted")
        self.gps = QLabel()
        self.gps.setObjectName("PvWarn")
        self.edit_label = ElidedLabel()
        self.edit_label.setObjectName("PvEdit")
        self.edit_label.setToolTip(t(EDIT_TIP))
        self.edit_link = QLabel(f"<a href='#kaldir' style='color:{LIGHT}'>{t('preview.remove_link')}</a>")
        self.edit_link.setTextFormat(Qt.TextFormat.RichText)
        self.edit_link.setToolTip(t("preview.remove_tip"))
        self.edit_link.linkActivated.connect(lambda _link: self.reset_edit())
        self.edit_link.hide()
        self.target = ElidedLabel(mode=Qt.TextElideMode.ElideMiddle)
        self.target.setObjectName("PvTarget")
        for label in (self.title, self.details, self.target):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info_row = QWidget(self)
        row = QHBoxLayout(self.info_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        row.addWidget(self.details)
        row.addWidget(self.gps)
        row.addWidget(self.edit_label)
        row.addWidget(self.edit_link)
        row.addStretch(1)
        row.addWidget(self.target)

        # Top right: editing, selection, viewing and closing, each group on its own capsule.
        self.left_btn = BarButton("", "rotate.left", media=True, tooltip=t("common.rotate_left_key"),
                                  accessible=t("common.rotate_left"))
        self.left_btn.clicked.connect(lambda: self.rotate(-1))
        self.right_btn = BarButton("", "rotate.right", media=True, tooltip=t("common.rotate_right_key"),
                                   accessible=t("common.rotate_right"))
        self.right_btn.clicked.connect(lambda: self.rotate(1))
        self.flip_btn = BarButton("", "flip", media=True, tooltip=t("common.flip_key"), accessible=t("common.flip"))
        self.flip_btn.clicked.connect(self.flip)
        self.crop_btn = BarButton(t("preview.crop"), "crop", media=True)
        self.crop_btn.setCheckable(True)
        self.crop_btn.setToolTip(t("preview.crop_tip"))
        self.crop_btn.clicked.connect(self._crop_clicked)
        self.compare_btn = BarButton("", "compare", media=True,
                                     tooltip=t("preview.original_tip"),
                                     accessible=t("preview.original"))
        self.compare_btn.pressed.connect(lambda: self.set_comparing(True))
        self.compare_btn.released.connect(lambda: self.set_comparing(False))
        self.toggle_btn = BarButton("", "checkmark.circle", media=True)
        self.toggle_btn.setToolTip(t("preview.toggle_tip"))
        self.toggle_btn.clicked.connect(self.toggle_selected)
        self.info_btn = BarButton("", "info", media=True, tooltip=t("preview.info_tip"),
                                  accessible=t("preview.info"))
        self.info_btn.setCheckable(True)
        self.info_btn.toggled.connect(self._show_info)
        self.zoom_btn = BarButton(percent(100), "zoom.in", media=True)
        self.zoom_btn.setToolTip(t("preview.zoom_tip"))
        self.zoom_btn.clicked.connect(lambda: self.toggle_zoom())
        self.full_btn = BarButton("", "fullscreen", media=True, tooltip=t("preview.fullscreen_tip"),
                                  accessible=t("preview.fullscreen"))
        self.full_btn.clicked.connect(self.toggle_fullscreen)
        self.reveal_btn = BarButton("", "folder", media=True, tooltip=t("preview.reveal_tip"),
                                    accessible=t("preview.reveal"))
        self.reveal_btn.clicked.connect(lambda: reveal(self.current().path))
        close_btn = BarButton("", "xmark", media=True, tooltip=t("preview.close_tip"), accessible=t("common.close"))
        close_btn.clicked.connect(self.accept)
        self.edit_tools = ToolGroup(self.left_btn, self.right_btn, self.flip_btn, self.crop_btn, self.compare_btn,
                                    parent=self, kind=glass.MEDIA)
        self.select_bar = ToolGroup(self.toggle_btn, parent=self, kind=glass.MEDIA)
        self.view_tools = ToolGroup(self.info_btn, self.zoom_btn, self.full_btn, self.reveal_btn, parent=self,
                                    kind=glass.MEDIA)
        self.close_bar = ToolGroup(close_btn, parent=self, kind=glass.MEDIA)
        buttons = (self.left_btn, self.right_btn, self.flip_btn, self.crop_btn, self.compare_btn, self.toggle_btn,
                   self.info_btn, self.zoom_btn, self.full_btn, self.reveal_btn, close_btn)
        for b in buttons:
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # arrow keys always go to navigation
            b.setAutoDefault(False)  # Enter must not press "Seçimden çıkar" (remove from selection) by accident
            b.setDefault(False)

        # Metadata panel (optional, a dark glass panel on the right).
        self.info_panel = InfoPanel(colors=MEDIA_COLORS)
        self.info_card = glass.GlassSurface(self, kind=glass.MEDIA, radius=20)
        self.info_card.setObjectName("Media")
        card_layout = QVBoxLayout(self.info_card)
        card_layout.setContentsMargins(6, 8, 6, 8)
        card_layout.addWidget(self.info_panel)
        self.info_card.hide()
        self.info_panel.contentChanged.connect(self._fit_info_card)
        # A click on its text (to select and copy it) gives the text the keyboard focus; the keys that
        # move between photos must not then scroll the panel instead (see `eventFilter`).
        self.info_panel.scroll.installEventFilter(self)

        # Bottom: the thumbnail strip (replaced by the crop tools in crop mode), the exposure or video
        # controls above it and a line of keyboard hints under it.
        self.strip_model = StripModel(items, selection, thumbs, self)
        self.strip = QListView()
        self.strip.setObjectName("PvStrip")
        self.strip.setModel(self.strip_model)
        self.strip.setItemDelegate(StripDelegate(self.strip_model, self))
        self.strip.setViewMode(QListView.ViewMode.IconMode)
        self.strip.setFlow(QListView.Flow.LeftToRight)
        self.strip.setWrapping(False)
        self.strip.setMovement(QListView.Movement.Static)
        self.strip.setUniformItemSizes(True)
        self.strip.setSpacing(0)
        self.strip.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.strip.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.strip.setFrameShape(QListView.Shape.NoFrame)
        self.strip.setFixedHeight(StripDelegate.SIZE.height() + 12)
        self.strip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.strip.viewport().setAutoFillBackground(False)
        self.strip.clicked.connect(lambda idx: self.go(idx.row()))
        self.footer = glass.GlassSurface(self, kind=glass.MEDIA, radius=22)
        self.footer.setObjectName("Media")
        foot_layout = QVBoxLayout(self.footer)
        foot_layout.setContentsMargins(10, 4, 10, 2)
        foot_layout.addWidget(self.strip)
        self.hint = ElidedLabel(t(PHOTO_HINT))
        self.hint.setParent(self)
        self.hint.setObjectName("PvMuted")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.exposure_bar = ExposureBar(self)
        self.exposure_bar.changed.connect(self._exposure_moved)
        self.exposure_bar.committed.connect(self._exposure_committed)
        self.exposure_bar.hide()

        self.crop_bar = CropBar(self)
        self.crop_bar.aspectChosen.connect(self._aspect_chosen)
        self.crop_bar.angleChanged.connect(self._angle_changed)
        self.crop_bar.straightening.connect(self._straightening)
        self.crop_bar.rotateRequested.connect(self.rotate)
        self.crop_bar.flipRequested.connect(self.flip)
        self.crop_bar.resetRequested.connect(self._reset_crop)
        self.crop_bar.cancelRequested.connect(self.cancel_crop)
        self.crop_bar.applyRequested.connect(self.apply_crop)
        self.crop_bar.hide()

        selection.changed.connect(self._selection_changed)
        self.edits.changed.connect(self._edits_changed)
        if plan is not None:
            plan.modelReset.connect(self._update_bar)
        if parent is not None:
            self.resize(parent.size())
        self.setWindowState(Qt.WindowState.WindowMaximized)
        self.go(index)

    # ------------------------------------------------------------ layout

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._relayout()

    def _laid_out(self, widget: QWidget) -> bool:
        """Does the widget take its place? (Also while a playing video's controls step aside: the
        picture does not grow under them, and they come back where they belong.)"""
        return not widget.isHidden() or not self._chrome_shown

    def _context_bar(self) -> QWidget | None:
        """The capsule above the strip: the video's controls or a photo's exposure."""
        if self.video_bar is not None and self._laid_out(self.video_bar):
            return self.video_bar
        if not self.exposure_bar.isHidden():
            return self.exposure_bar
        return None

    def _head_min(self) -> int:
        """The title capsule's narrowest width: NAME_MIN for the file name beside the position and the chip."""
        layout = self.head.layout()
        margins = layout.contentsMargins()
        parts = [part.sizeHint().width() + layout.spacing() for part in (self.position, self.chip)
                 if not part.isHidden()]
        return margins.left() + margins.right() + NAME_MIN + sum(parts)

    def _relayout(self) -> None:
        w, h = self.width(), self.height()
        self.view.setGeometry(0, 0, w, h)
        m, bar = MARGIN, 44
        groups = [g for g in (self.close_bar, self.view_tools, self.select_bar, self.edit_tools) if self._laid_out(g)]
        head_min = self._head_min()
        # In a narrow window the tool buttons show only their icons (their tooltips still say what they
        # do), so no group runs off the window's left edge or under the title.
        for level in (0, 1, 2):
            for group in groups:
                group.set_compact(level)
            if sum(g.sizeHint().width() + GAP for g in groups) <= w - 2 * m - head_min:
                break
        right = w - m
        for group in groups:
            gw = group.sizeHint().width()
            group.setGeometry(right - gw, m, gw, bar)
            right -= gw + GAP
        # As wide as the name needs, up to the tools (the loop above left NAME_MIN for the name if it could).
        head_w = max(0, min(self.head.sizeHint().width() + 40, right - m))
        self.head.setGeometry(m, m, head_w, bar)
        self.info_row.setGeometry(m + 8, m + bar + 6, w - 2 * m - 16, 22)
        top = m + bar + 6 + 22 + 6
        bottom = h - m
        if self._session is not None:
            ch = self.crop_bar.sizeHint().height()
            cw = min(1180, w - 2 * m)
            self.crop_bar.setGeometry((w - cw) // 2, bottom - ch, cw, ch)
            bottom -= ch + GAP
        else:
            self.hint.setGeometry(m, bottom - 18, w - 2 * m, 18)
            bottom -= 18 + 6
            strip_h = self.strip.height() + 8
            needed = len(self.items) * StripDelegate.SIZE.width() + 22
            sw = max(180, min(needed, w - 2 * m))
            self.footer.setGeometry((w - sw) // 2, bottom - strip_h, sw, strip_h)
            bottom -= strip_h + GAP
            context = self._context_bar()
            if context is not None:
                room = w - 2 * m
                cw = context.fit(room) if context is self.video_bar else min(800, room)
                ch = context.sizeHint().height()
                context.setGeometry((w - cw) // 2, bottom - ch, cw, ch)
                bottom -= ch + GAP
        safe_right = w - m
        self._info_area = QRect(w - m, top, 0, max(120, bottom - top))
        if self.info_card.isVisible():
            iw = min(340, max(260, w // 4))
            self._info_area = QRect(w - m - iw, top, iw, max(120, bottom - top))
            self._fit_info_card()
            safe_right = w - m - iw - GAP
        self.view.set_safe_rect(QRect(m, top, max(40, safe_right - m), max(40, bottom - top)))

    def _fit_info_card(self) -> None:
        """The card is as tall as its text, up to the room between the bars (then it scrolls)."""
        area = getattr(self, "_info_area", None)
        if area is None or area.width() <= 0 or not self.info_card.isVisible():
            return
        m = self.info_card.layout().contentsMargins()
        want = m.top() + m.bottom() + self.info_panel.content_height(area.width() - m.left() - m.right())
        self.info_card.setGeometry(area.x(), area.y(), area.width(), min(area.height(), max(120, want)))

    def _floating(self) -> list[QWidget]:
        widgets = [self.head, self.info_row, self.edit_tools, self.select_bar, self.view_tools, self.close_bar,
                   self.footer, self.hint, self.exposure_bar]
        if self.video_bar is not None:
            widgets.append(self.video_bar)
        return widgets

    def _set_chrome_visible(self, visible: bool) -> None:
        """A playing video: the controls step aside while the mouse rests (and come back when it moves)."""
        if visible == self._chrome_shown:
            return
        if not visible and (self._session is not None or self._player is None or not self._player.is_playing()):
            return
        self._chrome_shown = visible
        for w in self._floating():
            if w is self.exposure_bar and not self._is_photo_shown():
                continue
            if w is self.video_bar and not self._is_video(self.current()):
                continue
            w.setVisible(visible)
        self.view.arrows_visible = visible
        self.view.setCursor(Qt.CursorShape.ArrowCursor if visible else Qt.CursorShape.BlankCursor)
        self.view.update()

    def _wake_chrome(self) -> None:
        if not self._chrome_shown:
            self._set_chrome_visible(True)
        if self._player is not None and self._player.is_playing():
            self._idle.start()

    def _is_photo_shown(self) -> bool:
        return bool(self.items) and not self._is_video(self.current())

    # ------------------------------------------------------------ navigation

    def current(self) -> PreviewItem:
        return self.items[self._index]

    def go(self, index: int) -> None:
        if not self.items or self._session is not None:
            return
        index = max(0, min(index, len(self.items) - 1))
        if index == self._index:
            return
        self.exposure_bar.flush()  # a wheel step's exposure belongs to the photo being left
        previous = self._index
        self._index = index
        self.strip_model.current = index
        if 0 <= previous < len(self.items):
            self.strip_model.refresh(previous)
        self.strip_model.refresh(index)
        self.strip.scrollTo(self.strip_model.index(index), QAbstractItemView.ScrollHint.PositionAtCenter)
        self.view.set_full(False)
        self.zoom_btn.setText(percent(100))
        self.zoom_btn.set_icon_name("zoom.in")
        self.view.can_prev = index > 0
        self.view.can_next = index < len(self.items) - 1
        self.loader.clear_queue()
        self.developer.clear_queue()  # (developments for photos passed by are dropped)
        self._comparing = False
        self.view.set_edit(self._edit_of(self.current()))
        self._live_ev = None
        self._developed = False
        self._draft = self._exposed = None
        self._show_current()
        self._sync_video()
        self._sync_exposure()
        # The next and previous photos (and the one after next) are prepared in advance so navigation does not wait.
        for j in (index + 1, index - 1, index + 2):
            if 0 <= j < len(self.items):
                it = self.items[j]
                self._info(it)  # (the selection's info goes to the loader: not read again)
                self.loader.get(it.path, it.mtime, self._box_for(it), urgent=False)
        self._update_bar()
        if self.info_card.isVisible():
            self.info_panel.show_path(self.current().path)
        self._relayout()

    def next(self) -> None:
        self.go(self._index + 1)

    def prev(self) -> None:
        self.go(self._index - 1)

    def _edit_of(self, item: PreviewItem) -> Edit | None:
        return self.edits.get(item.path, item.mtime)

    def _box_for(self, item: PreviewItem) -> int:
        """Preparation size that fills the screen. For a cropped photo the image is prepared
        proportionally larger so that the cropped region looks sharp too."""
        edit = self._edit_of(item)
        scale = min(4.0, edit.detail_scale()) if edit is not None else 1.0
        if scale <= 1.2:
            return self.box
        return min(FULL_LIMIT, int(math.ceil(self.box * scale / 512) * 512))

    def _box(self) -> int | None:
        if self.view.is_full():
            return None
        if self._session is not None and self._shown_box is not None:
            return self._shown_box  # in crop mode the image at hand is good enough
        return self._box_for(self.current())

    def _show_current(self) -> None:
        item = self.current()
        box = self._box()
        self._shown_key = self.loader.key(item.path, item.mtime, box)
        self._shown_box = box
        self._info(item)  # (the selection's info goes to the loader: not read again)
        hit = self.loader.get(item.path, item.mtime, box)
        if hit is not None:
            self._display(hit)
            return
        if self.view.has_frame() and self._frame_path == item.path:
            return  # a paused video's frame stays (at 100 % or cropped), not its first frame
        current = self.view.current_pixmap()
        if self._shown_path == item.path and current is not None and not self.view.is_full():
            # Same photo (e.g. being prepared larger after a crop): keep the image at hand.
            self.view.set_image(current, natural=self.view.upright_size(), placeholder=True)
        elif box is not None:
            # While preparing, the thumbnail (if any) is shown blurry at the photo's actual size.
            pm = self.thumbs.get(item.path, item.mtime)
            placeholder = pm if pm is not LOADING and pm is not None else None
            self.view.set_image(placeholder, "" if placeholder is not None else t("preview.loading"),
                                natural=self._natural_size(item, placeholder, thumbnail=True), placeholder=True)
        else:
            self.view.set_image(current, t("preview.full_loading"),
                                natural=self._natural_size(item, current), placeholder=True)
        self._shown_path = item.path

    def _natural_size(self, item: PreviewItem, pixmap: QPixmap | QImage | QSize | None,
                      thumbnail: bool = False) -> tuple[float, float] | None:
        """The photo's actual size (logical pixels). If its info has not been read, the image's
        own size; for a thumbnail, screen size at the thumbnail's aspect ratio is assumed."""
        if pixmap is None:
            return None
        dpr = self.devicePixelRatioF() or 1.0
        info = self._info(item)
        if info is not None and info.width and info.height:
            return info.width / dpr, info.height / dpr
        w, h = float(pixmap.width()), float(pixmap.height())
        if thumbnail:
            scale = self.box / max(w, h)
            w, h = w * scale, h * scale
        return w / dpr, h / dpr

    def _info(self, item: PreviewItem) -> PhotoInfo | None:
        info = self.selection.info(item.path)
        if info is None:
            return self.loader.info(item.path)
        self.loader.remember_info(item.path, info)  # (kept if the photo is removed from the selection)
        return info

    def _display(self, hit: tuple[QImage | None, str]) -> None:
        image, error = hit
        if self.view.has_frame() and self._frame_path == self.current().path:
            return  # the video is playing: the arriving start frame (poster) must not replace the current frame
        self._shown_path = self.current().path
        if image is None:
            self._pending_crop = False
            self.view.set_image(None, t("preview.none_error", error=error) if error else t("tile.no_preview"))
            return
        if not self._show_developed():
            item = self.current()
            raw = is_raw_name(item.name)
            ev = 0.0 if raw else self._exposure(item)
            draft = bool(ev) and self._live_ev is not None

            def make() -> QImage:
                source = self._drafted(image, draft_image) if draft else image
                return exposed_image(source, ev)  # the same curve as processing

            pm = self._pixmap_of(image, ev, draft, make)
            # When fitting, the photo's actual size is used; at 100% the image is already full size (a reduced
            # copy is laid out at that size too).
            if self.view.is_full():
                dpr = pm.devicePixelRatio() or 1.0
                natural = (image.width() / dpr, image.height() / dpr) if draft else None
            else:
                natural = self._natural_size(item, image)
            self.view.set_image(pm, natural=natural, placeholder=draft)
            self._developed = False
            if raw and self._exposure(item):
                self.view.set_message(t("preview.exposing"))  # the RAW is being developed
        if self._pending_crop:
            self._pending_crop = False
            self.start_crop()

    @Slot(str)
    def _on_ready(self, key: str) -> None:
        if key == self._shown_key:
            hit = self.loader.get(*self._key_parts(key))
            if hit is not None:
                self._display(hit)
            self._update_bar()

    def _key_parts(self, key: str):
        item = self.current()
        return item.path, item.mtime, self._box()

    # ------------------------------------------------------------ zoom

    def toggle_zoom(self, focus: tuple[float, float, QPointF] | None = None) -> None:
        if self._session is not None:
            return
        full = not self.view.is_full()
        if full and focus is None:
            focus = (0.5, 0.5, QRectF(self.view.rect()).center())
        self.view.set_full(full, focus)
        self.zoom_btn.setText(t("preview.fit") if full else percent(100))
        self.zoom_btn.set_icon_name("zoom.out" if full else "zoom.in")
        self.view_tools.adjustSize()
        self._relayout()
        self._show_current()

    def _zoom_at(self, fx: float, fy: float, pos: QPointF) -> None:
        self.toggle_zoom((fx, fy, pos) if not self.view.is_full() else None)

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showMaximized()
        else:
            self.showFullScreen()
        self.full_btn.set_icon_name("fullscreen.exit" if self.isFullScreen() else "fullscreen")

    # ------------------------------------------------------------ before / after

    def set_comparing(self, on: bool) -> None:
        """Shows the photo as it is in the file (no rotation, crop or exposure) while O or the button is held."""
        if on == self._comparing or self._session is not None or not self.items:
            return
        self._comparing = on
        item = self.current()
        self.view.set_edit(None if on else self._edit_of(item))
        if self._is_video(item):
            return
        self._developed = False
        if on or not self._show_developed():
            self._show_current()
        self.view.set_message(t("preview.original_shown") if on else "")

    # ------------------------------------------------------------ editing

    def _set_edit(self, item: PreviewItem, edit: Edit | None, record: bool = True) -> None:
        """Stores the edit. If the edited photo is not selected, it is added to the selection
        (edits are applied only to photos that get processed)."""
        previous = self._edit_of(item)
        if (previous or Edit()) == (edit or Edit()):
            return
        if record:
            self._undo.append((item.path, item.mtime, previous))
            del self._undo[:-200]
        if edit is not None and not edit.is_identity() and item.path not in self.selection:
            saved = self._removed.pop(norm(item.path), None)
            if saved is not None:
                self.selection.restore(*saved)
            else:
                self.selection.add([item.path])
        self.edits.set(item.path, item.mtime, edit)

    def rotate(self, delta: int) -> None:
        """Rotates by 90° (+1 = clockwise). In crop mode the crop box rotates along."""
        if not self.items:
            return
        if self._session is not None:
            self._session.rotate(delta)
            self._sync_crop_bar()
            self.view.scene_changed()
            self._update_crop_info()
            return
        item = self.current()
        self._set_edit(item, (self._edit_of(item) or Edit()).rotated(delta))

    def flip(self) -> None:
        """Flips the photo or video left to right. In crop mode the crop box flips along."""
        if not self.items:
            return
        if self._session is not None:
            self._session.mirror()
            self._sync_crop_bar()
            self.view.scene_changed()
            self._update_crop_info()
            return
        item = self.current()
        self._set_edit(item, (self._edit_of(item) or Edit()).mirrored())

    def reset_edit(self) -> None:
        """Removes all edits of the photo."""
        if self._session is None and self.items:
            self._set_edit(self.current(), None)

    def undo(self) -> None:
        """Undoes the last edit (going to that photo if needed)."""
        if self._session is not None:
            return
        while self._undo:
            path, mtime, previous = self._undo.pop()
            index = next((i for i, it in enumerate(self.items) if it.path == path and it.mtime == mtime), None)
            if index is None:
                continue
            self.go(index)
            self.edits.set(path, mtime, previous)
            return

    def _edits_changed(self, keys: list[str]) -> None:
        for key in keys:
            row = self.strip_model.row_of(key)
            if row is not None:
                self.strip_model.refresh(row)
        if not self.items or key_of(self.current().path) not in keys:
            return
        if self._session is None:
            self.view.set_edit(self._edit_of(self.current()))
            if self._box() != self._shown_box:
                self._show_current()  # prepared larger for the cropped region
            if self._live_ev is None:  # e.g. undone, or every edit removed
                self._sync_exposure()
                self._refresh_exposure()
            self._sync_trim()
        self._update_bar()

    # ------------------------------------------------------------ exposure (RAW)

    def _sync_exposure(self) -> None:
        """The exposure bar is shown under photos (not videos), with the photo's stored exposure."""
        item = self.current()
        photo = not self._is_video(item)
        self.exposure_bar.setVisible(photo and self._session is None)
        if photo:
            self.exposure_bar.set_raw(is_raw_name(item.name))
            edit = self._edit_of(item)
            self.exposure_bar.set_value(edit.exposure if edit is not None else 0.0)

    def _exposure(self, item: PreviewItem) -> float:
        """The exposure to show: the slider's while it is moved, otherwise the stored one."""
        if self._is_video(item) or self._comparing:
            return 0.0
        if self._live_ev is not None and item is self.current():
            return self._live_ev
        edit = self._edit_of(item)
        return edit.exposure if edit is not None else 0.0

    def _show_developed(self) -> bool:
        """For a RAW with an exposure: shows its development with that exposure, if it is ready
        (otherwise it is started). False if the camera's JPEG is to be shown instead (and for
        photos that are not RAW: they get the exposure in `_display`)."""
        item = self.current()
        ev = self._exposure(item)
        if not ev or not is_raw_name(item.name):
            return False
        developed = self.developer.get(item.path, item.mtime)
        if developed is None:
            return False
        draft = self._live_ev is not None

        def make() -> QImage:
            source = self._drafted(developed, draft_preview) if draft else developed
            return _to_qimage(source.image(ev))

        pm = self._pixmap_of(developed, ev, draft, make)
        # The development is smaller than the photo: it is laid out at the photo's size, also at 100%.
        h, w = developed.pixels.shape[:2]
        self.view.set_image(pm, natural=self._natural_size(item, QSize(w, h)), placeholder=draft)
        self._developed = True
        return True

    def _drafted(self, source, reduce):
        """`source` (the prepared image or a RAW's development) reduced by `reduce`, made once per source:
        while the exposure slider moves, every position is shown from it, quick also on a 4K screen."""
        if self._draft is None or self._draft[0] is not source:
            self._draft = (source, reduce(source))
        return self._draft[1]

    def _pixmap_of(self, source, ev: float, draft: bool, make) -> QPixmap:
        """`make()` (the shown image of `source` with the exposure `ev`) as a pixmap. Made again only when
        the source, the exposure or the display scale changed: a rotation does not redo the exposure."""
        dpr = self.devicePixelRatioF() or 1.0
        memo = self._exposed
        if memo is None or memo[0] is not source or memo[1:4] != (ev, draft, dpr):
            pm = QPixmap.fromImage(make())
            pm.setDevicePixelRatio(dpr)
            self._exposed = memo = (source, ev, draft, dpr, pm)
        return memo[4]

    def _refresh_exposure(self) -> None:
        if not self.items or self.view.has_frame() or self._is_video(self.current()):
            return
        item = self.current()
        if not is_raw_name(item.name):
            self._show_current()  # the prepared image again, with the current exposure
            return
        if self._show_developed():
            return
        if self._developed:  # back to 0 EV: the camera's JPEG again
            self._developed = False
            self._show_current()
        elif self._exposure(item) and self.view.has_image():
            failed = self.developer.failed(item.path, item.mtime)
            self.view.set_message(t("preview.raw_failed") if failed
                                  else t("preview.exposing"))

    def _raw_ready(self, key: str) -> None:
        if self.items and key == self.developer.key(self.current().path, self.current().mtime):
            self._refresh_exposure()

    def _exposure_moved(self, ev: float) -> None:
        if self._session is not None or not self.items:
            return
        self._live_ev = ev
        if not self._exposure_timer.isActive():
            self._exposure_timer.start()

    def _exposure_committed(self, ev: float) -> None:
        if self._session is not None or not self.items:
            return
        item = self.current()
        self._live_ev = None
        self._set_edit(item, replace(self._edit_of(item) or Edit(), exposure=ev))
        self._refresh_exposure()

    # ------------------------------------------------------------ crop mode

    def _crop_clicked(self) -> None:
        if self._session is None:
            self.start_crop()
        else:
            self.apply_crop()
        self.crop_btn.setChecked(self._session is not None)

    def start_crop(self) -> None:
        if self._session is not None or not self.items:
            return
        self.cancel_cut()
        self.set_comparing(False)
        if not self.view.has_image():
            self.crop_btn.setChecked(False)
            return  # no image (a file that could not be opened)
        if self._player is not None:
            self._player.pause()  # cropping is done on the paused frame
        self._set_chrome_visible(True)
        if self.view.is_full():
            self.toggle_zoom()
        w, h = self.view.upright_size()
        self._session = CropSession(w, h, self._edit_of(self.current()))
        self._aspect_index = 0
        self.view.start_crop(self._session)
        self.view.can_prev = self.view.can_next = False
        for w in (self.footer, self.hint, self.exposure_bar):
            w.hide()
        if self.video_bar is not None:
            self.video_bar.hide()
        self.crop_bar.show()
        self.crop_bar.raise_()
        self.edit_link.hide()
        self._sync_crop_bar()
        self.crop_btn.setChecked(True)
        for b in (self.left_btn, self.right_btn, self.flip_btn, self.zoom_btn, self.toggle_btn, self.compare_btn):
            b.setEnabled(False)
        self._update_crop_info()
        self._relayout()
        self.setFocus()

    def _stop_crop(self) -> None:
        self._session = None
        self.view.stop_crop()
        self.view.can_prev = self._index > 0
        self.view.can_next = self._index < len(self.items) - 1
        self.crop_bar.hide()
        self.footer.show()
        self.hint.show()
        if self._is_video(self.current()):
            if self.video_bar is not None:
                self.video_bar.show()
        else:
            self.exposure_bar.show()
        self.crop_btn.setChecked(False)
        for b in (self.left_btn, self.right_btn, self.flip_btn, self.zoom_btn, self.toggle_btn, self.compare_btn):
            b.setEnabled(True)
        self._relayout()
        self.setFocus()

    def apply_crop(self) -> None:
        if self._session is None:
            return
        edit = self._session.edit()
        self._stop_crop()
        self._set_edit(self.current(), edit)
        self.view.set_edit(self._edit_of(self.current()))
        if self._box() != self._shown_box:
            self._show_current()
        self._update_bar()

    def cancel_crop(self) -> None:
        if self._session is None:
            return
        self._stop_crop()
        self.view.set_edit(self._edit_of(self.current()))
        self._update_bar()

    def _sync_crop_bar(self) -> None:
        s = self._session
        if s is None:
            return
        index = self._aspect_index
        value = ASPECTS[index][1]
        if isinstance(value, float) and s.aspect is not None and abs(value - s.aspect) > 1e-6:
            # After a 90° rotation the locked aspect ratio turns too (3:2 → 2:3).
            index = next((i for i, (_, v) in enumerate(ASPECTS) if isinstance(v, float) and abs(v - s.aspect) < 1e-6),
                         0)
            self._aspect_index = index
        self.crop_bar.set_values(index, s.angle)

    def _aspect_chosen(self, index: int) -> None:
        if self._session is None:
            return
        self._aspect_index = index
        self._session.set_aspect(self._session.aspect_value(ASPECTS[index][1]))
        self.view.scene_changed()
        self._update_crop_info()

    def _angle_changed(self, angle: float) -> None:
        if self._session is None:
            return
        self._session.set_angle(angle)
        self.view.scene_changed()
        self._update_crop_info()

    def _straightening(self, active: bool) -> None:
        self.view.fine_grid = active
        self.view.scene_changed()

    def _reset_crop(self) -> None:
        if self._session is None:
            return
        self._session.reset()
        self._aspect_index = 0
        self._sync_crop_bar()
        self.view.scene_changed()
        self._update_crop_info()

    def _update_crop_info(self) -> None:
        if self._session is None:
            return
        info = self._info(self.current())
        if info is not None and info.width and info.height:
            w, h = self._session.edit().output_size(info.width, info.height)
            self.crop_bar.size_label.setText(t("preview.pixels", w=number(w), h=number(h)))
        else:
            self.crop_bar.size_label.setText("")

    # ------------------------------------------------------------ selection

    def toggle_selected(self) -> None:
        if self._session is not None:
            return
        item = self.current()
        key = norm(item.path)
        if item.path in self.selection:
            saved = self.selection.entry(item.path)
            if saved is not None:
                self._removed[key] = saved
            self.selection.remove([item.path])
        else:
            saved = self._removed.pop(key, None)
            if saved is not None:
                self.selection.restore(*saved)  # back in its old place and with its folder root
            else:
                self.selection.add([item.path])

    def _selection_changed(self) -> None:
        self.strip_model.refresh(self._index)
        self.strip.viewport().update()
        self._update_bar()

    # ------------------------------------------------------------ info

    def _show_info(self, visible: bool) -> None:
        self.info_card.setVisible(visible)
        if visible:
            self.info_panel.show_path(self.current().path)
            self.info_card.raise_()
        self._relayout()

    def _update_bar(self) -> None:
        item = self.current()
        self.title.setText(item.name)
        self.position.setText(f"{number(self._index + 1)} / {number(len(self.items))}")
        chosen = item.path in self.selection
        self.chip.setText(t("preview.chosen") if chosen else t("preview.not_chosen"))
        self.chip.setObjectName("PvChipOn" if chosen else "PvChipOff")
        self.chip.style().unpolish(self.chip)
        self.chip.style().polish(self.chip)
        self.toggle_btn.setText(t("preview.remove_from_selection") if chosen else t("preview.add_to_selection"))
        self.toggle_btn.set_icon_name("minus.circle" if chosen else "plus.circle")
        self.select_bar.adjustSize()
        self.title.setToolTip(item.path)
        info = self._info(item)
        self.details.setText(_info_text(item, info))
        self.details.setToolTip(self.details.text())
        self.gps.setText(t("preview.has_gps") if info is not None and info.has_gps else "")
        edit = self._edit_of(item)
        if edit is None:
            self.edit_label.setText("")
        else:
            size = ""
            if info is not None and info.width and info.height and edit.has_geometry():
                w, h = edit.output_size(info.width, info.height)
                size = f" → {number(w)} × {number(h)}"
            self.edit_label.setText(f"{_sentence(edit.describe(future=True))}{size}")
        self.edit_link.setVisible(edit is not None and self._session is None)
        described = self.plan.describe(item.path) if self.plan is not None and chosen else None
        if described is None:
            self.target.setObjectName("PvTarget")
            self.target.setText("")
        elif described[1]:
            self.target.setObjectName("PvDanger")
            self.target.setText(t("preview.skipped", reason=described[1]))
        else:
            self.target.setObjectName("PvTarget")
            self.target.setText(t("preview.after", target=described[0]))
        self.target.style().unpolish(self.target)
        self.target.style().polish(self.target)
        self.target.setToolTip(described[1] or described[0] if described else "")
        if self._is_video(item) and self.video_bar is not None:
            self.video_bar.set_audio_removed(bool(getattr(edit, "mute", False)))
        self._relayout()

    # ------------------------------------------------------------ keyboard

    def keyPressEvent(self, event) -> None:
        key = event.key()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if self._session is not None:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.apply_crop()
            elif key == Qt.Key.Key_Escape:
                self.cancel_crop()
            elif key == Qt.Key.Key_R:
                self.rotate(1)
            elif key == Qt.Key.Key_L:
                self.rotate(-1)
            elif key == Qt.Key.Key_H:
                self.flip()
            elif key in (Qt.Key.Key_F11, Qt.Key.Key_F):
                self.toggle_fullscreen()
            else:
                event.accept()  # navigation and other shortcuts are off in crop mode
            return
        self._wake_chrome()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        video = self._is_video(self.current()) and self._player is not None
        if key == Qt.Key.Key_Escape and self._cut_from is not None:
            self.cancel_cut()  # the part being marked is dropped (the window stays open)
        elif video and (key in (Qt.Key.Key_K, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_P)):
            self._player.toggle()
        elif video and shift and key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self._player.step(-5000 if key == Qt.Key.Key_Left else 5000)
        elif video and key == Qt.Key.Key_M:
            self.video_bar.toggle_mute()
        elif video and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.video_bar.change_volume(10 if key == Qt.Key.Key_Up else -10)
        elif key == Qt.Key.Key_Z and ctrl:
            self.undo()
        elif key == Qt.Key.Key_O and not event.isAutoRepeat():
            self.set_comparing(True)
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_PageDown, Qt.Key.Key_Space):
            self.next()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_PageUp, Qt.Key.Key_Backspace):
            self.prev()
        elif key == Qt.Key.Key_Home:
            self.go(0)
        elif key == Qt.Key.Key_End:
            self.go(len(self.items) - 1)
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_X):
            self.toggle_selected()
        elif key == Qt.Key.Key_R:
            self.rotate(1)
        elif key == Qt.Key.Key_L:
            self.rotate(-1)
        elif key == Qt.Key.Key_H:
            self.flip()
        elif key == Qt.Key.Key_C:
            self.start_crop()
        elif key == Qt.Key.Key_Z:
            self.toggle_zoom()
        elif key == Qt.Key.Key_I:
            self.info_btn.toggle()
        elif key in (Qt.Key.Key_F11, Qt.Key.Key_F):
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_Escape and self.isFullScreen():
            self.showMaximized()
            self.full_btn.set_icon_name("fullscreen")
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()  # the dialog's default-button behavior is disabled
        elif key != Qt.Key.Key_O:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_O and not event.isAutoRepeat():
            self.set_comparing(False)
            return
        super().keyReleaseEvent(event)

    def eventFilter(self, obj, event) -> bool:
        """The info panel's scroll area would take the keys that move between photos (and ↑ / ↓, a
        video's volume) once its text has the focus: they go to the window instead."""
        if obj is self.info_panel.scroll and event.type() == QEvent.Type.KeyPress and self.items:
            keys = {Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown}
            if self._is_video(self.current()):
                keys |= {Qt.Key.Key_Up, Qt.Key.Key_Down}
            if event.key() in keys:
                self.keyPressEvent(event)
                return True
        return super().eventFilter(obj, event)

    def done(self, result: int) -> None:
        self.exposure_bar.flush()  # (a wheel step just before closing still counts)
        self._session = None
        self._idle.stop()
        if self._player is not None:
            self._player.shutdown()  # release the file and stop the conversion thread
        self.loader.shutdown()
        self.developer.shutdown()
        super().done(result)

    # ------------------------------------------------------------ video

    @staticmethod
    def _is_video(item: PreviewItem) -> bool:
        return is_video_name(item.name)

    def _ensure_player(self):
        if self._player is None:
            from .player import VideoBar, VideoPlayer

            self._player = VideoPlayer(self)
            self._player.frameReady.connect(self._on_frame)
            self._player.failed.connect(self._on_video_failed)
            self._player.playingChanged.connect(self._on_playing)
            self.video_bar = VideoBar(self._player, self)
            self.video_bar.trimStartRequested.connect(self.trim_start)
            self.video_bar.trimEndRequested.connect(self.trim_end)
            self.video_bar.trimClearRequested.connect(self.clear_trim)
            self.video_bar.cutRequested.connect(self.toggle_cut)
            self.video_bar.audioToggled.connect(self.set_audio_removed)
            self.video_bar.audio_btn.setVisible(_edit_has("mute"))
            self.video_bar.hide()
        return self._player

    def _sync_video(self) -> None:
        """If the shown item is a video, loads it into the player (without playing); otherwise unloads the player."""
        self.cancel_cut()
        item = self.current()
        video = self._is_video(item)
        if self._frame_path != item.path:
            self._frame_path = None
            if self.view.has_frame():
                self.view.set_frame(None)  # do not keep the previous video's frame
        self.view.video_mode = video
        self.view.show_play = video
        self.hint.setText(t(VIDEO_HINT if video else PHOTO_HINT))
        self.compare_btn.setVisible(not video)
        self.edit_tools.adjustSize()
        if not video:
            self._idle.stop()
            self._set_chrome_visible(True)
            if self._player is not None:
                self._player.unload()
                self.video_bar.hide()
            return
        player = self._ensure_player()
        video_info = getattr(self._info(item), "video", None)
        player.load(item.path, getattr(video_info, "vcodec", None))  # AV1: straight to the backup player
        duration = getattr(video_info, "duration", 0) or 0
        self.video_bar.reset(int(duration * 1000))
        self._sync_trim()
        self.video_bar.setVisible(self._session is None)
        edit = self._edit_of(item)
        self.video_bar.set_audio_removed(bool(getattr(edit, "mute", False)))

    def set_audio_removed(self, removed: bool) -> None:
        """Removes (or keeps) the video's sound when processing ("Sesi kaldır")."""
        if not self.items or not self._is_video(self.current()) or not _edit_has("mute"):
            return
        item = self.current()
        self._set_edit(item, replace(self._edit_of(item) or Edit(), mute=removed))
        self.video_bar.set_audio_removed(removed)

    # ------------------------------------------------------------ trimming (video)

    def _sync_trim(self) -> None:
        """Shows the kept parts of the current video on the seek slider."""
        if self.video_bar is None or not self.items or not self._is_video(self.current()):
            return
        edit = self._edit_of(self.current())
        cut = edit is not None and (edit.trim is not None or bool(edit.cuts))
        self.video_bar.set_kept(edit.segments(self._video_duration()) if cut else None)
        self.video_bar.set_audio_removed(bool(getattr(edit, "mute", False)))

    def _video_duration(self) -> float:
        info = getattr(self._info(self.current()), "video", None)
        duration = getattr(info, "duration", 0) or 0
        if not duration and self._player is not None:
            duration = self._player.duration() / 1000
        return duration

    def _set_trim(self, start: float, end: float | None) -> None:
        item = self.current()
        edit = self._edit_of(item) or Edit()
        duration = self._video_duration()
        if end is not None and duration and end >= duration - 0.05:
            end = None  # up to the end
        tidy = replace(edit, trim=(start, end)).tidied(duration, MIN_KEPT)
        if tidy is not None:  # (None: the removed parts would leave nothing)
            self._set_edit(item, tidy)

    def trim_start(self) -> None:
        """Keeps the video from the current position on."""
        if self._player is None or self._session is not None or not self._is_video(self.current()):
            return
        position = max(0.0, self._player.position() / 1000)
        edit = self._edit_of(self.current())
        end = edit.trim[1] if edit is not None and edit.trim else None
        if end is not None and position > end - MIN_KEPT:
            end = None  # the start moved past the end: the end is dropped
        self._set_trim(position, end)

    def trim_end(self) -> None:
        """Keeps the video up to the current position."""
        if self._player is None or self._session is not None or not self._is_video(self.current()):
            return
        position = max(0.0, self._player.position() / 1000)
        edit = self._edit_of(self.current())
        start = edit.trim[0] if edit is not None and edit.trim else 0.0
        if position < start + MIN_KEPT:
            start = 0.0  # the end moved before the start: the start is dropped
        if position < MIN_KEPT:
            return
        self._set_trim(start, position)

    def clear_trim(self) -> None:
        """The whole video is kept again (trim and removed parts dropped)."""
        if self._session is not None or not self.items:
            return
        self.cancel_cut()
        item = self.current()
        edit = self._edit_of(item)
        if edit is not None and (edit.trim is not None or edit.cuts):
            self._set_edit(item, replace(edit, trim=None, cuts=()))

    def toggle_cut(self) -> None:
        """Marks a part to remove: the first press marks its start at the current position, the
        second its end. Kept parts that would stay shorter than MIN_KEPT are removed with it."""
        if self._player is None or self._session is not None or not self._is_video(self.current()):
            return
        position = max(0.0, self._player.position() / 1000)
        if self._cut_from is None:
            self._cut_from = position
            self.video_bar.set_pending(position)
            return
        a, b = sorted((self._cut_from, position))
        self.cancel_cut()
        if b - a < MIN_CUT:
            return  # pressed twice at the same place: nothing to remove
        item = self.current()
        edit = self._edit_of(item) or Edit()
        tidy = replace(edit, cuts=(*edit.cuts, (a, b))).tidied(self._video_duration(), MIN_KEPT)
        if tidy is not None:  # (None: nothing would be left of the video)
            self._set_edit(item, tidy)

    def cancel_cut(self) -> None:
        """Drops the part being marked for removal (if any)."""
        self._cut_from = None
        if self.video_bar is not None:
            self.video_bar.set_pending(None)

    def _view_clicked(self) -> None:
        if self._session is None and self._player is not None and self._is_video(self.current()):
            self._player.toggle()

    def _on_frame(self, image: QImage, rotation: int) -> None:
        item = self.current()
        if self._player is None or self._player.path != item.path:
            return
        image.setDevicePixelRatio(self.devicePixelRatioF() or 1.0)
        self._shown_path = self._frame_path = item.path
        self.view.set_frame(image, rotation)

    def _on_playing(self, playing: bool) -> None:
        self.view.show_play = not playing
        self.view.update()
        if playing:
            self._idle.start()
        else:
            self._idle.stop()
            self._set_chrome_visible(True)

    def _on_video_failed(self, message: str) -> None:
        self.view.set_message(message)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.WindowStateChange:
            self.full_btn.set_icon_name("fullscreen.exit" if self.isFullScreen() else "fullscreen")
        return super().event(event)
