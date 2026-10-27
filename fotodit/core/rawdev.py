"""Getting an image from a RAW file (LibRaw / rawpy).

There are two methods:

* **Camera look** (default, "auto"): if the RAW contains a full-size JPEG processed by the camera
  itself, that is used. This is how the photo looked on the camera/phone (picture style, lens
  corrections and noise reduction included), and its metadata is cleaned at the byte level without
  recompression. If the preview is small or missing, the RAW data is developed instead.
* **Develop from RAW data** ("develop"): developed at full resolution with LibRaw — the camera's
  as-shot white balance, AHD demosaicing, sRGB output and the sRGB gamma curve (LibRaw's default is
  the BT.709 curve; the sRGB curve is chosen for correct tones in sRGB viewers). The orientation is
  applied by LibRaw.

**Exposure.** A photo whose exposure was adjusted (see `edits`) is always developed from the RAW
data, since only the RAW keeps detail beyond the camera JPEG's highlights and shadows. LibRaw
develops it to linear 16-bit values without brightening; the exposure and the sRGB curve are then
applied with a lookup table. 0 EV reproduces LibRaw's own automatic brightening (the brightest 1 %
of the pixels become white, see `white_point`); when brightening, a soft shoulder rolls the
highlights off instead of clipping them. The large preview and the thumbnails use the same
calculation on a half-size development, so they look like the result.

Developing a RAW is memory-intensive (hundreds of MB for a 45 MP RAW); at most two RAWs are
developed at the same time.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from ..i18n import t
from .edits import ev_text

RAW_AUTO = "auto"
RAW_DEVELOP = "develop"

FULL_PREVIEW_RATIO = 0.9
AUTO_BRIGHT_THR = 0.01  # LibRaw's default: the brightest 1 % of the pixels are clipped
_SLOTS = threading.BoundedSemaphore(2)

# LibRaw "flip" value → equivalent EXIF orientation value
FLIP_TO_EXIF = {0: 1, 1: 2, 2: 4, 3: 3, 4: 5, 5: 8, 6: 6, 7: 7}
FLIP_TRANSPOSE = {
    1: Image.Transpose.FLIP_LEFT_RIGHT,
    2: Image.Transpose.FLIP_TOP_BOTTOM,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.TRANSPOSE,
    5: Image.Transpose.ROTATE_90,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
}


class RawError(Exception):
    """Carries a Turkish error message to be shown to the user."""


@dataclass
class Rendering:
    image: Image.Image | None = None  # sRGB image developed from the RAW
    jpeg: bytes | None = None  # the camera's embedded full-size JPEG
    orientation: int = 1  # orientation to apply to `jpeg` (EXIF equivalent)
    notes: list[str] = field(default_factory=list)


def _rawpy():
    import rawpy

    return rawpy


def output_size(raw) -> tuple[int, int]:
    s = raw.sizes
    return (s.height, s.width) if s.flip & 4 else (s.width, s.height)


def camera_preview(raw) -> tuple[bytes, tuple[int, int]] | None:
    """The embedded JPEG preview and its size (None if there is none)."""
    rawpy = _rawpy()
    try:
        thumb = raw.extract_thumb()
    except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError, rawpy.LibRawError):
        return None
    if thumb.format != rawpy.ThumbFormat.JPEG:
        return None
    data = bytes(thumb.data)
    if not data.startswith(b"\xff\xd8"):
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            return data, im.size
    except Exception:  # noqa: BLE001 - broken preview
        return None


def explain_raw_error(exc: Exception) -> RawError:
    rawpy = _rawpy()
    if isinstance(exc, rawpy.LibRawFileUnsupportedError):
        return RawError(t("rawdev.unsupported"))
    if isinstance(exc, (rawpy.LibRawUnsufficientMemoryError, rawpy.LibRawTooBigError, MemoryError)):
        return RawError(t("rawdev.out_of_memory"))
    if isinstance(exc, (rawpy.LibRawIOError, rawpy.LibRawDataError)):
        return RawError(t("rawdev.damaged"))
    return RawError(t("rawdev.failed", error=exc))


def render(data: bytes, mode: str = RAW_AUTO, exposure: float = 0.0) -> Rendering:
    """`exposure`: the user's exposure adjustment in EV (0: none)."""
    rawpy = _rawpy()
    note = None
    with _SLOTS:
        try:
            with rawpy.imread(io.BytesIO(data)) as raw:
                if exposure:
                    linear = develop_linear(raw)
                    image = tone(linear, white_point(linear), exposure)
                    return Rendering(image=image,
                                     notes=[t("rawdev.exposure", ev=ev_text(exposure))])
                if mode == RAW_AUTO:
                    out_w, out_h = output_size(raw)
                    preview = camera_preview(raw)
                    if preview:
                        jpeg, (pw, ph) = preview
                        if max(pw, ph) >= FULL_PREVIEW_RATIO * max(out_w, out_h):
                            return Rendering(
                                jpeg=jpeg,
                                orientation=FLIP_TO_EXIF.get(raw.sizes.flip, 1),
                                notes=[t("rawdev.camera_jpeg")],
                            )
                        note = t("rawdev.small_preview", w=pw, h=ph)
                    else:
                        note = t("rawdev.no_preview")
                rgb = raw.postprocess(
                    use_camera_wb=True,
                    output_color=rawpy.ColorSpace.sRGB,
                    output_bps=8,
                    gamma=(2.4, 12.92),
                    highlight_mode=rawpy.HighlightMode.Clip,
                )
        except RawError:
            raise
        except Exception as exc:  # noqa: BLE001 - LibRaw errors are turned into Turkish messages
            raise explain_raw_error(exc) from exc
    image = Image.fromarray(rgb)
    if image.mode != "RGB":
        image = image.convert("RGB")
    return Rendering(image=image, notes=[note or t("rawdev.developed")])


