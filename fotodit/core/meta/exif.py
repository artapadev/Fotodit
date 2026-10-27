"""EXIF (TIFF structure) helpers: reading/patching the orientation, content analysis and the
date-only EXIF written when only the capture date is kept."""

from __future__ import annotations

import re
import struct
from datetime import datetime

from PIL import ExifTags, Image

from ...i18n import t
from ..tiffwriter import Ifd, build_tiff
from .model import Kind, ScanResult

EXIF_HEADER = b"Exif\x00\x00"

TAG_ORIENTATION = 0x0112
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_SOFTWARE = 0x0131
TAG_DATETIME = 0x0132
TAG_EXIF_IFD = 0x8769
TAG_GPS_IFD = 0x8825
TAG_DATETIME_ORIGINAL = 0x9003
TAG_DATETIME_DIGITIZED = 0x9004
TAG_SUBSEC_ORIGINAL = 0x9291
TAG_MAKERNOTE = 0x927C
TAG_PIXEL_X = 0xA002
TAG_PIXEL_Y = 0xA003
TAG_INTEROP_IFD = 0xA005

# Most leading bytes of a TIFF structure copied to read its IFD0, Exif and GPS directories (`metadata_span`).
SPAN_LIMIT = 64 * 1024 * 1024

# Fields that can identify a person/device (the keys of their names, shown in the EXIF finding's detail).
IDENTIFYING_TAGS = {
    0xA430: "exif.tag.owner",
    0xA431: "exif.tag.body_serial",
    0xA435: "exif.tag.lens_serial",
    0xA420: "exif.tag.unique_id",
    0x013B: "exif.tag.artist",
    0x8298: "exif.tag.copyright",
    0x9286: "exif.tag.user_comment",
    0x010E: "exif.tag.description",
    0x013C: "exif.tag.host",
    0x9C9B: "exif.tag.xp_title",
    0x9C9C: "exif.tag.xp_comment",
    0x9C9D: "exif.tag.xp_author",
    0x9C9E: "exif.tag.xp_keywords",
    0x9C9F: "exif.tag.xp_subject",
    0xA436: "exif.tag.image_title",
    0xA437: "exif.tag.photographer",
    0xA438: "exif.tag.editor",
}

_DATE_RE = re.compile(
    r"(\d{4})[:\-/.](\d{2})[:\-/.](\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?"
)
_EDGES = re.compile(r"^[\s\x00]+|[\s\x00]+$")  # NULs and whitespace (Unicode too) around a text value


def strip_exif_header(raw: bytes) -> bytes:
    """Removes the 'Exif\\0\\0' (or malformed 'Exif\\0\\xff') prefix; returns the raw TIFF bytes."""
    if raw[:4] == b"Exif" and len(raw) >= 6:
        return raw[6:]
    return raw


def with_exif_header(raw: bytes) -> bytes:
    return EXIF_HEADER + strip_exif_header(raw)


def _tiff_header(t: bytes) -> tuple[str, int] | None:
    if len(t) < 8:
        return None
    if t[:2] == b"II":
        e = "<"
    elif t[:2] == b"MM":
        e = ">"
    else:
        return None
    if struct.unpack_from(e + "H", t, 2)[0] != 42:
        return None
    return e, struct.unpack_from(e + "I", t, 4)[0]


def _find_entry(t: bytes, e: str, ifd: int, tag: int) -> int | None:
    if ifd <= 0 or ifd + 2 > len(t):
        return None
    count = struct.unpack_from(e + "H", t, ifd)[0]
    for i in range(count):
        off = ifd + 2 + 12 * i
        if off + 12 > len(t):
            return None
        if struct.unpack_from(e + "H", t, off)[0] == tag:
            return off
    return None


def read_orientation(raw: bytes) -> int | None:
    """Returns the Orientation value (1–8) from IFD0; None if it is missing or invalid."""
    t = strip_exif_header(raw)
    header = _tiff_header(t)
    if not header:
        return None
    e, ifd0 = header
    off = _find_entry(t, e, ifd0, TAG_ORIENTATION)
    if off is None:
        return None
    typ, count = struct.unpack_from(e + "HI", t, off + 2)
    if count < 1:
        return None
    if typ == 3:  # SHORT
        value = struct.unpack_from(e + "H", t, off + 8)[0]
    elif typ == 4:  # LONG
        value = struct.unpack_from(e + "I", t, off + 8)[0]
    else:
        return None
    return value if 1 <= value <= 8 else None


