"""Operations applied to a single video: metadata stripping, conversion to MP4, flipping, rotation and
cropping.

Unlike a photo, a video is not loaded into memory: it is read packet by packet from the
source file and written to the destination (the temp file provided by the runner).

**Copying without re-encoding (lossless).** Video and audio packets are written unchanged
into a new container; quality does not change at all and it is fast (disk speed). When
metadata is stripped, only the video, audio and subtitle tracks are copied (subtitles keep
only their language tag; subtitle tracks carrying a location/camera log are removed, see
`subtitles`): location/sensor logs, cover images, chapters, maker boxes and all tags are
left behind; user data (SEI) in H.264/HEVC streams, EXIF in MJPEG frames and in a camera AVI's
stream header, and the recording date in DV frames are removed too (see `bitstream`). Audio
tracks FFmpeg cannot decode (e.g. an iPhone's spatial audio) cannot be copied either; they are
left out with a note. Dates in the file header are zeroed. When converting to MP4,
SRT/ASS/WebVTT subtitles are converted to MP4's subtitle format.

**90° rotation** is done without re-encoding in MP4/MOV/3GP: only the orientation (display
matrix) changes, just as phones do for portrait shots. A horizontal flip is never written that way
(many players ignore a mirrored matrix): it is applied to the pixels (`hflip`, before the rotation).
A source already mirrored by its matrix keeps that matrix when copied and is flipped in the pixels
when re-encoded.

**Timestamps** stay as in the source, except that an MP4/MOV/3GP made from another container
starts at 0 (as FFmpeg does): an MPEG-TS/PS clock starting at 1.4 s (or at hours) would otherwise
become an empty stretch before the first frame.

**Re-encoding** happens only when needed: a flip, crop & straighten, or an incompatible codec when
converting to MP4 (e.g. HEVC, MJPEG, WMV, MPEG-2). Video is encoded to H.264 (x264,
CRF 18: visually indistinguishable quality), audio to AAC. HDR (HLG/PQ) videos are
tone-mapped to SDR (see `tonemap`); interlaced videos are converted to full frames. On
request the video is encoded on the GPU (see `hwenc`); if the GPU has a 10-bit HEVC
encoder, HDR videos stay HDR with the "keep codec" option (`VIDEO_KEEP`). If GPU encoding
fails or produces corrupted output, the video is re-encoded on the CPU.

**Trimming** (MP4/MOV/3GP output only) also works without re-encoding. A copied video can only
start at a keyframe, so the packets from the last keyframe before the start are copied too, with
negative timestamps: the muxer then writes an edit list that makes players start at exactly the
first kept frame (the same thing FFmpeg does when cutting without re-encoding). At the end, the
packets decoded before the end point are kept (FFmpeg's rule) and the edit list is shortened to
the end point (`boxes.end_presentation`; the end point is first moved onto the header's millisecond
grid, so that nothing is copied past the edit list's end). Those frames stay in the file, hidden (up to one
keyframe interval before the start, usually a second or two; a few frames after the end); the
result notes say so. Audio, subtitles and kept data tracks are cut to the same window.

**Removing parts** ("Bölüm çıkar", in the middle or at either end) re-encodes the video (and its
audio): the frames and samples of the removed parts are dropped and the rest is joined, so nothing
of them stays in the file and every player shows the same (edit lists with several entries are not
supported everywhere). Subtitle cues and chapters are cut and shifted along. See `_find_window`
and `_Window`.

**Removing the audio** (`Edit.mute`) leaves the audio tracks out, on the lossless copy path as well
as when re-encoding; by itself it never re-encodes anything.

**Keeping the capture date** ("Çekim tarihini koru", with metadata stripped): an MP4/MOV/3GP gets
the capture date as the creation and modification time of its movie, track and media headers
(mvhd, tkhd, mdhd), in UTC as the format requires, and nothing else; no tag is written. Other
containers keep it only as the file's date (set by the runner).

The written file is always verified (tracks, duration, decoding at the start and the end,
packet counts, that the metadata to be stripped is really gone). On any problem a
`VideoError` is raised; the source file is never touched.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from ...i18n import date_time, decimal, t
from ..edits import Edit, clock
from ..formats import MATRIX_FORMATS, Fmt
from . import bitstream, hwenc, subtitles, tonemap
from .boxes import EPOCH_1904, BoxError, end_presentation, read_structure
from .probe import ATTACHED_PIC, ProbeError, VideoInfo, parse_date, probe

VIDEO_COMPATIBLE = "h264"  # most compatible in MP4: H.264 + AAC
VIDEO_KEEP = "keep"  # no re-encoding if the codec fits in MP4 (e.g. HEVC stays)

LOSSLESS = "lossless"
REENCODED = "reencoded"
CONVERTED = "converted"

CRF = 18
AUDIO_RATES = {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000}

VIDEO_EDIT_NEEDS_MP4 = "video.edit_needs_mp4"  # the key of a skip reason others compare with
TRIM_SEEK_AFTER = 2.0  # s: for a later start, MP4/MOV/3GP sources are read from the keyframe on
AUDIO_PREROLL = 1.0  # s: audio decoded before a trimmed video's start (a decoder needs the moments before)
MOVIE_TIMESCALE = 1000  # the MP4/MOV header's time unit as FFmpeg writes it (the edit list counts in it)
_EPS = 1e-6

MUXERS = {
    Fmt.MP4: "mp4", Fmt.MOV: "mov", Fmt.THREEGP: "3gp", Fmt.AVI: "avi", Fmt.MKV: "matroska",
    Fmt.WEBM: "webm", Fmt.WMV: "asf", Fmt.MTS: "mpegts", Fmt.MPEG: "vob", Fmt.FLV: "flv", Fmt.OGV: "ogg",
}

# Codecs that can be copied without re-encoding when converting to MP4.
COPY_VIDEO = {VIDEO_COMPATIBLE: {"h264"}, VIDEO_KEEP: {"h264", "hevc", "av1", "vp9", "mpeg4"}}
COPY_AUDIO = {VIDEO_COMPATIBLE: {"aac", "mp3"}, VIDEO_KEEP: {"aac", "mp3", "alac", "ac3", "eac3", "opus", "flac"}}
_COMPATIBLE_PIX = {"yuv420p", "yuvj420p"}
# Tags not written when metadata is kept (the container's own technical tags).
_TECHNICAL_TAGS = {"major_brand", "minor_version", "compatible_brands", "encoder", "duration", "xmp",
                   "creation_time-eng"}


class VideoError(Exception):
    """Carries a Turkish error message to show to the user."""


class VideoCancelled(Exception):
    pass


@dataclass
class VideoPlan:
    out_fmt: Fmt
    muxer: str
    encode_video: bool = False
    reasons: list[str] = field(default_factory=list)  # why the video is re-encoded
    rotation: int = 0  # orientation to write for the copied video (clockwise)
    edit: Edit | None = None  # edit applied to the pixels (when re-encoding)
    tonemap: str | None = None
    audio_encode: bool = False
    keep_metadata: bool = False
    codec: str = "h264"  # codec of the re-encoded video
    encoder: hwenc.Encoder | None = None  # GPU encoder (None: x264)
    trim: tuple[float, float] | None = None  # the kept part, seconds from the start (end may be inf)
    cuts: tuple[tuple[float, float], ...] = ()  # parts removed inside the trim (seconds from the start)
    removed: int = 0  # how many parts the edit removes (inside the trim or at its edges)
    mute: bool = False  # the audio tracks are left out (Edit.mute, for a video that has audio)

    @property
    def lossless(self) -> bool:
        return not self.encode_video and not self.audio_encode

    def noop(self, info: VideoInfo) -> bool:
        """Will the file's content stay exactly the same? (Same container, no re-encoding,
        metadata kept, same orientation: e.g. a video that is already MP4 with "convert to MP4".)"""
        return (self.keep_metadata and self.lossless and self.out_fmt == info.fmt
                and self.rotation == info.rotation and self.trim is None and not self.mute)


@dataclass
class VideoResult:
    method: str
    out_fmt: Fmt
    notes: list[str] = field(default_factory=list)


def _opt(opts, name: str, default):
    return getattr(opts, name, default)


def output_format(fmt: Fmt, opts) -> Fmt:
    return Fmt.MP4 if _opt(opts, "convert_video", False) else fmt


def edit_reason(fmt: Fmt, opts, edit: Edit | None) -> str | None:
    """Why the edit cannot be applied with these options, or None (for skipping it in the processing plan).
    Removing the audio works in every container."""
    if edit is None or replace(edit, mute=False).is_identity():
        return None
    if output_format(fmt, opts) not in MATRIX_FORMATS:
        return t(VIDEO_EDIT_NEEDS_MP4)
    return None


def plan_video(info: VideoInfo, opts, edit: Edit | None = None, detect: bool = False) -> VideoPlan:
    """What will be done to the video (also shown in the UI). `detect`: test the GPU encoder if
    it has not been tried yet (may take a second or two; only during processing)."""
    if edit is not None and edit.is_identity():
        edit = None
    out_fmt = output_format(info.fmt, opts)
    policy = _opt(opts, "video_codec", VIDEO_COMPATIBLE)
    muxer = MUXERS[out_fmt]
    if out_fmt == Fmt.MPEG and info.vcodec == "mpeg1video":
        muxer = "mpeg"
    plan = VideoPlan(out_fmt, muxer, keep_metadata=not _opt(opts, "strip_metadata", False))
    if edit is not None and (edit.trim is not None or edit.cuts):
        kept = edit.segments(info.duration)
        start, end = edit.trim_window(info.duration)
        if kept:
            last = kept[-1][1]
            if (edit.trim is None or edit.trim[1] is None) and (last is None or end is None or last >= end - _EPS):
                last = None  # to the end of the video, whatever its exact duration
            plan.trim = (kept[0][0], float("inf") if last is None else last)
            plan.cuts = tuple((kept[i][1], kept[i + 1][0]) for i in range(len(kept) - 1))
        else:  # everything was cut away: reported when processing
            plan.trim = (start, start)
        # Removed parts (also at the start or the end) are removed for good: re-encoded, below.
        plan.removed = sum(1 for a, b in edit.cuts if b > start + _EPS and (end is None or a < end - _EPS))
    reasons = plan.reasons
    # With "convert to MP4" on, the codec policy also applies to videos that are already MP4: with
    # "most compatible" every output is H.264 + AAC.
    if _opt(opts, "convert_video", False):
        allowed = COPY_VIDEO.get(policy, COPY_VIDEO[VIDEO_COMPATIBLE])
        if info.vcodec not in allowed:
            reasons.append(f"{_codec(info.vcodec)} → H.264")
        elif policy == VIDEO_COMPATIBLE and info.pix_fmt not in _COMPATIBLE_PIX:
            reasons.append("10 bit / 4:2:2 H.264 → 8 bit H.264")
        audio_ok = COPY_AUDIO.get(policy, COPY_AUDIO[VIDEO_COMPATIBLE])
        plan.audio_encode = info.acodec is not None and info.acodec not in audio_ok
    if edit is not None:
        if edit.mirror:
            # Always in the pixels, never as a mirrored display matrix: many players ignore those.
            reasons.append(t("video.reason.flip"))
        if edit.angle or edit.crop is not None:
            reasons.append(t("video.reason.crop"))
        elif edit.quarter and out_fmt not in MATRIX_FORMATS:
            reasons.append(t("video.reason.rotate"))
        elif edit.quarter:
            structure = info.structure.video if info.structure else None
            if info.mirrored or (structure is not None and not structure.matrix_ok):
                reasons.append(t("video.reason.rotate"))
    if plan.removed:
        reasons.append(t("video.reason.cut"))
        plan.audio_encode = info.acodec is not None  # the audio is cut sample-exactly and joined
    if edit is not None and edit.mute and info.audio_streams:
        plan.mute = True
        plan.audio_encode = False  # there is no audio left to encode
    plan.encode_video = bool(reasons)
    if plan.encode_video:
        plan.edit = edit
        hardware = _opt(opts, "video_hw", False)
        lookup = hwenc.find if detect else hwenc.peek
        if hardware and info.hdr and policy == VIDEO_KEEP:
            plan.encoder = lookup("hevc")  # 10-bit HEVC: HDR is preserved
            if plan.encoder is not None:
                plan.codec = "hevc"
        if plan.codec == "h264":
            plan.tonemap = info.hdr
            plan.encoder = lookup("h264") if hardware else None
    else:
        quarter = edit.quarter if edit is not None else 0
        plan.rotation = (info.rotation + 90 * quarter) % 360
    return plan


def _codec(name: str) -> str:
    from .probe import CODEC_NAMES

    return CODEC_NAMES.get(name, name.upper())


def x264_preset(width: int, height: int, fps: float) -> str:
    """So encoding finishes in reasonable time on ordinary computers too: a faster preset for
    large/high-frame-rate videos (quality is fixed by the CRF; a faster preset only makes the
    file a little larger)."""
    rate = width * height * max(fps or 30.0, 1.0)
    return "fast" if rate <= 1280 * 720 * 30 else "veryfast"


# ======================================================================= processing


def process_video(src: str, dst: str, opts, edit: Edit | None = None, *, info: VideoInfo | None = None,
                  progress: Callable[[float], None] | None = None,
                  cancelled: Callable[[], bool] | None = None, taken: datetime | None = None) -> VideoResult:
    """Processes the `src` video according to the options, writes it to `dst` and verifies it.
    `taken`: the capture date (local time) that `keep_date` keeps when metadata is stripped."""
    from . import quiet_ffmpeg

    quiet_ffmpeg()
    try:
        info = info or probe(src)
    except ProbeError as exc:
        raise VideoError(str(exc)) from exc
    plan = plan_video(info, opts, edit, detect=True)
    date_notes: list[str] = []
    created = None
    if not plan.keep_metadata and _opt(opts, "keep_date", False) and taken is not None:
        when = date_time(taken)
        created = _utc(taken) if plan.out_fmt in MATRIX_FORMATS else None
        if created is not None:
            date_notes.append(t("video.date_kept", when=when))
        elif plan.out_fmt in MATRIX_FORMATS:
            date_notes.append(t("video.date_file_only", when=when))
        else:
            date_notes.append(t("video.date_container", when=when))
    try:
        job = _attempt(src, dst, info, plan, progress, cancelled, created)
    except _HardwareFailed as failure:
        # The GPU could not encode it: redo on the CPU (x264; tone-mapped to H.264 if HDR).
        plan = replace(plan, encoder=None, codec="h264", tonemap=info.hdr)
        job = _attempt(src, dst, info, plan, progress, cancelled, created)
        job.notes.append(t("video.gpu_failed", encoder=failure.encoder.label, reason=failure.reason.rstrip(".")))
    if plan.trim is not None:
        start, end = plan.trim
        last = min(end, info.duration) if info.duration else end
        bounds = [start, *(point for cut in plan.cuts for point in cut), last]
        kept = ", ".join(f"{clock(a)} – {clock(b) if b != float('inf') else t('edit.end')}"
                         for a, b in zip(bounds[::2], bounds[1::2]))
        if plan.removed:
            job.notes.append(t("video.trimmed_cut", n=plan.removed, kept=kept))
        else:
            job.notes.append(t("video.trimmed", kept=kept))
        hidden = job.window.hidden if job.window is not None and not plan.encode_video else 0.0
        if hidden >= 0.05:
            job.notes.append(t("video.hidden_start", seconds=decimal(hidden), cut=t("player.cut")))
    method = LOSSLESS if plan.lossless else (CONVERTED if plan.out_fmt != info.fmt else REENCODED)
    return VideoResult(method, plan.out_fmt, job.notes + date_notes)


def _utc(taken: datetime) -> datetime | None:
    """The capture date (local time on this computer) in UTC, for the MP4/MOV header; None if it
    cannot be converted or falls outside 1971–2100 (the years `boxes.qt_datetime` takes for a real
    header date)."""
    try:
        utc = taken.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
    except (OverflowError, OSError, ValueError):
        return None
    return utc if 1971 <= utc.year <= 2100 else None


def _header_seconds(when: datetime) -> int:
    """A UTC time as the MP4/MOV header stores it: seconds since 1904."""
    return int((when - EPOCH_1904).total_seconds())


class _HardwareFailed(Exception):
    def __init__(self, encoder: hwenc.Encoder, reason: str):
        super().__init__(reason)
        self.encoder, self.reason = encoder, reason


class _BadOutput(VideoError):
    """The encoded video differs noticeably from the source (e.g. frames corrupted by a driver bug)."""


def _attempt(src, dst, info: VideoInfo, plan: VideoPlan, progress, cancelled, created: datetime | None) -> _Job:
    job = _Job(src, dst, info, plan, progress, cancelled, created)
    try:
        job.run()
        _verify(job)
    except VideoCancelled:
        _remove(dst)
        raise
    except MemoryError as exc:
        _remove(dst)
        raise VideoError(t("video.out_of_memory")) from exc
    except Exception as exc:  # noqa: BLE001 - FFmpeg errors are explained to the user
        _remove(dst)
        reason = str(exc) if isinstance(exc, VideoError) else _explain(exc)
        if plan.encoder is not None and not _disk_full(exc):
            if isinstance(exc, _BadOutput):
                hwenc.mark_broken(plan.encoder)
            raise _HardwareFailed(plan.encoder, reason) from exc
        if isinstance(exc, VideoError):
            raise
        raise VideoError(reason) from exc
    if plan.encoder is not None:
        job.notes.append(t("video.gpu_used", encoder=plan.encoder.label))
    return job


def _disk_full(exc: Exception) -> bool:
    return isinstance(exc, OSError) and getattr(exc, "errno", None) == 28


def _explain(exc: Exception) -> str:
    import av

    if isinstance(exc, av.error.InvalidDataError):
        return t("video.damaged")
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return t("video.disk_full")
    if isinstance(exc, av.error.FFmpegError):
        return t("video.failed", error=exc.strerror or exc)
    return t("video.failed", error=exc)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class _Job:
    def __init__(self, src, dst, info: VideoInfo, plan: VideoPlan, progress, cancelled,
                 created: datetime | None = None):
        self.src, self.dst, self.info, self.plan = src, dst, info, plan
        self.progress = progress or (lambda f: None)
        self.cancelled = cancelled or (lambda: False)
        self.created = created  # the capture date (UTC) written into the MP4/MOV header (keep_date)
        self.notes: list[str] = []
        self.written: dict[int, int] = {}  # output stream index → packets written
        self.expected_streams: list[str] = []
        self.out_size: tuple[int, int] | None = None  # size of the re-encoded video
        self.reference = None  # small grayscale copy of the first frame fed to the encoder (for verification)
        self.drop_data = False
        self.window: _Window | None = None  # the kept part of a trimmed video
        self.offset = 0.0  # s: taken off every time when an MP4/MOV is made from another container (see _run)
        self._last_report = 0.0

    def in_options(self) -> dict:
        return {} if self.info.fmt in MATRIX_FORMATS else {"fflags": "+genpts"}

    def shift(self, time_base) -> int:
        """`offset` in `time_base` units (0 for a trimmed video: its window moves the times itself)."""
        return int(round(self.offset / time_base)) if self.offset and time_base else 0

    def expected_duration(self) -> float:
        """The output's duration (0 if not known)."""
        w, duration = self.window, self.info.duration
        if w is None or not duration:
            return duration
        end = min(w.end, w.base + duration)
        return max(0.0, end - w.start - sum(max(0.0, min(b, end) - a) for a, b in w.cuts))

    # ------------------------------------------------------------ setup

    def run(self) -> None:
        if self.plan.trim is not None and self.window is None:
            self.window = _find_window(self.src, self.info, self.in_options(), *self.plan.trim, self.plan.cuts)
            if self._ends_early():
                # The edit list ends on the header's millisecond grid: nothing that starts after that may be
                # written, or FFmpeg leaves it out when the file is read back (and the packet count differs).
                w = self.window
                w.end = w.start + math.floor((w.end - w.start) * MOVIE_TIMESCALE + _EPS) / MOVIE_TIMESCALE
        try:
            self._run()
        except _RetryWithoutData:
            _remove(self.dst)
            self.drop_data = True
            self.written.clear()
            self.expected_streams.clear()
            self.notes.clear()
            self.reference = None
            self._run()
        if self._ends_early():
            # The frames a copied video needs past its end (B-frame references) are not shown.
            end_presentation(self.dst, self.expected_duration())

    def _ends_early(self) -> bool:
        """Does a trimmed MP4/MOV/3GP end before the source does? Its edit list is then shortened to the
        end point (`boxes.end_presentation`)."""
        w = self.window
        return w is not None and self.plan.out_fmt in MATRIX_FORMATS and w.end < w.base + self.info.duration - 0.05

    def _run(self) -> None:
        import av

        plan, info = self.plan, self.info
        strip = not plan.keep_metadata
        with av.open(self.src, options=self.in_options()) as inp:
            w = self.window
            if w is not None and w.seek:
                video = next(s for s in inp.streams.video if not s.disposition & ATTACHED_PIC)
                inp.seek(w.key_pts, stream=video, backward=True, any_frame=False)
            if w is None and plan.out_fmt in MATRIX_FORMATS and info.fmt not in MATRIX_FORMATS:
                # As FFmpeg does, the MP4/MOV starts at 0: kept as is, the clock of an MPEG-TS/PS (1.4 s, or
                # hours) would become an empty edit at the start, shown as black before the first frame.
                self.offset = max(0.0, (inp.start_time or 0) / 1_000_000)
            mux_opts = {"fflags": "+bitexact"}
            flags = []
            if plan.out_fmt in MATRIX_FORMATS:
                if plan.out_fmt != info.fmt:
                    flags.append("faststart")
                if plan.keep_metadata and any(k.lower().startswith("com.") for k in info.tags):
                    flags.append("use_metadata_tags")
            if flags:
                mux_opts["movflags"] = "+" + "+".join(flags)
            if plan.out_fmt == Fmt.MTS and _m2ts(self.src):
                mux_opts["mpegts_m2ts_mode"] = "1"
            # Written through a file object that is closed whatever happens: if FFmpeg cannot write the
            # header, PyAV would keep a file it opened itself open (and, on Windows, locked) for good.
            # Unbuffered, so that FFmpeg reads back what was written when it moves the index forward.
            with open(self.dst, "wb", buffering=0) as fh:
                out = av.open(fh, "w", format=plan.muxer, options=mux_opts)
                try:
                    handlers = self._setup(inp, out, strip)
                    if plan.keep_metadata:
                        out.metadata.update({k: v for k, v in info.tags.items()
                                             if k.lower() not in _TECHNICAL_TAGS})
                        self._copy_chapters(inp, out)
                    elif self.created is not None:
                        # The MP4/MOV muxer writes it as the creation and modification time of the movie,
                        # track and media headers (mvhd, tkhd, mdhd); it writes no tag for it.
                        out.metadata["creation_time"] = f"{self.created:%Y-%m-%dT%H:%M:%S}.000000Z"
                    self._loop(inp, out, handlers)
                finally:
                    out.close()

    def _setup(self, inp, out, strip: bool) -> dict:
        plan = self.plan
        handlers = {}
        video = next((s for s in inp.streams.video if not s.disposition & ATTACHED_PIC), None)
        if video is None:
            raise VideoError(t("video.no_picture"))
        if plan.encode_video:
            handlers[video.index] = _VideoEncoder(self, video, out)
        else:
            handlers[video.index] = _Copier(self, video, out, strip, rotation=plan.rotation)
        self.expected_streams.append("video")
        unsupported = 0
        for s in inp.streams.audio:
            if plan.mute:
                continue
            if s.codec_context is None:
                # FFmpeg has no decoder for it (e.g. an iPhone's spatial audio track next to the stereo one):
                # PyAV can neither copy nor convert such a track.
                unsupported += 1
                continue
            if plan.audio_encode:
                handlers[s.index] = _AudioEncoder(self, s, out)
            else:
                handlers[s.index] = _Copier(self, s, out, strip)
            self.expected_streams.append("audio")
        if plan.mute:
            self.notes.append(t("video.muted"))
        elif unsupported:
            self.notes.append(t("video.audio_unsupported", n=unsupported))
        removed, lost = [], []  # stripped as metadata / could not be carried over to the new format
        others = [s for s in inp.streams if s.index not in handlers and s.type != "audio"]
        for s in others:
            if s.type == "video" and s.disposition & ATTACHED_PIC:
                (removed if strip else lost).append(t("video.track.cover"))
                continue
            if s.type == "subtitle":
                handler = self._subtitle(s, out, strip, removed, lost)
                if handler is not None:
                    handlers[s.index] = handler
                    self.expected_streams.append("subtitle")
                continue
            keep = plan.keep_metadata and not self.drop_data and self._can_keep(s)
            if keep:
                try:
                    handlers[s.index] = _Copier(self, s, out, strip=False)
                    self.expected_streams.append(s.type)
                    continue
                except Exception:  # noqa: BLE001 - the container does not accept this track
                    pass
            (removed if strip else lost).append(_stream_kind(s))
        if removed:
            self.notes.append(t("video.removed", items=", ".join(sorted(set(removed)))))
        if lost:
            self.notes.append(t("video.lost", items=", ".join(sorted(set(lost)))))
        return handlers

    def _can_keep(self, s) -> bool:
        """With metadata kept, can an extra track (data, attachment) go into the new container? MP4 takes
        GoPro's telemetry ('gpmd', FFmpeg's bin_data) but not a timecode ('tmcd') or Apple's 'mebx'
        track, which only MOV holds: the file header could not be written with one of those."""
        if self.plan.out_fmt == Fmt.MP4 and s.type == "data" and s.name != "bin_data":
            return False
        if self.plan.out_fmt == self.info.fmt:
            return s.type in ("data", "attachment")
        return s.type == "data" and self.plan.out_fmt in MATRIX_FORMATS

    def _subtitle(self, s, out, strip: bool, removed: list[str], lost: list[str]):
        """Handler for a subtitle track: copied as is or converted to MP4's subtitle format.
        Tracks carrying a location/camera log are removed when metadata is stripped."""
        track = self.info.subtitle(s.index)
        codec = track.codec if track is not None else subtitles.codec_name(s)
        if strip and track is not None and track.telemetry:
            removed.append(t("video.track.subtitle_log", log=track.telemetry))
            return None
        if self.drop_data:
            lost.append(t("video.track.subtitle"))
            return None
        matrix = self.plan.out_fmt in MATRIX_FORMATS
        if self.plan.out_fmt == self.info.fmt or (matrix and codec in subtitles.MP4_COPYABLE):
            try:
                return _Copier(self, s, out, strip)
            except Exception:  # noqa: BLE001 - the container does not accept this track: try converting it
                pass
        if matrix and codec in subtitles.TEXT_CODECS:
            try:
                return _SubtitleConverter(self, s, out)
            except Exception:  # noqa: BLE001
                pass
        lost.append(t("video.track.subtitle"))
        return None

    def _copy_chapters(self, inp, out) -> None:
        try:
            chapters = inp.chapters()
            if chapters and self.window is not None:
                chapters = self.window.chapters(chapters)
            elif chapters and self.offset:
                chapters = [dict(c, start=c["start"] - self.shift(c["time_base"]),
                                 end=c["end"] - self.shift(c["time_base"])) for c in chapters]
            if chapters:
                out.set_chapters(chapters)
        except Exception:  # noqa: BLE001 - if the chapters cannot be copied, the video is still processed
            self.notes.append(t("video.no_chapters"))

    # ------------------------------------------------------------ loop

    def _loop(self, inp, out, handlers: dict) -> None:
        import av

        streams = [s for s in inp.streams if s.index in handlers]
        video_index = next(i for i, h in handlers.items() if isinstance(h, (_VideoEncoder, _Copier))
                           and h.stream.type == "video")
        w = self.window
        duration = self.expected_duration() if w is not None else self.info.duration or 0.0
        # A trimmed video is read only up to its end: then the video and audio handlers have finished.
        dense = [h for h in handlers.values() if h.stream.type in ("video", "audio")]
        try:
            for packet in inp.demux(*streams):
                if self.cancelled():
                    raise VideoCancelled
                handler = handlers[packet.stream.index]
                handler.feed(packet)
                if packet.stream.index == video_index and duration and packet.pts is not None \
                        and packet.time_base is not None:
                    t = float(packet.pts * packet.time_base)
                    self._report((w.out_time(t) if w is not None else t) / duration)
                if w is not None and handler.finished and all(h.finished for h in dense):
                    break
            for handler in handlers.values():
                handler.flush()
        except av.error.FFmpegError as exc:
            # If the header could not be written, the extra tracks are most likely at fault: retry without them.
            if not self.drop_data and any(h.stream.type in ("data", "subtitle") for h in handlers.values()) \
                    and not any(self.written.values()):
                raise _RetryWithoutData from exc
            raise
        self.progress(1.0)

    def _report(self, fraction: float) -> None:
        now = time.monotonic()
        if now - self._last_report >= 0.2:
            self._last_report = now
            self.progress(min(max(fraction, 0.0), 0.999))

    def mux(self, out, packet, index: int) -> None:
        out.mux(packet)
        self.written[index] = self.written.get(index, 0) + 1


