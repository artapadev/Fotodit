"""Video playback in the large preview (Qt Multimedia, FFmpeg backend).

Frames are taken from a QVideoSink and painted in the preview's own image area
(no QVideoWidget): this way rotation, straightening and cropping show up instantly
during playback too, just as for photos, and crop mode works on the paused frame.

Decoding runs on the CPU (Qt's GPU/hardware decoding is turned off): the frames are
painted on the CPU anyway, so they would have to be copied back from the GPU; on top
of that, some drivers (e.g. Intel's HEVC) took seconds to initialize for every video.
Converting frames to images (~40 ms for a 4K 10-bit HDR frame) happens on a separate
thread; if it cannot keep up, intermediate frames are skipped and the UI stays smooth.

Videos Qt cannot decode (e.g. AV1: Qt's FFmpeg decodes it only on the GPU) are played
with the PyAV backup player (`softplayer`). If Qt cannot open a video, or no frames
arrive although the video time advances, playback also switches to the backup player
from the current position; the user is notified only if that one fails as well.

The bar under the video also trims it: "Buradan başlasın" / "Burada bitsin" set the kept
part at the current position, "Bölüm çıkar" marks a part to remove (pressed at its start, then
at its end), and the seek slider shows the kept parts on its groove.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, QRectF, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout

from ..core.video.probe import duration_text
from ..i18n import t
from . import glass, theme
from .controls import BarButton, GlassSlider
from .softplayer import SOFT_CODECS, SoftPlayer

KEPT = "#F2F2F2"  # the parts of a trimmed video that are kept (the removed parts stay faint)
MARKING = "#F87171"  # the part being marked for removal

NO_FRAME_POSITION = 2000  # ms: still no frame once the video has advanced this far → its picture cannot be decoded
NO_FRAME_TIMEOUT = 15.0  # s: if the video does not advance at all, the same verdict after this long at the latest

# Qt's FFmpeg plugin reads this when the first player is created (see Qt "Advanced FFmpeg Configuration").
os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", ",")


def _quiet_qt_ffmpeg() -> None:
    """Qt Multimedia's own FFmpeg (the avutil in the PySide6 folder) also writes a stream dump
    for every video; its log level is set through that same DLL (if Qt has already loaded it,
    this is the same instance)."""
    import ctypes
    import glob

    import PySide6

    for path in glob.glob(os.path.join(os.path.dirname(PySide6.__file__), "avutil-*.dll")):
        try:
            ctypes.CDLL(path).av_log_set_level(8)  # AV_LOG_FATAL: Qt reports errors through its own API
        except (OSError, AttributeError):
            pass


class _Converter(QObject):
    """Converts a frame to a QImage in the background."""

    done = Signal(QImage, int, int)  # image, extra rotation, load serial

    @Slot(object, int)
    def convert(self, frame, serial: int) -> None:
        # toImage() already applies the video's orientation (surface transform, portrait phone video);
        # the only thing it does not apply is the frame's own extra rotation.
        self.done.emit(frame.toImage(), frame.rotation().value % 360, serial)


class VideoPlayer(QObject):
    frameReady = Signal(QImage, int)  # frame, clockwise rotation to apply when displaying (degrees)
    positionChanged = Signal(int)  # ms
    durationChanged = Signal(int)
    playingChanged = Signal(bool)
    failed = Signal(str)
    _request = Signal(object, int)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

        _quiet_qt_ffmpeg()

        self._states = QMediaPlayer.PlaybackState
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._frame)
        # Qt emits these signals with 64-bit integers; they cannot be connected directly.
        self.player.positionChanged.connect(lambda ms: self._soft is None and self.positionChanged.emit(int(ms)))
        self.player.durationChanged.connect(lambda ms: self._soft is None and self.durationChanged.emit(int(ms)))
        self.player.playbackStateChanged.connect(self._state)
        self.player.errorOccurred.connect(self._error)
        self.player.mediaStatusChanged.connect(self._status)
        self._end_status = QMediaPlayer.MediaStatus.EndOfMedia
        self.path: str | None = None
        self._frames = 0
        self._started = 0.0
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(500)
        self._watchdog.timeout.connect(self._check_frames)
        self._serial = 0  # incremented on every load: a late frame of the previous video must not be shown
        self._busy = False
        self._pending = None
        self._thread = QThread(self)
        self._converter = _Converter()
        self._converter.moveToThread(self._thread)
        self._request.connect(self._converter.convert)
        self._converter.done.connect(self._converted)
        self._thread.start()
        self._backup: SoftPlayer | None = None  # PyAV backup player (created when first needed)
        self._soft: SoftPlayer | None = None  # the backup player while it is in use, else None

    # ------------------------------------------------------------ control

    def load(self, path: str, codec: str | None = None) -> None:
        """`codec`: the video's codec, if known (e.g. "av1" is opened directly with the backup player)."""
        if path == self.path:
            return
        self.unload()
        self.path = path
        self._frames = 0
        self._started = 0.0
        if codec in SOFT_CODECS:
            self._use_backup(0, play=False)
        else:
            self.player.setSource(QUrl.fromLocalFile(path))

    def unload(self) -> None:
        """Releases the file (on Windows an open file cannot be moved or deleted)."""
        self._watchdog.stop()
        self._serial += 1
        self._pending = None
        if self._soft is not None:
            self._soft.unload()
            self._soft = None
        if self.path is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        self.path = None

    def uses_backup(self) -> bool:
        """Is the video currently being played by the PyAV backup player?"""
        return self._soft is not None

    def _use_backup(self, position: int, play: bool) -> None:
        """A video Qt cannot play: switches to the PyAV backup player (from the current position)."""
        if self._backup is None:
            backup = self._backup = SoftPlayer(self)
            backup.frameReady.connect(self._backup_frame)
            backup.positionChanged.connect(lambda ms: self._soft is not None and self.positionChanged.emit(ms))
            backup.durationChanged.connect(lambda ms: self._soft is not None and self.durationChanged.emit(ms))
            backup.playingChanged.connect(lambda on: self._soft is not None and self.playingChanged.emit(on))
            backup.failed.connect(lambda text: self._soft is not None and self.failed.emit(text))
        self._watchdog.stop()
        self.player.stop()
        self.player.setSource(QUrl())  # let Qt release the file
        self._soft = self._backup
        self._soft.set_volume(self.audio.volume())
        self._soft.set_muted(self.audio.isMuted())
        self._soft.load(self.path, position)
        if play:
            self._soft.play()

    def _backup_frame(self, image: QImage, rotation: int) -> None:
        if self._soft is not None and self.path is not None:
            self._frames += 1
            self.frameReady.emit(image, rotation)

    def shutdown(self) -> None:
        """Releases the file and stops the conversion thread (when the window closes)."""
        self.unload()
        self._thread.quit()
        self._thread.wait(3000)

    def is_playing(self) -> bool:
        if self._soft is not None:
            return self._soft.is_playing()
        return self.player.playbackState() == self._states.PlayingState

    def play(self) -> None:
        if self.path is None:
            return
        if self._soft is not None:
            self._soft.play()
            return
        self.player.play()
        if not self._frames:
            self._started = time.monotonic()
            self._watchdog.start()

    def pause(self) -> None:
        if self._soft is not None:
            self._soft.pause()
        elif self.path is not None:
            self.player.pause()

    def toggle(self) -> None:
        self.pause() if self.is_playing() else self.play()

    def seek(self, ms: int) -> None:
        if self.path is None:
            return
        if self._soft is not None:
            self._soft.seek(ms)
            return
        duration = self.player.duration()
        self.player.setPosition(max(0, min(ms, duration - 1 if duration > 0 else ms)))

    def step(self, delta_ms: int) -> None:
        self.seek(self.position() + delta_ms)

    def position(self) -> int:
        return self._soft.position() if self._soft is not None else self.player.position()

    def duration(self) -> int:
        return self._soft.duration() if self._soft is not None else self.player.duration()

    def set_volume(self, value: float) -> None:
        self.audio.setVolume(max(0.0, min(1.0, value)))
        if self._soft is not None:
            self._soft.set_volume(value)

    def volume(self) -> float:
        return self.audio.volume()

    def set_muted(self, muted: bool) -> None:
        self.audio.setMuted(muted)
        if self._soft is not None:
            self._soft.set_muted(muted)

    def muted(self) -> bool:
        return self.audio.isMuted()

    # ------------------------------------------------------------ events

    def _frame(self, frame) -> None:
        if not frame.isValid() or self.path is None or self._soft is not None:
            return
        if self._busy:
            self._pending = frame  # while a conversion is running, only the newest incoming frame waits
            return
        self._busy = True
        self._request.emit(frame, self._serial)

    def _converted(self, image: QImage, rotation: int, serial: int) -> None:
        self._busy = False
        if serial == self._serial and self.path is not None and not image.isNull():
            self._frames += 1
            self._watchdog.stop()
            self.frameReady.emit(image, rotation)
        pending, self._pending = self._pending, None
        if pending is not None and self.path is not None:
            self._frame(pending)

    def _state(self, _state) -> None:
        if self._soft is None:
            self.playingChanged.emit(self.is_playing())

    def _error(self, _error, message: str) -> None:
        """Qt could not open or decode the file: the backup player tries (and reports if it fails too)."""
        if self._soft is not None or self.path is None:
            return
        self._watchdog.stop()
        self._use_backup(max(0, self.player.position()), play=bool(self._started))

    def _check_frames(self) -> None:
        """No frames arriving during playback? Opening can be slow (e.g. on a network drive), so the
        decision is based on how far the video time has advanced."""
        if self.path is None or self._frames or self._soft is not None:
            self._watchdog.stop()
            return
        if not self.is_playing():
            return
        if self.player.position() >= NO_FRAME_POSITION or time.monotonic() - self._started > NO_FRAME_TIMEOUT:
            self._no_frames()

    def _status(self, status) -> None:
        if status == self._end_status and self.path is not None and not self._frames and self._soft is None:
            self._no_frames()

    def _no_frames(self) -> None:
        """Qt cannot decode the picture: playback continues in the backup player from the current position."""
        self._watchdog.stop()
        ended = self.player.mediaStatus() == self._end_status
        self._use_backup(0 if ended else max(0, self.player.position() - 500), play=True)


