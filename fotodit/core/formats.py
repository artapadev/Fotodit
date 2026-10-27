"""Supported image and video formats; format detection from the content (magic bytes).

File extensions are not trusted: many ".jpg" files downloaded from the internet are actually PNG
or WebP. During processing the real format is always determined from the file's content; the
extension is only used for quick pre-filtering in folder listings and as a hint for telling
TIFF-based RAW files apart.

Most RAW files (DNG, NEF, ARW, CR2, PEF …) are structurally TIFF. Pillow takes them for TIFF and
opens only the small preview inside, so files that look like TIFF are checked further (DNG version
tag, CFA sensor data).

For videos the format is the container (MP4, MOV, AVI, MKV …); the codecs inside (H.264, HEVC …)
are read by `fotodit.core.video`. MP4/MOV/3GP use the same box structure as HEIC (ISO BMFF, 'ftyp'): if
the 'ftyp' brand is not an image brand, the file is treated as a video.
"""

from __future__ import annotations

import mmap
import os
from enum import Enum


class Fmt(str, Enum):
    JPEG = "JPEG"
    PNG = "PNG"
    WEBP = "WEBP"
    GIF = "GIF"
    TIFF = "TIFF"
    BMP = "BMP"
    HEIF = "HEIF"
    AVIF = "AVIF"
    RAW = "RAW"
    # Videos (container format)
    MP4 = "MP4"
    MOV = "MOV"
    THREEGP = "3GP"
    AVI = "AVI"
    MKV = "MKV"
    WEBM = "WEBM"
    WMV = "WMV"
    MTS = "MTS"  # MPEG-TS / M2TS (AVCHD cameras)
    MPEG = "MPEG"  # MPEG program stream (MPG, VOB)
    FLV = "FLV"
    OGV = "OGV"


VIDEO_FORMATS = frozenset({
    Fmt.MP4, Fmt.MOV, Fmt.THREEGP, Fmt.AVI, Fmt.MKV, Fmt.WEBM, Fmt.WMV, Fmt.MTS, Fmt.MPEG, Fmt.FLV, Fmt.OGV,
})
# Containers that can carry the orientation (display matrix), so the video can be
# rotated without re-encoding.
MATRIX_FORMATS = frozenset({Fmt.MP4, Fmt.MOV, Fmt.THREEGP})

# Video file extensions. ".ts" is deliberately missing: it clashes with TypeScript
# source files; cameras use ".mts"/".m2ts".
VIDEO_EXTENSIONS: dict[str, Fmt] = {
    ".mp4": Fmt.MP4,
    ".m4v": Fmt.MP4,
    ".mov": Fmt.MOV,
    ".qt": Fmt.MOV,
    ".3gp": Fmt.THREEGP,
    ".3g2": Fmt.THREEGP,
    ".avi": Fmt.AVI,
    ".mkv": Fmt.MKV,
    ".webm": Fmt.WEBM,
    ".wmv": Fmt.WMV,
    ".asf": Fmt.WMV,
    ".mts": Fmt.MTS,
    ".m2ts": Fmt.MTS,
    ".m2t": Fmt.MTS,
    ".mpg": Fmt.MPEG,
    ".mpeg": Fmt.MPEG,
    ".vob": Fmt.MPEG,
    ".flv": Fmt.FLV,
    ".ogv": Fmt.OGV,
}


# Camera raw (RAW) file extensions.
RAW_EXTENSIONS = {
    ".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf", ".rw2", ".raw",
    ".rwl", ".raf", ".pef", ".ptx", ".srw", ".x3f", ".3fr", ".fff", ".iiq", ".erf", ".kdc", ".dcr",
    ".mrw", ".mef", ".mos", ".gpr",
}