class _RetryWithoutData(Exception):
    pass


# ================================================================== trimming


@dataclass
class _Window:
    """The kept part of a trimmed video on the source's own timeline (seconds): from `start` to
    `end`, without the `cuts`. A kept time moves back by `start` and by the removed time before it,
    so the output starts at 0 and the parts follow each other."""

    start: float  # the first kept frame's time
    end: float  # nothing from this time on is kept (inf: to the end)
    base: float  # the source's start time (where the player's 0:00 is)
    key_pts: int  # the keyframe copying/decoding starts from (video time base)
    seek: bool  # jump to that keyframe instead of reading from the beginning
    cuts: tuple[tuple[float, float], ...] = ()  # removed parts (source times, sorted)
    hidden: float = 0.0  # s: a copied video keeps this much before the start (hidden by the edit list)

    def shift(self, time_base) -> int:
        return int(round(self.start / time_base))

    def removed_before(self, t: float) -> float:
        return sum(min(b, t) - a for a, b in self.cuts if a < t)

    def kept(self, t: float) -> bool:
        """Is the moment `t` in the output?"""
        return (self.start - _EPS <= t < self.end - _EPS
                and not any(a - _EPS <= t < b - _EPS for a, b in self.cuts))

    def out_time(self, t: float) -> float:
        """Where the moment `t` is in the output (negative before the start)."""
        return t - self.start - self.removed_before(t)

    def pieces(self, t0: float, t1: float) -> list[tuple[float, float]]:
        """The kept parts of the span [t0, t1) (source times)."""
        bounds = [self.start, *(t for cut in self.cuts for t in cut), self.end]
        out = []
        for i in range(0, len(bounds), 2):
            a, b = max(t0, bounds[i]), min(t1, bounds[i + 1])
            if b - a > _EPS:
                out.append((a, b))
        return out

    def span(self, t0: float, t1: float) -> tuple[float, float] | None:
        """Where the kept parts of [t0, t1) are in the output: (start, length), or None if nothing
        of it is kept. The parts are joined (a removed part in between leaves no gap)."""
        pieces = self.pieces(t0, t1)
        if not pieces:
            return None
        return self.out_time(pieces[0][0]), sum(b - a for a, b in pieces)

    def chapters(self, chapters: list[dict]) -> list[dict]:
        """The chapters that fall in the window, cut to it and shifted."""
        kept = []
        for chapter in chapters:
            tb = chapter["time_base"]
            span = self.span(float(chapter["start"] * tb), float(chapter["end"] * tb))
            if span is None:
                continue
            begin, length = span
            kept.append(dict(chapter, start=int(round(begin / tb)), end=int(round((begin + length) / tb))))
        return kept


