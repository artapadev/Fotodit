"""PNG chunk parser, lossless metadata stripper and scanner.

A PNG file is a signature plus [length, type, data, CRC] chunks. Stripping
uses an allowlist: the chunks needed to decode and display the image correctly
are copied as is (their CRCs do not change), the rest are dropped.

Kept: IHDR, PLTE, tRNS, IDAT, IEND; APNG animation chunks
(acTL, fcTL, fdAT); color interpretation chunks (gAMA, cHRM, sRGB, sBIT, cICP,
mDCV, cLLI). These carry no personal data and determine how the image looks.

Dropped: tEXt/zTXt/iTXt (author, description, XMP, AI prompts…),
eXIf, iCCP, tIME, pHYs, bKGD, sPLT, hIST, caBX (C2PA), iDOT and all unrecognized
ancillary chunks. An unrecognized *critical* chunk (e.g. Apple CgBI) raises an
error, because the file cannot be stripped safely.
"""

from __future__ import annotations

import struct
import zlib
from datetime import datetime
from email.utils import parsedate_to_datetime

from ...i18n import date_time, t
from .exif import analyze_exif, parse_exif_datetime, read_orientation
from .model import FormatError, Kind, ScanResult
from .xmp import analyze_xmp

SIGNATURE = b"\x89PNG\r\n\x1a\n"

KEEP = {
    b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND",
    b"acTL", b"fcTL", b"fdAT",
    b"gAMA", b"cHRM", b"sRGB", b"sBIT", b"cICP", b"mDCV", b"cLLI", b"mDCv", b"cLLi",
}
# The end chunk as `strip` writes it: empty (bytes inside an IEND would survive stripping otherwise).
IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"

# Caps on what a compressed chunk is inflated to: a chunk of a few KB can inflate to gigabytes.
ICC_LIMIT = 16 * 1024 * 1024  # a color profile (iCCP)
TEXT_LIMIT = 8 * 1024 * 1024  # a text (zTXt, iTXt): an XMP packet can take several MB
HIDDEN_MARGIN = 1024 * 1024  # image data inflated beyond what IHDR needs before `hidden_idat_data` stops


class PngError(FormatError):
    pass


def iter_chunks(data: bytes):
    """Yields (type, data_start, data_end, chunk_start, chunk_end); stops at IEND."""
    if not data.startswith(SIGNATURE):
        raise PngError(t("png.not_png"))
    pos = len(SIGNATURE)
    n = len(data)
    while pos + 12 <= n:
        length = struct.unpack_from(">I", data, pos)[0]
        ctype = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > n:
            raise PngError(t("png.damaged"))
        yield ctype, pos + 8, pos + 8 + length, pos, end
        pos = end
        if ctype == b"IEND":
            return
    raise PngError(t("png.no_iend"))


def _end_offset(data: bytes) -> int:
    end = len(SIGNATURE)
    for *_, chunk_end in iter_chunks(data):
        end = chunk_end
    return end


def strip(data: bytes) -> bytes:
    out = bytearray(SIGNATURE)
    for ctype, _, _, start, end in iter_chunks(data):
        if ctype == b"IEND":
            out += IEND
        elif ctype in KEEP:
            out += data[start:end]
        elif ctype[0] & 0x20 == 0:  # uppercase = critical chunk
            raise PngError(
                t("png.critical_chunk", chunk=ctype.decode("latin1"))
            )
    return bytes(out)


def image_prefix(data: bytes, limit: int, complete: bool = True) -> bytes | None:
    """The first `limit` bytes of the chunks `strip` keeps (the image itself), read from the start of
    a PNG (`data`; `complete`: the whole file). PNGs whose stripped bytes are the same have the same
    prefix, so it tells different pictures apart without reading whole files. None if `data` ends too
    soon."""
    if not data.startswith(SIGNATURE):
        return None
    out = bytearray()
    n = len(data)
    pos = len(SIGNATURE)
    while len(out) < limit:
        if pos + 12 > n:
            return bytes(out) if complete else None
        ctype = data[pos + 4:pos + 8]
        end = pos + 12 + struct.unpack_from(">I", data, pos)[0]
        if ctype == b"IEND":
            return bytes(out + IEND)
        if ctype in KEEP:
            out += data[pos:min(end, n)]
        pos = end
    return bytes(out[:limit])