def patch_orientation(raw: bytes, value: int = 1) -> bytes:
    """Changes the Orientation value in place; all other bytes (including MakerNote
    offsets) are preserved. The prefix (if any) is kept."""
    prefix_len = len(raw) - len(strip_exif_header(raw))
    buf = bytearray(raw)
    t = raw[prefix_len:]
    header = _tiff_header(t)
    if not header:
        return raw
    e, ifd0 = header
    off = _find_entry(t, e, ifd0, TAG_ORIENTATION)
    if off is None:
        return raw
    typ = struct.unpack_from(e + "H", t, off + 2)[0]
    base = prefix_len + off + 8
    if typ == 3:
        struct.pack_into(e + "H", buf, base, value)
    elif typ == 4:
        struct.pack_into(e + "I", buf, base, value)
    return bytes(buf)


_TYPE_SIZES = {1: 1, 3: 2, 4: 4}


def _values(t: bytes, e: str, off: int) -> list[int]:
    """Values of a BYTE/SHORT/LONG IFD entry (read from its offset if they do not fit inline)."""
    typ, count = struct.unpack_from(e + "HI", t, off + 2)
    size = _TYPE_SIZES.get(typ)
    if size is None or count > 4096:
        return []
    start = off + 8 if size * count <= 4 else struct.unpack_from(e + "I", t, off + 8)[0]
    if start + size * count > len(t):
        return []
    fmt = e + {1: "B", 3: "H", 4: "I"}[typ] * count
    return list(struct.unpack_from(fmt, t, start))


def drop_thumbnail(raw: bytes) -> bytes:
    """Removes the EXIF thumbnail (IFD1): the thumbnail bytes and the IFD1 directory are zeroed
    and unlinked from IFD0. Other bytes and offsets (including MakerNote) stay unchanged.

    Once a photo is rotated or cropped, the old thumbnail no longer matches the image (File
    Explorer may show thumbnails from it) and may contain the region that was cropped away."""
    prefix_len = len(raw) - len(strip_exif_header(raw))
    t = raw[prefix_len:]
    header = _tiff_header(t)
    if not header:
        return raw
    e, ifd0 = header
    if ifd0 <= 0 or ifd0 + 2 > len(t):
        return raw
    link = ifd0 + 2 + 12 * struct.unpack_from(e + "H", t, ifd0)[0]
    if link + 4 > len(t):
        return raw
    ifd1 = struct.unpack_from(e + "I", t, link)[0]
    if not ifd1:
        return raw
    buf = bytearray(raw)

    def zero(start: int, length: int) -> None:
        start, end = max(0, start), min(len(t), start + max(0, length))
        if end > start:
            buf[prefix_len + start:prefix_len + end] = bytes(end - start)

    if 0 < ifd1 and ifd1 + 2 <= len(t):
        count = struct.unpack_from(e + "H", t, ifd1)[0]
        entries = {}
        for i in range(count):
            off = ifd1 + 2 + 12 * i
            if off + 12 > len(t):
                break
            entries[struct.unpack_from(e + "H", t, off)[0]] = off
        if 0x0201 in entries and 0x0202 in entries:  # JPEG thumbnail
            offsets, lengths = _values(t, e, entries[0x0201]), _values(t, e, entries[0x0202])
            if offsets and lengths:
                zero(offsets[0], lengths[0])
        if 0x0111 in entries and 0x0117 in entries:  # uncompressed thumbnail strips
            for start, length in zip(_values(t, e, entries[0x0111]), _values(t, e, entries[0x0117]), strict=False):
                zero(start, length)
        zero(ifd1, 2 + 12 * count + 4)
    struct.pack_into(e + "I", buf, prefix_len + link, 0)
    return bytes(buf)


def patch_pixel_size(raw: bytes, width: int, height: int) -> bytes:
    """Updates PixelXDimension/PixelYDimension in the EXIF sub-IFD in place (if present)."""
    prefix_len = len(raw) - len(strip_exif_header(raw))
    t = raw[prefix_len:]
    header = _tiff_header(t)
    if not header:
        return raw
    e, ifd0 = header
    pointer = _find_entry(t, e, ifd0, 0x8769)
    if pointer is None:
        return raw
    sub = _values(t, e, pointer)
    if not sub:
        return raw
    buf = bytearray(raw)
    for tag, value in ((TAG_PIXEL_X, width), (TAG_PIXEL_Y, height)):
        off = _find_entry(t, e, sub[0], tag)
        if off is None:
            continue
        typ, count = struct.unpack_from(e + "HI", t, off + 2)
        if count != 1:
            continue
        if typ == 3 and value <= 0xFFFF:
            struct.pack_into(e + "H", buf, prefix_len + off + 8, value)
        elif typ == 4:
            struct.pack_into(e + "I", buf, prefix_len + off + 8, value)
    return bytes(buf)


