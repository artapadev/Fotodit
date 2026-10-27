"""Metadata of RAW files (DNG, CR2, CR3, NEF, ARW, RAF, ORF, RW2 …).

RAW files cannot be cleaned in place by deleting metadata: information needed
to decode the image, such as color matrices, black/white levels and the sensor
pattern, also lives in the metadata. Fotodit cleans a RAW by converting it to JPG.

This module reads the metadata inside a RAW (info panel, sort date,
conversion that keeps metadata) and tells TIFF-based RAWs apart from ordinary
TIFFs. Three container types are supported:

* TIFF-based (DNG, CR2, NEF, ARW, PEF, SRW, ORF, RW2, 3FR, IIQ …),
* Fujifilm RAF (the header embeds a JPEG with EXIF),
* Canon CR3 (ISO media file; EXIF in the 'CMT1'–'CMT4' boxes).
"""

from __future__ import annotations

import struct
import warnings
from dataclasses import dataclass, field

from PIL import Image

from ...i18n import t
from .exif import (
    TAG_DATETIME,
    TAG_DATETIME_DIGITIZED,
    TAG_DATETIME_ORIGINAL,
    TAG_MAKE,
    TAG_MAKERNOTE,
    TAG_MODEL,
    TAG_ORIENTATION,
    TAG_SUBSEC_ORIGINAL,
    analyze_exif,
    camera_name,
    gps_coordinates,
    metadata_span,
    parse_exif_datetime,
)
from .model import Kind, ScanResult
from .xmp import analyze_xmp

HEAD_LIMIT = 8 * 1024 * 1024  # leading part read for EXIF when its directories cannot be measured (`metadata_span`)
_TIFF_MAGICS = {42, 0x4F52, 0x5352, 0x0055}  # TIFF, Olympus ORF (RO/RS), Panasonic RW2 (U)
CANON_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")
PREVIEW_UUID = bytes.fromhex("eaf42b5e1c984b88b9fbb7dc406e4d16")
XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")

# Descriptive IFD0 tags carried over to the JPEG in a conversion that keeps metadata.
_DESCRIPTIVE = (270, 271, 272, 305, 306, 315, 316, 33432, 0x9C9B, 0x9C9C, 0x9C9D, 0x9C9E, 0x9C9F)
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}


# ------------------------------------------------------------------ TIFF structure


def tiff_header(data) -> tuple[str, int] | None:
    if len(data) < 8:
        return None
    head = bytes(data[:8])
    if head[:2] == b"II":
        e = "<"
    elif head[:2] == b"MM":
        e = ">"
    else:
        return None
    if struct.unpack_from(e + "H", head, 2)[0] not in _TIFF_MAGICS:
        return None
    return e, struct.unpack_from(e + "I", head, 4)[0]


def _entry_span(data, e: str, entry) -> tuple[int, int] | None:
    typ, count, pos = entry
    total = _TYPE_SIZE.get(typ, 1) * count
    start = pos if total <= 4 else struct.unpack_from(e + "I", data, pos)[0]
    if count > 1 << 26 or start + total > len(data):
        return None
    return start, total


def _numbers(data, e: str, entry, limit: int = 4096) -> list[int]:
    typ, count, _ = entry
    fmt = {1: "B", 7: "B", 3: "H", 4: "I", 13: "I"}.get(typ)
    span = _entry_span(data, e, entry)
    if fmt is None or span is None or count > limit:
        return []
    return list(struct.unpack_from(e + f"{count}{fmt}", data, span[0]))


def _blob(data, e: str, entry) -> bytes:
    span = _entry_span(data, e, entry)
    return bytes(data[span[0]:span[0] + span[1]]) if span else b""


