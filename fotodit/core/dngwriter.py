"""Synthetic DNG generator (for the self-test and tests).

Builds a DNG from the given RGB image with the structure a real camera would write: 12-bit linear
CFA (RGGB Bayer mosaic) raw data, DNG color tags (camera color space = linear sRGB), EXIF (capture
date, serial number), GPS, optional XMP and an optional embedded JPEG preview. LibRaw processes the
file like a real RAW.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from .tiffwriter import Ifd, build_tiff

# XYZ → linear sRGB (D65). DNG ColorMatrix1 maps XYZ to the camera space.
_XYZ_TO_SRGB = ((3.2406, -1.5372, -0.4986), (-0.9689, 1.8758, 0.0415), (0.0557, -0.2040, 1.0570))
WHITE = 4095


def _mosaic(im: Image.Image) -> bytes:
    rgb = np.asarray(im.convert("RGB"), dtype=np.float64) / 255.0
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    vals = np.clip(np.round(lin * WHITE), 0, WHITE).astype("<u2")
    h, w = vals.shape[:2]
    cfa = np.empty((h, w), "<u2")
    cfa[0::2, 0::2] = vals[0::2, 0::2, 0]
    cfa[0::2, 1::2] = vals[0::2, 1::2, 1]
    cfa[1::2, 0::2] = vals[1::2, 0::2, 1]
    cfa[1::2, 1::2] = vals[1::2, 1::2, 2]
    return cfa.tobytes()


def make_dng(
    stored: Image.Image,
    *,
    orientation: int = 1,
    preview: str | None = None,
    make: str = "Fotodit",
    model: str = "Test Kamera",
    taken: str = "2024:05:12 14:30:22",
    gps: tuple[float, float] | None = (41.00833, 28.97833),
    xmp: bytes | None = None,
) -> bytes:
    """`stored`: the image as on the sensor (not rotated); both dimensions must be even.
    `preview`: None, "full" (full-size embedded JPEG) or "small" (1/4)."""
    w, h = stored.size
    assert w % 2 == 0 and h % 2 == 0
    e = "<"
    raw = (
        Ifd(e).long(254, 0).long(256, w).long(257, h).short(258, 16).short(259, 1).short(262, 32803)
        .pointer(273, "raw_data").short(277, 1).long(278, h).long(279, w * h * 2).short(284, 1)
        .shorts(33421, (2, 2)).byte(33422, bytes((0, 1, 1, 2)))
        .long(50714, 0).long(50717, WHITE)
    )
    main = raw if preview is None else Ifd(e)
    # The non-ASCII Software value is intentional: tests check that UTF-8 in an ASCII tag decodes correctly.
    main.ascii(271, make).ascii(272, model).short(274, orientation).ascii(305, "Fotodit DNG yazıcısı")
    main.ascii(306, taken)
    main.byte(50706, bytes((1, 4, 0, 0))).byte(50707, bytes((1, 1, 0, 0))).ascii(50708, f"{make} {model}")
    main.srationals(50721, [(int(round(v * 10000)), 10000) for row in _XYZ_TO_SRGB for v in row])
    main.short(50778, 21).rationals(50728, [(1, 1), (1, 1), (1, 1)])
    main.pointer(34665, "exif")
    if gps:
        main.pointer(34853, "gps")
    if xmp:
        main.byte(700, xmp)

    exif = Ifd(e).ascii(0x9003, taken).ascii(0x9004, taken).ascii(0xA431, "SN-DNG-000123")
    ifds = [("main", main)]
    blobs = {"raw_data": _mosaic(stored)}
    if preview is not None:
        pv = stored if preview == "full" else stored.resize((w // 4, h // 4), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        pv.convert("RGB").save(buf, "JPEG", quality=92)
        jpeg = buf.getvalue()
        main.long(254, 1).long(256, pv.width).long(257, pv.height).shorts(258, (8, 8, 8)).short(259, 7)
        main.short(262, 6).pointer(273, "preview").short(277, 3).long(278, pv.height).long(279, len(jpeg))
        main.short(284, 1).pointer(330, "raw")
        ifds.append(("raw", raw))
        blobs["preview"] = jpeg
    ifds.append(("exif", exif))
    if gps:
        lat, lon = gps

        def dms(v: float):
            v = abs(v)
            d = int(v)
            m = int((v - d) * 60)
            s = round(((v - d) * 60 - m) * 60 * 100)
            return [(d, 1), (m, 1), (s, 100)]

        g = (
            Ifd(e).byte(0, bytes((2, 3, 0, 0))).ascii(1, "N" if lat >= 0 else "S").rationals(2, dms(lat))
            .ascii(3, "E" if lon >= 0 else "W").rationals(4, dms(lon))
        )
        ifds.append(("gps", g))
    return build_tiff(ifds, blobs, e)
