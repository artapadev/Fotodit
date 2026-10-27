"""WebP (RIFF) parser, lossless metadata stripper and scanner.

Kept: VP8/VP8L (image), ALPH (transparency), ANIM/ANMF (animation) and the
VP8X header (with its ICC/EXIF/XMP flags cleared). Dropped: ICCP, EXIF,
"XMP ", all unrecognized chunks and any data appended after the RIFF. An
animation frame (ANMF) keeps only its image sub-chunks (a frame may carry
unknown chunks too), and the VP8X and ANIM headers are cut to their defined size.
"""

from __future__ import annotations

import struct

from ...i18n import t
from .exif import analyze_exif, read_orientation
from .model import FormatError, Kind, ScanResult
from .xmp import analyze_xmp

KEEP = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF"}
FRAME_KEEP = {b"ALPH", b"VP8 ", b"VP8L"}  # the sub-chunks of an animation frame (ANMF) that hold its image
FRAME_HEADER = 16  # an ANMF frame's position, size, duration and flags, before its sub-chunks
HEADER_SIZES = {b"VP8X": 10, b"ANIM": 6}  # payload sizes of the header chunks: bytes beyond can only hide data
FLAG_ICC, FLAG_ALPHA, FLAG_EXIF, FLAG_XMP = 0x20, 0x10, 0x08, 0x04


class WebpError(FormatError):
    pass


def _riff_end(data: bytes) -> int:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise WebpError(t("webp.not_webp"))
    size = struct.unpack_from("<I", data, 4)[0]
    end = 8 + size
    if end > len(data):
        raise WebpError(t("webp.truncated"))
    return end


def iter_chunks(data: bytes):
    """Yields (fourcc, data_start, data_end, chunk_start, chunk_end)."""
    end = _riff_end(data)
    pos = 12
    while pos + 8 <= end:
        fourcc = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        data_end = pos + 8 + size
        chunk_end = data_end + (size & 1)
        if data_end > end:
            raise WebpError(t("webp.bad_length"))
        yield fourcc, pos + 8, data_end, pos, min(chunk_end, end)
        pos = chunk_end


def _frame_chunks(data: bytes, start: int, end: int):
    """Yields (fourcc, data_start, data_end, chunk_start, chunk_end) for the sub-chunks of the ANMF frame
    whose payload is data[start:end] (after its 16-byte header)."""
    pos = start + FRAME_HEADER
    while pos + 8 <= end:
        fourcc = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        data_end = pos + 8 + size
        if data_end > end:
            raise WebpError(t("webp.bad_length"))
        chunk_end = data_end + (size & 1)
        yield fourcc, pos + 8, data_end, pos, min(chunk_end, end)
        pos = chunk_end


def _kept(data: bytes, fourcc: bytes, ds: int, de: int, start: int, end: int) -> bytearray:
    """A chunk as `strip` keeps it: an ANMF frame with only its image sub-chunks, a VP8X or ANIM header cut
    to its defined size; anything shorter, and the image chunks, as they are."""
    size = HEADER_SIZES.get(fourcc)
    if size is not None and de - ds > size:
        return bytearray(fourcc + struct.pack("<I", size) + data[ds:ds + size])
    if fourcc != b"ANMF" or de - ds < FRAME_HEADER:
        return bytearray(data[start:end])
    frame = bytearray(data[ds:ds + FRAME_HEADER])
    for sub, _, _, sub_start, sub_end in _frame_chunks(data, ds, de):
        if sub in FRAME_KEEP:
            frame += data[sub_start:sub_end]
            frame += b"\x00" * (len(frame) & 1)  # a pad byte the frame's end cut off
    return bytearray(b"ANMF" + struct.pack("<I", len(frame))) + frame


def strip(data: bytes) -> bytes:
    body = bytearray()
    for fourcc, ds, de, start, end in iter_chunks(data):
        if fourcc not in KEEP:
            continue
        chunk = _kept(data, fourcc, ds, de, start, end)
        if fourcc == b"VP8X" and de - ds >= 1:
            chunk[8] &= ~(FLAG_ICC | FLAG_EXIF | FLAG_XMP) & 0xFF
        body += chunk
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + bytes(body)


