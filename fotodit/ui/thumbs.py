"""Generating and caching thumbnails in the background.

The grid only requests thumbnails for the tiles visible on screen; requests are handled
newest first (tiles that come into view while scrolling go first). When the folder
changes, the old requests still in the queue are cancelled.

A video's thumbnail is a frame taken from its beginning (at most 1 s in); its duration
is learned at the same time (for the "▶ 0:42" label on the tiles).

Rotated/cropped photos show their thumbnail with the edit applied: for a rotation the
ready thumbnail is rotated instantly (not decoded again); for a crop the image is decoded
larger so that the cropped region looks sharp, and until that is ready a cropped version
of the thumbnail is shown. A photo whose exposure was adjusted gets it in its thumbnail too (a
RAW is developed from its RAW data, as it will be processed), and a trimmed video shows a frame
from the start of the kept part; until those are ready, the plain thumbnail is shown.
"""

from __future__ import annotations

import os
from collections import OrderedDict

from PIL import Image
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtGui import QImage, QPixmap

from ..core import edits
from ..core.edits import Edit
from ..core.formats import is_raw_name, is_video_name
from ..core.imaging import apply_orientation, open_image, to_color_mode

LOADING = object()  # not ready yet
_MISSING = object()
DETAIL = 1.3  # if a crop needs more magnification than this, the image is decoded larger
MAX_DETAIL = 4.0


def make_thumbnail(path: str, box: int, edit: Edit | None = None, mtime: float | None = None) -> QImage | None:
    try:
        # The image is decoded larger so that the cropped region is also sharp at `box` size.
        decode_box = int(box * min(MAX_DETAIL, edit.detail_scale())) if edit is not None else box
        if is_video_name(path):
            from ..core.video.thumb import poster

            kept = edit.segments(0.0) if edit is not None and (edit.trim or edit.cuts) else []
            im = poster(path, decode_box, mtime, start=kept[0][0] if kept else 0.0)
            if im is None:
                return None
        elif is_raw_name(path):
            # RAW: Pillow only sees the TIFF preview; LibRaw's embedded JPEG is used instead.
            from ..core.rawdev import linear_preview
            from ..core.rawdev import thumbnail as raw_thumbnail

            developed = linear_preview(path, decode_box) if edit is not None and edit.exposure else None
            im = developed.image(edit.exposure) if developed is not None else raw_thumbnail(path, decode_box)
            if im is None:
                return None
        else:
            with open_image(path) as src:
                src.draft("RGB", (decode_box, decode_box))  # JPEG: reduced-size decoding, HEIC: embedded thumbnail
                src.thumbnail((decode_box, decode_box), Image.Resampling.BICUBIC, reducing_gap=2.0)
                im = apply_orientation(src)
        if edit is not None:
            im = edits.apply(to_color_mode(im), edit)
            if edit.exposure and not is_raw_name(path) and not is_video_name(path):
                im = edits.apply_exposure(im, edit.exposure)  # a RAW was developed with it above
            im.thumbnail((box, box), Image.Resampling.BICUBIC)
        return _to_qimage(im)
    except Exception:  # noqa: BLE001 - broken file: a placeholder is shown
        return None


def _to_qimage(im: Image.Image) -> QImage:
    im = to_color_mode(im)
    if im.mode == "LA":
        im = im.convert("RGBA")
    elif im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    data = im.tobytes("raw", im.mode)
    fmt = QImage.Format.Format_RGBA8888 if im.mode == "RGBA" else QImage.Format.Format_RGB888
    return QImage(data, im.width, im.height, im.width * len(im.mode), fmt).copy()


class _Emitter(QObject):
    done = Signal(object, object)  # key, QImage | None


class _Task(QRunnable):
    def __init__(self, key: tuple, box: int, emitter: _Emitter):
        super().__init__()
        self.key, self.box, self.emitter = key, box, emitter

    def run(self) -> None:
        path, mtime, edit = self.key
        self.emitter.done.emit(self.key, make_thumbnail(path, self.box, edit, mtime))


