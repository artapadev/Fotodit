"""Video information: duration, size, orientation, codecs, capture date, device, location.

The information is gathered from the tags FFmpeg (PyAV) reads and, for MP4/MOV, also
directly from the boxes (`boxes`) and from SEI messages in the first frames of an
H.264/HEVC stream (the MDPM record of AVCHD cameras).

Date rules (so that sorting and renaming use the same "local time" as for photos):

* The iPhone's `com.apple.quicktime.creationdate` tag is written with a time zone:
  it is used as is (the time at the capture location). AVCHD's MDPM date is local too.
* The MP4/MOV header date (mvhd) is UTC per the standard; phones write it that way.
  If the file name contains a date (VID_20240712_183010) that differs from UTC by
  a whole time zone offset, the (local) time from the file name is used; otherwise
  the date is converted to the computer's time zone. Cameras and GoPro, however,
  write local time into the header; theirs is not converted.
* AVI dates are local; MKV/WebM/WMV dates are UTC.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ...i18n import date_time, t
from ..formats import MATRIX_FORMATS, Fmt, sniff_file
from . import bitstream, subtitles
from .boxes import (
    LOCATION_BOXES,
    LOCATION_TIMED,
    MAKER_BOXES,
    TIMED_METADATA,
    BoxError,
    Structure,
    matrix_rotation,
    qt_datetime,
    read_structure,
)

HLG, PQ = "HLG", "PQ"
_TRC_HDR = {18: HLG, 16: PQ}
ATTACHED_PIC = 1024  # AV_DISPOSITION_ATTACHED_PIC

# Makers that write local time into the header date (phones write UTC).
LOCAL_TIME_MAKERS = ("gopro", "canon", "nikon", "panasonic", "fujifilm", "olympus", "om digital", "pentax",
                     "ricoh", "leica", "kodak", "casio", "jvc", "sigma", "hasselblad", "dji")
PROBE_PACKETS = 12  # number of leading video packets read for SEI (MDPM)

X264_UUID = bytes.fromhex("dc45e9bde6d948b7962cd820d923eeef")
_ISO6709 = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")


class ProbeError(Exception):
    """Carries a Turkish message to show to the user."""


@dataclass
class VideoInfo:
    fmt: Fmt
    duration: float = 0.0  # seconds
    width: int = 0  # coded frame size (before rotation)
    height: int = 0
    rotation: int = 0  # clockwise rotation for display (0/90/180/270)
    mirrored: bool = False  # displayed flipped left-right (before the rotation), by a mirrored display matrix
    fps: float = 0.0
    vcodec: str = ""
    profile: str = ""
    pix_fmt: str = ""
    bit_depth: int = 8
    hdr: str | None = None  # "HLG" / "PQ"
    acodec: str | None = None
    channels: int = 0
    sample_rate: int = 0
    audio_streams: int = 0
    unsupported_audio: int = 0  # audio tracks FFmpeg has no decoder for (e.g. iPhone spatial audio): left out
    bitrate: int = 0
    subtitle_tracks: list[subtitles.SubtitleTrack] = field(default_factory=list)
    data_tracks: list[str] = field(default_factory=list)  # descriptions
    location_tracks: list[str] = field(default_factory=list)  # the data tracks that log a location
    attachments: int = 0
    covers: int = 0
    chapters: int = 0
    tags: dict[str, str] = field(default_factory=dict)  # container tags
    stream_tags: list[dict[str, str]] = field(default_factory=list)
    structure: Structure | None = None
    mdpm: dict[int, bytes] = field(default_factory=dict)
    sei: list[str] = field(default_factory=list)  # user data embedded in the stream (descriptions)
    taken: datetime | None = None
    taken_note: str = ""
    camera: str | None = None
    gps: tuple[float, float] | None = None
    gps_sources: list[str] = field(default_factory=list)

    @property
    def display_size(self) -> tuple[int, int]:
        """Size as displayed (orientation applied)."""
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height

    @property
    def has_gps(self) -> bool:
        return bool(self.gps_sources)

    def subtitle(self, index: int) -> subtitles.SubtitleTrack | None:
        return next((t for t in self.subtitle_tracks if t.index == index), None)

    def short(self) -> str:
        """Short summary: "0:42 · 4K HEVC HDR"."""
        parts = [duration_text(self.duration)] if self.duration else []
        w, h = self.display_size
        side = max(w, h)
        res = "8K" if side >= 7680 else "4K" if side >= 3840 else "2.7K" if side >= 2704 else (
            f"{min(w, h)}p" if side else "")
        codec = CODEC_NAMES.get(self.vcodec, self.vcodec.upper())
        parts.append(" ".join(x for x in (res, codec, "HDR" if self.hdr else "") if x))
        return " · ".join(p for p in parts if p)


CODEC_NAMES = {
    "h264": "H.264", "hevc": "HEVC", "av1": "AV1", "vp9": "VP9", "vp8": "VP8", "mpeg4": "MPEG-4",
    "mpeg2video": "MPEG-2", "mpeg1video": "MPEG-1", "mjpeg": "MJPEG", "wmv3": "WMV", "wmv2": "WMV",
    "wmv1": "WMV", "vc1": "VC-1", "prores": "ProRes", "h263": "H.263", "dvvideo": "DV", "theora": "Theora",
    "msmpeg4v3": "DivX 3", "flv1": "FLV", "hap": "HAP", "dnxhd": "DNxHD",
}


def duration_text(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ------------------------------------------------------------------ reading


def probe(path: str, fmt: Fmt | None = None) -> VideoInfo:
    import av

    from . import quiet_ffmpeg

    quiet_ffmpeg()

    fmt = fmt or sniff_file(path)
    info = VideoInfo(fmt or Fmt.MP4)
    try:
        container = av.open(path, options={"export_xmp": "1"} if fmt in MATRIX_FORMATS else {})
    except av.error.InvalidDataError as exc:
        raise ProbeError(t("probe.unsupported")) from exc
    except (av.error.FFmpegError, OSError) as exc:
        raise ProbeError(t("error.video_open", error=exc)) from exc
    with container:
        info.tags = {str(k): str(v) for k, v in container.metadata.items()}
        info.duration = (container.duration or 0) / 1_000_000
        info.bitrate = container.bit_rate or 0
        try:
            info.chapters = len(container.chapters())
        except Exception:  # noqa: BLE001 - old/obscure formats
            info.chapters = 0
        video = None
        for s in container.streams:
            info.stream_tags.append({str(k): str(v) for k, v in s.metadata.items()})
            if s.type == "video":
                if s.disposition & ATTACHED_PIC:
                    info.covers += 1
                elif video is None:
                    video = s
            elif s.type == "audio":
                if s.codec_context is None:  # no decoder in FFmpeg: PyAV can neither copy nor convert it
                    info.unsupported_audio += 1
                    continue
                info.audio_streams += 1
                if info.acodec is None:
                    cc = s.codec_context
                    info.acodec = cc.codec.canonical_name
                    info.channels = getattr(cc, "channels", 0) or 0
                    info.sample_rate = cc.sample_rate or 0
            elif s.type == "subtitle":
                info.subtitle_tracks.append(subtitles.SubtitleTrack(
                    s.index, subtitles.codec_name(s), str(s.metadata.get("language", ""))))
            elif s.type == "attachment":
                info.attachments += 1
            else:
                label, located = _data_label(s)
                info.data_tracks.append(label)
                if located:
                    info.location_tracks.append(label)
        if video is None:
            raise ProbeError(t("probe.no_picture"))
        cc = video.codec_context
        info.vcodec = cc.codec.canonical_name
        info.profile = str(cc.profile or "")
        info.width, info.height = cc.width, cc.height
        info.pix_fmt = cc.pix_fmt or ""
        info.bit_depth = 10 if "10" in info.pix_fmt or "12" in info.pix_fmt else 8
        info.hdr = _TRC_HDR.get(cc.color_trc)
        rate = video.average_rate or video.guessed_rate or video.base_rate
        info.fps = float(rate) if rate else 0.0
        if not info.duration and video.duration and video.time_base:
            info.duration = float(video.duration * video.time_base)
        if info.vcodec in ("h264", "hevc"):
            _read_sei(container, video, info)
        elif info.vcodec == "dvvideo":
            _read_dv(container, video, info)
        if bitstream.camera_extradata(info.vcodec, cc.extradata):
            info.sei.append(t("probe.sei.stream_header"))
        if info.fmt in (Fmt.MKV, Fmt.WEBM):
            info.rotation, info.mirrored = _first_frame_orientation(container, video)
        if info.subtitle_tracks:
            _inspect_subtitles(container, info)
    if info.fmt in MATRIX_FORMATS:
        try:
            info.structure = read_structure(path)
        except (BoxError, OSError):
            info.structure = None
        track = info.structure.video if info.structure else None
        if track is not None and track.matrix_ok:
            info.rotation, info.mirrored = track.rotation, track.mirrored
        if info.structure is not None:
            _add_structure_tracks(info)
    _conclude(path, info)
    return info


def _data_label(stream) -> tuple[str, bool]:
    tag = ""
    try:
        tag = stream.codec_context.codec_tag if stream.codec_context else ""
    except Exception:  # noqa: BLE001
        tag = ""
    return _track_label(str(tag).encode("latin-1", "replace")[:4], stream.metadata.get("handler_name", ""))


def _track_label(entry: bytes, handler: str) -> tuple[str, bool]:
    """A timed metadata track's description, and whether it logs a location."""
    key = TIMED_METADATA.get(entry)
    name = handler or entry.decode("latin-1", "replace").strip("\x00 ") or t("probe.unknown")
    label = t(key) if key else t("probe.data_track", name=name)
    return label, entry in LOCATION_TIMED or "GPS" in handler