class TrimSlider(GlassSlider):
    """The seek slider; for a trimmed video the kept parts are drawn on its track, and while a part
    to remove is being marked, the part from its start to the current position."""

    def __init__(self, parent=None):
        super().__init__(parent, media=True)
        self.setObjectName("PvSeek")
        self._kept: list[tuple[int, int | None]] | None = None  # ms; end None: to the end
        self._pending: int | None = None  # ms: where the part being marked starts

    def set_kept(self, kept: list[tuple[int, int | None]] | None) -> None:
        if kept != self._kept:
            self._kept = kept
            self.update()

    def kept(self) -> list[tuple[int, int | None]] | None:
        return self._kept

    def set_pending(self, ms: int | None) -> None:
        if ms != self._pending:
            self._pending = ms
            self.update()

    def pending(self) -> int | None:
        return self._pending

    def paint_track(self, p: QPainter, cy: float) -> None:
        left, span = self._span()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 60))
        p.drawRoundedRect(QRectF(left, cy - 2, span, 4), 2, 2)
        if self.maximum() <= self.minimum():
            return
        x = self._x_of(self.value())
        if self._kept:
            p.setBrush(QColor(KEPT))
            for start, end in self._kept:
                x0, x1 = self._x_of(start), self._x_of(self.maximum() if end is None else end)
                p.drawRoundedRect(QRectF(x0, cy - 3, max(2.0, x1 - x0), 6), 3, 3)
                for edge in (x0, x1):
                    p.drawRoundedRect(QRectF(edge - 1.5, cy - 9, 3, 18), 1.5, 1.5)
        else:
            p.setBrush(QColor(255, 255, 255, 230))
            p.drawRoundedRect(QRectF(left, cy - 2, max(0.0, x - left), 4), 2, 2)
        if self._pending is not None:
            mark = self._x_of(self._pending)
            x0, x1 = sorted((mark, x))
            p.setBrush(QColor(MARKING))
            p.drawRoundedRect(QRectF(x0, cy - 3, max(2.0, x1 - x0), 6), 3, 3)
            p.drawRoundedRect(QRectF(mark - 1.5, cy - 9, 3, 18), 1.5, 1.5)