def _chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)


def _inflate(stream: bytes, limit: int) -> bytes | None:
    """The zlib stream inflated; None if it is broken or incomplete, or inflates to more than `limit` bytes."""
    dec = zlib.decompressobj()
    try:
        out = dec.decompress(stream, limit + 1)
    except zlib.error:
        return None
    return out if dec.eof and len(out) <= limit else None


def with_exif(data: bytes, exif: bytes) -> bytes:
    """The PNG with an eXIf chunk inserted before the image data: before the first IDAT, or before the
    animation chunks of an APNG. `exif`: the TIFF-structured EXIF, without the "Exif\\0\\0" header
    (PNG does not use it)."""
    for ctype, _, _, start, _ in iter_chunks(data):
        if ctype in (b"IDAT", b"acTL", b"fcTL"):
            return data[:start] + _chunk(b"eXIf", exif) + data[start:]
    raise PngError(t("png.no_image"))


_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def _expected_raw_size(data: bytes) -> int | None:
    """Number of bytes the decompressed IDAT stream should have according to IHDR."""
    for ctype, ds, de, _, _ in iter_chunks(data):
        if ctype != b"IHDR" or de - ds < 13:
            return None
        w, h, depth, color, _, _, interlace = struct.unpack_from(">IIBBBBB", data, ds)
        bpp = _CHANNELS.get(color, 0) * depth
        if not bpp:
            return None
        if not interlace:
            return h * (1 + (w * bpp + 7) // 8)
        total = 0
        for xs, ys, xstep, ystep in _ADAM7:
            pw = (w - xs + xstep - 1) // xstep if w > xs else 0
            ph = (h - ys + ystep - 1) // ystep if h > ys else 0
            if pw and ph:
                total += ph * (1 + (pw * bpp + 7) // 8)
        return total
    return None


def hidden_idat_data(data: bytes) -> int:
    """Does the IDAT stream hold more (hidden) data than the image needs?

    With some buggy editors, the original version of a cropped screenshot can
    remain inside or after the compressed data ("aCropalypse").
    Returns the number of excess bytes (0 = clean); once the excess passes HIDDEN_MARGIN the rest of
    the stream is not inflated (it could inflate to gigabytes).
    """
    expected = _expected_raw_size(data)
    if expected is None:
        return 0
    dec = zlib.decompressobj()
    total = 0
    extra = 0
    try:
        for ctype, ds, de, _, _ in iter_chunks(data):
            if ctype != b"IDAT":
                continue
            chunk = data[ds:de]
            if dec.eof:
                extra += len(chunk)
                continue
            while chunk:
                out = dec.decompress(chunk, 1 << 20)
                total += len(out)
                if total > expected + HIDDEN_MARGIN:
                    return total - expected + extra
                chunk = dec.unconsumed_tail
            if dec.eof:
                extra += len(dec.unused_data)
    except zlib.error:
        return 0  # broken stream: the decode verification reports it separately
    return max(0, total - expected) + extra


def icc_profile(data: bytes) -> bytes | None:
    for ctype, ds, de, _, _ in iter_chunks(data):
        if ctype == b"iCCP":
            body = data[ds:de]
            sep = body.find(b"\x00")
            if sep < 0 or sep + 2 > len(body):
                return None
            return _inflate(body[sep + 2:], ICC_LIMIT)  # too large: treated as an unreadable profile
    return None


def exif_block(data: bytes) -> bytes | None:
    for ctype, ds, de, _, _ in iter_chunks(data):
        if ctype in (b"eXIf", b"exIf", b"zxIf"):
            return data[ds:de]
    return None


def orientation(data: bytes) -> int | None:
    raw = exif_block(data)
    return read_orientation(raw) if raw else None


def _text_chunk(ctype: bytes, body: bytes) -> tuple[str, bytes]:
    """(key, value) — the value decompressed when possible (empty if it is broken or larger than
    TEXT_LIMIT; the chunk still counts as found)."""
    sep = body.find(b"\x00")
    key = body[:max(sep, 0)].decode("latin1", "replace")
    rest = body[sep + 1:] if sep >= 0 else b""
    if ctype == b"zTXt" and rest:
        return key, _inflate(rest[1:], TEXT_LIMIT) or b""
    if ctype == b"iTXt" and len(rest) >= 2:
        compressed = rest[0] == 1
        rest = rest[2:]
        # language tag and translated keyword
        for _ in range(2):
            z = rest.find(b"\x00")
            rest = rest[z + 1:] if z >= 0 else b""
        return key, (_inflate(rest, TEXT_LIMIT) or b"") if compressed else rest
    return key, rest


def scan(data: bytes, res: ScanResult) -> None:
    text_keys: list[str] = []
    text_size = 0
    for ctype, ds, de, start, end in iter_chunks(data):
        body = data[ds:de]
        size = de - ds
        if ctype in KEEP:
            continue
        if ctype in (b"tEXt", b"zTXt", b"iTXt"):
            key, value = _text_chunk(ctype, body)
            if key == "XML:com.adobe.xmp":
                analyze_xmp(value, res)
            elif key.lower() == "raw profile type exif":
                res.add(Kind.EXIF, t("png.exif_text"), size)
            else:
                text_keys.append(_describe_text_key(key))
                text_size += size
                if res.summary.taken is None and key.lower() in ("creation time", "date:create"):
                    res.summary.taken = _text_datetime(value[:64])
        elif ctype in (b"eXIf", b"exIf", b"zxIf"):
            analyze_exif(body, res)
        elif ctype == b"iCCP":
            name = body.split(b"\x00", 1)[0].decode("latin1", "replace")
            res.add(Kind.ICC, name, size)
        elif ctype == b"tIME":
            if len(body) == 7:
                y, mo, d, h, mi, s = struct.unpack(">HBBBBB", body)
                try:
                    stamp = date_time(datetime(y, mo, d, h, mi))
                except ValueError:  # an impossible date: shown as it is
                    stamp = f"{y}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}"
                res.add(Kind.TIMESTAMP, stamp, size)
            else:
                res.add(Kind.TIMESTAMP, "", size)
        elif ctype == b"caBX":
            res.add(Kind.C2PA, "", size)
        elif ctype == b"pHYs":
            res.add(Kind.OTHER, t("png.dpi"), size)
        elif ctype == b"iDOT":
            res.add(Kind.OTHER, t("png.screenshot"), size)
        else:
            res.add(Kind.OTHER, ctype.decode("latin1", "replace"), size)
    if text_keys:
        unique = list(dict.fromkeys(text_keys))
        res.add(Kind.TEXT, ", ".join(unique[:6]), text_size)
    trailer_start = _end_offset(data)
    if trailer_start < len(data) and data[trailer_start:].strip(b"\x00"):
        res.add(Kind.TRAILER, "", len(data) - trailer_start)
    hidden = hidden_idat_data(data)
    if hidden:
        res.add(Kind.TRAILER, t("png.hidden"), hidden)
    icc = icc_profile(data)
    if icc:
        from ..colors import icc_description

        res.summary.icc_description = icc_description(icc)


def _text_datetime(value: bytes):
    """PNG 'Creation Time' text: ISO format or RFC 1123 ('Sat, 12 May 2024 …')."""
    taken = parse_exif_datetime(value)
    if taken:
        return taken
    try:
        return parsedate_to_datetime(value.decode("latin1").strip()).replace(tzinfo=None)
    except (TypeError, ValueError, IndexError):
        return None


_AI_KEYS = {"parameters", "prompt", "workflow", "sd-metadata", "dream", "invokeai_metadata", "generation_data"}


def _describe_text_key(key: str) -> str:
    if key.lower() in _AI_KEYS:
        return t("png.ai", key=key)
    return key