def _read_sei(container, video, info: VideoInfo) -> None:
    scrubber = bitstream.SeiScrubber(info.vcodec, video.codec_context.extradata, collect=True)
    try:
        for n, packet in enumerate(container.demux(video)):
            if n >= PROBE_PACKETS:
                break
            if packet.size:
                scrubber.scrub(bytes(packet))
    except Exception:  # noqa: BLE001 - if the SEI cannot be read, only this info is missing
        return
    for payload in scrubber.mdpm:
        info.mdpm.update({k: v for k, v in bitstream.mdpm_tags(payload).items() if k not in info.mdpm})
    if scrubber.mdpm:
        info.sei.append(t("probe.sei.mdpm"))
    for data in scrubber.user_data:
        if data.startswith(X264_UUID):
            info.sei.append(t("probe.sei.x264"))
        else:
            info.sei.append(t("probe.sei.maker"))


def _read_dv(container, video, info: VideoInfo) -> None:
    """DV camcorders write the recording date and time into every frame; looks at the first one."""
    try:
        for packet in container.demux(video):
            if packet.size:
                if bitstream.dv_has_dates(bytes(packet)):
                    info.sei.append(t("probe.sei.dv_date"))
                return
    except Exception:  # noqa: BLE001 - if the frame cannot be read, only this info is missing
        return