TRIM_NOTE = "player.trim_note"
CUT_TIP = "player.cut_tip"
CUT_END_TIP = "player.cut_end_tip"
CUT_LABEL, CUT_END_LABEL = "player.cut", "player.cut_end"
AUDIO_TIP = "player.audio_tip"


class VideoBar(glass.GlassSurface):
    """Play/pause, time, seek slider and volume on the first row; trimming, removing parts and
    removing the sound on the second. A capsule of media glass over the video."""

    WIDTH = 900  # the bar's usual width (wider when the names of its buttons need it, see `fit`)

    trimStartRequested = Signal()  # keep the video from the current position
    trimEndRequested = Signal()  # keep it up to the current position
    trimClearRequested = Signal()
    cutRequested = Signal()  # mark the start, then the end, of a part to remove
    audioToggled = Signal(bool)  # remove (True) or keep the sound

    def __init__(self, player: VideoPlayer, parent=None):
        super().__init__(parent, kind=glass.MEDIA, radius=22)
        self.setObjectName("Media")
        self.player = player
        self.play_btn = BarButton("", "play.fill", media=True, tooltip=t("player.play_tip"),
                                  accessible=t("menu.play"))
        self.play_btn.clicked.connect(player.toggle)
        self.time = QLabel("0:00 / 0:00")
        self.time.setObjectName("PvMuted")
        self.time.setFont(theme.tabular(self.time.font()))
        self.slider = TrimSlider()
        self.slider.setRange(0, 0)
        self.slider.setToolTip(t("player.seek_tip"))
        self.slider.setAccessibleName(t("player.position"))
        # Any change the user makes seeks (dragging, a click on the track, a screen reader); the slider
        # following playback does not.
        self._following = False
        self.slider.valueChanged.connect(self._seek)
        self.slider.sliderPressed.connect(lambda: player.seek(self.slider.value()))
        self.mute_btn = BarButton("", "speaker", media=True, tooltip=t("player.mute_tip"), accessible=t("player.mute"))
        self.mute_btn.clicked.connect(self.toggle_mute)
        self.volume = GlassSlider(media=True)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(player.volume() * 100))
        self.volume.setFixedWidth(96)
        self.volume.setToolTip(t("player.volume_tip"))
        self.volume.setAccessibleName(t("player.volume"))
        self.volume.valueChanged.connect(lambda v: player.set_volume(v / 100))
        self.start_btn = BarButton(t("player.start_here"), "trim.start", media=True)
        self.start_btn.setToolTip(t("player.start_tip") + "\n" + t(TRIM_NOTE, cut=t(CUT_LABEL)))
        self.start_btn.clicked.connect(self.trimStartRequested)
        self.end_btn = BarButton(t("player.end_here"), "trim.end", media=True)
        self.end_btn.setToolTip(t("player.end_tip") + "\n" + t(TRIM_NOTE, cut=t(CUT_LABEL)))
        self.end_btn.clicked.connect(self.trimEndRequested)
        self.cut_btn = BarButton(t(CUT_LABEL), "scissors", media=True)
        self.cut_btn.setToolTip(t(CUT_TIP))
        self.cut_btn.clicked.connect(self.cutRequested)
        self.clear_btn = BarButton(t("player.clear_trim"), "undo", media=True)
        self.clear_btn.setToolTip(t("player.clear_trim_tip"))
        self.clear_btn.clicked.connect(self.trimClearRequested)
        self.clear_btn.hide()
        self.audio_btn = BarButton(t("player.remove_sound"), "speaker.slash", media=True)
        self.audio_btn.setCheckable(True)
        self.audio_btn.setToolTip(t(AUDIO_TIP))
        self.audio_btn.toggled.connect(self.audioToggled)
        for w in (self.play_btn, self.mute_btn, self.slider, self.volume, self.start_btn, self.end_btn,
                  self.cut_btn, self.clear_btn, self.audio_btn):
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(self.play_btn)
        top.addWidget(self.time)
        top.addWidget(self.slider, 1)
        top.addWidget(self.mute_btn)
        top.addWidget(self.volume)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(2)
        for b in (self.start_btn, self.end_btn, self.cut_btn, self.clear_btn):
            bottom.addWidget(b)
        bottom.addStretch(1)
        bottom.addWidget(self.audio_btn)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)
        layout.addLayout(top)
        layout.addLayout(bottom)
        self._rows = (top, bottom)
        self._cut_measured = ("", 0)  # (font, the cut button's width with its longer name)
        player.positionChanged.connect(self._position)
        player.durationChanged.connect(self._duration)
        player.playingChanged.connect(self._playing)

    def fit(self, room: int) -> int:
        """The bar's width in a place `room` pixels wide: WIDTH, or wider when the names of the second
        row's buttons need it. If they do not fit even then, that row shows only icons (their tooltips
        still say what they do), so that no button ever lies on top of another one."""
        self._set_compact(False)
        if self._needed() > room:
            self._set_compact(True)
        return min(max(self.WIDTH, self._needed()), room)

    def _set_compact(self, compact: bool) -> None:
        for b in (self.start_btn, self.end_btn, self.cut_btn, self.clear_btn, self.audio_btn):
            b.set_compact(compact)
        # With its name shown, the cut button is as wide as its longer name, so the row does not jump
        # while a part is being marked.
        self.cut_btn.setMinimumWidth(0 if compact else self._cut_width())

    def _cut_width(self) -> int:
        """The cut button's width with the longer of its two names (measured again only for a new font)."""
        key = self.cut_btn.font().key()
        if self._cut_measured[0] != key:
            text = self.cut_btn.text()
            widths = []
            for label in (CUT_LABEL, CUT_END_LABEL):
                self.cut_btn.setText(t(label))
                widths.append(self.cut_btn.sizeHint().width())
            self.cut_btn.setText(text)
            self._cut_measured = (key, max(widths))
        return self._cut_measured[1]

    def _needed(self) -> int:
        """The width the two rows need as their buttons are now (names shown or not)."""
        for row in (*self._rows, self.layout()):
            row.invalidate()  # (a button's new size reaches the rows only when they are laid out again)
        return self.layout().totalMinimumSize().width()

    def reset(self, duration_ms: int = 0) -> None:
        self._follow(lambda: (self.slider.setRange(0, max(0, duration_ms)), self.slider.setValue(0)))
        self._playing(False)
        self._update_time(0, duration_ms)

    def _follow(self, change) -> None:
        """Changes the slider without seeking (it shows where playback is)."""
        self._following = True
        try:
            change()
        finally:
            self._following = False

    def _seek(self, ms: int) -> None:
        if not self._following:
            self.player.seek(ms)

    def set_kept(self, kept: list[tuple[float, float | None]] | None) -> None:
        """Shows the kept parts (seconds) on the slider; None: the whole video is kept."""
        def ms(t: float | None) -> int | None:
            return None if t is None else int(round(t * 1000))

        self.slider.set_kept(None if kept is None else [(ms(a), ms(b)) for a, b in kept])
        self.clear_btn.setVisible(kept is not None)

    def set_audio_removed(self, removed: bool) -> None:
        """Shows whether the sound will be removed (without signals)."""
        self.audio_btn.blockSignals(True)
        self.audio_btn.setChecked(removed)
        self.audio_btn.blockSignals(False)
        self.audio_btn.setText(t("player.sound_removed") if removed else t("player.remove_sound"))
        self.audio_btn.updateGeometry()

    def set_pending(self, seconds: float | None) -> None:
        """Shows that a part to remove is being marked: from `seconds` to the current position."""
        self.slider.set_pending(None if seconds is None else int(round(seconds * 1000)))
        self.cut_btn.setText(t(CUT_END_LABEL if seconds is not None else CUT_LABEL))
        self.cut_btn.setToolTip(t(CUT_END_TIP if seconds is not None else CUT_TIP))
        self.cut_btn.update()

    def toggle_mute(self) -> None:
        self.player.set_muted(not self.player.muted())
        muted = self.player.muted()
        self.mute_btn.set_icon_name("speaker.slash" if muted else "speaker")
        self.mute_btn.setAccessibleName(t("player.unmute") if muted else t("player.mute"))

    def change_volume(self, delta: int) -> None:
        self.volume.setValue(max(0, min(100, self.volume.value() + delta)))

    def _position(self, ms: int) -> None:
        if not self.slider.isSliderDown():
            self._follow(lambda: self.slider.setValue(ms))
        self._update_time(ms, self.slider.maximum())

    def _duration(self, ms: int) -> None:
        self._follow(lambda: self.slider.setRange(0, max(0, ms)))
        self._update_time(self.slider.value(), ms)

    def _playing(self, playing: bool) -> None:
        self.play_btn.set_icon_name("pause.fill" if playing else "play.fill")
        self.play_btn.setAccessibleName(t("player.pause") if playing else t("menu.play"))

    def _update_time(self, ms: int, total: int) -> None:
        self.time.setText(f"{duration_text(ms / 1000)} / {duration_text(total / 1000)}")
