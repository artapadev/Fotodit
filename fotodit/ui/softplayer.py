"""Backup player: decodes video and audio with PyAV and sends the audio to Qt's audio output.

Qt's FFmpeg cannot play some videos: it decodes AV1 only on the GPU (which most computers
do not support), and some files it cannot open at all. PyAV, which Fotodit uses for
processing, can decode them (dav1d for AV1). This player offers the same signals and
methods as the Qt one; `player.VideoPlayer` switches to it when needed.

* Video and audio are decoded on separate threads, with the file opened twice: even if
  the audio is stored ahead of or behind the video in the file, the queues stay small and
  neither holds up the other. Frames are scaled down to at most 1920×1080 and converted
  to images.
* The clock follows the amount of audio played (the wall clock if there is no audio): the
  video is shown in sync with the audio and late frames are skipped; if decoding cannot
  keep up, the audio waits too.
* Seeking happens on the decoders' own threads: they jump to the nearest keyframe before
  the target and skip the frames up to the target. Seeking while paused shows the frame
  at the target.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage

from ..i18n import t

MAX_W, MAX_H = 1920, 1080
VIDEO_QUEUE = 6  # decoded frames buffered ahead
AUDIO_AHEAD_MS = 1000  # decoded audio buffered ahead
TICK_MS = 8
SOFT_CODECS = {"av1"}  # codecs Qt cannot decode (with hardware decoding turned off)


class _Reader(threading.Thread):
    """Decodes the video or audio track of the file and puts the results in a queue."""

    def __init__(self, path: str, kind: str, audio_spec: tuple[str, str, int] | None = None):
        super().__init__(name=f"backup-player-{kind}", daemon=True)
        self.path, self.kind, self.audio_spec = path, kind, audio_spec
        self.cond = threading.Condition()
        self.queue: deque[tuple[int, object]] = deque()  # (ms, QImage or audio bytes)
        self.queued_ms = 0  # duration of the queued audio
        self.seek_to: int | None = 0
        self.generation = 0
        self.stop = False
        self.eof = False
        self.ready = False  # file opened, track looked up (found or not)
        self.absent = False  # no track of this kind (or it cannot be decoded)
        self.error: str | None = None
        self.duration_ms = 0
        self.rotation = 0  # clockwise, applied when displaying
        self.bytes_per_ms = 0.0

    # ------------------------------------------------------ from the main thread

    def request_seek(self, ms: int) -> None:
        with self.cond:
            self.seek_to = ms
            self.generation += 1
            self.queue.clear()
            self.queued_ms = 0
            self.eof = False
            self.cond.notify_all()

    def take(self, due_ms: int | None) -> object | None:
        """The item at the head of the queue (with `due_ms`: the last of the items due by that time)."""
        item = None
        with self.cond:
            if due_ms is None:
                if self.queue:
                    item = self.queue.popleft()
            else:
                while self.queue and self.queue[0][0] <= due_ms:
                    item = self.queue.popleft()  # late frames are skipped
            if item is not None:
                if self.kind == "audio":
                    self.queued_ms = max(0, self.queued_ms - int(len(item[1]) / self.bytes_per_ms))
                self.cond.notify_all()
        return item

    def drained(self) -> bool:
        with self.cond:
            return self.absent or (self.eof and not self.queue)

    def halt(self) -> None:
        with self.cond:
            self.stop = True
            self.cond.notify_all()

    # ----------------------------------------------------------------- decoding

    def _full(self) -> bool:
        if self.kind == "video":
            return len(self.queue) >= VIDEO_QUEUE
        return self.queued_ms >= AUDIO_AHEAD_MS

    def run(self) -> None:
        import av

        from ..core.video import quiet_ffmpeg

        quiet_ffmpeg()
        try:
            container = av.open(self.path)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self._finish(error=t("player.open_failed", error=exc))
            return
        try:
            with container:
                self._loop(av, container)
        except Exception as exc:  # noqa: BLE001
            self._finish(error=None if self.kind == "audio" else t("player.play_failed", error=exc))

    def _finish(self, error: str | None = None) -> None:
        with self.cond:
            self.error = error
            self.absent = self.kind == "audio"  # if the audio cannot be decoded, the video plays silently
            self.eof = self.ready = True
            self.cond.notify_all()

    def _loop(self, av, container) -> None:
        from ..core.video.probe import ATTACHED_PIC

        if self.kind == "video":
            stream = next((s for s in container.streams.video if not s.disposition & ATTACHED_PIC), None)
        else:
            stream = container.streams.audio[0] if container.streams.audio and self.audio_spec else None
        if stream is None:
            self._finish(error=t("player.no_picture") if self.kind == "video" else None)
            return
        stream.thread_type = "AUTO"
        duration = (container.duration or 0) / 1000
        if not duration and stream.duration and stream.time_base:
            duration = float(stream.duration * stream.time_base) * 1000
        resampler = None
        if self.kind == "audio":
            fmt, layout, rate = self.audio_spec
            self.bytes_per_ms = rate * (1 if layout == "mono" else 2) * (2 if fmt == "s16" else 4) / 1000
        with self.cond:
            self.duration_ms = int(duration)
            self.ready = True
        demux = None
        target, generation = 0, -1
        while True:
            with self.cond:
                while not self.stop and self.seek_to is None and (self.eof or self._full()):
                    self.cond.wait(0.1)
                if self.stop:
                    return
                if self.seek_to is not None:
                    target, self.seek_to = self.seek_to, None
                    generation = self.generation
                    demux = None
            if demux is None:
                try:
                    container.seek(max(0, target) * 1000, backward=True, any_frame=False)
                except av.error.FFmpegError:
                    container.seek(0)
                demux = container.demux(stream)
                if self.kind == "audio":
                    resampler = av.AudioResampler(format=self.audio_spec[0], layout=self.audio_spec[1],
                                                  rate=self.audio_spec[2])
            try:
                packet = next(demux)
            except StopIteration:
                with self.cond:
                    if generation == self.generation:
                        self.eof = True
                continue
            try:
                frames = packet.decode()
            except av.error.FFmpegError:
                continue  # a corrupt packet is skipped
            for frame in frames:
                item = self._video(frame, target) if self.kind == "video" else self._audio(frame, resampler, target)
                if item is None:
                    continue
                with self.cond:
                    if generation != self.generation:
                        break
                    self.queue.append(item)
                    if self.kind == "audio":
                        self.queued_ms += int(len(item[1]) / self.bytes_per_ms)

    def _video(self, frame, target: int):
        from ..core.video.probe import frame_orientation

        if frame.time is None:
            return None
        ms = int(frame.time * 1000)
        if ms < target - 1:
            return None  # frames before the seek target are skipped
        # (PyAV's frame.rotation reads a mirrored video, QuickTime's "Flip Horizontal", as a half turn.)
        self.rotation, mirrored = frame_orientation(frame)
        image = _to_image(frame)
        return ms, image.flipped(Qt.Orientation.Horizontal) if mirrored else image

    def _audio(self, frame, resampler, target: int):
        if frame.time is None:
            return None
        start = int(frame.time * 1000)
        data = b"".join(out.to_ndarray().tobytes() for out in resampler.resample(frame))
        if start < target and data:  # audio before the seek target is cut off (at a sample boundary)
            size = 2 if self.audio_spec[1] == "mono" else 4
            size *= 1 if self.audio_spec[0] == "s16" else 2
            cut = int((target - start) * self.bytes_per_ms)
            data = data[cut - cut % size:]
            start = target
        return (start, data) if data else None


def _to_image(frame) -> QImage:
    from ..core.video.thumb import colorspace_of

    w, h = frame.width, frame.height
    scale = min(1.0, MAX_W / max(w, h), MAX_H / min(w, h))
    tw, th = max(2, int(w * scale) & ~1), max(2, int(h * scale) & ~1)
    hdr = frame.color_trc in (16, 18)  # no tone mapping: good enough for a preview
    rgb = frame.reformat(width=tw, height=th, format="rgb24",
                         src_colorspace="BT2020" if hdr else colorspace_of(frame), interpolation="BILINEAR")
    array = rgb.to_ndarray()
    return QImage(array.data, tw, th, array.strides[0], QImage.Format.Format_RGB888).copy()


class SoftPlayer(QObject):
    """Same signals and methods as `player.VideoPlayer`."""

    frameReady = Signal(QImage, int)
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playingChanged = Signal(bool)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.path: str | None = None
        self._video: _Reader | None = None
        self._audio: _Reader | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._sink = None  # QAudioSink (if there is an audio device)
        self._io = None  # the device the audio is written to (reopened after a seek)
        self._audio_spec: tuple[str, str, int] | None = None
        self._carry = b""
        self._audio_done = False  # the audio ended before the video: the clock runs on wall time
        self._playing = False
        self._ended = False
        self._position = 0  # position while paused or before the audio starts (ms)
        self._audio_base = 0
        self._wall_base = 0
        self._wall_start = 0.0
        self._show_next = True
        self._duration_sent = 0
        self._last_emit = 0.0
        self._volume = 1.0
        self._muted = False
        self._reported = False

    # --------------------------------------------------------------- control

    def load(self, path: str, position: int = 0) -> None:
        self.unload()
        self.path = path
        self._audio_spec = self._open_audio()
        self._video = _Reader(path, "video")
        self._audio = _Reader(path, "audio", self._audio_spec) if self._audio_spec else None
        self._position = self._wall_base = max(0, position)
        for reader in self._readers():
            if position:
                reader.request_seek(position)
            reader.start()
        self._show_next = True
        self._ended = self._reported = False
        self._duration_sent = 0
        self._timer.start()

    def unload(self) -> None:
        """Releases the file (the decoders stop and the file is closed)."""
        self._timer.stop()
        readers = self._readers()
        for reader in readers:
            reader.halt()
        for reader in readers:
            reader.join(3.0)
        self._video = self._audio = None
        self._stop_audio()
        if self._playing:
            self._playing = False
            self.playingChanged.emit(False)
        self.path = None

    def shutdown(self) -> None:
        self.unload()

    def is_playing(self) -> bool:
        return self._playing

    def play(self) -> None:
        if self.path is None or self._playing:
            return
        if self._ended:
            self.seek(0)
        self._playing = True
        self._wall_base, self._wall_start = self._position, time.monotonic()
        if self._io is not None:
            self._sink.resume()
        self.playingChanged.emit(True)

    def pause(self) -> None:
        if not self._playing:
            return
        self._position = self._clock()
        self._playing = False
        if self._io is not None:
            self._sink.suspend()
        self.playingChanged.emit(False)

    def toggle(self) -> None:
        self.pause() if self._playing else self.play()

    def seek(self, ms: int) -> None:
        if self._video is None:
            return
        duration = self.duration()
        ms = max(0, min(ms, duration - 1 if duration > 0 else ms))
        self._position = self._wall_base = ms
        self._wall_start = time.monotonic()
        self._ended = False
        self._stop_audio()
        self._show_next = True
        for reader in self._readers():
            reader.request_seek(ms)
        self.positionChanged.emit(ms)

    def step(self, delta_ms: int) -> None:
        self.seek(self._clock() + delta_ms)

    def position(self) -> int:
        return self._clock()

    def duration(self) -> int:
        return self._video.duration_ms if self._video is not None else 0

    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, value))
        self._apply_volume()

    def volume(self) -> float:
        return self._volume

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        self._apply_volume()

    def muted(self) -> bool:
        return self._muted

    def _readers(self) -> list[_Reader]:
        return [r for r in (self._video, self._audio) if r is not None]

    # ------------------------------------------------------------------ audio

    def _open_audio(self) -> tuple[str, str, int] | None:
        """A format the audio device supports, and its QAudioSink (without a device the video plays silently)."""
        from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

        if self._sink is not None:
            return self._audio_spec
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            return None
        candidates = []
        for rate in dict.fromkeys((48000, 44100, device.preferredFormat().sampleRate())):
            for sample in (QAudioFormat.SampleFormat.Int16, QAudioFormat.SampleFormat.Float):
                fmt = QAudioFormat()
                fmt.setSampleRate(rate)
                fmt.setChannelCount(2)
                fmt.setSampleFormat(sample)
                candidates.append(fmt)
        chosen = next((f for f in candidates if device.isFormatSupported(f)), None)
        if chosen is None:
            return None
        self._sink = QAudioSink(device, chosen, self)
        self._apply_volume()
        sample = "s16" if chosen.sampleFormat() == QAudioFormat.SampleFormat.Int16 else "flt"
        return sample, "stereo", chosen.sampleRate()

    def _apply_volume(self) -> None:
        if self._sink is not None:
            self._sink.setVolume(0.0 if self._muted else self._volume)

    def _stop_audio(self) -> None:
        self._carry = b""
        self._audio_done = False
        if self._io is not None:
            self._sink.stop()
        self._io = None

    def _audio_clock(self) -> bool:
        """Is the clock driven by the audio? (It follows the wall clock if there is no audio track, the
        audio cannot be decoded or it ended before the video.)"""
        return (self._sink is not None and self._audio is not None and not self._audio.absent
                and not self._audio_done)

    def _audio_played_out(self) -> bool:
        return self._audio.drained() and not self._carry and (
            self._io is None or self._sink.bytesFree() >= self._sink.bufferSize())

    def _feed_audio(self) -> None:
        reader = self._audio
        if not self._audio_clock() or not reader.ready:
            return
        if self._io is None:
            if not reader.queue:
                return
            self._audio_base = self._position
            self._io = self._sink.start()
            if not self._playing:
                self._sink.suspend()
        free = self._sink.bytesFree()
        while free > 0:
            if not self._carry:
                item = reader.take(None)
                if item is None:
                    break
                self._carry = item[1]
            chunk, self._carry = self._carry[:free], self._carry[free:]
            written = self._io.write(chunk)
            if written is None or written <= 0:
                self._carry = chunk + self._carry
                break
            if written < len(chunk):
                self._carry = chunk[written:] + self._carry
                break
            free -= written

    def _clock(self) -> int:
        if not self._playing:
            return self._position
        if self._audio_clock():
            if self._io is None:
                return self._position  # the clock does not advance until the audio starts
            return self._audio_base + self._sink.processedUSecs() // 1000
        return self._wall_base + int((time.monotonic() - self._wall_start) * 1000)

    # ---------------------------------------------------------------- display

    def _tick(self) -> None:
        video = self._video
        if video is None:
            return
        if video.error and not self._reported:
            self._reported = True
            self.pause()
            self.failed.emit(video.error)
            return
        if video.duration_ms and video.duration_ms != self._duration_sent:
            self._duration_sent = video.duration_ms
            self.durationChanged.emit(video.duration_ms)
        if self._playing or self._io is None:
            self._feed_audio()
        if self._playing and self._audio_clock() and self._audio.ready and self._audio_played_out():
            # The audio ended (or there is none at the seek position) but the video goes on: switch to the wall clock.
            self._wall_base, self._wall_start = self._clock(), time.monotonic()
            self._audio_done = True
        now = self._clock()
        frame = None
        if self._show_next:
            frame = video.take(None)
            self._show_next = frame is None
        elif self._playing:
            frame = video.take(now + TICK_MS)
        if frame is not None:
            self.frameReady.emit(frame[1], video.rotation)
        if self._playing and time.monotonic() - self._last_emit >= 0.1:
            self._last_emit = time.monotonic()
            self.positionChanged.emit(min(now, video.duration_ms) if video.duration_ms else now)
        if self._playing and self._finished():
            self._position = max(now, video.duration_ms)
            self._playing, self._ended = False, True
            self._stop_audio()
            self.positionChanged.emit(self._position)
            self.playingChanged.emit(False)

    def _finished(self) -> bool:
        if not self._video.drained():
            return False
        return not self._audio_clock() or self._audio_played_out()