class ThumbnailLoader(QObject):
    ready = Signal(str)  # file path

    def __init__(self, box: int, dpr: float = 1.0, parent: QObject | None = None, limit: int = 1500):
        """`box`: in logical pixels; on high-DPI screens `box * dpr` physical pixels
        are generated so that thumbnails do not look blurry."""
        super().__init__(parent)
        self.dpr = max(1.0, dpr)
        self.box = int(box * self.dpr)
        self.limit = limit
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(2, min(4, (os.cpu_count() or 4) - 1)))
        self._cache: OrderedDict[tuple, QPixmap | None] = OrderedDict()
        self._interim: OrderedDict[tuple, QPixmap] = OrderedDict()  # shown while the sharp one is being prepared
        self._pending: set[tuple] = set()
        self._priority = 0
        self._emitter = _Emitter()
        self._emitter.done.connect(self._on_done)

    def get(self, path: str, mtime: float, edit: Edit | None = None):
        """QPixmap if ready (or None for a broken file), otherwise LOADING. If `edit`
        is given, the thumbnail is returned with the edit applied."""
        if edit is not None and edit.is_identity():
            edit = None
        key = (path, mtime, edit)
        hit = self._cache.get(key, _MISSING)
        if hit is not _MISSING:
            self._cache.move_to_end(key)
            return hit
        if edit is None:
            self._request(key)
            return LOADING
        # Its own thumbnail: a crop that needs more detail, a RAW exposure or a video trim.
        own = edit.detail_scale() > DETAIL or bool(edit.exposure) or edit.trim is not None or bool(edit.cuts)
        if own:
            self._request(key)
        base = self._cache.get((path, mtime, None), _MISSING)
        if base is None:
            return None  # file that cannot be opened
        if base is _MISSING:
            if not own:
                self._request((path, mtime, None))
            return LOADING
        derived = self._interim.get(key)
        if derived is None:
            derived = self._derive(base, edit)
        if own:
            self._interim[key] = derived
            while len(self._interim) > 200:
                self._interim.popitem(last=False)
        else:
            self._store(key, derived)
        return derived

    @staticmethod
    def duration(path: str, mtime: float) -> float | None:
        """The video's duration in seconds (if its thumbnail has been generated)."""
        from ..core.video.thumb import known_duration

        return known_duration(path, mtime)

    def _derive(self, base: QPixmap, edit: Edit) -> QPixmap:
        from .crop import edited_image

        pixmap = QPixmap.fromImage(edited_image(base.toImage(), edit))
        pixmap.setDevicePixelRatio(base.devicePixelRatio())
        return pixmap

    def _request(self, key: tuple) -> None:
        if key not in self._pending and key not in self._cache:
            self._pending.add(key)
            self._priority += 1
            self._pool.start(_Task(key, self.box, self._emitter), self._priority)

    def clear_queue(self) -> None:
        """Cancels the requests that have not started yet (when the folder changes)."""
        self._pool.clear()
        self._pending.clear()

    def forget(self, paths) -> None:
        """Drops the files' thumbnails from the cache. A run applied to the originals keeps
        the file's mtime (when metadata is kept), so this is needed to keep the old thumbnail
        (e.g. the unrotated version) from staying under the same key."""
        names = {os.path.normcase(os.path.abspath(p)) for p in ([paths] if isinstance(paths, str) else paths)}
        for cache in (self._cache, self._interim):
            for key in [k for k in cache if os.path.normcase(os.path.abspath(k[0])) in names]:
                del cache[key]

    def _store(self, key: tuple, pixmap: QPixmap | None) -> None:
        self._cache[key] = pixmap
        self._cache.move_to_end(key)
        while len(self._cache) > self.limit:
            self._cache.popitem(last=False)

    @Slot(object, object)
    def _on_done(self, key: tuple, image) -> None:
        self._pending.discard(key)
        pixmap = None
        if image is not None:
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(self.dpr)
        self._interim.pop(key, None)
        self._store(key, pixmap)
        self.ready.emit(key[0])

    def shutdown(self) -> None:
        self._pool.clear()
        self._pool.waitForDone(2000)