# Extensions shown in folder listings.
EXTENSIONS: dict[str, Fmt] = {
    ".jpg": Fmt.JPEG,
    ".jpeg": Fmt.JPEG,
    ".jpe": Fmt.JPEG,
    ".jfif": Fmt.JPEG,
    ".png": Fmt.PNG,
    ".apng": Fmt.PNG,
    ".webp": Fmt.WEBP,
    ".gif": Fmt.GIF,
    ".tif": Fmt.TIFF,
    ".tiff": Fmt.TIFF,
    ".bmp": Fmt.BMP,
    ".dib": Fmt.BMP,
    ".heic": Fmt.HEIF,
    ".heif": Fmt.HEIF,
    ".hif": Fmt.HEIF,
    ".avif": Fmt.AVIF,
    **{ext: Fmt.RAW for ext in RAW_EXTENSIONS},
    **VIDEO_EXTENSIONS,
}

# Extension to write for a format (when renaming or converting).
# RAW, MPEG-TS (.mts/.m2ts) and MPEG have no single extension; the original extension is kept.
PREFERRED_EXT: dict[Fmt, str] = {
    Fmt.JPEG: ".jpg",
    Fmt.PNG: ".png",
    Fmt.WEBP: ".webp",
    Fmt.GIF: ".gif",
    Fmt.TIFF: ".tif",
    Fmt.BMP: ".bmp",
    Fmt.HEIF: ".heic",
    Fmt.AVIF: ".avif",
    Fmt.MP4: ".mp4",
    Fmt.MOV: ".mov",
    Fmt.THREEGP: ".3gp",
    Fmt.AVI: ".avi",
    Fmt.MKV: ".mkv",
    Fmt.WEBM: ".webm",
    Fmt.WMV: ".wmv",
    Fmt.FLV: ".flv",
    Fmt.OGV: ".ogv",
}

DISPLAY_NAME: dict[Fmt, str] = {
    Fmt.JPEG: "JPEG",
    Fmt.PNG: "PNG",
    Fmt.WEBP: "WebP",
    Fmt.GIF: "GIF",
    Fmt.TIFF: "TIFF",
    Fmt.BMP: "BMP",
    Fmt.HEIF: "HEIC",
    Fmt.AVIF: "AVIF",
    Fmt.RAW: "RAW",
    Fmt.MP4: "MP4",
    Fmt.MOV: "MOV",
    Fmt.THREEGP: "3GP",
    Fmt.AVI: "AVI",
    Fmt.MKV: "MKV",
    Fmt.WEBM: "WebM",
    Fmt.WMV: "WMV",
    Fmt.MTS: "MTS",
    Fmt.MPEG: "MPEG",
    Fmt.FLV: "FLV",
    Fmt.OGV: "OGV",
}

SNIFF_BYTES = 1024  # MPEG-TS packets are 188/192 bytes: several sync bytes must be visible

