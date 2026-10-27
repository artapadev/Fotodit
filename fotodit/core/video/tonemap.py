"""Converting HDR (HLG / PQ, BT.2020) video to SDR (BT.709): tone mapping.

Since the iPhone 12, iPhones record video in HDR (HLG, 10-bit) by default. If the
color and brightness information is carried over unchanged when these videos are
converted to H.264 (8-bit SDR), the picture looks washed out and gray. Here every
pixel goes through this transform:

1. HLG/PQ signal → linear display light (cd/m²). For HLG, the BT.2100 inverse OETF
   and the system gamma (1.2) for a 1000 cd/m² display; for PQ, the EOTF.
2. BT.2020 color space → BT.709 (3×3 matrix in linear light; out-of-gamut parts are clipped).
3. Brightness mapping: BT.2408's HDR reference white (203 cd/m²) lands at ~96%
   signal level in SDR; brighter areas are compressed gently with an exponential
   shoulder (without blowing out highlights). The mapping is applied to the brightest channel
   (maxRGB), so the hue does not change.
4. Encoding with the BT.709 / BT.1886 display gamma (2.4).

PyAV's FFmpeg build has no `zscale`/`libplacebo` filter that could do this; the transform
is written to a 3D table (LUT, 65³) and applied with FFmpeg's `lut3d` filter. For
thumbnails the same transform is applied directly to the pixels.
"""

from __future__ import annotations

import atexit
import os
import tempfile
import threading

import numpy as np

HLG, PQ = "HLG", "PQ"
REFERENCE_WHITE = 203.0  # cd/m² (BT.2408)
HLG_PEAK = 1000.0
KNEE = 0.75  # brightness is kept unchanged up to this linear level
LUT_SIZE = 65

_BT2020_TO_709 = np.array([
    [1.6605, -0.5876, -0.0728],
    [-0.1246, 1.1329, -0.0083],
    [-0.0182, -0.1006, 1.1187],
], dtype=np.float64)
_LUMA_2020 = np.array([0.2627, 0.6780, 0.0593])


def _hlg_to_nits(signal: np.ndarray) -> np.ndarray:
    a, b, c = 0.17883277, 0.28466892, 0.55991073
    e = np.clip(signal, 0.0, 1.0)
    scene = np.where(e <= 0.5, e * e / 3.0, (np.exp((e - c) / a) + b) / 12.0)
    ys = np.clip(scene @ _LUMA_2020, 1e-9, None)[..., None]
    gamma = 1.2
    return HLG_PEAK * np.power(ys, gamma - 1.0) * scene


def _pq_to_nits(signal: np.ndarray) -> np.ndarray:
    m1, m2 = 2610 / 16384, 2523 / 4096 * 128
    c1, c2, c3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
    e = np.power(np.clip(signal, 0.0, 1.0), 1.0 / m2)
    return 10000.0 * np.power(np.maximum(e - c1, 0.0) / (c2 - c3 * e), 1.0 / m1)


def _shoulder(x: np.ndarray) -> np.ndarray:
    """Linear light (reference white = 1) → linear SDR (0–1): unchanged up to the knee,
    then an exponential shoulder with a continuous slope."""
    k = KNEE
    return np.where(x <= k, x, k + (1.0 - k) * (1.0 - np.exp(-(x - k) / (1.0 - k))))


def tonemap(signal: np.ndarray, kind: str) -> np.ndarray:
    """BT.2020 HLG/PQ R'G'B' (0–1, channels on the last axis) → BT.709 SDR R'G'B' (0–1)."""
    nits = _hlg_to_nits(signal) if kind == HLG else _pq_to_nits(signal)
    linear = np.clip((nits / REFERENCE_WHITE) @ _BT2020_TO_709.T, 0.0, None)
    peak = linear.max(axis=-1, keepdims=True)
    scale = np.where(peak > 1e-9, _shoulder(peak) / np.maximum(peak, 1e-9), 1.0)
    sdr = np.clip(linear * scale, 0.0, 1.0)
    return np.power(sdr, 1.0 / 2.4)


def cube_text(kind: str, size: int = LUT_SIZE) -> str:
    """3D table in Adobe .cube format (R is the fastest-changing axis)."""
    grid = np.linspace(0.0, 1.0, size)
    b, g, r = np.meshgrid(grid, grid, grid, indexing="ij")
    rgb = np.stack([r, g, b], axis=-1).reshape(-1, 3)
    out = tonemap(rgb, kind)
    lines = [f"TITLE \"Fotodit {kind} to SDR\"", f"LUT_3D_SIZE {size}"]
    lines.extend(f"{x:.6f} {y:.6f} {z:.6f}" for x, y, z in out)
    return "\n".join(lines) + "\n"


_lock = threading.Lock()
_files: dict[str, str] = {}


def lut_file(kind: str) -> str:
    """The table's file in the temp folder (generated once per session)."""
    with _lock:
        path = _files.get(kind)
        if path and os.path.exists(path):
            return path
        fd, path = tempfile.mkstemp(prefix=f"fotodit-{kind.lower()}-", suffix=".cube")
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(cube_text(kind))
        if not _files:
            atexit.register(cleanup)  # do not leave the ~7 MB table in the temp folder
        _files[kind] = path
        return path


def cleanup() -> None:
    with _lock:
        for path in _files.values():
            try:
                os.remove(path)
            except OSError:
                pass
        _files.clear()
