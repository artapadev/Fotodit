"""Opening images, normalizing pixel modes and making copies that carry no metadata.

Some Pillow savers copy metadata from the source *on their own* (verified with Pillow 12.3 /
pillow-heif 1.8):

* JPEG → the source's comment (COM) segment,
* PNG / AVIF / TIFF → the ICC profile; TIFF also XMP, IPTC and Photoshop tags,
* HEIC (pillow-heif) → EXIF, XMP and thumbnail settings.

That is why every re-encode uses a new image object from `clean_copy()`, with an empty info dict
and none of the plugin-specific attributes (tag_v2, quantization …).
"""

from __future__ import annotations

import threading
import warnings

from PIL import Image, ImageOps

# Anything above 400 megapixels (e.g. huge panoramas) is treated as a "decompression bomb": Pillow refuses
# an image above twice MAX_IMAGE_PIXELS, and its mere warning between the two is not needed.
Image.MAX_IMAGE_PIXELS = 200_000_000
warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)
# JPEGs with a broken MPF index are read as plain JPEGs; MPF is metadata to be stripped anyway.
warnings.filterwarnings("ignore", message="Image appears to be a malformed MPO file")

_plugin_lock = threading.Lock()
_plugins_ready = False

WHITE = (255, 255, 255)
_HIGH_BIT = {"I;16", "I;16L", "I;16B", "I;16N", "I", "F"}


def ensure_plugins() -> None:
    global _plugins_ready
    if _plugins_ready:
        return
    with _plugin_lock:
        if _plugins_ready:
            return
        import pillow_heif

        # No need to read depth/auxiliary images (speed); thumbnails stay enabled
        # for fast previews.
        pillow_heif.register_heif_opener(depth_images=False, aux_images=False)
        _plugins_ready = True


# Only these decoders are allowed, so that a file with a .jpg extension that is actually,
# say, EPS cannot make Pillow run external tools such as Ghostscript.
# (MPO files are opened by the JPEG decoder; they have no separate registration.)
ALLOWED_DECODERS = ("JPEG", "PNG", "WEBP", "GIF", "TIFF", "BMP", "DIB", "HEIF", "AVIF")


def open_image(fp) -> Image.Image:
    ensure_plugins()
    return Image.open(fp, formats=ALLOWED_DECODERS)


def clean_copy(im: Image.Image) -> Image.Image:
    """A new image object with the same pixels that carries no metadata at all.

    `Image.copy()` returns the base `Image` class (plugin attributes are not copied); only the
    transparency entry of the info dict is kept.
    """
    im.load()
    out = im.copy()
    out.info = {k: im.info[k] for k in ("transparency",) if k in im.info}
    return out


ORIENTATION_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def orient(im: Image.Image, orientation: int) -> Image.Image:
    """Applies the given EXIF orientation value (1–8) to the pixels."""
    method = ORIENTATION_TRANSPOSE.get(orientation)
    return im.transpose(method) if method is not None else im.copy()


def apply_orientation(im: Image.Image) -> Image.Image:
    """Applies the EXIF orientation to the pixels (needed for JPEG, PNG, WebP, AVIF; no effect on
    HEIC and TIFF, where it is already applied on opening).

    `exif_transpose` can fail on broken EXIF; in that case the orientation value is read by hand.
    """
    try:
        return ImageOps.exif_transpose(im)
    except Exception:  # noqa: BLE001 - broken EXIF
        try:
            orientation = im.getexif().get(0x0112)
        except Exception:  # noqa: BLE001
            orientation = None
        method = ORIENTATION_TRANSPOSE.get(orientation)
        return im.transpose(method) if method is not None else im.copy()


def to_color_mode(im: Image.Image) -> Image.Image:
    """Reduces palette, 1-bit and high-bit-depth modes to 8-bit modes that the color conversion
    can work with (RGB/RGBA/L/LA/CMYK/LAB)."""
    mode = im.mode
    if mode in ("RGB", "RGBA", "L", "LA", "CMYK", "LAB"):
        return im
    if mode in ("P", "PA"):
        return im.convert("RGBA" if im.has_transparency_data else "RGB")
    if mode == "1":
        return im.convert("L")
    if mode in _HIGH_BIT:
        return high_bit_to_l(im)
    if mode in ("RGBa", "RGBX"):
        return im.convert("RGBA" if mode == "RGBa" else "RGB")
    if mode == "La":
        return im.convert("LA")
    return im.convert("RGB")


def high_bit_to_l(im: Image.Image) -> Image.Image:
    """Reduces 16/32-bit grayscale images to 8 bits by scaling.

    Pillow's direct `convert("L")` clips 16-bit values at 255 (the image comes out pure white),
    so the values are scaled first.
    """
    mode = im.mode
    if mode.startswith("I;16"):
        return im.convert("I").point(lambda v: v / 257).convert("L")
    lo, hi = im.getextrema()
    if mode == "F":
        if lo >= 0.0 and hi <= 1.0:
            return im.point(lambda v: v * 255).convert("L")
    elif lo >= 0 and hi <= 255:
        return im.convert("L")
    elif lo >= 0 and hi <= 65535:
        return im.point(lambda v: v / 257).convert("L")
    span = (hi - lo) or 1
    return im.point(lambda v: (v - lo) * (255.0 / span)).convert("L")


def flatten(im: Image.Image, background=WHITE) -> Image.Image:
    """JPEG does not support transparency: transparent areas are placed on a white background.

    (Simply dropping the alpha channel would turn transparent areas black or random colors.)
    """
    im = to_color_mode(im)
    mode = im.mode
    if mode in ("RGB", "L"):
        return im
    if mode == "RGBA":
        base = Image.new("RGB", im.size, background)
        base.paste(im.convert("RGB"), mask=im.getchannel("A"))
        return base
    if mode == "LA":
        base = Image.new("L", im.size, 255)
        base.paste(im.getchannel("L"), mask=im.getchannel("A"))
        return base
    return im.convert("RGB")


def transpose_qtable(table) -> list[int]:
    """Transpose of an 8x8 quantization table (a 90°/270° rotation swaps the horizontal and
    vertical frequencies)."""
    t = list(table)
    return [t[(i % 8) * 8 + i // 8] for i in range(64)]