def _find_window(src: str, info: VideoInfo, in_opts: dict, start: float, end: float,
                 cuts: tuple[tuple[float, float], ...] = ()) -> _Window:
    """Where a trimmed video starts: the first frame at or after `start` (seconds from the video's
    start) and the last keyframe before it, which copying or decoding has to start from. Only the
    video packets are read, without decoding; an MP4/MOV/3GP source is read from a keyframe shortly
    before the start (its index makes jumping there exact). `cuts`: parts removed in between."""
    if end <= start + _EPS:
        raise VideoError(t("video.empty_all"))
    seek = info.fmt in MATRIX_FORMATS and start > TRIM_SEEK_AFTER
    found = _scan_window(src, in_opts, start, seek)
    if found["key"] is None and seek:  # the jump went past the start: read from the beginning
        seek = False
        found = _scan_window(src, in_opts, start, False)
    if found["first"] is None:
        raise VideoError(t("video.empty_start"))
    tb, base = found["time_base"], found["base"]
    begin = float(found["first"] * tb)
    stop = base + end
    if stop <= begin + _EPS:
        raise VideoError(t("video.empty_end"))
    # The removed parts on the source's timeline, inside the window (the first kept frame may lie a
    # little after the requested start).
    removed = tuple((max(base + a, begin), base + b) for a, b in cuts
                    if base + b > begin + _EPS and base + a < stop - _EPS)
    if stop - begin - sum(min(b, stop) - a for a, b in removed) <= _EPS:
        raise VideoError(t("video.empty_all"))
    key = found["key"] if found["key"] is not None else found["first_packet"]
    return _Window(begin, stop, base, key, seek, removed, hidden=max(0.0, begin - float(key * tb)))


