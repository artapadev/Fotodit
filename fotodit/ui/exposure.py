"""Exposure of photos in the large preview.

`ExposureBar` is the slider under a photo. While it is dragged the preview shows the result live.
For a RAW, `RawDeveloper` develops the RAW data once at half size in the background (see
`rawdev.linear_preview`), and every slider position is then a quick lookup table on that image;
other photos get the same lookup table on the prepared image (`exposed_image`). While the slider
moves, the lookup runs on a reduced copy (`draft_image`, `draft_preview`): on a 4K screen the full
image took a fifth of a second per position. The edit is stored when the slider is released, and
the sharp result follows.
"""

from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QHBoxLayout, QLabel

from ..core.edits import MAX_EXPOSURE, ev_text, exposure_curve
from ..core.rawdev import LinearPreview, linear_preview
from ..i18n import t
from . import glass, theme
from .controls import BarButton, GlassSlider, IconView
from .widgets import ElidedLabel

RAW_NOTE = "exposure.raw_note"
PHOTO_NOTE = "exposure.photo_note"
DRAFT_SIDE = 1600  # device px: the long side of the reduced copy shown while the slider moves


def draft_image(image: QImage) -> QImage:
    """`image` reduced to at most DRAFT_SIDE pixels on its long side (for the live preview)."""
    if max(image.width(), image.height()) <= DRAFT_SIDE:
        return image
    out = image.scaled(DRAFT_SIDE, DRAFT_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    out.setDevicePixelRatio(image.devicePixelRatio())
    return out


def draft_preview(preview: LinearPreview) -> LinearPreview:
    """A RAW's development reduced the same way (its pixels averaged in n × n blocks)."""
    import numpy as np

    h, w = preview.pixels.shape[:2]
    n = -(-max(h, w) // DRAFT_SIDE)
    if n <= 1:
        return preview
    h, w = h // n, w // n
    blocks = preview.pixels[:h * n, :w * n].reshape(h, n, w, n, 3)
    pixels = (blocks.sum(axis=(1, 3), dtype=np.uint32) // (n * n)).astype(np.uint16)
    return LinearPreview(np.ascontiguousarray(pixels), preview.white)


def exposed_image(image: QImage, ev: float) -> QImage:
    """The image with `ev` stops of exposure (the same curve as processing, see `edits.apply_exposure`)."""
    if not ev or image.isNull():
        return image
    import numpy as np

    formats = {QImage.Format.Format_RGB888: 3, QImage.Format.Format_RGBA8888: 4}
    if image.format() not in formats:
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    fmt = image.format()
    channels = formats[fmt]
    w, h, stride = image.width(), image.height(), image.bytesPerLine()
    rows = np.frombuffer(image.constBits(), np.uint8, count=stride * h).reshape(h, stride)
    pixels = rows[:, :w * channels].reshape(h, w, channels)
    out = pixels.copy()
    out[..., :3] = np.asarray(exposure_curve(ev), np.uint8)[pixels[..., :3]]
    result = QImage(out.data, w, h, w * channels, fmt).copy()
    result.setDevicePixelRatio(image.devicePixelRatio())
    return result


class ExposureBar(glass.GlassSurface):
    """Exposure slider (−3 … +3 EV in steps of 0.1) with its value and a reset button, on a glass
    capsule over the photo (the large preview's media glass)."""

    changed = Signal(float)  # while moving (live preview)
    committed = Signal(float)  # the value to store

    def __init__(self, parent=None):
        super().__init__(parent, kind=glass.MEDIA, radius=None)
        self.setObjectName("Media")
        self.slider = GlassSlider(centered=True, media=True)
        steps = int(round(MAX_EXPOSURE * 10))
        self.slider.setRange(-steps, steps)
        self.slider.setPageStep(5)
        self.slider.setMinimumWidth(160)
        self.slider.setMaximumWidth(360)
        self.slider.setToolTip(t("exposure.tip"))
        self.slider.setAccessibleName(t("exposure.name"))
        self.value = QLabel(ev_text(0.0))
        self.value.setFont(theme.tabular(self.value.font()))
        self.value.setMinimumWidth(64)
        self.reset_btn = BarButton(t("common.reset"), "undo", media=True)
        self.reset_btn.setToolTip(t("exposure.reset_tip"))
        self.reset_btn.clicked.connect(self.reset)
        self.reset_btn.setEnabled(False)  # at 0 EV there is nothing to reset
        self.note = ElidedLabel(t(RAW_NOTE))
        self.note.setObjectName("PvMuted")
        for w in (self.slider, self.reset_btn):
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the arrow keys keep moving between photos
        # A click on the groove moves the slider without dragging: stored after a short pause.
        self._commit = QTimer(self)
        self._commit.setSingleShot(True)
        self._commit.setInterval(350)
        self._commit.timeout.connect(lambda: self.committed.emit(self.ev()))
        self.slider.valueChanged.connect(self._moved)
        self.slider.sliderReleased.connect(self._released)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 4, 6, 4)
        layout.setSpacing(10)
        layout.addWidget(IconView("sun", "#F2F2F2", 18))
        layout.addWidget(self.slider, 2)
        layout.addWidget(self.value)
        layout.addWidget(self.reset_btn)
        layout.addSpacing(4)
        layout.addWidget(self.note, 3)

    def ev(self) -> float:
        return self.slider.value() / 10

    def set_raw(self, raw: bool) -> None:
        """The note next to the slider: a RAW is developed from its RAW data, other photos are adjusted."""
        self.note.setText(t(RAW_NOTE if raw else PHOTO_NOTE))
        self.note.setToolTip(
            t("exposure.raw_tip") if raw else
            t("exposure.photo_tip"))

    def set_value(self, ev: float) -> None:
        """Shows a stored value (without signals)."""
        self._commit.stop()
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(ev * 10)))
        self.slider.blockSignals(False)
        self.value.setText(ev_text(self.ev()))
        self.reset_btn.setEnabled(self.slider.value() != 0)

    def reset(self) -> None:
        self.set_value(0.0)
        self.changed.emit(0.0)
        self.committed.emit(0.0)

    def flush(self) -> None:
        """Commits a change still waiting for its timer (a wheel step), before its photo is left."""
        if self._commit.isActive():
            self._commit.stop()
            self.committed.emit(self.ev())

    def _moved(self, _value: int) -> None:
        self.value.setText(ev_text(self.ev()))
        self.reset_btn.setEnabled(self.slider.value() != 0)
        self.changed.emit(self.ev())
        if not self.slider.isSliderDown():
            self._commit.start()

    def _released(self) -> None:
        self._commit.stop()
        self.committed.emit(self.ev())