def frame_orientation(frame) -> tuple[int, bool]:
    """(clockwise rotation, flipped left-right first?) of a decoded frame, from its display matrix.
    PyAV's `frame.rotation` alone reads a flip as a half turn."""
    data = frame.side_data.get("DISPLAYMATRIX")
    if data is None:
        return 0, False
    rotation, mirrored, _ = matrix_rotation(struct.unpack("=9i", bytes(data)[:36]))
    return rotation, mirrored


def _first_frame_orientation(container, video) -> tuple[int, bool]:
    """In MKV/WebM the orientation (if any) comes with the decoded frame; decodes the first frame."""
    try:
        container.seek(0)
        for n, packet in enumerate(container.demux(video)):
            for frame in packet.decode():
                return frame_orientation(frame)
            if n > 30:
                break
    except Exception:  # noqa: BLE001 - if it cannot be read, no rotation is assumed
        pass
    return 0, False


def _inspect_subtitles(container, info: VideoInfo) -> None:
    """Looks at the first cues of text subtitles: a location/camera log or real subtitles?"""
    streams = [s for s in container.streams if s.type == "subtitle"]
    texts = subtitles.sample(container, streams)
    for track in info.subtitle_tracks:
        track.telemetry, track.location = subtitles.telemetry(texts.get(track.index, []))


def _add_structure_tracks(info: VideoInfo) -> None:
    """Timed metadata tracks as the boxes describe them: FFmpeg does not expose some of them as data
    streams (iPhone 'mebx' …), and for those it does, PyAV gives no sample entry ('gpmd', 'djmd' …),
    so only the boxes tell which ones log a location. Subtitle and chapter tracks are not counted
    here: subtitles are inspected separately by their content (`subtitle_tracks`), chapters are
    counted in `chapters`."""
    known = len(info.data_tracks)
    extra = []
    for track in info.structure.tracks:
        if track.handler in (b"vide", b"soun") or track.handler in subtitles.HANDLERS:
            continue
        extra.append(_track_label(track.entry, track.handler_name))
    if len(extra) >= known:
        info.data_tracks = [label for label, _ in extra]
        info.location_tracks = [label for label, located in extra if located]
    else:
        info.location_tracks.extend(label for label, located in extra
                                    if located and label not in info.location_tracks)


# ----------------------------------------------------------- date, device, location


def _tag(tags: dict[str, str], *names: str) -> str | None:
    lower = {k.lower(): v for k, v in tags.items()}
    for name in names:
        value = lower.get(name.lower())
        if value and value.strip():
            return value.strip()
    return None


