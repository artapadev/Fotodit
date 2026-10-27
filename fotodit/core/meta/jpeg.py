"""JPEG segment parser, lossless metadata stripper and scanner.

A JPEG file consists of marker segments. The image itself is the
SOF/DHT/DQT/DRI/SOS segments plus the compressed scan data after SOS.
Metadata lives in the APP0–APP15 and COM segments and in data appended
to the file after the EOI marker (trailer).

Stripping uses an allowlist: the segments needed to decode the image are
copied byte for byte and everything else is dropped. The scan data is not
recompressed, so there is zero quality loss. Two exceptions are kept:

* APP0 "JFIF": the header of the JFIF standard; it holds no personal data. Its
  optional thumbnail is dropped, leaving the canonical 16-byte form.
* APP14 "Adobe": carries the transform flag CMYK/YCCK JPEGs need for correct
  color decoding; dropping it breaks the colors. It holds no personal data.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ...i18n import t
from .exif import analyze_exif, read_orientation
from .model import FormatError, Kind, ScanResult
from .xmp import analyze_xmp

SOI, EOI, SOS, COM = 0xD8, 0xD9, 0xDA, 0xFE
APP0, APP1, APP2, APP11, APP13, APP14 = 0xE0, 0xE1, 0xE2, 0xEB, 0xED, 0xEE
_STANDALONE = {0x01, *range(0xD0, 0xD8)}
_SOF = {m for m in range(0xC0, 0xD0) if m not in (0xC4, 0xC8, 0xCC)}
_PROGRESSIVE_SOF = {0xC2, 0xC6, 0xCA, 0xCE}

XMP_ID = b"http://ns.adobe.com/xap/1.0/\x00"
XMP_EXT_ID = b"http://ns.adobe.com/xmp/extension/\x00"
ICC_ID = b"ICC_PROFILE\x00"
ADOBE_SIZE = 12  # APP14 "Adobe": the identifier, version, two flag words and the color transform


class JpegError(FormatError):
    pass


@dataclass(frozen=True)
class Segment:
    marker: int
    start: int  # the marker's 0xFF byte
    end: int  # where the segment (for SOS, its scan data) ends
    payload_start: int
    payload_end: int


@dataclass(frozen=True)
class FrameInfo:
    marker: int
    precision: int
    width: int
    height: int
    components: int

    @property
    def progressive(self) -> bool:
        return self.marker in _PROGRESSIVE_SOF


class JpegLayout:
    def __init__(self, data: bytes, segments: list[Segment], eoi_end: int | None):
        self.data = data
        self.segments = segments
        self.eoi_end = eoi_end

    def payload(self, seg: Segment) -> bytes:
        return self.data[seg.payload_start:seg.payload_end]

    @property
    def trailer(self) -> bytes:
        return self.data[self.eoi_end:] if self.eoi_end is not None else b""

    def app(self, marker: int, prefix: bytes) -> list[bytes]:
        out = []
        for seg in self.segments:
            if seg.marker == marker:
                p = self.payload(seg)
                if p.startswith(prefix):
                    out.append(p)
        return out

    def frame(self) -> FrameInfo | None:
        for seg in self.segments:
            if seg.marker in _SOF:
                p = self.payload(seg)
                if len(p) >= 6:
                    precision, height, width, comps = struct.unpack_from(">BHHB", p, 0)
                    return FrameInfo(seg.marker, precision, width, height, comps)
        return None

    def exif(self) -> bytes | None:
        for p in self.app(APP1, b"Exif\x00"):
            return p
        return None

    def orientation(self) -> int | None:
        raw = self.exif()
        return read_orientation(raw) if raw else None

    def icc_profile(self) -> bytes | None:
        """Joins the APP2 ICC_PROFILE chunks in sequence-number order."""
        chunks: dict[int, bytes] = {}
        total = 0
        for p in self.app(APP2, ICC_ID):
            if len(p) < 14:
                continue
            seq, count = p[12], p[13]
            chunks.setdefault(seq, p[14:])
            total = max(total, count)
        if not chunks:
            return None
        if total and sorted(chunks) == list(range(1, total + 1)):
            return b"".join(chunks[i] for i in range(1, total + 1))
        return b"".join(chunks[k] for k in sorted(chunks))

    def has_adobe_marker(self) -> bool:
        return bool(self.app(APP14, b"Adobe"))


def _scan_end(data: bytes, pos: int) -> int:
    """Finds the position of the marker that ends the compressed data after SOS."""
    n = len(data)
    find = data.find
    while True:
        i = find(b"\xff", pos)
        if i < 0 or i + 1 >= n:
            return n
        b = data[i + 1]
        if b == 0x00 or 0xD0 <= b <= 0xD7:
            pos = i + 2
        elif b == 0xFF:
            pos = i + 1
        else:
            return i


def parse(data: bytes) -> JpegLayout:
    if data[:2] != b"\xff\xd8":
        raise JpegError(t("jpeg.not_jpeg"))
    n = len(data)
    segments = [Segment(SOI, 0, 2, 2, 2)]
    pos = 2
    while pos < n:
        if data[pos] != 0xFF:
            # Some programs leave padding/garbage between segments; skip to the
            # next marker. Safe because we are outside the scan data.
            nxt = data.find(b"\xff", pos)
            if nxt < 0:
                break
            pos = nxt
            continue
        while pos + 1 < n and data[pos + 1] == 0xFF:  # fill bytes
            pos += 1
        if pos + 1 >= n:
            break
        marker = data[pos + 1]
        start = pos
        if marker == 0x00 or marker == SOI:
            pos += 2
            continue
        if marker in _STANDALONE:
            segments.append(Segment(marker, start, pos + 2, pos + 2, pos + 2))
            pos += 2
            continue
        if marker == EOI:
            segments.append(Segment(EOI, start, pos + 2, pos + 2, pos + 2))
            return JpegLayout(data, segments, pos + 2)
        if pos + 4 > n:
            raise JpegError(t("jpeg.truncated_header"))
        length = struct.unpack_from(">H", data, pos + 2)[0]
        seg_end = pos + 2 + length
        if length < 2 or seg_end > n:
            raise JpegError(t("jpeg.bad_length"))
        if marker == SOS:
            scan_end = _scan_end(data, seg_end)
            segments.append(Segment(SOS, start, scan_end, pos + 4, seg_end))
            pos = scan_end
            continue
        segments.append(Segment(marker, start, seg_end, pos + 4, seg_end))
        pos = seg_end
    return JpegLayout(data, segments, None)


def _canonical_jfif(payload: bytes) -> bytes | None:
    """Writes the JFIF header as a canonical 16-byte segment without a thumbnail."""
    if len(payload) < 12:
        return None
    version = payload[5:7]
    units = payload[7]
    xd, yd = struct.unpack_from(">HH", payload, 8)
    if xd == 0 or yd == 0 or units > 2:
        units, xd, yd = 0, 1, 1
    body = b"JFIF\x00" + version + bytes([units]) + struct.pack(">HH", xd, yd) + b"\x00\x00"
    return b"\xff\xe0" + struct.pack(">H", len(body) + 2) + body


def insert_segments(data: bytes, segments: list[tuple[int, bytes]]) -> bytes:
    """Inserts APPn segments right after SOI (and after the JFIF header, if present)."""
    pos = 2
    if data[2:4] == b"\xff\xe0":
        pos = 4 + struct.unpack(">H", data[4:6])[0]
    blob = b"".join(b"\xff" + bytes([m]) + struct.pack(">H", len(p) + 2) + p for m, p in segments)
    return data[:pos] + blob + data[pos:]


def image_prefix(data: bytes, limit: int, complete: bool = True) -> bytes | None:
    """The first `limit` bytes of the image itself, read from the start of a JPEG (`data`; `complete`:
    the whole file): its segments without APPn and comments, the compressed data included, in the
    order `strip` keeps them. JPEGs whose stripped bytes are the same have the same prefix, so it tells
    different pictures apart without reading whole files. None if `data` ends too soon."""
    if data[:2] != b"\xff\xd8":
        return None
    out = bytearray()
    n = len(data)
    pos = 2
    while len(out) < limit:
        if pos + 1 >= n:
            return bytes(out) if complete else None
        if data[pos] != 0xFF:  # padding between segments, skipped as `parse` does
            nxt = data.find(b"\xff", pos)
            if nxt < 0:
                return bytes(out) if complete else None
            pos = nxt
            continue
        while pos + 1 < n and data[pos + 1] == 0xFF:  # fill bytes
            pos += 1
        if pos + 1 >= n:
            continue
        marker = data[pos + 1]
        if marker == 0x00 or marker == SOI:
            pos += 2
            continue
        if marker in _STANDALONE or marker == EOI:
            out += data[pos:pos + 2]
            if marker == EOI:
                return bytes(out)
            pos += 2
            continue
        if pos + 4 > n:
            return bytes(out) if complete else None
        end = pos + 2 + struct.unpack_from(">H", data, pos + 2)[0]
        if marker == SOS:
            end = _scan_end(data, min(end, n))
            out += data[pos:end]
        elif end > n:
            return bytes(out) if complete else None
        elif not (0xE0 <= marker <= 0xEF or marker == COM):
            out += data[pos:end]
        pos = end
    return bytes(out[:limit])


def strip(data: bytes) -> bytes:
    """Removes all metadata losslessly (the scan data is kept as is)."""
    layout = parse(data)
    out = bytearray(b"\xff\xd8")
    for seg in layout.segments[1:]:
        m = seg.marker
        if m == EOI:
            break
        if m == APP0:
            p = layout.payload(seg)
            if p.startswith(b"JFIF\x00"):
                jfif = _canonical_jfif(p)
                if jfif:
                    out += jfif
            continue
        if m == APP14:
            p = layout.payload(seg)
            if p.startswith(b"Adobe"):
                p = p[:ADOBE_SIZE]  # its fields only: bytes after them can only hide data
                out += b"\xff\xee" + struct.pack(">H", len(p) + 2) + p
            continue
        if 0xE0 <= m <= 0xEF or m == COM:
            continue
        out += data[seg.start:seg.end]
    out += b"\xff\xd9"
    return bytes(out)


# --------------------------------------------------------------------------- scanning


def _photoshop_resources(payload: bytes) -> list[tuple[int, bytes]]:
    """Lists the 8BIM resource blocks inside APP13 'Photoshop 3.0'."""
    out = []
    p = payload[len(b"Photoshop 3.0\x00"):]
    i = 0
    while i + 12 <= len(p) and p[i:i + 4] == b"8BIM":
        rid = struct.unpack_from(">H", p, i + 4)[0]
        name_len = p[i + 6]
        j = i + 7 + name_len
        if (name_len + 1) % 2:
            j += 1
        if j + 4 > len(p):
            break
        size = struct.unpack_from(">I", p, j)[0]
        data = p[j + 4:j + 4 + size]
        out.append((rid, data))
        i = j + 4 + size + (size % 2)
    return out


PHOTOSHOP_THUMBNAILS = {0x0409, 0x040C}


def irb_without_thumbnails(payload: bytes) -> bytes:
    """The APP13 'Photoshop 3.0' block with its thumbnail resources removed (IPTC etc.
    are kept). In an edited photo the old thumbnail no longer matches the image."""
    out = bytearray(b"Photoshop 3.0\x00")
    for rid, data in _photoshop_resources(payload):
        if rid in PHOTOSHOP_THUMBNAILS:
            continue
        out += b"8BIM" + struct.pack(">H", rid) + b"\x00\x00" + struct.pack(">I", len(data)) + data
        if len(data) & 1:
            out += b"\x00"
    return bytes(out)


def _describe_trailer(trailer: bytes) -> tuple[Kind, str]:
    if trailer.endswith(b"SEFT") or b"SEFH" in trailer[:64]:
        return Kind.TRAILER, t("jpeg.samsung")
    if b"ftyp" in trailer[:64]:
        return Kind.TRAILER, t("jpeg.motion")
    if b"\xff\xd8\xff" in trailer[:4096]:
        return Kind.EXTRA_IMAGE, t("jpeg.extra_jpeg")
    return Kind.TRAILER, ""


def scan(data: bytes, res: ScanResult) -> None:
    layout = parse(data)
    for seg in layout.segments:
        m = seg.marker
        if not (0xE0 <= m <= 0xEF or m == COM):
            continue
        p = layout.payload(seg)
        size = len(p)
        if m == COM:
            text = p[:60].decode("utf-8", "replace").strip("\x00 ")
            res.add(Kind.COMMENT, text, size)
        elif m == APP0:
            if p.startswith(b"JFIF\x00"):
                if len(p) >= 14 and p[12] * p[13] > 0:
                    res.add(Kind.THUMBNAIL, t("jpeg.in_jfif"), size)
            elif p.startswith(b"JFXX\x00"):
                res.add(Kind.THUMBNAIL, "JFXX", size)
            else:
                res.add(Kind.OTHER, _ident(p), size)
        elif m == APP1:
            if p.startswith(b"Exif\x00"):
                analyze_exif(p, res)
            elif p.startswith(XMP_ID):
                analyze_xmp(p[len(XMP_ID):], res)
            elif p.startswith(XMP_EXT_ID):
                analyze_xmp(p[len(XMP_EXT_ID) + 40:], res, extended=True)
            else:
                res.add(Kind.OTHER, _ident(p), size)
        elif m == APP2:
            if p.startswith(ICC_ID):
                if p[12:13] in (b"\x01", b""):
                    res.add(Kind.ICC, "", size)
            elif p.startswith(b"MPF\x00"):
                res.add(Kind.EXTRA_IMAGE, t("jpeg.mpf"), size)
            elif p.startswith(b"urn:iso:std:iso:ts:21496"):
                res.add(Kind.EXTRA_IMAGE, t("jpeg.gain_map"), size)
            elif p.startswith(b"FPXR"):
                res.add(Kind.OTHER, "FlashPix", size)
            else:
                res.add(Kind.OTHER, _ident(p), size)
        elif m == APP11 and (p.startswith(b"JP") or b"jumb" in p[:32] or b"c2pa" in p[:64]):
            res.add(Kind.C2PA, "", size)
        elif m == APP13 and p.startswith(b"Photoshop 3.0\x00"):
            resources = _photoshop_resources(p)
            ids = {rid for rid, _ in resources}
            if 0x0404 in ids:
                res.add(Kind.IPTC, "", len(dict(resources)[0x0404]))
            if ids & {0x0409, 0x040C}:
                res.add(Kind.THUMBNAIL, "Photoshop", 0)
            if ids - {0x0404, 0x0409, 0x040C}:
                res.add(Kind.PHOTOSHOP, t("jpeg.resources", n=len(ids)), size)
        elif m == APP14 and p.startswith(b"Adobe"):
            continue  # color transform flag: not metadata
        else:
            res.add(Kind.OTHER, f"APP{m - 0xE0} {_ident(p)}".strip(), size)
    if layout.eoi_end is not None:
        trailer = layout.trailer
        if trailer.strip(b"\x00\xff \r\n"):
            kind, detail = _describe_trailer(trailer)
            res.add(kind, detail, len(trailer))
    icc = layout.icc_profile()
    if icc:
        from ..colors import icc_description

        res.summary.icc_description = icc_description(icc)
        for f in res.findings:
            if f.kind == Kind.ICC and not f.detail:
                f.detail = res.summary.icc_description or ""
                f.size = len(icc)


def _ident(p: bytes) -> str:
    head = p[:24].split(b"\x00", 1)[0]
    text = "".join(chr(c) for c in head if 32 <= c < 127)
    return text[:20]
