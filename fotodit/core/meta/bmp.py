"""BMP header scanner.

BMP carries almost no metadata. The only exception is the color profile
information in V4/V5 headers: an embedded ICC profile (MBED) or the path of a
profile file on the computer (LINK) — the latter can be a file path that
contains a user name.
"""

from __future__ import annotations

import struct

from ...i18n import t
from .model import Kind, ScanResult

LCS_LINKED = 0x4C494E4B  # 'LINK'
LCS_EMBEDDED = 0x4D424544  # 'MBED'


def color_profile(data: bytes) -> tuple[int, bytes | None]:
    """(CSType, profile bytes) — (0, None) without a V4/V5 header; profile bytes are None if absent."""
    if len(data) < 18 or data[:2] != b"BM":
        return 0, None
    header_size = struct.unpack_from("<I", data, 14)[0]
    if header_size < 108 or len(data) < 14 + 60:
        return 0, None
    cstype = struct.unpack_from("<I", data, 14 + 56)[0]
    if header_size >= 124 and cstype in (LCS_LINKED, LCS_EMBEDDED) and len(data) >= 14 + 124:
        offset, size = struct.unpack_from("<II", data, 14 + 112)
        start = 14 + offset
        if size and start + size <= len(data):
            return cstype, data[start:start + size]
    return cstype, None


def content_end(data: bytes) -> int:
    """Where the headers, the pixels and the profile data (if any) end."""
    if len(data) < 26:
        return len(data)
    bf_size, = struct.unpack_from("<I", data, 2)
    pixel_offset, header_size = struct.unpack_from("<II", data, 10)
    pix_end = pixel_offset
    if header_size >= 40 and len(data) >= 38:
        w, h, _, bpp, compression, size_image = struct.unpack_from("<iiHHII", data, 18)
        if compression in (0, 3, 6):
            pix_end = pixel_offset + ((abs(w) * bpp + 31) // 32) * 4 * abs(h)
        else:
            pix_end = pixel_offset + size_image
    elif header_size == 12:
        w, h, _, bpp = struct.unpack_from("<HHHH", data, 18)
        pix_end = pixel_offset + ((w * bpp + 31) // 32) * 4 * h
    end = pix_end
    if header_size >= 124 and len(data) >= 14 + 124:
        offset, size = struct.unpack_from("<II", data, 14 + 112)
        if size:
            end = max(end, 14 + offset + size)
    if bf_size <= len(data):
        end = max(end, bf_size)
    return min(end, len(data))


def trailing_bytes(data: bytes) -> int:
    rest = data[content_end(data):]
    return len(rest) if rest.strip(b"\x00") else 0


def scan(data: bytes, res: ScanResult) -> None:
    trailing = trailing_bytes(data)
    if trailing:
        res.add(Kind.TRAILER, "", trailing)
    cstype, profile = color_profile(data)
    if cstype == LCS_EMBEDDED and profile:
        from ..colors import icc_description

        res.summary.icc_description = icc_description(profile)
        res.add(Kind.ICC, res.summary.icc_description or "", len(profile))
    elif cstype == LCS_LINKED:
        path = (profile or b"").split(b"\x00", 1)[0].decode("latin1", "replace")
        res.add(Kind.OTHER, t("bmp.linked_profile", path=path)[:80], len(profile or b""))