def _scan_window(src: str, in_opts: dict, start: float, seek: bool) -> dict:
    import av

    with av.open(src, options=in_opts) as c:
        video = next((s for s in c.streams.video if not s.disposition & ATTACHED_PIC), None)
        if video is None or not video.time_base:
            raise VideoError(t("video.no_picture"))
        tb = video.time_base
        if c.start_time is not None:
            base = c.start_time / 1_000_000
        else:
            base = float(video.start_time * tb) if video.start_time is not None else 0.0
        at = base + start
        if seek:
            c.seek(int(at / tb), stream=video, backward=True, any_frame=False)
        found = {"key": None, "first": None, "first_packet": None, "time_base": tb, "base": base}
        after = 0
        later = []  # keyframes after the requested start
        for packet in c.demux(video):
            if packet.pts is None:
                continue
            if found["first_packet"] is None:
                found["first_packet"] = packet.pts
            when = float(packet.pts * tb)
            if packet.is_keyframe:
                if when <= at + _EPS:
                    found["key"] = packet.pts
                else:
                    later.append(packet.pts)
            if when >= at - _EPS:
                found["first"] = packet.pts if found["first"] is None else min(found["first"], packet.pts)
                after += 1
                if after >= 32:  # well past any B-frame reordering: no earlier frame can come any more
                    break
    if found["first"] in later:
        # The first kept frame is a keyframe itself (a start just before it; every frame in an all-intra
        # video): starting at an earlier one would copy a whole group of frames that players, and FFmpeg
        # reading the file back, leave out.
        found["key"] = found["first"]
    return found