def thumbnail(path: str, box: int, min_fraction: float = 0.0) -> Image.Image | None:
    """Quick preview: the embedded JPEG (or a half-size development if there is none).

    `min_fraction`: if the long side of the embedded preview is shorter than this fraction of the
    requested size (capped at the RAW's own size) — e.g. only a thumbnail is embedded but the large
    preview needs more — the RAW data is developed at half size.
    """
    rawpy = _rawpy()
    try:
        with rawpy.imread(path) as raw:
            flip = raw.sizes.flip
            min_side = min(box, max(output_size(raw))) * min_fraction
            try:
                thumb = raw.extract_thumb()
            except rawpy.LibRawError:
                thumb = None
            im = None
            if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
                im = Image.open(io.BytesIO(bytes(thumb.data)))
                if max(im.size) < min_side:
                    im = None
                else:
                    own_orientation = im.getexif().get(0x0112) is not None
                    im.draft("RGB", (box, box))
                    im.thumbnail((box, box), Image.Resampling.BICUBIC)
                    if own_orientation:
                        from .imaging import apply_orientation

                        return apply_orientation(im)
            elif thumb is not None:
                im = Image.fromarray(thumb.data)
                if max(im.size) < min_side:
                    im = None
                else:
                    im.thumbnail((box, box), Image.Resampling.BICUBIC)
            if im is None:
                with _SLOTS:
                    rgb = raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8, gamma=(2.4, 12.92))
                im = Image.fromarray(rgb)  # LibRaw already applies the orientation
                im.thumbnail((box, box), Image.Resampling.BICUBIC)
                return im
            method = FLIP_TRANSPOSE.get(flip)
            return im.transpose(method) if method is not None else im
    except Exception:  # noqa: BLE001 - without a preview a placeholder is shown
        return None


# ------------------------------------------------------------------- exposure