def parse_iso6709(text: str) -> tuple[float, float] | None:
    m = _ISO6709.match(text.strip())
    if not m:
        return None
    try:
        lat, lon = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    if abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0):
        return None
    return lat, lon


def parse_date(text: str) -> tuple[datetime, timedelta | None] | None:
    """An ISO 8601-like date, plus its UTC offset if it has a time zone (Z included)."""
    t = text.strip().replace(" ", "T", 1) if re.match(r"\d{4}-\d\d-\d\d \d", text.strip()) else text.strip()
    m = re.match(r"(\d{4})-?(\d\d)-?(\d\d)(?:T(\d\d):?(\d\d)(?::?(\d\d))?(?:[.,]\d+)?)?\s*(Z|[+-]\d\d:?\d\d)?$", t)
    if not m:
        return None
    y, mo, d, hh, mm, ss, tz = m.groups()
    try:
        when = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
    except ValueError:
        return None
    if not 1971 <= when.year <= 2100:
        return None
    offset = None
    if tz:
        if tz == "Z":
            offset = timedelta(0)
        else:
            sign = -1 if tz[0] == "-" else 1
            digits = tz[1:].replace(":", "")
            offset = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return when, offset


def utc_to_local(when: datetime) -> datetime:
    """UTC → this computer's time zone (with the DST rule in effect on that date)."""
    return when.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def _camera(info: VideoInfo) -> str | None:
    from ..meta.exif import camera_name

    tags = info.tags
    make = _tag(tags, "com.apple.quicktime.make", "make", "com.android.manufacturer", "manufacturer")
    model = _tag(tags, "com.apple.quicktime.model", "model", "com.android.model")
    if make or model:
        return camera_name(make, model)
    handlers = " ".join(t.get("handler_name", "") for t in info.stream_tags)
    for brand in ("GoPro", "DJI", "Insta360"):
        if brand.lower() in handlers.lower():
            return brand
    maker = bitstream.mdpm_camera(info.mdpm)
    if maker:
        return maker
    st = info.structure
    if st is not None:
        boxes = set(st.udta)
        for prefix, brand in ((b"CN", "Canon"), (b"NC", "Nikon"), (b"PANA", "Panasonic"), (b"FFMV", "Fujifilm"),
                              (b"FIRM", "GoPro"), (b"smta", "Samsung")):
            if any(b.startswith(prefix) for b in boxes):
                return brand
    return None


def _date(path: str, info: VideoInfo) -> tuple[datetime | None, str]:
    from ..photoinfo import filename_date

    tags = info.tags
    apple = _tag(tags, "com.apple.quicktime.creationdate")
    if apple:
        parsed = parse_date(apple)
        if parsed:
            return parsed[0], t("probe.date.local")
    if info.mdpm:
        stamp = bitstream.mdpm_datetime(info.mdpm)
        if stamp:
            return stamp[0], t("probe.date.camera")
    explicit = _tag(tags, "date", "com.android.capture.date")
    if explicit:
        parsed = parse_date(explicit)
        if parsed and parsed[1] is not None and parsed[1] != timedelta(0):
            return parsed[0], t("probe.date.local")
    raw = _tag(tags, "creation_time")
    parsed = parse_date(raw) if raw else None
    if parsed is None and info.structure is not None:
        when = qt_datetime(info.structure.created)
        if when is not None:
            parsed = (when, timedelta(0))
    if parsed is None:
        return None, ""
    when, _ = parsed
    if info.fmt == Fmt.AVI:
        return when, t("probe.date.plain")
    camera = (info.camera or "").lower()
    if info.fmt in MATRIX_FORMATS and any(camera.startswith(m) for m in LOCAL_TIME_MAKERS):
        return when, t("probe.date.plain")
    # UTC: if the local time in the file name matches it up to a whole time zone offset, use that.
    named = filename_date(os.path.basename(path))
    if named is not None:
        diff = (named - when).total_seconds()
        if abs(diff) <= 14 * 3600 and abs(diff - round(diff / 900) * 900) <= 120:
            return named, t("probe.date.filename")
    return utc_to_local(when), t("probe.date.utc")


