"""Readable hints and date extraction based on the contents of XMP packets."""

from __future__ import annotations

import re
from datetime import datetime

from ...i18n import t
from .exif import parse_exif_datetime
from .model import Kind, ScanResult

# Namespace/tag fragments inside XMP → the key of the hint shown to the user.
_HINTS = (
    (b"GPSLatitude", "xmp.hint.location"),
    (b"mwg-rs:Regions", "xmp.hint.faces"),
    (b"MP:RegionInfo", "xmp.hint.faces"),
    (b"PersonInImage", "xmp.hint.people"),
    (b"xmpMM:History", "xmp.hint.history"),
    (b"xmpMM:DocumentID", "xmp.hint.document_id"),
    (b"crs:", "xmp.hint.camera_raw"),
    (b"GCamera:MotionPhoto", "xmp.hint.motion_photo"),
    (b"GCamera:MicroVideo", "xmp.hint.motion_photo"),
    (b"GDepth:", "xmp.hint.depth"),
    (b"GImage:Data", "xmp.hint.original"),
    (b"hdrgm:", "xmp.hint.gain_map"),
    (b"drone-dji:", "xmp.hint.drone"),
    (b"dc:creator", "xmp.hint.author"),
    (b"dc:rights", "xmp.hint.rights"),
    (b"dc:subject", "xmp.hint.keywords"),
    (b"Iptc4xmp", "xmp.hint.iptc"),
    (b"photoshop:", "xmp.hint.photoshop"),
    (b"xmp:CreatorTool", "xmp.hint.software"),
    (b"DigitalSourceType", "xmp.hint.ai"),
)

_DATE_PATTERNS = [
    re.compile(p)
    for p in (
        rb'exif:DateTimeOriginal\s*=\s*"([^"]+)"',
        rb"<exif:DateTimeOriginal>([^<]+)<",
        rb'photoshop:DateCreated\s*=\s*"([^"]+)"',
        rb"<photoshop:DateCreated>([^<]+)<",
        rb'xmp:CreateDate\s*=\s*"([^"]+)"',
        rb"<xmp:CreateDate>([^<]+)<",
    )
]


def xmp_hints(xmp: bytes) -> list[str]:
    seen: list[str] = []
    for needle, hint in _HINTS:
        if needle in xmp and t(hint) not in seen:
            seen.append(t(hint))
    return seen


def analyze_xmp(xmp: bytes, res: ScanResult, *, extended: bool = False) -> None:
    hints = xmp_hints(xmp)
    detail = (t("xmp.extended") + " · " if extended else "") + ", ".join(hints)
    res.add(Kind.XMP, detail.strip(" ·"), len(xmp))
    if b"GPSLatitude" in xmp and not res.has_gps():
        res.add(Kind.GPS, t("xmp.inside"))
    if res.summary.taken is None:
        res.summary.taken = xmp_datetime(xmp)


def xmp_datetime(xmp: bytes) -> datetime | None:
    for pattern in _DATE_PATTERNS:
        m = pattern.search(xmp)
        if m:
            taken = parse_exif_datetime(m.group(1))
            if taken:
                return taken
    return None