_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"}
_AVIF_BRANDS = {b"avif", b"avis"}
_GENERIC_BRANDS = {b"mif1", b"msf1", b"miaf", b"MiHE", b"MiHB", b"MiPr"}
_ASF_GUID = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
# First boxes of old QuickTime files that start without 'ftyp'
_QT_ATOMS = {b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"}

# RAW signatures that do not look like TIFF
_RAW_MAGIC = (
    b"IIRO", b"IIRS", b"MMOR",  # Olympus ORF
    b"IIU\x00",  # Panasonic RW2 / RAW / Leica RWL
    b"FUJIFILMCCD-RAW",  # Fujifilm RAF
    b"FOVb",  # Sigma X3F
    b"\x00MRM",  # Minolta MRW
    b"II\x1a\x00\x00\x00HEAPCCDR",  # Canon CRW
)


def extension(name: str | None) -> str:
    return os.path.splitext(name)[1].lower() if name else ""


def is_supported_name(name: str) -> bool:
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in EXTENSIONS


def is_raw_name(name: str) -> bool:
    return extension(name) in RAW_EXTENSIONS


def is_video_name(name: str) -> bool:
    return extension(name) in VIDEO_EXTENSIONS


def is_video(fmt: Fmt | None) -> bool:
    return fmt in VIDEO_FORMATS


def sniff(head: bytes) -> Fmt | None:
    """Determines the format from the first bytes of a file; returns None if it is not recognized.

    TIFF-based RAWs (DNG, NEF, ARW …) look like TIFF here; use `detect()` for a definitive answer.
    """
    head = bytes(head[:SNIFF_BYTES])
    if head[:3] == b"\xff\xd8\xff":
        return Fmt.JPEG
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return Fmt.PNG
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return Fmt.WEBP
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return Fmt.GIF
    if any(head.startswith(m) for m in _RAW_MAGIC):
        return Fmt.RAW
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        if head[8:10] == b"CR":  # Canon CR2
            return Fmt.RAW
        return Fmt.TIFF
    if head[:2] == b"BM" and len(head) >= 18:
        return Fmt.BMP
    if head[4:8] == b"ftyp":
        if head[8:12] == b"crx ":  # Canon CR3
            return Fmt.RAW
        return _sniff_ftyp(head)
    return _sniff_video(head)


def _sniff_video(head: bytes) -> Fmt | None:
    """Video containers without an 'ftyp' box."""
    if head[4:8] in _QT_ATOMS and int.from_bytes(head[0:4], "big") >= 8:
        return Fmt.MOV
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return Fmt.AVI
    if head[:4] == bytes.fromhex("1a45dfa3"):  # EBML (Matroska / WebM)
        return Fmt.WEBM if b"webm" in head[:64] else Fmt.MKV
    if head[:16] == _ASF_GUID:
        return Fmt.WMV
    if head[:4] == b"FLV" + bytes([1]):
        return Fmt.FLV
    if head[:4] == b"OggS":
        return Fmt.OGV
    if head[:4] in (bytes.fromhex("000001ba"), bytes.fromhex("000001b3")):  # MPEG program / MPEG-1 stream
        return Fmt.MPEG
    for offset, step in ((0, 188), (4, 192)):  # MPEG-TS, M2TS (4-byte timestamp + 188)
        if len(head) >= offset + 2 * step + 1 and all(head[offset + i * step] == 0x47 for i in range(3)):
            return Fmt.MTS
    return None


def _sniff_ftyp(head: bytes) -> Fmt | None:
    box_size = int.from_bytes(head[0:4], "big")
    end = min(len(head), box_size if box_size >= 16 else len(head))
    major = head[8:12]
    brands = [major] + [head[i:i + 4] for i in range(16, end - 3, 4)]
    if major in _AVIF_BRANDS:
        return Fmt.AVIF
    if major in _HEIF_BRANDS:
        return Fmt.HEIF
    if major in _GENERIC_BRANDS or any(b in _GENERIC_BRANDS for b in brands):
        if any(b in _AVIF_BRANDS for b in brands):
            return Fmt.AVIF
        if any(b in _HEIF_BRANDS for b in brands):
            return Fmt.HEIF
        return None  # unrecognized image file (not a video)
    # No image brand, so it is a video: MP4, MOV (QuickTime) or 3GP.
    if major == b"qt  ":
        return Fmt.MOV
    if major[:2] == b"3g":
        return Fmt.THREEGP
    return Fmt.MP4


def detect(data, name: str | None = None) -> Fmt | None:
    """Definitive format detection from the full content (bytes or mmap); `name` gives the extension hint."""
    fmt = sniff(data[:SNIFF_BYTES])
    raw_ext = extension(name) in RAW_EXTENSIONS
    if fmt == Fmt.TIFF:
        from .meta.raw import tiff_looks_raw

        if raw_ext or tiff_looks_raw(data):
            return Fmt.RAW
    if fmt is None and raw_ext:
        return Fmt.RAW  # RAW types with an unknown signature: left to LibRaw
    return fmt


def sniff_file(path: str) -> Fmt | None:
    with open(path, "rb") as fh:
        head = fh.read(SNIFF_BYTES)
        fmt = sniff(head)
        if fmt != Fmt.TIFF and not (fmt is None and is_raw_name(path)):
            return fmt
        size = os.fstat(fh.fileno()).st_size
        if size == 0:
            return None
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            return detect(mm, path)
