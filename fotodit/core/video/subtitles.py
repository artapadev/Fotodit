"""Subtitle tracks: detecting those that carry a location/camera log, and converting to MP4's subtitle format.

Subtitles are mostly the user's own content (movie subtitles, captions added later):
they are kept when metadata is stripped, with only their language tag left. But some
devices use the subtitle track as a logbook: DJI drones write the location, altitude and
camera settings for every frame, dash cams write the date/time, speed and location.
Such tracks are recognized by their first cues and removed when metadata is stripped.

When converting to MP4, SRT, ASS and WebVTT subtitles are converted to MP4's own
subtitle format (mov_text); text styling (italic, bold) is preserved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...i18n import t

TEXT_CODECS = {
    "mov_text", "subrip", "srt", "ass", "ssa", "webvtt", "text", "microdvd", "subviewer", "subviewer1", "sami",
    "realtext", "jacosub", "mpl2", "pjs", "stl", "vplayer",
}
MP4_COPYABLE = {"mov_text", "dvd_subtitle"}  # subtitle codecs that can be written to MP4 as is
HANDLERS = {b"sbtl", b"subp", b"subt", b"clcp", b"text"}  # handler types of subtitle (and chapter) tracks in MP4/MOV

SAMPLE_CUES = 30  # cues inspected per track
SAMPLE_BYTES = 32 << 20  # at most this much of the file is read (long videos with sparse subtitles)

# The same ASS header as FFmpeg's default (used when the source has none of its own).
DEFAULT_ASS_HEADER = (
    b"[Script Info]\r\nScriptType: v4.00+\r\nPlayResX: 384\r\nPlayResY: 288\r\nScaledBorderAndShadow: yes\r\n\r\n"
    b"[V4+ Styles]\r\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
    b"Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
    b"MarginL, MarginR, MarginV, Encoding\r\n"
    b"Style: Default,Arial,16,&Hffffff,&Hffffff,&H0,&H0,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,0\r\n\r\n"
    b"[Events]\r\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
)

# Languages whose names are known (ISO 639-2; the old bibliographic codes map to the same name).
LANGUAGES = {code: code for code in (
    "tur", "eng", "deu", "fra", "spa", "ita", "rus", "ara", "por", "nld", "jpn", "kor", "zho", "ell", "aze", "kur",
    "fas", "pol", "ukr", "bul", "ron",
)} | {"ger": "deu", "fre": "fra", "dut": "nld", "chi": "zho", "gre": "ell", "per": "fas", "rum": "ron"}

# Patterns seen in the cues of log tracks.
_COORDS = re.compile(r"-?\d{1,3}\.\d{4,}\s*°?\s*[NSEW]?\s*[,;/ ]\s*-?\d{1,3}\.\d{4,}")
_HEMISPHERE = re.compile(r"\b[NS]\s?\d{1,2}[.°]\d{3,}.{0,40}?\b[EW]\s?\d{1,3}[.°]\d{3,}", re.IGNORECASE)
_LOCATION_WORDS = re.compile(
    r"\bGPS\b|latitude|longitude|\blat(?:itude)?\s*[:=]|\blon(?:g|gitude)?\s*[:=]|rel_alt|abs_alt"
    r"|\$G[PNL][A-Z]{3},|\bHOME\s*\(", re.IGNORECASE)
_CAMERA_WORDS = re.compile(
    r"\bISO\s*[:=]?\s*\d{2,5}\b|\bshutter\b|\bfnum\b|\bSS\s+[\d/.]+|\bEV\s*[:=]?\s*[-+]?\d|\bF/\d|focal_len|DZOOM"
    r"|SrtCnt|DiffTime|\bct\s*:\s*\d", re.IGNORECASE)
_STAMP = re.compile(r"\b(?:19|20)\d{2}[-/.](?:0[1-9]|1[0-2])[-/.](?:0[1-9]|[12]\d|3[01])\b"
                    r"|\b\d{1,3}\s?(?:km/h|kmh|mph)\b", re.IGNORECASE)
_ASS_TAGS = re.compile(r"\{[^}]*\}")
_HTML_TAGS = re.compile(r"<[^>]+>")


@dataclass
class SubtitleTrack:
    index: int  # stream index in the container
    codec: str
    language: str = ""
    telemetry: str | None = None  # what it is, if it carries a location/camera log (removed when metadata is stripped)
    location: bool = False

    @property
    def label(self) -> str:
        return language_name(self.language)


def language_name(code: str) -> str:
    code = (code or "").strip().lower()
    if not code or code == "und":
        return t("lang.und")
    return t(f"lang.{LANGUAGES[code]}") if code in LANGUAGES else code


def plain_text(line: bytes | str) -> str:
    """Plain text from an ASS event line ("0,0,Default,,0,0,0,,Text") or from SRT text."""
    text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    parts = text.split(",", 8)
    if len(parts) == 9 and parts[0].strip().lstrip("-").isdigit():
        text = parts[8]
    text = _ASS_TAGS.sub("", text).replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    return _HTML_TAGS.sub("", text).strip()


def packet_texts(stream, packet) -> list[str]:
    """Texts of a subtitle packet (taken from the raw data if it cannot be decoded)."""
    try:
        subtitle_set = stream.codec_context.decode2(packet)
        if subtitle_set is not None:
            texts = []
            for rect in subtitle_set:
                raw = getattr(rect, "ass", b"") or getattr(rect, "text", b"")
                if raw:
                    texts.append(plain_text(raw))
            return texts
        return []
    except Exception:  # noqa: BLE001 - an undecodable packet is inspected in raw form
        data = bytes(packet)
        if len(data) >= 2 and int.from_bytes(data[:2], "big") <= len(data) - 2:  # mov_text: length + text
            data = data[2:2 + int.from_bytes(data[:2], "big")]
        return [plain_text(data)]


def sample(container, streams) -> dict[int, list[str]]:
    """The first cues of text subtitle tracks (at most SAMPLE_CUES per track; at most
    SAMPLE_BYTES are read from the start of the file). Rewinds the container first."""
    wanted = {s.index: s for s in streams if codec_name(s) in TEXT_CODECS}
    texts: dict[int, list[str]] = {i: [] for i in wanted}
    if not wanted:
        return texts
    try:
        container.seek(0)
    except Exception:  # noqa: BLE001 - a stream that cannot be rewound is read from where it is
        pass
    try:
        for packet in container.demux():
            if packet.pos is not None and packet.pos > SAMPLE_BYTES:
                break
            stream = wanted.get(packet.stream.index)
            if stream is None or not packet.size or len(texts[stream.index]) >= SAMPLE_CUES:
                continue
            texts[stream.index].extend(t for t in packet_texts(stream, packet) if t)
            if all(len(t) >= SAMPLE_CUES for t in texts.values()):
                break
    except Exception:  # noqa: BLE001 - whatever could be read is inspected
        pass
    return texts


def telemetry(texts: list[str]) -> tuple[str | None, bool]:
    """Do the cues belong to a log track? Returns (description or None, has a location).

    At least half of the cues must match a log pattern: the word "GPS" appearing once in
    a movie subtitle does not get the track removed. Tracks with only dates/speeds need at
    least 3 cues (a single-cue "Birthday 2024-05-12" subtitle is not a log)."""
    cues = [text for text in texts if text.strip()]
    if not cues:
        return None, False
    location = [bool(_COORDS.search(c) or _HEMISPHERE.search(c) or _LOCATION_WORDS.search(c)) for c in cues]
    camera = [bool(_CAMERA_WORDS.search(c)) for c in cues]
    stamp = [bool(_STAMP.search(c)) for c in cues]
    half = (len(cues) + 1) // 2
    has_location = sum(location) >= half
    has_camera = sum(camera) >= half
    if sum(a or b for a, b in zip(location, camera, strict=True)) >= half:
        if has_location and has_camera:
            return t("subtitles.log.location_camera"), True
        if has_location:
            return t("subtitles.log.location"), True
        if has_camera:
            return t("subtitles.log.camera"), False
        return t("subtitles.log.location_camera"), any(location)
    if len(cues) >= 3 and sum(stamp) >= half:
        return t("subtitles.log.stamp"), False
    return None, False


def codec_name(stream) -> str:
    try:
        return stream.codec_context.codec.canonical_name if stream.codec_context is not None else ""
    except Exception:  # noqa: BLE001 - track without a decoder
        return ""