def _location(info: VideoInfo) -> None:
    tags = info.tags
    keys = []
    for key, value in tags.items():
        lk = key.lower()
        if "iso6709" in lk or lk.startswith("location") or lk.endswith(".location"):
            if value.strip() and key.split("-")[0] not in keys:
                keys.append(key.split("-")[0])
                info.gps = info.gps or parse_iso6709(value)
        elif lk == "xmp" and (b"GPSLatitude" in value.encode("utf-8", "ignore")):
            info.gps_sources.append(t("probe.gps.xmp"))
    if keys:
        info.gps_sources.insert(0, t("probe.gps.tag", keys=", ".join(keys)))
    if bitstream.mdpm_has_gps(info.mdpm):
        info.gps = info.gps or bitstream.mdpm_gps(info.mdpm)
        info.gps_sources.append(t("probe.gps.mdpm"))
    st = info.structure
    if st is not None:
        boxes = sorted(b.decode("latin-1").replace("\xa9", "©").strip() for b in set(st.udta) & LOCATION_BOXES)
        if boxes:
            info.gps_sources.append(t("probe.gps.box", boxes=", ".join(boxes)))
    info.gps_sources.extend(info.location_tracks)
    if any(t.location for t in info.subtitle_tracks):
        info.gps_sources.append(t("probe.gps.subtitle"))


def _conclude(path: str, info: VideoInfo) -> None:
    info.camera = _camera(info)
    info.taken, info.taken_note = _date(path, info)
    _location(info)


# ---------------------------------------------------------------- metadata list


def scan_result(info: VideoInfo):
    """The metadata list shown to the user (the video counterpart of `meta.scan_file` for photos)."""
    from ..meta.model import Kind, ScanResult

    res = ScanResult()
    coords = f" — {info.gps[0]:.5f}, {info.gps[1]:.5f}" if info.gps else ""
    for src in info.gps_sources:
        res.add(Kind.GPS, src + coords)
    skip = {"major_brand", "minor_version", "compatible_brands", "encoder", "duration", "xmp"}
    tags = [k for k in info.tags if k.lower() not in skip]
    if tags:
        labels = list(dict.fromkeys(_tag_label(k) for k in tags))
        res.add(Kind.TAGS, ", ".join(labels[:12]) + (" …" if len(labels) > 12 else ""))
    if "xmp" in {k.lower() for k in info.tags} or (info.structure and info.structure.xmp):
        res.add(Kind.XMP)
    encoder = _tag(info.tags, "encoder")
    if encoder:
        res.add(Kind.TAGS, t("probe.software", name=encoder))
    st = info.structure
    if st is not None:
        for box in dict.fromkeys(st.udta):
            if box in MAKER_BOXES:
                res.add(Kind.MAKERNOTE, t(MAKER_BOXES[box]))
        created = qt_datetime(st.created)
        if created is not None:
            res.add(Kind.TIMESTAMP, t("probe.header_date", when=date_time(created)))
        if st.trailing:
            res.add(Kind.TRAILER, t("probe.bytes", n=st.trailing), st.trailing)
    for label in info.data_tracks:
        res.add(Kind.TRACK, label)
    for track in info.subtitle_tracks:  # real subtitles are not metadata; they are kept
        if track.telemetry:
            res.add(Kind.SUBTITLE, track.telemetry)
    if info.covers:
        res.add(Kind.THUMBNAIL, t("video.track.cover"))
    if info.chapters:
        res.add(Kind.CHAPTERS, t("probe.chapters", n=info.chapters))
    if info.attachments:
        res.add(Kind.OTHER, t("probe.attachments", n=info.attachments))
    for label in dict.fromkeys(info.sei):
        res.add(Kind.EMBEDDED, label)
    s = res.summary
    s.camera, s.taken, s.gps, s.software = info.camera, info.taken, info.gps, encoder
    return res


_TAG_LABELS = {  # tag → the key of its name
    "creation_time": "probe.tag.created",
    "com.apple.quicktime.creationdate": "probe.tag.taken",
    "com.apple.quicktime.make": "probe.tag.make",
    "com.apple.quicktime.model": "probe.tag.model",
    "com.apple.quicktime.software": "probe.tag.software_version",
    "com.apple.quicktime.location.iso6709": "probe.tag.location",
    "com.apple.quicktime.location.accuracy.horizontal": "probe.tag.accuracy",
    "location": "probe.tag.location",
    "location-eng": "probe.tag.location",
    "make": "probe.tag.make",
    "model": "probe.tag.model",
    "date": "probe.tag.date",
    "title": "probe.tag.title",
    "comment": "probe.tag.comment",
    "artist": "probe.tag.artist",
    "copyright": "probe.tag.copyright",
    "description": "probe.tag.description",
    "software": "probe.tag.software",
}


def _tag_label(key: str) -> str:
    label = _TAG_LABELS.get(key.lower())
    return t(label) if label else key