def iter_ifds(data, e: str, first: int, limit: int = 64):
    """(offset, {tag: (type, count, value_pos)}) — the IFD chain and the SubIFDs."""
    n = len(data)
    todo = [first]
    seen: set[int] = set()
    while todo and len(seen) < limit:
        off = todo.pop(0)
        if off in seen or off <= 0 or off + 2 > n:
            continue
        seen.add(off)
        count = struct.unpack_from(e + "H", data, off)[0]
        if count > 2000 or off + 2 + count * 12 + 4 > n:
            continue
        tags = {}
        for i in range(count):
            p = off + 2 + 12 * i
            tag, typ, cnt = struct.unpack_from(e + "HHI", data, p)
            tags[tag] = (typ, cnt, p + 8)
        yield off, tags
        nxt = struct.unpack_from(e + "I", data, off + 2 + count * 12)[0]
        if nxt:
            todo.append(nxt)
        if 330 in tags:
            todo.extend(_numbers(data, e, tags[330], limit=64))


def tiff_looks_raw(data) -> bool:
    """Is a file with a standard TIFF header actually a RAW? (DNG version or CFA sensor data)"""
    header = tiff_header(data)
    if not header:
        return False
    e, first = header
    for _, tags in iter_ifds(data, e, first):
        if 50706 in tags:
            return True
        if 262 in tags and _numbers(data, e, tags[262])[:1] in ([32803], [34892]):
            return True
    return False


# ------------------------------------------------------------------ containers


def container(data) -> str:
    head = bytes(data[:16])
    if head.startswith(b"FUJIFILMCCD-RAW"):
        return "raf"
    if head[4:8] == b"ftyp" and head[8:12] == b"crx ":
        return "cr3"
    if tiff_header(data):
        return "tiff"
    return "other"


def raf_jpeg(data) -> bytes | None:
    """The JPEG embedded in the RAF header (the camera's own JPEG; it contains EXIF)."""
    if len(data) < 92:
        return None
    off, length = struct.unpack_from(">II", data, 84)
    if not off or not length or off + length > len(data):
        return None
    jpeg = bytes(data[off:off + length])
    return jpeg if jpeg.startswith(b"\xff\xd8") else None


def _boxes(data, start: int, end: int):
    from .bmff import iter_boxes

    try:
        yield from iter_boxes(data, start, end)
    except ValueError:
        return


@dataclass
class Cr3:
    cmt: dict[str, bytes] = field(default_factory=dict)
    previews: int = 0
    xmp: bytes | None = None


def parse_cr3(data) -> Cr3:
    out = Cr3()
    for typ, ps, pe, start in _boxes(data, 0, len(data)):
        uid = bytes(data[start + 8:start + 24]) if typ == b"uuid" else b""
        if typ == b"moov":
            for mtyp, mps, mpe, mstart in _boxes(data, ps, pe):
                if mtyp == b"uuid" and bytes(data[mstart + 8:mstart + 24]) == CANON_UUID:
                    for ctyp, cps, cpe, _ in _boxes(data, mps, mpe):
                        name = ctyp.decode("latin1", "replace")
                        if name.startswith("CMT"):
                            out.cmt[name] = bytes(data[cps:cpe])
                        elif ctyp == b"THMB":
                            out.previews += 1
        elif uid == PREVIEW_UUID:
            out.previews += 1
        elif uid == XMP_UUID:
            out.xmp = bytes(data[ps:pe])
    return out


# ------------------------------------------------------------------ EXIF parts


@dataclass
class ExifParts:
    ifd0: dict
    exif: dict
    gps: dict
    interop: dict
    makernote: bytes | None
    blob: bytes | None  # TIFF bytes that analyze_exif can inspect
    tiff_file: bool = False


def _load_exif(blob: bytes) -> Image.Exif | None:
    ex = Image.Exif()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ex.load(b"Exif\x00\x00" + blob)
    except Exception:  # noqa: BLE001 - broken EXIF
        return None
    return ex


def _sub(ex: Image.Exif, tag: int) -> dict:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return dict(ex.get_ifd(tag) or {})
    except Exception:  # noqa: BLE001
        return {}


def _normalized_head(data) -> bytes:
    """The leading bytes of a TIFF-based RAW that its IFD0, Exif and GPS directories take up, wherever they
    lie (the first HEAD_LIMIT bytes if that cannot be measured), with a standard TIFF magic for Pillow."""
    e, first = tiff_header(data)
    end = metadata_span(data, e, first)
    head = bytes(data[:HEAD_LIMIT if end is None else end])
    magic = b"*\x00" if head[:2] == b"II" else b"\x00*"
    return head[:2] + magic + head[4:]