def _slice_audio(frame, a: int, b: int):
    """Samples a…b of a decoded audio frame (as a new frame)."""
    import av
    import numpy as np

    data = frame.to_ndarray()
    if not frame.format.is_planar:
        channels = len(frame.layout.channels)
        a, b = a * channels, b * channels
    part = av.AudioFrame.from_ndarray(np.ascontiguousarray(data[:, a:b]), format=frame.format.name,
                                      layout=frame.layout.name)
    part.sample_rate = frame.sample_rate
    return part


def _stream_kind(s) -> str:
    kind = s.type if s.type in ("subtitle", "data", "attachment", "video", "audio") else "other"
    return t(f"video.track.{kind}")


def _m2ts(path: str) -> bool:
    """Does it use 192-byte packets (M2TS/AVCHD)?"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4 + 192 * 3)
    except OSError:
        return False
    return len(head) > 4 + 192 * 2 and all(head[4 + i * 192] == 0x47 for i in range(3))


# ================================================================== track handlers


class _Copier:
    """Copies packets without re-encoding; when metadata is stripped, removes embedded
    user data. For a trimmed video only the packets of the window are copied (see `_trim`)."""

    finished = False  # trimmed: past the end of the window

    def __init__(self, job: _Job, stream, out, strip: bool, rotation: int | None = None):
        self.job, self.stream, self.out = job, stream, out
        self.ostream = out.add_stream_from_template(stream, opaque=True)
        self.index = self.ostream.index
        self.last_dts: int | None = None
        self.window = job.window
        self.shift = job.shift(stream.time_base)  # an MP4/MOV made from a TS/PS starts at 0
        self.started = self.window is None or stream.type != "video"  # video: from the keyframe on
        codec = stream.codec_context.codec.canonical_name if stream.codec_context is not None else ""
        self.scrubber = None
        self.filter = None
        if strip and stream.type == "video":
            if codec in ("h264", "hevc"):
                self.scrubber = bitstream.SeiScrubber(codec, stream.codec_context.extradata)
                if codec == "hevc" and stream.codec_context.extradata:
                    cleaned = bitstream.scrub_hvcc(stream.codec_context.extradata)
                    if cleaned is not None:
                        self.ostream.codec_context.extradata = cleaned
            elif codec == "mjpeg":
                self.filter = bitstream.strip_mjpeg
            elif codec in ("mpeg1video", "mpeg2video"):
                self.filter = bitstream.strip_mpeg_user_data
            elif codec == "dvvideo":
                self.filter = bitstream.strip_dv
            if bitstream.camera_extradata(codec, stream.codec_context.extradata if codec else None):
                # Camera EXIF from an AVI's stream header, which FFmpeg passes on as the codec configuration.
                self.ostream.codec_context.extradata = None
        if stream.type == "video":
            if codec == "hevc" and job.plan.out_fmt in MATRIX_FORMATS and stream.codec_context.extradata:
                self.ostream.codec_context.codec_tag = "hvc1"  # the tag Apple devices expect
            if rotation is not None and job.plan.out_fmt in MATRIX_FORMATS and not (
                    job.info.mirrored and rotation == job.info.rotation):
                # PyAV measures rotation counterclockwise; write 0 explicitly too (so the source's does not carry over).
                # A mirrored source keeps its own matrix (copied with the track): as a rotation it would be upside down.
                self.ostream.set_display_rotation((-rotation) % 360)
        if strip and stream.type != "subtitle":
            self.ostream.metadata.clear()
        else:  # subtitles keep their language even when metadata is stripped (players identify tracks by it)
            _keep_language(stream, self.ostream)
        if stream.type == "subtitle":
            _copy_disposition(stream, self.ostream)

    def feed(self, packet) -> None:
        import av

        if packet.dts is None and packet.pts is None:
            return  # end-of-stream marker
        if packet.size == 0:
            return
        if self.window is not None and not self._trim(packet):
            return
        data = None
        if self.scrubber is not None:
            data = self.scrubber.scrub(bytes(packet))
        elif self.filter is not None:
            data = self.filter(bytes(packet))
        if data is not None:
            fresh = av.Packet(data)
            fresh.pts, fresh.dts, fresh.duration = packet.pts, packet.dts, packet.duration
            fresh.time_base = packet.time_base
            fresh.is_keyframe = packet.is_keyframe
            packet = fresh
        if self.shift:
            if packet.pts is not None:
                packet.pts -= self.shift
            if packet.dts is not None:
                packet.dts -= self.shift
        if packet.dts is None and self.last_dts is not None:
            packet.dts = packet.pts
        # The first packets of an MKV with B-frames have no decoding time: the muxer works it out. Given
        # their presentation time instead, the repair below would show those frames out of order.
        if packet.dts is not None:
            # In files from some cameras the timestamps go backwards; the container does not accept that.
            if self.last_dts is not None and packet.dts <= self.last_dts:
                packet.dts = self.last_dts + 1
                if packet.pts is not None and packet.pts < packet.dts:
                    packet.pts = packet.dts
            self.last_dts = packet.dts
        packet.stream = self.ostream
        self.job.mux(self.out, packet, self.index)

    def _trim(self, packet) -> bool:
        """Is the packet in the window? If so, its timestamps are moved to the output's timeline.
        Video is copied from the keyframe before the start (frames before the start get negative
        times: the edit list hides them) up to the last packet decoded before the end; a copied
        video has no removed parts (removing them re-encodes it). Audio packets overlapping the
        window are kept, subtitle cues are cut to it (a cue spanning a removed part gets shorter),
        data samples must fall inside it."""
        w = self.window
        tb = packet.time_base or self.stream.time_base
        pts = packet.pts if packet.pts is not None else packet.dts
        t = float(pts * tb)
        if self.stream.type == "video":
            if not self.started:
                # The keyframe found beforehand; or, should reading have begun past it, the first keyframe.
                if pts != w.key_pts and not (packet.is_keyframe and t > w.start):
                    return False
                self.started = True
            dts = packet.dts if packet.dts is not None else pts
            if float(dts * tb) >= w.end - _EPS:
                self.finished = True
                return False
            shift = w.shift(tb)
        else:
            if t >= w.end - _EPS:
                self.finished = True
                return False
            length = float((packet.duration or 0) * tb)
            if length:
                span = w.span(t, t + length)
            else:
                span = (w.out_time(t), 0.0) if w.kept(t) else None
            if span is None:
                return False
            if self.stream.type == "subtitle":
                packet.pts = packet.dts = int(round(span[0] / tb))
                if length:
                    packet.duration = max(1, int(round(span[1] / tb)))
                return True
            if self.stream.type != "audio" and not w.kept(t):
                return False
            shift = w.shift(tb) + int(round(w.removed_before(t) / tb))
        if packet.pts is not None:
            packet.pts -= shift
        if packet.dts is not None:
            packet.dts -= shift
        return True

    def flush(self) -> None:
        pass


def _keep_language(stream, ostream) -> None:
    language = stream.metadata.get("language")
    ostream.metadata.clear()
    if language and language != "und":
        ostream.metadata["language"] = language


def _copy_disposition(stream, ostream) -> None:
    """Default/forced subtitle flags (players use them to decide which track to show)."""
    try:
        ostream.disposition = stream.disposition
    except Exception:  # noqa: BLE001 - if the flags cannot be copied, the subtitle is still kept
        pass


class _SubtitleConverter:
    """Converts text subtitles (SRT, ASS, WebVTT …) to MP4's subtitle format (mov_text);
    text styles such as italic/bold are preserved."""

    finished = False

    def __init__(self, job: _Job, stream, out):
        self.job, self.stream, self.out = job, stream, out
        self.window = job.window
        self.decoder = stream.codec_context
        try:
            self.decoder.open(strict=False)  # ASS's own styles are read from the header
        except Exception:  # noqa: BLE001 - it is opened anyway when the first packet is decoded
            pass
        self.ostream = out.add_stream("mov_text")
        self.ostream.time_base = stream.time_base
        self.encoder = self.ostream.codec_context
        self.encoder.subtitle_header = self.decoder.subtitle_header or subtitles.DEFAULT_ASS_HEADER
        self.index = self.ostream.index
        self.last_dts: int | None = None
        self.skipped = 0
        _keep_language(stream, self.ostream)
        _copy_disposition(stream, self.ostream)

    def feed(self, packet) -> None:
        if packet.pts is None or not packet.size or packet.time_base is None:
            return
        try:
            subtitle_set = self.decoder.decode2(packet)
            if subtitle_set is None or not len(subtitle_set):
                return
            converted = self.encoder.encode_subtitle(subtitle_set)
        except Exception:  # noqa: BLE001 - a single cue that cannot be converted is skipped
            self.skipped += 1
            return
        if converted is None or not converted.size:
            return
        tb = packet.time_base
        start = packet.pts + int(round(subtitle_set.start_display_time / 1000 / tb)) - self.job.shift(tb)
        duration = packet.duration
        if not duration and subtitle_set.end_display_time > subtitle_set.start_display_time:
            duration = int(round((subtitle_set.end_display_time - subtitle_set.start_display_time) / 1000 / tb))
        duration = duration or int(round(3 / tb))  # cue without a duration: 3 seconds
        w = self.window
        if w is not None:  # trimmed: the cue is cut to the window and moved
            begin = float(start * tb)
            if begin >= w.end - _EPS:
                self.finished = True
                return
            span = w.span(begin, float((start + duration) * tb))
            if span is None:
                return
            start = int(round(span[0] / tb))
            duration = max(1, int(round(span[1] / tb)))
        converted.time_base = tb
        converted.pts = converted.dts = start
        converted.duration = duration
        if self.last_dts is not None and converted.dts <= self.last_dts:
            converted.pts = converted.dts = self.last_dts + 1
        self.last_dts = converted.dts
        converted.stream = self.ostream
        self.job.mux(self.out, converted, self.index)

    def flush(self) -> None:
        if self.skipped:
            self.job.notes.append(t("video.subtitle_skipped", n=self.skipped))


class _VideoEncoder:
    """Decodes the video and encodes it (with its orientation, edit and HDR handling applied) to
    H.264 (or, on the GPU, to HEVC with HDR preserved). A trimmed video is cut frame-exactly."""

    finished = False

    def __init__(self, job: _Job, stream, out):
        import av

        self.job, self.stream, self.out = job, stream, out
        self.window = job.window
        self.started = self.window is None  # trimmed: decoding starts at the window's keyframe
        self.shift = job.shift(stream.time_base)  # an MP4/MOV made from a TS/PS starts at 0
        self.last_pts: int | None = None
        self.decoder = stream.codec_context
        self.decoder.thread_type = "AUTO"
        info, hardware = job.info, job.plan.encoder
        rate = stream.average_rate or stream.guessed_rate or Fraction(30)
        self.ostream = out.add_stream(hardware.name if hardware else "libx264", rate=rate)
        self.index = self.ostream.index
        self.graph = None
        self.encoder = self.ostream.codec_context
        self.encoder.time_base = stream.time_base
        self.pix_fmt = hardware.pix_fmt if hardware else "yuv420p"
        self.encoder.pix_fmt = self.pix_fmt
        self.geometry, self.size = geometry(info, job.plan.edit)
        self.encoder.width, self.encoder.height = self.size
        job.out_size = self.size
        if hardware is not None:
            if hardware.bit_rate is not None:
                self.encoder.bit_rate = hardware.bit_rate
            self.encoder.options = hardware.settings
        else:
            self.encoder.options = {"crf": str(CRF), "preset": x264_preset(*self.size, info.fps)}
        if job.plan.codec == "hevc" and job.plan.out_fmt in MATRIX_FORMATS:
            self.encoder.codec_tag = "hvc1"  # the tag Apple devices expect
        if job.plan.tonemap:
            self.encoder.color_primaries = self.encoder.color_trc = self.encoder.colorspace = 1  # BT.709
        else:
            for attr in ("color_primaries", "color_trc", "colorspace"):
                value = getattr(self.decoder, attr, None)
                if value not in (None, 2):  # 2: unspecified
                    setattr(self.encoder, attr, value)
        self.encoder.color_range = 1  # limited (video) range (16–235)
        sar = stream.sample_aspect_ratio
        if sar and sar != 1:
            # Non-square pixels (HDV/AVCHD 1440×1080, DV, DVD) stay so; a quarter turn turns them too.
            turns = info.rotation // 90 + (job.plan.edit.quarter if job.plan.edit is not None else 0)
            self.encoder.sample_aspect_ratio = 1 / sar if turns % 2 else sar
        self.scrubber: bitstream.SeiScrubber | None = None
        self.ostream.metadata.clear()
        if not job.plan.keep_metadata:
            pass
        elif stream.metadata.get("language", "und") != "und":
            self.ostream.metadata["language"] = stream.metadata["language"]
        # Open the encoder now: if it cannot be opened (GPU) we find out right away, and the settings
        # line in its header data (x265 …) is cleaned before it gets written to the file header.
        self.encoder.open()
        cleaned = bitstream.scrub_extradata(job.plan.codec, self.encoder.extradata)
        if cleaned is not None:
            self.encoder.extradata = cleaned
        self.av = av
        self.count = 0
        self.fallback_pts = 0

    def _build(self, frame) -> None:
        av = self.av
        plan = self.job.plan
        steps: list[tuple[str, dict]] = []
        if frame.interlaced_frame:
            steps.append(("bwdif", {"mode": "send_frame", "parity": "auto", "deint": "interlaced"}))
        steps.extend(self.geometry)
        if plan.tonemap:
            steps.append(("scale", {"in_color_matrix": "bt2020", "in_range": "tv", "out_range": "pc"}))
            steps.append(("format", {"pix_fmts": "gbrp16le"}))
            steps.append(("lut3d", {"file": tonemap.lut_file(plan.tonemap), "interp": "tetrahedral"}))
            steps.append(("scale", {"out_color_matrix": "bt709", "out_range": "tv"}))
        elif frame.format.name.startswith("yuvj") or frame.color_range == 2:
            steps.append(("scale", {"in_range": "pc", "out_range": "tv"}))  # MJPEG: full range → limited range
        steps.append(("format", {"pix_fmts": self.pix_fmt}))
        graph = av.filter.Graph()
        node = graph.add_buffer(template=self.stream)
        for name, args in steps:
            nxt = graph.add(name, **args)
            node.link_to(nxt)
            node = nxt
        sink = graph.add("buffersink")
        node.link_to(sink)
        graph.configure()
        self.graph = graph
        if plan.tonemap:
            self.job.notes.append(t("video.tonemapped", hdr=plan.tonemap))
        if frame.interlaced_frame:
            self.job.notes.append(t("video.deinterlaced"))

    def feed(self, packet) -> None:
        if not self.started:
            # Read from the beginning (a container without an index to jump with): the packets before the
            # keyframe that decoding starts from are not decoded at all; their frames would be dropped.
            w = self.window
            pts = packet.pts if packet.pts is not None else packet.dts
            tb = packet.time_base or self.stream.time_base
            if pts is None or (pts != w.key_pts and not (packet.is_keyframe and float(pts * tb) > w.start)):
                return
            self.started = True
        for frame in self.decoder.decode(packet):
            self._frame(frame)

    def _frame(self, frame) -> None:
        w = self.window
        if w is not None and frame.pts is not None:
            tb = frame.time_base or self.stream.time_base
            t = float(frame.pts * tb)
            if t >= w.end - _EPS:
                self.finished = True
                return
            if not w.kept(t):
                return
            pts = int(round(w.out_time(t) / tb))
            if self.last_pts is not None and pts <= self.last_pts:
                pts = self.last_pts + 1  # the frames on either side of a removed part (coarse time base)
            frame.pts = self.last_pts = pts
        elif self.shift and frame.pts is not None:
            frame.pts -= self.shift
        if self.graph is None:
            self._build(frame)
        if frame.pts is None:
            frame.pts = self.fallback_pts
        self.fallback_pts = frame.pts + 1
        self.graph.push(frame)
        self._drain()

    def _drain(self, final: bool = False) -> None:
        av = self.av
        while True:
            try:
                out = self.graph.pull()
            except (BlockingIOError, av.error.EOFError):
                break
            out.pict_type = 0  # do not force the source's I-frames on the encoder
            if self.job.reference is None:
                self.job.reference = _thumbnail(out)  # compared with the output's first frame during verification
            self._encode(out)

    def _encode(self, frame) -> None:
        for packet in self.encoder.encode(frame):
            self._write(packet)

    def _write(self, packet) -> None:
        av = self.av
        if self.scrubber is None:
            self.scrubber = bitstream.SeiScrubber(self.job.plan.codec, self.encoder.extradata)
        data = self.scrubber.scrub(bytes(packet))  # x264's settings line (SEI) or other encoder notes
        if data is not None:
            fresh = av.Packet(data)
            fresh.pts, fresh.dts, fresh.duration = packet.pts, packet.dts, packet.duration
            fresh.time_base = packet.time_base
            fresh.is_keyframe = packet.is_keyframe
            packet = fresh
        packet.stream = self.ostream
        self.job.mux(self.out, packet, self.index)
        self.count += 1

    def flush(self) -> None:
        for frame in _drain_decoder(self.decoder):
            self._frame(frame)
        if self.graph is not None:
            try:
                self.graph.push(None)
            except Exception:  # noqa: BLE001 - empty graph
                pass
            self._drain(final=True)
        for packet in self.encoder.encode(None):
            self._write(packet)


class _AudioEncoder:
    """Encodes audio to AAC (for audio codecs that do not fit in MP4); a trimmed video's audio is
    cut sample-exactly."""

    finished = False

    def __init__(self, job: _Job, stream, out):
        import av

        self.job, self.stream, self.out = job, stream, out
        self.window = job.window
        self.decoder = stream.codec_context
        channels = getattr(self.decoder, "channels", 2) or 2
        layout = "mono" if channels == 1 else "5.1" if channels >= 6 else "stereo"
        rate = self.decoder.sample_rate if self.decoder.sample_rate in AUDIO_RATES else 48000
        self.ostream = out.add_stream("aac", rate=rate, layout=layout)
        self.index = self.ostream.index
        self.encoder = self.ostream.codec_context
        self.encoder.bit_rate = {"mono": 128_000, "stereo": 192_000}.get(layout, 384_000)
        self.resampler = av.AudioResampler(format="fltp", layout=layout, rate=rate)
        self.rate = rate
        self.next_pts: int | None = None
        self.ostream.metadata.clear()
        self.av = av

    def feed(self, packet) -> None:
        w = self.window
        if w is not None and packet.pts is not None and packet.time_base is not None and \
                float((packet.pts + (packet.duration or 0)) * packet.time_base) < w.start - AUDIO_PREROLL:
            return  # long before a trimmed video's start: not decoded
        for frame in self.decoder.decode(packet):
            self._frame(frame)

    def _frame(self, frame) -> None:
        for part in self._trim(frame) if self.window is not None else [frame]:
            if self.next_pts is None:
                start = part.pts * part.time_base if part.pts is not None and part.time_base else 0
                self.next_pts = int(round((float(start) - self.job.offset) * self.rate))
            part.pts = None
            for out in self.resampler.resample(part):
                self._encode(out)

    def _trim(self, frame) -> list:
        """The parts of the frame inside the window, without the removed parts (the encoder
        joins them); the first one's time is moved to the output's timeline."""
        w = self.window
        tb = frame.time_base or self.stream.time_base
        rate = frame.sample_rate
        if frame.pts is None or not rate or not tb:
            return [frame]
        t0 = float(frame.pts * tb)
        n = frame.samples
        if t0 >= w.end - _EPS:
            self.finished = True
            return []
        spans = [(max(0, int(round((a - t0) * rate))), min(n, int(round((b - t0) * rate))))
                 for a, b in w.pieces(t0, t0 + n / rate)]
        spans = [(a, b) for a, b in spans if b > a]
        if not spans:
            return []
        parts = [frame]
        if spans != [(0, n)]:
            try:
                parts = [_slice_audio(frame, a, b) for a, b in spans]
            except Exception:  # noqa: BLE001 - an unusual sample format: the whole frame is kept
                spans = [(0, n)]
        first = parts[0]
        first.pts = int(round(w.out_time(t0 + spans[0][0] / rate) / tb))
        first.time_base = tb
        return parts

    def _encode(self, frame) -> None:
        frame.pts = self.next_pts
        frame.time_base = Fraction(1, self.rate)
        self.next_pts += frame.samples
        for packet in self.encoder.encode(frame):
            packet.stream = self.ostream
            self.job.mux(self.out, packet, self.index)

    def flush(self) -> None:
        for frame in _drain_decoder(self.decoder):
            self._frame(frame)
        if self.next_pts is not None:
            for out in self.resampler.resample(None):
                self._encode(out)
        for packet in self.encoder.encode(None):
            packet.stream = self.ostream
            self.job.mux(self.out, packet, self.index)


def _drain_decoder(decoder) -> list:
    """Frames left in the decoder. If the source's last (empty) packet has already flushed the
    decoder, a second flush raises an "end of file" error; then there are no frames left."""
    import av

    try:
        return list(decoder.decode(None))
    except av.error.EOFError:
        return []


def geometry(info: VideoInfo, edit: Edit | None) -> tuple[list[tuple[str, dict]], tuple[int, int]]:
    """Filters that make the frame upright (orientation) and apply the edit (flip first), plus the
    output size. H.264 (4:2:0) needs even dimensions: the crop box is aligned to even pixels."""
    steps = [("hflip", {})] if info.mirrored else []  # a mirrored source: flipped, then turned
    steps.extend(_quarter_filters(info.rotation // 90))
    w, h = info.display_size
    if edit is not None:
        if edit.mirror:
            steps.append(("hflip", {}))
        steps.extend(_quarter_filters(edit.quarter))
        cw, ch = (int(round(v)) for v in edit.canvas_size(w, h))
        if edit.angle:
            steps.append(("rotate", {"a": f"{edit.angle}*PI/180", "ow": "iw", "oh": "ih", "c": "black"}))
        left, top, bw, bh = edit.pixels_on_canvas(cw, ch)
        x0, y0 = max(0, int(round(left))) & ~1, max(0, int(round(top))) & ~1
        w, h = max(2, min(bw, cw - x0) & ~1), max(2, min(bh, ch - y0) & ~1)
        if (x0, y0, w, h) != (0, 0, cw, ch):
            steps.append(("crop", {"w": str(w), "h": str(h), "x": str(x0), "y": str(y0)}))
        else:
            w, h = cw, ch
    if w % 2 or h % 2:
        steps.append(("crop", {"w": str(w & ~1), "h": str(h & ~1), "x": "0", "y": "0"}))
        w, h = w & ~1, h & ~1
    return steps, (w, h)


def _quarter_filters(quarter: int) -> list[tuple[str, dict]]:
    """Filters for a clockwise rotation by `quarter` × 90°."""
    quarter %= 4
    if quarter == 1:
        return [("transpose", {"dir": "clock"})]
    if quarter == 2:
        return [("hflip", {}), ("vflip", {})]
    if quarter == 3:
        return [("transpose", {"dir": "cclock"})]
    return []


# ================================================================== verification

_ALLOWED_CONTAINER_TAGS = {"major_brand", "minor_version", "compatible_brands"}
_ALLOWED_STREAM_TAGS = {"handler_name", "vendor_id", "language", "duration", "encoder"}
_GENERIC_HANDLERS = {"", "VideoHandler", "SoundHandler", "DataHandler", "SubtitleHandler"}
_SIGNATURES = (
    (b"com.apple.quicktime", "video.sig.apple"),
    (b"<x:xmpmeta", "video.sig.xmp"),
    (b"<?xpacket", "video.sig.xmp"),
    (bitstream.MDPM_MAGIC, "video.sig.mdpm"),
    (b"Exif\x00\x00", "video.sig.exif"),
    (b"ISO6709", "video.sig.location"),
    (b"com.android", "video.sig.android"),
)


def _verify(job: _Job) -> None:
    import av

    plan = job.plan
    if not os.path.exists(job.dst) or os.path.getsize(job.dst) == 0:
        raise VideoError(t("video.verify_empty"))
    try:
        container = av.open(job.dst)
    except av.error.FFmpegError as exc:
        raise VideoError(t("video.verify_open", error=exc)) from exc
    with container:
        types = [s.type for s in container.streams]
        if types.count("video") < 1 or types.count("audio") != job.expected_streams.count("audio"):
            raise VideoError(t("video.verify_tracks"))
        duration = (container.duration or 0) / 1_000_000
        expected = job.expected_duration()
        if expected > 1 and abs(duration - expected) > max(1.0, expected * 0.03):
            raise VideoError(t("video.verify_duration", duration=decimal(duration), expected=decimal(expected)))
        if not plan.keep_metadata:
            _verify_clean(container, job)
        video = container.streams.video[0]
        codec = video.codec_context.codec.canonical_name
        scrubber = None
        if not plan.keep_metadata and codec in ("h264", "hevc"):
            scrubber = bitstream.SeiScrubber(codec, video.codec_context.extradata)
        dv = not plan.keep_metadata and codec == "dvvideo"  # every DV frame must have lost its recording date
        dv_dates = False
        counts = {s.index: 0 for s in container.streams}
        # If metadata was stripped, also check that the kept subtitles carry no log (location …).
        cues: dict[int, list[str]] = {} if plan.keep_metadata else {
            s.index: [] for s in container.streams if s.type == "subtitle"}
        frames = 0
        for packet in container.demux():
            if packet.dts is None and packet.pts is None:
                continue
            counts[packet.stream.index] = counts.get(packet.stream.index, 0) + 1
            texts = cues.get(packet.stream.index)
            if texts is not None and packet.size and len(texts) < subtitles.SAMPLE_CUES:
                texts.extend(subtitles.packet_texts(packet.stream, packet))
            if packet.stream.index != video.index:
                continue
            if scrubber is not None and counts[video.index] <= 60 and packet.size:
                scrubber.scrub(bytes(packet))
            if dv and not dv_dates and packet.size:
                dv_dates = bitstream.dv_has_dates(bytes(packet))
            if frames < 2:
                for frame in packet.decode():
                    frames += 1
                    _check_frame(job, frame, first=frames == 1)
        if scrubber is not None and scrubber.removed:
            raise VideoError(t("video.verify_sei"))
        if dv_dates:
            raise VideoError(t("video.leftovers", items=t("video.sig.dv_date")))
        if any(subtitles.telemetry(texts)[0] for texts in cues.values()):
            raise VideoError(t("video.verify_subtitle_log"))
        if frames == 0:
            for frame in _flush_decode(video):
                frames += 1
                _check_frame(job, frame, first=frames == 1)
        if frames == 0:
            raise VideoError(t("video.verify_decode"))
        exact = plan.out_fmt in (*MATRIX_FORMATS, Fmt.MKV, Fmt.WEBM, Fmt.AVI)
        for index, n in job.written.items():
            got = counts.get(index, 0)
            if container.streams[index].type == "subtitle":
                short = got < n  # MP4 inserts empty samples between subtitle cues
            else:
                short = got != n if exact else got < n * 0.9
            if short:
                raise VideoError(t("video.verify_packets"))
    _verify_tail(job.dst)
    if plan.out_fmt in MATRIX_FORMATS:
        _verify_structure(job)


def _flush_decode(stream):
    try:
        yield from stream.codec_context.decode(None)
    except Exception:  # noqa: BLE001
        return


MIN_SIMILARITY = 25.0  # dB; a correct encode scores above 40 dB on the thumbnails, a corrupted frame below 15


def _thumbnail(frame):
    import numpy as np

    return frame.reformat(width=64, height=48, format="gray").to_ndarray().astype(np.float32)


def _check_frame(job: _Job, frame, first: bool) -> None:
    """Do the re-encoded video's size and first frame match what went into the encoder?"""
    if job.out_size is not None and (frame.width, frame.height) != job.out_size:
        raise VideoError(t("video.verify_size"))
    if first and job.reference is not None:
        mse = float(((_thumbnail(frame) - job.reference) ** 2).mean())
        similarity = 99.0 if mse == 0 else 10 * math.log10(255 * 255 / mse)
        if similarity < MIN_SIMILARITY:
            raise _BadOutput(t("video.verify_broken", db=f"{similarity:.0f}"))