def image_prefix(data: bytes, limit: int, complete: bool = True) -> bytes | None:
    """The first `limit` bytes of the image chunks `strip` keeps (VP8X and ANIM left out: `strip` may change
    them; of an animation frame its header and image sub-chunks), read from the start of a WebP (`data`;
    `complete`: the whole file). WebPs whose stripped bytes are the same have the same prefix, so it tells
    different pictures apart without reading whole files. None if `data` ends too soon."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    out = bytearray()
    n = len(data)
    riff_end = 8 + struct.unpack_from("<I", data, 4)[0]
    pos = 12
    while len(out) < limit:
        if pos + 8 > riff_end:
            return bytes(out)
        if pos + 8 > n:
            return bytes(out) if complete else None
        fourcc = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        end = min(pos + 8 + size + (size & 1), riff_end)
        if fourcc == b"ANMF":
            frame_end = min(pos + 8 + size, riff_end)
            out += data[pos + 8:min(pos + 8 + FRAME_HEADER, frame_end, n)]
            sub = pos + 8 + FRAME_HEADER
            while sub + 8 <= frame_end and len(out) < limit:
                if sub + 8 > n:
                    return bytes(out) if complete else None
                sub_size = struct.unpack_from("<I", data, sub + 4)[0]
                sub_end = min(sub + 8 + sub_size + (sub_size & 1), frame_end)
                if data[sub:sub + 4] in FRAME_KEEP:
                    out += data[sub:min(sub_end, n)]
                sub = sub_end
        elif fourcc in KEEP and fourcc not in HEADER_SIZES:
            out += data[pos:min(end, n)]
        pos = end
    return bytes(out[:limit])


def _chunk(data: bytes, wanted: bytes) -> bytes | None:
    for fourcc, ds, de, _, _ in iter_chunks(data):
        if fourcc == wanted:
            return data[ds:de]
    return None


def icc_profile(data: bytes) -> bytes | None:
    return _chunk(data, b"ICCP")


def exif_block(data: bytes) -> bytes | None:
    return _chunk(data, b"EXIF")


def orientation(data: bytes) -> int | None:
    raw = exif_block(data)
    return read_orientation(raw) if raw else None


def _simple_canvas(data: bytes) -> tuple[int, int, bool]:
    """(width, height, has alpha) of a simple WebP, read from its VP8/VP8L bitstream header."""
    for fourcc, ds, de, _, _ in iter_chunks(data):
        head = data[ds:min(de, ds + 10)]
        if fourcc == b"VP8L" and len(head) >= 5 and head[0] == 0x2F:
            bits = int.from_bytes(head[1:5], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, bool(bits >> 28 & 1)
        if fourcc == b"VP8 " and len(head) >= 10 and head[3:6] == b"\x9d\x01\x2a":
            width, height = int.from_bytes(head[6:8], "little") & 0x3FFF, int.from_bytes(head[8:10], "little") & 0x3FFF
            if width and height:
                return width, height, False
            break  # a frame with no width or height: no canvas can be given for it
    raise WebpError(t("webp.no_header"))


def with_exif(data: bytes, exif: bytes) -> bytes:
    """The WebP with an EXIF chunk after the image data (where the container specification puts it)
    and the EXIF flag set in its VP8X header. A simple file (VP8 or VP8L only) cannot carry EXIF: it
    becomes an extended one, with a VP8X header that gives the canvas size (and, for a VP8L with
    transparency, the alpha flag). `exif`: the TIFF-structured EXIF, without the "Exif\\0\\0" header."""
    chunks = [(fourcc, bytearray(data[start:end])) for fourcc, _, _, start, end in iter_chunks(data)]
    if not any(fourcc == b"VP8X" for fourcc, _ in chunks):
        width, height, alpha = _simple_canvas(data)
        header = bytes([FLAG_ALPHA if alpha else 0, 0, 0, 0]) + (width - 1).to_bytes(3, "little") \
            + (height - 1).to_bytes(3, "little")
        chunks.insert(0, (b"VP8X", bytearray(b"VP8X" + struct.pack("<I", len(header)) + header)))
    image = (b"VP8 ", b"VP8L", b"ALPH", b"ANIM", b"ANMF")
    at = max((i + 1 for i, (fourcc, _) in enumerate(chunks) if fourcc in image), default=len(chunks))
    chunks.insert(at, (b"EXIF", bytearray(b"EXIF" + struct.pack("<I", len(exif)) + exif + b"\x00" * (len(exif) & 1))))
    body = bytearray()
    for fourcc, chunk in chunks:
        if fourcc == b"VP8X":
            chunk[8] |= FLAG_EXIF
        body += chunk
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + bytes(body)


def is_lossless(data: bytes) -> bool:
    """Is the image stored losslessly (VP8L)? In an animation the frames' data is inside the ANMF
    chunks (after a 16-byte frame header): one lossless frame is enough."""
    for fourcc, ds, de, _, _ in iter_chunks(data):
        if fourcc == b"VP8L":
            return True
        pos = ds + 16
        while fourcc == b"ANMF" and pos + 8 <= de:
            if data[pos:pos + 4] == b"VP8L":
                return True
            size = struct.unpack_from("<I", data, pos + 4)[0]
            pos += 8 + size + (size & 1)
    return False


def is_animated(data: bytes) -> bool:
    return any(fourcc in (b"ANIM", b"ANMF") for fourcc, *_ in iter_chunks(data))


def _report(fourcc: bytes, payload: bytes, res: ScanResult) -> None:
    if fourcc == b"EXIF":
        analyze_exif(payload, res)
    elif fourcc in (b"XMP ", b"XMP\x00"):
        analyze_xmp(payload, res)
    elif fourcc == b"C2PA":
        res.add(Kind.C2PA, "", len(payload))
    elif fourcc == b"ICCP":
        from ..colors import icc_description

        res.summary.icc_description = icc_description(payload)
        res.add(Kind.ICC, res.summary.icc_description or "", len(payload))
    else:
        res.add(Kind.OTHER, fourcc.decode("latin1", "replace").strip(), len(payload))


def scan(data: bytes, res: ScanResult) -> None:
    for fourcc, ds, de, _, _ in iter_chunks(data):
        if fourcc == b"ANMF":  # the chunks a frame carries besides its image
            for sub, sub_ds, sub_de, _, _ in _frame_chunks(data, ds, de):
                if sub not in FRAME_KEEP:
                    _report(sub, data[sub_ds:sub_de], res)
        elif fourcc not in KEEP:
            _report(fourcc, data[ds:de], res)
    end = _riff_end(data)
    if end < len(data) and data[end:].strip(b"\x00"):
        res.add(Kind.TRAILER, "", len(data) - end)