def _compose(ifd0: dict, exif: dict, gps: dict, makernote: bytes | None = None) -> bytes | None:
    ex = Image.Exif()
    for tag, value in ifd0.items():
        if tag in _DESCRIPTIVE or tag == TAG_ORIENTATION:
            ex[tag] = value
    sub = {k: v for k, v in exif.items() if k != TAG_MAKERNOTE and k != 0xA005}
    if makernote:
        sub[TAG_MAKERNOTE] = makernote
    if sub:
        ex[0x8769] = sub
    if gps:
        ex[0x8825] = gps
    try:
        return ex.tobytes()[6:] if len(ex) else None
    except Exception:  # noqa: BLE001 - odd values that cannot be written
        return None


def exif_parts(data) -> ExifParts | None:
    kind = container(data)
    if kind == "tiff":
        head = _normalized_head(data)
        ex = _load_exif(head)
        if ex is None:
            return None
        exif = _sub(ex, 0x8769)
        return ExifParts(dict(ex), exif, _sub(ex, 0x8825), _sub(ex, 0xA005), exif.get(TAG_MAKERNOTE), head, True)
    if kind == "cr3":
        cr3 = parse_cr3(data)
        loaded = {k: _load_exif(v) for k, v in cr3.cmt.items()}
        ifd0 = dict(loaded["CMT1"]) if loaded.get("CMT1") is not None else {}
        exif = dict(loaded["CMT2"]) if loaded.get("CMT2") is not None else {}
        gps = dict(loaded["CMT4"]) if loaded.get("CMT4") is not None else {}
        makernote = cr3.cmt.get("CMT3")
        if not (ifd0 or exif or gps):
            return None
        return ExifParts(ifd0, exif, gps, {}, makernote, _compose(ifd0, exif, gps, makernote))
    if kind == "raf":
        jpeg = raf_jpeg(data)
        if jpeg:
            from .jpeg import parse

            raw = parse(jpeg).exif()
            ex = _load_exif(raw[6:]) if raw else None
            if ex is not None:
                exif = _sub(ex, 0x8769)
                return ExifParts(dict(ex), exif, _sub(ex, 0x8825), _sub(ex, 0xA005), exif.get(TAG_MAKERNOTE), raw[6:])
    return None


def taken_of(parts: ExifParts):
    """Capture date with milliseconds (for sorting); None if there is none."""
    for value in (parts.exif.get(TAG_DATETIME_ORIGINAL), parts.exif.get(TAG_DATETIME_DIGITIZED), parts.ifd0.get(TAG_DATETIME)):
        taken = parse_exif_datetime(value)
        if taken:
            sub = parts.exif.get(TAG_SUBSEC_ORIGINAL)
            digits = "".join(ch for ch in str(sub or "") if ch.isdecimal())[:3]  # not isdigit: "²" fails int()
            return taken.replace(microsecond=int(digits.ljust(3, "0")) * 1000 if digits else 0)
    return None


def camera_of(parts: ExifParts) -> str | None:
    return camera_name(parts.ifd0.get(TAG_MAKE), parts.ifd0.get(TAG_MODEL))


def gps_of(parts: ExifParts) -> tuple[float, float] | None:
    return gps_coordinates(parts.gps) if parts.gps else None


def is_adobe_rgb(parts: ExifParts | None) -> bool:
    """If the camera was in Adobe RGB mode, the embedded JPEG preview is Adobe RGB too."""
    if not parts:
        return False
    color_space = parts.exif.get(0xA001)
    index = str(parts.interop.get(1, "") or "").strip("\x00 ").upper()
    return color_space == 2 or (index == "R03" and color_space != 1)