def _verify_tail(path: str) -> None:
    """Seeks to a frame near the end and decodes it: is the end of the file readable too?"""
    import av

    with av.open(path) as c:
        video = c.streams.video[0]
        duration = (c.duration or 0) / 1_000_000
        if duration > 2:
            try:
                c.seek(int((duration - 1.5) * 1_000_000), any_frame=False)
            except av.error.FFmpegError:
                return
        for frame in c.decode(video):
            return
    raise VideoError(t("video.verify_end"))


def _kept_date(job: _Job, key: str, value: str) -> bool:
    """Is it the tag FFmpeg shows for the header dates, holding exactly the capture date written on
    purpose (keep_date)?"""
    if job.created is None or key.lower() != "creation_time":
        return False
    return parse_date(value) == (job.created, timedelta(0))


def _verify_clean(container, job: _Job) -> None:
    leftovers = [k for k, v in container.metadata.items()
                 if k.lower() not in _ALLOWED_CONTAINER_TAGS
                 and not (k.lower() in ("encoder", "software") and v.startswith("Lav"))
                 and not _kept_date(job, k, v)]
    for s in container.streams:
        for k, v in s.metadata.items():
            key = k.lower()
            if _kept_date(job, k, v):
                continue
            if key not in _ALLOWED_STREAM_TAGS:
                leftovers.append(k)
            elif key == "handler_name" and v not in _GENERIC_HANDLERS:
                leftovers.append(f"{k}={v}")
            elif key == "encoder" and not v.startswith("Lav"):
                leftovers.append(k)
        if s.type not in ("video", "audio", "subtitle"):
            leftovers.append(t("video.verify_track", type=s.type))
        elif s.type == "video" and s.codec_context is not None and bitstream.camera_extradata(
                s.codec_context.codec.canonical_name, s.codec_context.extradata):
            leftovers.append(t("video.sig.exif"))  # camera data in the (AVI) stream header
    if leftovers:
        raise VideoError(t("video.leftovers", items=", ".join(leftovers[:6])))
    with open(job.dst, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        head = fh.read(1 << 20)
        fh.seek(max(0, size - (1 << 20)))
        tail = fh.read()
    for blob in (head, tail):
        if job.plan.out_fmt in MATRIX_FORMATS:
            continue  # MP4/MOV: the boxes are checked separately (no chance matches in the video data)
        found = [t(name) for sig, name in _SIGNATURES if len(sig) >= 10 and sig in blob]
        if found:
            raise VideoError(t("video.leftovers", items=", ".join(found)))


def _verify_structure(job: _Job) -> None:
    plan = job.plan
    try:
        st = read_structure(job.dst)
    except (BoxError, OSError) as exc:
        raise VideoError(t("video.verify_structure", error=exc)) from exc
    track = st.video
    expected = 0 if plan.encode_video else plan.rotation
    mirrored = job.info.mirrored and not plan.encode_video  # a copy keeps a mirrored source's own matrix
    if track is None or track.rotation != expected or track.mirrored != mirrored:
        raise VideoError(t("video.verify_rotation"))
    if plan.keep_metadata:
        return
    problems = []
    if st.keys or st.items or st.uuids or st.xmp:
        problems.append(t("video.problem.tags"))
    if any(t not in (b"meta",) for t in st.udta) or any(t.udta for t in st.tracks):
        problems.append(t("video.problem.udta"))
    # The header dates are zero, or exactly the capture date when it is kept (keep_date).
    stamp = _header_seconds(job.created) if job.created is not None else 0
    if any(v != stamp for v in (st.created, st.modified, *(v for t in st.tracks for v in (
            t.created, t.modified, t.media_created, t.media_modified)))):
        problems.append(t("video.problem.dates"))
    kept_subtitles = sum(1 for t in st.tracks if t.handler in subtitles.HANDLERS)
    if any(t.handler not in (b"vide", b"soun") and t.handler not in subtitles.HANDLERS for t in st.tracks) \
            or kept_subtitles != job.expected_streams.count("subtitle"):
        problems.append(t("video.problem.tracks"))
    if st.trailing:
        problems.append(t("video.problem.trailing"))
    with open(job.dst, "rb") as fh:
        for typ, pos, size in st.top:
            if typ == b"mdat":
                continue
            fh.seek(pos)
            blob = fh.read(size)
            problems.extend(t(name) for sig, name in _SIGNATURES if sig in blob)
    if problems:
        raise VideoError(t("video.leftovers", items=", ".join(sorted(set(problems)))))
