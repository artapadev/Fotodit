"""Builds ICC profiles from color space definitions (chromaticity coordinates + transfer curve).

Some files declare their color space without embedding an ICC profile:

* HEIC/AVIF: numeric codes in the "nclx" box (ITU-T H.273), e.g. Display P3 = 12.
* Cameras shooting in Adobe RGB: only the "R03" interoperability index in EXIF.

Since this information is lost too when the metadata is stripped, the pixels must be converted to
sRGB first. LittleCMS does not understand nclx codes, so an equivalent matrix/curve ICC profile is
computed here (adapted to D50 with Bradford).
"""

from __future__ import annotations

import functools
import struct

D65 = (0.3127, 0.3290)
D50_XYZ = (0.9642, 1.0, 0.8249)

# H.273 color primaries: xy coordinates of (R, G, B, white)
PRIMARIES = {
    4: ((0.67, 0.33), (0.21, 0.71), (0.14, 0.08), (0.310, 0.316)),  # BT.470 M
    5: ((0.64, 0.33), (0.29, 0.60), (0.15, 0.06), D65),  # BT.601 625
    6: ((0.630, 0.340), (0.310, 0.595), (0.155, 0.070), D65),  # BT.601 525
    7: ((0.630, 0.340), (0.310, 0.595), (0.155, 0.070), D65),  # SMPTE 240M
    9: ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046), D65),  # BT.2020
    11: ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.314, 0.351)),  # DCI-P3
    12: ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060), D65),  # Display P3
}
SRGB_LIKE_PRIMARIES = {1, 2}  # BT.709 = sRGB; 2 = unspecified
HDR_TRANSFERS = {16, 18}  # PQ, HLG

ADOBE_RGB = ((0.64, 0.33), (0.21, 0.71), (0.15, 0.06), D65)
D50 = (0.3457, 0.3585)
PROPHOTO_RGB = ((0.7347, 0.2653), (0.1596, 0.8404), (0.0366, 0.0001), D50)  # ROMM RGB, gamma 1.8


def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _mat_vec(a, v):
    return [sum(a[i][k] * v[k] for k in range(3)) for i in range(3)]


def _inverse(m):
    (a, b, c), (d, e, f), (g, h, i) = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return [
        [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
        [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
        [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det],
    ]


def _xyz(xy):
    x, y = xy
    return [x / y, 1.0, (1 - x - y) / y]


_BRADFORD = [[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]]


def colorants_d50(red, green, blue, white) -> list[list[float]]:
    """Columns of the RGB→XYZ matrix (adapted to D50): rXYZ, gXYZ, bXYZ."""
    prim = [_xyz(red), _xyz(green), _xyz(blue)]
    m = [[prim[j][i] for j in range(3)] for i in range(3)]
    w = _xyz(white)
    s = _mat_vec(_inverse(m), w)
    rgb_to_xyz = [[m[i][j] * s[j] for j in range(3)] for i in range(3)]
    src = _mat_vec(_BRADFORD, w)
    dst = _mat_vec(_BRADFORD, list(D50_XYZ))
    scale = [[dst[i] / src[i] if i == j else 0.0 for j in range(3)] for i in range(3)]
    adapt = _mat_mul(_inverse(_BRADFORD), _mat_mul(scale, _BRADFORD))
    full = _mat_mul(adapt, rgb_to_xyz)
    return [[full[i][j] for i in range(3)] for j in range(3)]


def _s15(v: float) -> bytes:
    return struct.pack(">i", int(round(v * 65536)))


def _curve(kind) -> bytes:
    if isinstance(kind, float):
        return b"curv" + b"\x00" * 4 + struct.pack(">I", 1) + struct.pack(">H", int(round(kind * 256)))
    values = []
    for i in range(1024):
        v = i / 1023
        if kind == "srgb":
            lin = v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
        elif kind == "bt709":
            lin = v / 4.5 if v < 0.081 else ((v + 0.099) / 1.099) ** (1 / 0.45)
        else:
            lin = v
        values.append(int(round(min(max(lin, 0.0), 1.0) * 65535)))
    return b"curv" + b"\x00" * 4 + struct.pack(">I", 1024) + b"".join(struct.pack(">H", x) for x in values)


def _desc(text: str) -> bytes:
    raw = text.encode("ascii") + b"\x00"
    return b"desc" + b"\x00" * 4 + struct.pack(">I", len(raw)) + raw + b"\x00" * 8 + b"\x00" * 3 + b"\x00" * 67


def _xyz_tag(v) -> bytes:
    return b"XYZ " + b"\x00" * 4 + b"".join(_s15(c) for c in v)


def rgb_profile(description: str, primaries, trc) -> bytes:
    red, green, blue, white = primaries
    r, g, b = colorants_d50(red, green, blue, white)
    curve = _curve(trc)
    tags = [
        (b"desc", _desc(description)),
        (b"cprt", b"text" + b"\x00" * 4 + b"No copyright, use freely\x00"),
        (b"wtpt", _xyz_tag(D50_XYZ)),
        (b"rXYZ", _xyz_tag(r)),
        (b"gXYZ", _xyz_tag(g)),
        (b"bXYZ", _xyz_tag(b)),
        (b"rTRC", curve),
        (b"gTRC", curve),
        (b"bTRC", curve),
    ]
    offset = 128 + 4 + 12 * len(tags)
    table, body = b"", b""
    for sig, payload in tags:
        payload += b"\x00" * (-len(payload) % 4)
        table += sig + struct.pack(">II", offset + len(body), len(payload))
        body += payload
    header = (
        struct.pack(">I", offset + len(body)) + b"lcms" + bytes([2, 0x10, 0, 0]) + b"mntr" + b"RGB " + b"XYZ "
        + b"\x00" * 12 + b"acsp" + b"\x00" * 24 + b"\x00" * 4 + _s15(D50_XYZ[0]) + _s15(D50_XYZ[1])
        + _s15(D50_XYZ[2]) + b"\x00" * 4 + b"\x00" * 16 + b"\x00" * 28
    )
    return header + struct.pack(">I", len(tags)) + table + body


def _trc_for(transfer: int):
    if transfer in (1, 6, 14, 15):
        return "bt709"
    if transfer == 4:
        return 2.2
    if transfer == 5:
        return 2.8
    if transfer == 8:
        return "linear"
    return "srgb"  # 13 (sRGB) and unspecified


_NAMES = {4: "BT.470 M", 5: "BT.601 (625)", 6: "BT.601 (525)", 7: "SMPTE 240M", 9: "BT.2020", 11: "DCI-P3", 12: "Display P3"}


@functools.lru_cache(maxsize=32)
def nclx_profile(primaries: int, transfer: int) -> bytes | None:
    """ICC profile equivalent to the nclx codes; None if they are equivalent to sRGB."""
    if primaries in SRGB_LIKE_PRIMARIES or primaries not in PRIMARIES:
        return None
    return rgb_profile(f"{_NAMES[primaries]} (nclx)", PRIMARIES[primaries], _trc_for(transfer))


@functools.lru_cache(maxsize=1)
def adobe_rgb_profile() -> bytes:
    return rgb_profile("Adobe RGB (1998) compatible", ADOBE_RGB, 563 / 256)
