"""Format-independent metadata scanning interface."""

from __future__ import annotations

import mmap
import os

from ..formats import Fmt, detect, is_video, sniff_file
from . import bmff, bmp, gif, jpeg, png, raw, tiff, webp
from .exif import analyze_exif
from .model import Finding, ImageSummary, Kind, ScanResult
from .xmp import analyze_xmp

__all__ = [
    "Finding",
    "ImageSummary",
    "Kind",
    "ScanResult",
    "residual_signatures",
    "scan_bytes",
    "scan_file",
]

_SCANNERS = {
    Fmt.JPEG: jpeg.scan,
    Fmt.PNG: png.scan,
    Fmt.WEBP: webp.scan,
    Fmt.GIF: gif.scan,
    Fmt.TIFF: tiff.scan,
    Fmt.BMP: bmp.scan,
    Fmt.HEIF: bmff.scan,
    Fmt.AVIF: bmff.scan,
    Fmt.RAW: raw.scan,
}


def scan_bytes(data, fmt: Fmt | None = None, *, enrich: bool = True, name: str | None = None) -> ScanResult:
    """Finds all metadata in the file contents.

    `enrich`: for HEIF/AVIF, the EXIF/XMP details (GPS, camera, date) are read
    through the decoder. Not needed for verification; the container structure is enough.
    `name`: file name (so TIFF-based RAW is also recognized by its extension).
    """
    fmt = fmt or detect(data, name)
    res = ScanResult()
    if fmt is None:
        return res
    _SCANNERS[fmt](data, res)
    if enrich and fmt in (Fmt.HEIF, Fmt.AVIF) and (Kind.EXIF in res.kinds() or Kind.XMP in res.kinds()):
        _enrich_bmff(data, res)
    return res


def scan_file(path: str) -> ScanResult:
    fmt = sniff_file(path)
    if is_video(fmt):
        from ..video.probe import probe, scan_result

        return scan_result(probe(path, fmt))
    if fmt in (Fmt.RAW, Fmt.TIFF) and os.path.getsize(path) > 0:
        # RAW and TIFF files are large and their metadata lives in directories: scan them through a
        # memory map instead of reading them whole.
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            return scan_bytes(mm, fmt)
    with open(path, "rb") as fh:
        data = fh.read()
    return scan_bytes(data, fmt, name=path)


def _enrich_bmff(data: bytes, res: ScanResult) -> None:
    """Takes the EXIF/XMP content of a HEIF/AVIF from the decoder and analyzes it in detail."""
    import io

    from ..imaging import open_image

    try:
        with open_image(io.BytesIO(data)) as im:
            exif_raw = im.info.get("exif")
            xmp_raw = im.info.get("xmp")
            orientation = im.info.get("original_orientation")
    except Exception:
        return
    detailed = ScanResult()
    if exif_raw:
        analyze_exif(exif_raw, detailed)
    if xmp_raw:
        analyze_xmp(xmp_raw if isinstance(xmp_raw, bytes) else str(xmp_raw).encode(), detailed)
    if not detailed.findings:
        return
    res.findings = [f for f in res.findings if f.kind not in (Kind.EXIF, Kind.XMP)] + detailed.findings
    s, d = res.summary, detailed.summary
    s.camera = s.camera or d.camera
    s.software = s.software or d.software
    s.taken = s.taken or d.taken
    s.gps = s.gps or d.gps
    if orientation:
        s.orientation = orientation
    elif s.orientation is None:
        s.orientation = d.orientation


# Signatures that must not appear anywhere in the output, long enough that they
# are almost impossible to occur by chance.
_SIGNATURES = (
    (b"Exif\x00\x00II*\x00", "EXIF"),
    (b"Exif\x00\x00MM\x00*", "EXIF"),
    (b"<x:xmpmeta", "XMP"),
    (b"<?xpacket", "XMP"),
    (b"http://ns.adobe.com/xap/1.0/", "XMP"),
    (b"http://ns.adobe.com/xmp/extension/", "XMP"),
    (b"ICC_PROFILE\x00", "ICC"),
    (b"Photoshop 3.0\x00", "Photoshop/IPTC"),
    (b"GPSLatitude", "GPS"),
    (b"XML:com.adobe.xmp", "XMP"),
)


def residual_signatures(data: bytes) -> list[str]:
    return sorted({name for sig, name in _SIGNATURES if sig in data})