def for_edited(raw: bytes, size: tuple[int, int] | None = None) -> bytes:
    """EXIF to carry over to an edited (rotated/cropped) photo: orientation 1, old thumbnail
    removed, pixel dimensions set to the new size. The prefix (if any) is kept."""
    out = drop_thumbnail(patch_orientation(raw, 1))
    return patch_pixel_size(out, *size) if size else out


def date_only_exif(taken: datetime) -> bytes:
    """A minimal EXIF block that holds only the capture date ("Çekim tarihini koru"): DateTimeOriginal
    ("YYYY:MM:DD HH:MM:SS", local time) in the Exif IFD, reached from IFD0 by the ExifIFD pointer.
    Nothing else: no time zone, sub-seconds, orientation or device. Little-endian and laid out the
    same way every time, with the "Exif\\0\\0" header (70 bytes)."""
    text = (f"{taken.year:04d}:{taken.month:02d}:{taken.day:02d} "
            f"{taken.hour:02d}:{taken.minute:02d}:{taken.second:02d}")
    ifds = [("ifd0", Ifd("<").pointer(TAG_EXIF_IFD, "exif")), ("exif", Ifd("<").ascii(TAG_DATETIME_ORIGINAL, text))]
    return EXIF_HEADER + build_tiff(ifds, e="<")


_VALUE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
# The pointers followed inside the directory each pointer leads to: IFD0 → Exif and GPS, Exif → Interop.
_FOLLOWED = {TAG_EXIF_IFD: (TAG_INTEROP_IFD,), TAG_GPS_IFD: ()}


def metadata_span(data, e: str, ifd0: int) -> int | None:
    """How many leading bytes of a TIFF structure (`data`, bytes or a memory map; byte order `e`, IFD0 at
    offset `ifd0`) its IFD0, Exif IFD (with its Interop IFD) and GPS IFD take up, the values stored outside
    their entries included: what `analyze_exif` needs, without the image data of a large file. None if a
    directory runs past the end, or the span is larger than SPAN_LIMIT: the structure is read whole then."""
    n = len(data)
    end = 8
    todo: list[tuple[int, tuple[int, ...]]] = [(ifd0, (TAG_EXIF_IFD, TAG_GPS_IFD))]
    seen: set[int] = set()
    while todo:
        ifd, follow = todo.pop()
        if ifd in seen:
            continue
        seen.add(ifd)
        if ifd <= 0 or ifd + 2 > n:
            if ifd == ifd0:
                return None
            continue  # a pointer outside the file: that directory cannot be read anyway
        count = struct.unpack_from(e + "H", data, ifd)[0]
        table_end = ifd + 2 + 12 * count + 4
        if table_end > n:
            return None
        end = max(end, table_end)
        for i in range(count):
            off = ifd + 2 + 12 * i
            tag, typ, cnt = struct.unpack_from(e + "HHI", data, off)
            size = _VALUE_SIZES.get(typ, 1) * cnt
            if size > 4:
                start = struct.unpack_from(e + "I", data, off + 8)[0]
                if start + size <= n:  # a value outside the file is skipped by the reader anyway
                    end = max(end, start + size)
            if tag in follow and cnt:
                pointer = struct.unpack_from(e + ("H" if typ == 3 else "I"), data, off + 8)[0]
                todo.append((pointer, _FOLLOWED.get(tag, ())))
        if end > SPAN_LIMIT:
            return None
    return end


def load_exif(raw: bytes) -> Image.Exif | None:
    ex = Image.Exif()
    try:
        ex.load(with_exif_header(raw))
    except Exception:
        return None
    return ex


def parse_exif_datetime(value) -> datetime | None:
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    if not isinstance(value, str):
        return None
    m = _DATE_RE.search(value)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return float("nan")


def gps_coordinates(gps: dict) -> tuple[float, float] | None:
    try:
        lat_v, lon_v = gps.get(2), gps.get(4)
        if not lat_v or not lon_v or len(lat_v) < 3 or len(lon_v) < 3:
            return None
        lat = _to_float(lat_v[0]) + _to_float(lat_v[1]) / 60 + _to_float(lat_v[2]) / 3600
        lon = _to_float(lon_v[0]) + _to_float(lon_v[1]) / 60 + _to_float(lon_v[2]) / 3600
        if str(gps.get(1, "N")).upper().startswith("S"):
            lat = -lat
        if str(gps.get(3, "E")).upper().startswith("W"):
            lon = -lon
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return round(lat, 6), round(lon, 6)
    except (TypeError, ValueError, IndexError):
        return None


