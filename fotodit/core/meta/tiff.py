"""TIFF IFD scanner.

In TIFF, the tags that describe the image structure and the metadata tags live in
the same directories (IFDs). Every tag other than the structural ones counts as
metadata. TIFF is stripped by rewriting it with Pillow; this module is used both
to inspect the input and to verify that the output is clean. When only the capture
date is kept, its Exif IFD is added here at the byte level (`with_exif`): Pillow
cannot write an Exif IFD through libtiff, which every compressed TIFF goes through.
"""

from __future__ import annotations

import struct

from ...i18n import t
from .exif import analyze_exif, metadata_span, report_gps
from .model import FormatError, Kind, ScanResult

# TIFF 6.0 baseline structural tags (needed to decode the image).
STRUCTURAL = {
    254, 255, 256, 257, 258, 259, 262, 263, 266, 273, 274, 277, 278, 279, 280, 281,
    282, 283, 284, 290, 291, 292, 293, 296, 301, 317, 318, 319, 320, 321, 322, 323,
    324, 325, 330, 332, 338, 339, 340, 341, 347, *range(512, 522), 529, 530, 531, 532,
}

_GROUPS = {
    700: Kind.XMP,
    33723: Kind.IPTC,
    34377: Kind.PHOTOSHOP,
    37724: Kind.PHOTOSHOP,
    34675: Kind.ICC,
    34853: Kind.GPS,
    52545: Kind.C2PA,
}
_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
# Size of one number in the multi-byte types (a RATIONAL is two LONGs): swapped when the byte order changes.
_UNIT_SIZES = {3: 2, 4: 4, 5: 4, 8: 2, 9: 4, 10: 4, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
EXIF_IFD = 34665
# Limits of the walk through all directories, so that a damaged or hostile file cannot make it endless.
MAX_SUBIFDS = 64  # SubIFD pointers followed per directory (as `raw.iter_ifds` does)
MAX_IFDS = 4096  # directories walked in all (pages and SubIFDs)
MAX_ENTRIES = 1 << 20  # entries read in all

Entry = tuple[int, int, int, bytes]  # (tag, type, count, value bytes)


class TiffError(FormatError):
    pass


def _byte_order(data: bytes) -> str:
    if data[:4] == b"II*\x00":
        return "<"
    if data[:4] == b"MM\x00*":
        return ">"
    raise TiffError(t("tiff.not_tiff"))


def _entries(data: bytes, e: str, ifd: int) -> tuple[list[Entry], int]:
    """The IFD's entries with their value bytes, and the offset of the next IFD."""
    n = len(data)
    if ifd <= 0 or ifd + 2 > n:
        raise TiffError(t("tiff.damaged"))
    count = struct.unpack_from(e + "H", data, ifd)[0]
    if ifd + 2 + count * 12 + 4 > n:
        raise TiffError(t("tiff.damaged"))
    out = []
    for i in range(count):
        off = ifd + 2 + i * 12
        tag, typ, cnt = struct.unpack_from(e + "HHI", data, off)
        size = _TYPE_SIZES.get(typ, 1) * cnt
        start = off + 8 if size <= 4 else struct.unpack_from(e + "I", data, off + 8)[0]
        if start + size > n:
            raise TiffError(t("tiff.damaged"))
        out.append((tag, typ, cnt, bytes(data[start:start + size])))
    return out, struct.unpack_from(e + "I", data, ifd + 2 + count * 12)[0]


def exif_entries(data: bytes) -> list[Entry] | None:
    """The entries of the Exif IFD the first page points to (tag 34665), values in the file's byte
    order; None if it has none."""
    e = _byte_order(data)
    entries, _ = _entries(data, e, struct.unpack_from(e + "I", data, 4)[0])
    pointer = next((value for tag, _, _, value in entries if tag == EXIF_IFD), None)
    if pointer is None:
        return None
    return _entries(data, e, struct.unpack_from(e + "I", pointer[:4])[0])[0]


def _reordered(typ: int, value: bytes) -> bytes:
    unit = _UNIT_SIZES.get(typ)
    if not unit:
        return value
    return b"".join(value[i:i + unit][::-1] for i in range(0, len(value), unit))


def with_exif(data: bytes, exif: bytes) -> bytes:
    """The TIFF with the Exif IFD of `exif` (a TIFF-structured EXIF block) added to its first page,
    for a TIFF that has none (a stripped one). The Exif IFD and a new copy of the first page's
    directory, with the pointer to it among its sorted entries, are appended; the header points to
    the new directory and the old one is zeroed. The image data and the other pages do not move."""
    e = _byte_order(data)
    source = exif_entries(exif) or []
    if _byte_order(exif) != e:
        source = [(tag, typ, cnt, _reordered(typ, value)) for tag, typ, cnt, value in source]
    first = struct.unpack_from(e + "I", data, 4)[0]
    entries, following = _entries(data, e, first)
    out = bytearray(data) + b"\x00" * (len(data) & 1)  # an IFD starts on a word boundary
    exif_at = len(out)
    head = bytearray(struct.pack(e + "H", len(source)))
    values = bytearray()
    pos = exif_at + 2 + 12 * len(source) + 4
    for tag, typ, cnt, value in sorted(source):
        if len(value) <= 4:
            head += struct.pack(e + "HHI", tag, typ, cnt) + value.ljust(4, b"\x00")
        else:
            head += struct.pack(e + "HHII", tag, typ, cnt, pos + len(values))
            values += value + b"\x00" * (len(value) & 1)
    out += head + struct.pack(e + "I", 0) + values
    # The first page's directory again, with the pointer among its entries (sorted by tag). Values that
    # do not fit in an entry stay where they are: only the entries' 12 bytes are copied.
    kept = [(tag, data[first + 2 + 12 * i:first + 14 + 12 * i]) for i, (tag, *_) in enumerate(entries)
            if tag != EXIF_IFD]
    kept.append((EXIF_IFD, struct.pack(e + "HHII", EXIF_IFD, 4, 1, exif_at)))
    page_at = len(out)
    out += struct.pack(e + "H", len(kept)) + b"".join(raw for _, raw in sorted(kept)) + struct.pack(e + "I", following)
    old = 2 + 12 * len(entries) + 4
    out[first:first + old] = bytes(old)
    struct.pack_into(e + "I", out, 4, page_at)
    return bytes(out)


def _walk(data: bytes):
    """Yields (directory offset, tag, entry offset) for the entries of all IFDs (pages and SubIFDs),
    within MAX_SUBIFDS, MAX_IFDS and MAX_ENTRIES."""
    e = _byte_order(data)
    n = len(data)
    todo = [struct.unpack_from(e + "I", data, 4)[0]]
    seen: set[int] = set()
    entries = 0
    while todo and len(seen) < MAX_IFDS and entries < MAX_ENTRIES:
        ifd = todo.pop()
        if ifd in seen or ifd <= 0 or ifd + 2 > n:
            continue
        seen.add(ifd)
        count = struct.unpack_from(e + "H", data, ifd)[0]
        if ifd + 2 + count * 12 + 4 > n:
            raise TiffError(t("tiff.damaged"))
        entries += count
        for i in range(count):
            off = ifd + 2 + i * 12
            tag, typ, cnt = struct.unpack_from(e + "HHI", data, off)
            yield ifd, tag, off
            if tag == 330:  # SubIFDs
                size = _TYPE_SIZES.get(typ, 4) * cnt
                ptr = off + 8 if size <= 4 else struct.unpack_from(e + "I", data, off + 8)[0]
                for j in range(min(cnt, MAX_SUBIFDS)):
                    if ptr + 4 * (j + 1) > n:
                        break
                    todo.append(struct.unpack_from(e + "I", data, ptr + 4 * j)[0])
        todo.append(struct.unpack_from(e + "I", data, ifd + 2 + count * 12)[0])


def count_tag(data: bytes, tag: int) -> int:
    """In how many IFDs (pages and SubIFDs) the tag appears."""
    return sum(1 for _, found, _ in _walk(data) if found == tag)


def _gps_fields(data: bytes, e: str, offset: int) -> dict:
    """The fields of the GPS IFD at `offset` as the EXIF analysis reads them (texts, ratios as numbers);
    {} if it cannot be read."""
    try:
        entries, _ = _entries(data, e, offset)
    except TiffError:
        return {}
    fields: dict = {}
    for tag, typ, cnt, value in entries:
        if typ == 2:  # ASCII
            fields[tag] = value.split(b"\x00", 1)[0].decode("latin-1")
        elif typ == 5:  # RATIONAL: two LONGs each
            numbers = struct.unpack(e + f"{2 * cnt}I", value)
            fields[tag] = tuple(a / b if b else float("nan") for a, b in zip(numbers[::2], numbers[1::2]))
        else:
            fields[tag] = value
    return fields


def scan(data: bytes, res: ScanResult) -> None:
    e = _byte_order(data)
    first = struct.unpack_from(e + "I", data, 4)[0]
    other: set[int] = set()
    exif_like = False
    gps_elsewhere: list[int] = []  # GPS IFDs of the other pages and SubIFDs
    for ifd, tag, entry in _walk(data):
        if tag in STRUCTURAL:
            continue
        kind = _GROUPS.get(tag)
        if kind == Kind.GPS:
            exif_like = True  # the first page's is reported by the EXIF analysis
            if ifd != first:
                gps_elsewhere.append(struct.unpack_from(e + "I", data, entry + 8)[0])
            continue
        if kind:
            if kind not in res.kinds():
                res.add(kind)
        elif tag == 34665 or tag in (269, 270, 271, 272, 285, 305, 306, 315, 316, 33432):
            exif_like = True
        else:
            other.add(tag)
    if exif_like:
        before = len(res.findings)
        # Only the leading bytes the first page's directories take up, not the image data of a large file.
        end = metadata_span(data, e, first)
        analyze_exif(data[:end] if end is not None else data, res, tiff_file=True)
        # In a TIFF the "field count" also covers structural tags; drop it so it does not mislead.
        for f in res.findings[before:]:
            if f.kind == Kind.EXIF:
                f.detail = f.detail.split(" · ", 1)[1] if " · " in f.detail else ""
                f.size = 0
    for offset in gps_elsewhere:  # the EXIF analysis reads the first page's GPS IFD only
        if res.has_gps():
            break
        report_gps(_gps_fields(data, e, offset), res)
    if other:
        res.add(Kind.OTHER, t("tiff.extra_tags", n=len(other)))