# ------------------------------------------------------------------ developing


class _Emitter(QObject):
    done = Signal(str, object)  # key, LinearPreview | None


class _Task(QRunnable):
    def __init__(self, key: str, path: str, box: int, emitter: _Emitter):
        super().__init__()
        self.key, self.path, self.box, self.emitter = key, path, box, emitter
        self.started = False
        self.dropped = False  # the user moved on before it started: it is not developed

    def run(self) -> None:
        self.started = True
        if not self.dropped:
            self.emitter.done.emit(self.key, linear_preview(self.path, self.box))


class RawDeveloper(QObject):
    """Half-size linear developments of RAW photos for the live exposure preview; the last few
    are kept (each is a few tens of MB). The newest request is developed first, and those not
    started yet are dropped when the user moves on (`clear_queue`): each takes a second or more,
    and the photo on screen would wait for all the ones passed by."""

    ready = Signal(str)  # key

    def __init__(self, box: int, parent: QObject | None = None, keep: int = 3):
        super().__init__(parent)
        self.box = box
        self.keep = keep
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._cache: OrderedDict[str, LinearPreview | None] = OrderedDict()
        self._tasks: dict[str, _Task] = {}  # key → its development, until it is done or dropped
        self._priority = 0
        self._emitter = _Emitter()
        self._emitter.done.connect(self._on_done)

    @staticmethod
    def key(path: str, mtime: float) -> str:
        return f"{path}|{mtime}"

    def get(self, path: str, mtime: float) -> LinearPreview | None:
        """The development if it is ready (None: not yet, or it failed); otherwise it is started."""
        key = self.key(path, mtime)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        if key not in self._tasks:
            self._tasks[key] = task = _Task(key, path, self.box, self._emitter)
            self._priority += 1
            self._pool.start(task, self._priority)
        return None

    def failed(self, path: str, mtime: float) -> bool:
        key = self.key(path, mtime)
        return key in self._cache and self._cache[key] is None

    def clear_queue(self) -> None:
        """Drops the developments that have not started (the one running finishes)."""
        for key, task in list(self._tasks.items()):
            if not task.started:
                task.dropped = True
                del self._tasks[key]

    @Slot(str, object)
    def _on_done(self, key: str, preview) -> None:
        self._tasks.pop(key, None)
        self._cache[key] = preview
        while len(self._cache) > self.keep:
            self._cache.popitem(last=False)
        self.ready.emit(key)

    def shutdown(self) -> None:
        self.clear_queue()
        self._pool.clear()
        self._pool.waitForDone(5000)