def _text(value) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    if not isinstance(value, str):
        return None
    # EXIF text fields should be ASCII, but many programs write UTF-8; Pillow
    # reads them as Latin-1 ("yazÄ±cÄ±"). Fixed up when the bytes are valid UTF-8.
    try:
        value = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    value = _EDGES.sub("", value)
    return value or None


def _int(value) -> int:
    """An EXIF number as an int; 0 for an odd value (a text, a list, a ratio over zero)."""
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def camera_name(make, model) -> str | None:
    make, model = _text(make), _text(model)
    if make and model:
        words = make.split()
        if words and model.lower().startswith(words[0].lower()):
            return model
        return f"{make} {model}"
    return model or make


def report_gps(gps: dict, res: ScanResult) -> None:
    """Adds the location finding of a GPS IFD's fields: its coordinates when they can be read."""
    coords = gps_coordinates(gps)
    detail = f"{coords[0]:.5f}, {coords[1]:.5f}" if coords else (t("exif.fields", n=len(gps)) if gps else "")
    res.add(Kind.GPS, detail)
    if coords and res.summary.gps is None:
        res.summary.gps = coords


def analyze_exif(raw: bytes, res: ScanResult, *, tiff_file: bool = False) -> None:
    """Inspects an EXIF block and writes the findings and the summary into `res`.

    `tiff_file`: if the data is a TIFF file itself, IFD1 is not a thumbnail but
    the second page; the thumbnail check is skipped (and IFD1, which may lie outside the
    `metadata_span` given, is not read).
    """
    ex = load_exif(raw)
    if ex is None:
        res.add(Kind.EXIF, t("exif.unreadable"), len(raw))
        return
    exif_ifd = _safe_ifd(ex, ExifTags.IFD.Exif)
    gps_ifd = _safe_ifd(ex, ExifTags.IFD.GPSInfo)
    ifd1 = {} if tiff_file else _safe_ifd(ex, ExifTags.IFD.IFD1)

    total = len(ex) + len(exif_ifd) + len(gps_ifd)
    notes = [t("exif.fields", n=total)]
    ids = sorted(
        {t(name) for tag, name in IDENTIFYING_TAGS.items() if _text(ex.get(tag)) or _text(exif_ifd.get(tag))}
    )
    if ids:
        notes.append(", ".join(ids))
    res.add(Kind.EXIF, " · ".join(notes), len(raw))

    if gps_ifd:
        report_gps(gps_ifd, res)
    makernote = exif_ifd.get(TAG_MAKERNOTE)
    if makernote:
        size = len(makernote) if isinstance(makernote, (bytes, bytearray)) else 0
        res.add(Kind.MAKERNOTE, _text(ex.get(TAG_MAKE)) or "", size)
    if not tiff_file and (ifd1.get(0x0201) or ifd1.get(0x0111)):
        res.add(Kind.THUMBNAIL, t("exif.inside"), _int(ifd1.get(0x0202, 0)))

    summary = res.summary
    summary.camera = summary.camera or camera_name(ex.get(TAG_MAKE), ex.get(TAG_MODEL))
    summary.software = summary.software or _text(ex.get(TAG_SOFTWARE))
    if summary.orientation is None:
        summary.orientation = read_orientation(raw)
    if summary.taken is None:
        taken, ms = capture_datetime(ex)
        if taken:
            summary.taken = taken.replace(microsecond=ms * 1000)


def capture_datetime(ex: Image.Exif) -> tuple[datetime | None, int]:
    """(date, milliseconds) — for sub-second precision when sorting."""
    exif_ifd = _safe_ifd(ex, ExifTags.IFD.Exif)
    for value, subsec_tag in (
        (exif_ifd.get(TAG_DATETIME_ORIGINAL), TAG_SUBSEC_ORIGINAL),
        (exif_ifd.get(TAG_DATETIME_DIGITIZED), 0x9292),
        (ex.get(TAG_DATETIME), 0x9290),
    ):
        taken = parse_exif_datetime(value)
        if taken:
            sub = _text(exif_ifd.get(subsec_tag)) or ""
            digits = "".join(ch for ch in sub if ch.isdecimal())[:3]  # not isdigit: "²" is a digit int() rejects
            return taken, int(digits.ljust(3, "0")) if digits else 0
    return None, 0


def _safe_ifd(ex: Image.Exif, tag) -> dict:
    try:
        return ex.get_ifd(tag) or {}
    except Exception:
        return {}