def exif_for_jpeg(data) -> tuple[bytes | None, bool]:
    """EXIF to write into the JPEG in a conversion that keeps metadata (orientation = 1).

    The maker-specific MakerNote is not carried over (its offsets break in another file).
    Returns: (EXIF bytes with the "Exif\\0\\0" prefix, whether the MakerNote was skipped)
    """
    parts = exif_parts(data)
    if parts is None:
        return None, False
    ifd0 = dict(parts.ifd0)
    if TAG_ORIENTATION in ifd0:
        ifd0[TAG_ORIENTATION] = 1
    blob = _compose(ifd0, parts.exif, parts.gps)
    return (b"Exif\x00\x00" + blob if blob else None), bool(parts.makernote)


def xmp_of(data) -> bytes | None:
    kind = container(data)
    if kind == "tiff":
        header = tiff_header(data)
        if header:
            e, first = header
            for _, tags in iter_ifds(data, e, first, limit=1):
                if 700 in tags:
                    return _blob(data, e, tags[700]) or None
    if kind == "cr3":
        return parse_cr3(data).xmp
    if kind == "raf":
        jpeg = raf_jpeg(data)
        if jpeg:
            from .jpeg import APP1, XMP_ID, parse

            for p in parse(jpeg).app(APP1, XMP_ID):
                return p[len(XMP_ID):]
    return None


# ------------------------------------------------------------------------ scanning


def scan(data, res: ScanResult) -> None:
    kind = container(data)
    if kind == "raf":
        jpeg = raf_jpeg(data)
        if jpeg:
            from .jpeg import scan as scan_jpeg

            scan_jpeg(jpeg, res)
            res.add(Kind.THUMBNAIL, t("raw.camera_jpeg"), len(jpeg))
        res.add(Kind.OTHER, t("raw.raf"))
        return
    parts = exif_parts(data)
    if parts and parts.blob:
        before = len(res.findings)
        analyze_exif(parts.blob, res, tiff_file=parts.tiff_file)
        for f in res.findings[before:]:
            if f.kind == Kind.EXIF:
                # In a RAW the "field count" also covers structural tags; drop it so it does not mislead.
                f.detail = f.detail.split(" · ", 1)[1] if " · " in f.detail else t("raw.inside")
                f.size = 0
        if parts.makernote and Kind.MAKERNOTE not in res.kinds():
            res.add(Kind.MAKERNOTE, "", len(parts.makernote))
    else:
        res.add(Kind.EXIF, t("raw.inside"))
    if kind == "tiff":
        _scan_tiff_extras(data, res)
    elif kind == "cr3":
        cr3 = parse_cr3(data)
        if cr3.previews:
            res.add(Kind.THUMBNAIL, t("raw.previews", n=cr3.previews))
        if cr3.xmp:
            analyze_xmp(cr3.xmp, res)


def _scan_tiff_extras(data, res: ScanResult) -> None:
    e, first = tiff_header(data)
    previews = 0
    for _, tags in iter_ifds(data, e, first):
        photometric = _numbers(data, e, tags[262])[:1] if 262 in tags else []
        subfile = (_numbers(data, e, tags[254])[:1] if 254 in tags else []) or [0]
        compression = _numbers(data, e, tags[259])[:1] if 259 in tags else []
        is_raw = photometric in ([32803], [34892]) or (subfile == [0] and compression not in ([6], [7]))
        if 513 in tags or (not is_raw and (subfile[0] & 1 or compression in ([6], [7]))):
            previews += 1
        if 700 in tags and Kind.XMP not in res.kinds():
            analyze_xmp(_blob(data, e, tags[700]), res)
        if 33723 in tags and Kind.IPTC not in res.kinds():
            res.add(Kind.IPTC, "", _entry_span(data, e, tags[33723])[1] if _entry_span(data, e, tags[33723]) else 0)
        if 34675 in tags and Kind.ICC not in res.kinds():
            res.add(Kind.ICC, "")
        if 50740 in tags:
            res.add(Kind.OTHER, t("raw.private"))
        if 50972 in tags:
            res.add(Kind.OTHER, t("raw.original"))
        if 50827 in tags:
            name = _blob(data, e, tags[50827]).split(b"\x00", 1)[0].decode("utf-8", "replace")
            res.add(Kind.OTHER, t("raw.original_name", name=name)[:80])
    if previews:
        res.add(Kind.THUMBNAIL, t("raw.previews", n=previews))
