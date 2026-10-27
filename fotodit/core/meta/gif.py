"""GIF block parser, lossless metadata stripper and scanner.

In GIF, metadata lives in comment extensions (0x21 0xFE) and application extensions
(0x21 0xFF: "XMP DataXMP", "ICCRGBG1012", "MGK8BIM0000" …).
The NETSCAPE2.0 / ANIMEXTS1.0 extensions that carry the loop count, graphic control
extensions (frame duration, transparency) and image blocks are kept; the two extensions
are cut to their fields (further sub-blocks can only hide data).
"""

from __future__ import annotations

from ...i18n import t
from .model import FormatError, Kind, ScanResult
from .xmp import analyze_xmp

_LOOP_APPS = {b"NETSCAPE2.0", b"ANIMEXTS1.0"}
LOOP_SIZE = 19  # 21 FF 0B, the 11-byte identifier, the loop sub-block (03 01 <count>), 00
GCE_SIZE = 8  # 21 F9 04 <flags, delay, transparent color index>, 00


class GifError(FormatError):
    pass


def _skip_sub_blocks(data: bytes, pos: int) -> int:
    n = len(data)
    while True:
        if pos >= n:
            raise GifError(t("gif.truncated"))
        size = data[pos]
        pos += 1
        if size == 0:
            return pos
        pos += size


def iter_blocks(data: bytes):
    """Yields ('header'|'image'|'ext'|'trailer', label, start, end)."""
    if data[:6] not in (b"GIF87a", b"GIF89a") or len(data) < 13:
        raise GifError(t("gif.not_gif"))
    packed = data[10]
    pos = 13
    if packed & 0x80:
        pos += 3 * (2 << (packed & 0x07))
    yield "header", 0, 0, pos
    n = len(data)
    while pos < n:
        intro = data[pos]
        if intro == 0x3B:
            yield "trailer", 0, pos, pos + 1
            return
        if intro == 0x21:
            if pos + 2 > n:
                raise GifError(t("gif.truncated"))
            label = data[pos + 1]
            end = _skip_sub_blocks(data, pos + 2)
            yield "ext", label, pos, end
            pos = end
        elif intro == 0x2C:
            if pos + 10 > n:
                raise GifError(t("gif.truncated"))
            flags = data[pos + 9]
            p = pos + 10
            if flags & 0x80:
                p += 3 * (2 << (flags & 0x07))
            end = _skip_sub_blocks(data, p + 1)  # LZW minimum code size byte
            yield "image", 0, pos, end
            pos = end
        else:
            raise GifError(t("gif.unknown_block"))
    raise GifError(t("gif.no_trailer"))


def _app_id(data: bytes, start: int) -> bytes:
    # 0x21 0xFF 0x0B <11 bytes: application identifier + authentication code>
    if data[start + 2] == 11:
        return data[start + 3:start + 14]
    return b""


def _kept_extension(data: bytes, label: int, start: int, end: int) -> bytes:
    """A graphic control or loop extension as `strip` keeps it: the 4-byte block, the loop sub-block (none
    without a loop count). An extension no longer than that stays as it is."""
    if label == 0xF9:
        if end - start <= GCE_SIZE:
            return data[start:end]
        size = min(data[start + 2], 4)
        return b"\x21\xf9" + bytes([size]) + data[start + 3:start + 3 + size] + b"\x00"
    if end - start <= LOOP_SIZE:
        return data[start:end]
    block = data[start + 15:start + 15 + data[start + 14]]  # the first sub-block after the identifier
    if len(block) < 3 or block[0] != 1:
        return b""
    return data[start:start + 14] + b"\x03" + block[:3] + b"\x00"


def strip(data: bytes) -> bytes:
    """Keeps only the header, color tables, image blocks, graphic control and
    loop extensions (cut to their fields). The plain text extension (0x01) is dropped
    too: almost no viewer renders it, but it can carry hidden text."""
    out = bytearray()
    for kind, label, start, end in iter_blocks(data):
        if kind == "ext":
            if label == 0xF9 or (label == 0xFF and _app_id(data, start) in _LOOP_APPS):
                out += _kept_extension(data, label, start, end)
            continue
        out += data[start:end]
    return bytes(out)


def scan(data: bytes, res: ScanResult) -> None:
    trailer_end = len(data)
    for kind, label, start, end in iter_blocks(data):
        if kind == "trailer":
            trailer_end = end
        if kind != "ext":
            continue
        if label == 0xFE:
            text = _sub_block_data(data, start + 2)
            res.add(Kind.COMMENT, text[:60].decode("latin1", "replace"), len(text))
        elif label == 0xFF:
            app = _app_id(data, start)
            if app in _LOOP_APPS:
                continue
            payload = _sub_block_data(data, start + 14)
            if app == b"XMP DataXMP":
                # XMP is written as "raw" data that does not follow the sub-block format.
                analyze_xmp(data[start + 14:end], res)
            elif app.startswith(b"ICCRGBG1"):
                res.add(Kind.ICC, "", len(payload))
            elif app.startswith((b"MGK8BIM", b"MGKIPTC")):
                res.add(Kind.IPTC, "", len(payload))
            elif app.startswith(b"C2PA"):
                res.add(Kind.C2PA, "", len(payload))
            else:
                res.add(Kind.OTHER, app.decode("latin1", "replace"), len(payload))
        elif label == 0x01:
            text = _sub_block_data(data, start + 15)
            res.add(Kind.TEXT, t("gif.plain_text", text=text[:40].decode("latin1", "replace")), len(text))
        elif label != 0xF9:
            res.add(Kind.OTHER, t("gif.extension", label=f"{label:02X}"), end - start)
    if trailer_end < len(data) and data[trailer_end:].strip(b"\x00"):
        res.add(Kind.TRAILER, "", len(data) - trailer_end)


def _sub_block_data(data: bytes, pos: int) -> bytes:
    out = bytearray()
    n = len(data)
    while pos < n:
        size = data[pos]
        if size == 0:
            break
        out += data[pos + 1:pos + 1 + size]
        pos += 1 + size
    return bytes(out)