def develop_linear(raw, half_size: bool = False) -> np.ndarray:
    """The RAW data as linear 16-bit sRGB (height × width × 3): the camera's white balance, no
    brightening and no gamma curve. The orientation is applied by LibRaw."""
    rawpy = _rawpy()
    return raw.postprocess(
        use_camera_wb=True,
        output_color=rawpy.ColorSpace.sRGB,
        output_bps=16,
        gamma=(1, 1),
        no_auto_bright=True,
        highlight_mode=rawpy.HighlightMode.Clip,
        half_size=half_size,
    )


def white_point(rgb: np.ndarray) -> float:
    """The linear value LibRaw's automatic brightening maps to white. As in LibRaw, each channel's
    histogram (in steps of 8) is walked down from the top until more than 1 % of the pixels lie
    above; the brightest channel wins, and the search stops at 32 (very dark images)."""
    perc = rgb.shape[0] * rgb.shape[1] * AUTO_BRIGHT_THR
    top = 32
    for c in range(rgb.shape[2]):
        hist = np.bincount((rgb[..., c] >> 3).ravel(), minlength=0x2000)
        above = np.cumsum(hist[0x1FFF:32:-1])  # bins 0x1FFF … 33
        hit = np.flatnonzero(above > perc)
        if hit.size:
            top = max(top, 0x1FFF - int(hit[0]))
    return float(top << 3)


def exposure_lut(white: float, ev: float) -> np.ndarray:
    """16-bit linear value → 8-bit sRGB with `ev` stops of exposure; at 0 EV `white` becomes white
    (the same curve as LibRaw's 8-bit output). When brightening, the highlights roll off along a
    soft shoulder (from 75–100 % of white, depending on the amount) instead of clipping hard."""
    x = np.arange(65536, dtype=np.float64) * (2.0 ** ev / max(white, 1.0))
    if ev > 0:
        knee = 1.0 - 0.25 * min(1.0, ev)
        over = x > knee
        x[over] = knee + (1.0 - knee) * -np.expm1(-(x[over] - knee) / (1.0 - knee))
    x = np.clip(x, 0.0, 1.0)
    srgb = np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)
    return np.minimum(np.floor(srgb * 256), 255).astype(np.uint8)


def tone(rgb: np.ndarray, white: float, ev: float) -> Image.Image:
    """A linear development (see `develop_linear`) with the exposure applied, as an 8-bit RGB image."""
    lut = exposure_lut(white, ev)
    out = np.empty(rgb.shape, np.uint8)
    rows = max(1, (1 << 22) // max(1, rgb.shape[1] * rgb.shape[2]))  # ~4 M values at a time
    for y in range(0, rgb.shape[0], rows):
        out[y:y + rows] = lut[rgb[y:y + rows]]
    return Image.fromarray(out, "RGB")


@dataclass
class LinearPreview:
    """A small linear development, for showing the exposure live (see `tone`)."""

    pixels: np.ndarray  # height × width × 3, uint16
    white: float

    def image(self, ev: float) -> Image.Image:
        return tone(self.pixels, self.white, ev)


def linear_preview(path: str, box: int) -> LinearPreview | None:
    """The RAW developed at half size and reduced to at most `box` pixels on its long side (None if
    it cannot be developed). The white point is measured before reducing, like the full development's."""
    rawpy = _rawpy()
    try:
        with _SLOTS, rawpy.imread(path) as raw:
            rgb = develop_linear(raw, half_size=True)
    except Exception:  # noqa: BLE001 - without a preview the camera's JPEG stays on screen
        return None
    white = white_point(rgb)
    h, w = rgb.shape[:2]
    factor = -(-max(h, w) // max(1, box))
    if factor > 1:
        h, w = h // factor, w // factor
        blocks = rgb[:h * factor, :w * factor].reshape(h, factor, w, factor, 3)
        rgb = (blocks.sum(axis=(1, 3), dtype=np.uint32) // (factor * factor)).astype(np.uint16)
    return LinearPreview(np.ascontiguousarray(rgb), white)
