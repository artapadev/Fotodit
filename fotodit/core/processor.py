"""Operations on the content of a single file: stripping metadata, converting to JPG, shrinking
for sharing and applying the user's edit (flip, rotation, straightening, crop, exposure).

If there is an edit, the image is re-encoded: JPEG reuses the source's own quantization
tables (quality does not drop), and PNG/lossless WebP/TIFF/BMP stay lossless. If metadata
stripping is off, the metadata is kept; only the orientation is set to 1, and thumbnails
of the old image (EXIF, Photoshop) and extra images are removed (they may show the
cropped-away area and do not match the edited image).

Colors are converted to sRGB before a color profile is removed (see `colors`). With
`keep_color_profile`, a Display P3 / Adobe RGB / ProPhoto RGB image keeps its pixels and gets a
clean standard profile instead (a JPEG then stays lossless); verification accepts exactly that
profile. In the same way, `keep_date` ("Çekim tarihini koru") gives a stripped photo back exactly
one piece of information, the capture date, in a freshly built EXIF of a single tag
(`meta.exif.date_only_exif`); GIF and BMP cannot carry EXIF, so the date is kept only as the
file's date (set by the runner).

With `max_side`, a photo whose long side is larger is shrunk (Lanczos) after the orientation, the
edit and the color conversion, before it is encoded: JPEG keeps its quantization tables, lossless
formats stay lossless. A photo within the limit takes its usual path (a JPEG stays lossless).

Everything happens in memory (bytes → bytes), and the output is verified before it is
written to disk:

1. If metadata was stripped, the output is scanned again; any metadata found, or any
   trace caught by the signature scan, makes the operation fail.
2. The output is decoded from start to end (to be sure no broken file was produced)
   and its dimensions are compared with the expected ones.
3. On lossless paths, the decoded pixels must match the source exactly.

If any of these steps fails, `ProcessError` is raised and the file is not touched.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime

from PIL import ExifTags, Image, ImageSequence, JpegImagePlugin
from PIL.PngImagePlugin import PngInfo, iTXt

from ..i18n import date_time, percent, t
from . import colors, edits, iccgen, rawdev
from .errors import explain
from .formats import DISPLAY_NAME, Fmt, detect, sniff
from .imaging import (
    ORIENTATION_TRANSPOSE,
    apply_orientation,
    clean_copy,
    flatten,
    open_image,
    orient,
    to_color_mode,
    transpose_qtable,
)
from .meta import bmp as bmp_meta
from .meta import gif as gif_meta
from .meta import jpeg as jpeg_meta
from .meta import png as png_meta
from .meta import raw as raw_meta
from .meta import residual_signatures, scan_bytes
from .meta import tiff as tiff_meta
from .meta import webp as webp_meta
from .meta.exif import (
    TAG_EXIF_IFD,
    date_only_exif,
    for_edited,
    load_exif,
    patch_orientation,
    strip_exif_header,
    with_exif_header,
)
from .meta.model import FormatError, Kind
from .rawdev import RAW_AUTO, RawError
from .video.process import VIDEO_COMPATIBLE

Edit = edits.Edit

LOSSLESS = "lossless"
REENCODED = "reencoded"
CONVERTED = "converted"
UNCHANGED = "unchanged"

HEIF_QUALITY = 90
AVIF_QUALITY = 90
WEBP_QUALITY = 95
FALLBACK_JPEG_QUALITY = 95
JPEG_MAX_SIDE = 65500  # libjpeg-turbo's limit (not 65535)
MAX_APP_PAYLOAD = 65533
# Pillow's reducing_gap when shrinking: big factors are first reduced by a whole number (fast), then
# resampled with Lanczos over at least this factor (no visible difference from Lanczos alone).
RESIZE_GAP = 3.0

_LOSSLESS_TIFF = {"raw", "tiff_lzw", "tiff_adobe_deflate", "tiff_deflate", "packbits"}
_BILEVEL_TIFF = {"group3", "group4", "tiff_ccitt"}


def _tiff_compression(compression: str, pages: list[Image.Image], notes: list[str]) -> str:
    """Keeps the source's lossless compression; lossy (JPEG) or unknown compression becomes LZW."""
    if compression in _LOSSLESS_TIFF or (compression in _BILEVEL_TIFF and all(p.mode == "1" for p in pages)):
        return compression
    notes.append(t("processor.tiff_lzw"))
    return "tiff_lzw"


# Keys of texts other modules compare with (skip reasons) or look for (notes).
RAW_NEEDS_JPEG = "processor.raw_needs_jpeg"
RAW_EDIT_NEEDS_JPEG = "processor.raw_edit_needs_jpeg"
RAW_RESIZE_NEEDS_JPEG = "processor.raw_resize_needs_jpeg"
UNKNOWN_PROFILE = "processor.color_unknown"
ORIENTED_NOTE = "processor.oriented"


class ProcessError(Exception):
    """Carries an error message, in the app's language, to be shown to the user."""


@dataclass
class ContentOptions:
    strip_metadata: bool = False  # photos and videos
    convert_to_jpeg: bool = False  # photos
    jpeg_quality: int = 95
    raw_mode: str = RAW_AUTO
    convert_video: bool = False  # videos → MP4
    video_codec: str = VIDEO_COMPATIBLE
    video_hw: bool = False  # re-encode with the GPU (hardware) encoder, if there is one
    keep_color_profile: bool = False  # keep wide color spaces (P3, Adobe RGB …) with a clean profile
    keep_date: bool = False  # with stripping: keep the capture date (a date-only EXIF and the file's date)
    max_side: int = 0  # photos: shrink so that the long side is at most this many pixels (0: original size)


@dataclass
class ContentResult:
    data: bytes | None  # None → content unchanged, the source file is used as is
    fmt: Fmt
    method: str
    notes: list[str] = field(default_factory=list)
    expected_size: tuple[int, int] | None = None
    profile: bytes | None = None  # the clean color profile embedded on purpose (keep_color_profile)
    date_exif: bytes | None = None  # the date-only EXIF embedded on purpose (keep_date)


def process_bytes(data: bytes, opts: ContentOptions, name: str | None = None,
                  edit: Edit | None = None, taken: datetime | None = None) -> ContentResult:
    """`name`: file name (to also recognize TIFF-based RAWs by their extension).
    `edit`: the user's edit (None if there is none).
    `taken`: the capture date (local time) that `keep_date` keeps when metadata is stripped; None if
    the file has none (a modification time is not a capture date)."""
    fmt = detect(data, name)
    if fmt is None:
        raise ProcessError(t("processor.unknown_format"))
    if edit is not None:
        edit = edit.photo_only()
    date = date_only_exif(taken) if opts.strip_metadata and opts.keep_date and taken is not None else None
    try:
        if fmt == Fmt.RAW:
            if not opts.convert_to_jpeg:
                if opts.strip_metadata:
                    raise ProcessError(t(RAW_NEEDS_JPEG))
                if edit is not None:
                    raise ProcessError(t(RAW_EDIT_NEEDS_JPEG))
                if not _raw_within(data, opts.max_side):
                    raise ProcessError(t(RAW_RESIZE_NEEDS_JPEG))
                return ContentResult(None, fmt, UNCHANGED)
            result = _convert_raw(data, opts, edit)
        elif opts.convert_to_jpeg and fmt != Fmt.JPEG:
            result = _convert_to_jpeg(data, fmt, opts, edit)
        elif opts.strip_metadata:
            result = _strip(data, fmt, edit, opts.keep_color_profile, opts.max_side, date)
        elif edit is not None or not _within(data, opts.max_side):
            result = _edit_keeping_metadata(data, fmt, edit, opts.max_side)
        else:
            return ContentResult(None, fmt, UNCHANGED)
        if date is not None:
            _add_date(result, date, taken)
        _verify(data, result, opts)
    except ProcessError:
        raise
    except RawError as exc:
        raise ProcessError(str(exc)) from exc
    except FormatError as exc:
        raise ProcessError(str(exc)) from exc
    except (
        Image.DecompressionBombError, MemoryError, OSError, ValueError, SyntaxError,
        EOFError, RuntimeError, KeyError, IndexError,
    ) as exc:
        raise ProcessError(explain(exc)) from exc
    return result


# --------------------------------------------------------------------------- helpers


def _open_loaded(data: bytes) -> Image.Image:
    im = open_image(io.BytesIO(data))
    im.load()
    return im


def _save(im: Image.Image, fmt: str, **kwargs) -> bytes:
    buf = io.BytesIO()
    im.save(buf, fmt, **kwargs)
    return buf.getvalue()


def _base_mode(mode: str) -> str:
    return {"RGBA": "RGB", "LA": "L"}.get(mode, mode)


def _color_note(icc: bytes | None, keep: bool = False) -> str:
    desc = (colors.icc_description(icc) if icc else "CIELAB") or t("processor.embedded_profile")
    if keep:  # wide colors were to be kept, but the profile is not a standard one
        return t(UNKNOWN_PROFILE, source=desc)
    return t("processor.color_converted", source=desc)


def _kept_profile(icc: bytes | None, mode: str, keep: bool) -> colors.StandardProfile | None:
    """With "keep the wide color space": the clean standard profile to embed instead of converting
    the pixels to sRGB (None: convert as usual)."""
    if not keep or not icc or _base_mode(mode) not in ("RGB", "P", "PA"):
        return None
    return colors.standard_profile(icc)


def _kept_note(kept: colors.StandardProfile) -> str:
    return t("processor.wide_kept", name=kept.name)


def _tone(px: Image.Image, edit: Edit | None) -> Image.Image:
    """The edit's exposure, for photos that are not RAW (a RAW gets it while it is developed)."""
    return edits.apply_exposure(px, edit.exposure) if edit is not None and edit.exposure else px


def _check_jpeg_size(size: tuple[int, int]) -> None:
    if max(size) > JPEG_MAX_SIDE:
        raise ProcessError(
            t("processor.too_big_for_jpeg", w=size[0], h=size[1], max=JPEG_MAX_SIDE)
        )


# ------------------------------------------------------------- shrinking for sharing


def _target_size(size: tuple[int, int], max_side: int) -> tuple[int, int] | None:
    """The size a photo shrinks to so that its long side is `max_side` pixels (rounded, at least 1);
    None if it already fits or there is no limit (0)."""
    w, h = size
    if not max_side or max(w, h) <= max_side:
        return None
    if w >= h:
        return max_side, max(1, (2 * h * max_side + w) // (2 * w))
    return max(1, (2 * w * max_side + h) // (2 * h)), max_side


def _within(data: bytes, max_side: int) -> bool:
    """Is the photo within the size limit (every page of a TIFF)? Only the headers are read."""
    if not max_side:
        return True
    with open_image(io.BytesIO(data)) as im:
        sizes = [im.size]
        if im.format == "TIFF":
            for index in range(1, getattr(im, "n_frames", 1)):
                im.seek(index)
                sizes.append(im.size)
    return all(_target_size(size, max_side) is None for size in sizes)


def _raw_within(data: bytes, max_side: int) -> bool:
    """Is the RAW's developed image within the size limit?"""
    if not max_side:
        return True
    import rawpy

    try:
        with rawpy.imread(io.BytesIO(data)) as raw:
            size = rawdev.output_size(raw)
    except Exception as exc:  # noqa: BLE001 - LibRaw errors are turned into Turkish messages
        raise rawdev.explain_raw_error(exc) from exc
    return _target_size(size, max_side) is None


def _shrink(px: Image.Image, max_side: int, notes: list[str], full: tuple[int, int] | None = None) -> Image.Image:
    """Shrinks the photo so that its long side is at most `max_side` pixels (0: original size), with
    Lanczos. `full`: the photo's size at full resolution when it was decoded smaller (see `_draft`)."""
    size = full or px.size
    target = _target_size(size, max_side)
    if target is None:
        return px
    if px.mode in ("P", "PA", "1"):  # Pillow resizes these with the nearest pixel only
        px = px.convert("L" if px.mode == "1" else "RGBA" if px.mode == "PA" or px.has_transparency_data else "RGB")
    note = t("processor.shrunk", w=size[0], h=size[1], new_w=target[0], new_h=target[1])
    if note not in notes:
        notes.append(note)
    # Pillow cannot reduce 16-bit images by a whole number first: they are resampled in one step.
    gap = None if px.mode.startswith("I;16") else RESIZE_GAP
    return px.resize(target, Image.Resampling.LANCZOS, reducing_gap=gap)


def _draft(im: Image.Image, orientation: int, edit: Edit | None, max_side: int) -> tuple[int, int] | None:
    """For a JPEG that will shrink a lot: has libjpeg decode it at 1/2, 1/4 or 1/8 of its size (much
    faster), still at least as large as the result. Not with a crop or straightening, whose boxes are
    measured on the full image. Returns the full-size result's size (before shrinking), or None."""
    if not max_side or (edit is not None and (edit.crop is not None or edit.angle)):
        return None
    swapped = (orientation in (5, 6, 7, 8)) != (edit is not None and edit.swaps_axes())
    size = (im.size[1], im.size[0]) if swapped else im.size
    target = _target_size(size, max_side)
    if target is None:
        return None
    im.draft(im.mode, (target[1], target[0]) if swapped else target)
    return size


# ------------------------------------------------------------- keeping the capture date


def _add_date(result: ContentResult, date: bytes, taken: datetime) -> None:
    """Embeds the date-only EXIF (`keep_date`) in the stripped output. HEIC and AVIF got it from
    their encoder (`_strip_bmff`); GIF and BMP cannot carry EXIF, so only the file's date keeps it."""
    when = date_time(taken)
    fmt = result.fmt
    if fmt in (Fmt.GIF, Fmt.BMP):
        result.notes.append(t("processor.date_file_only", when=when, fmt=DISPLAY_NAME[fmt]))
        return
    if fmt == Fmt.JPEG:
        result.data = jpeg_meta.insert_segments(result.data, [(jpeg_meta.APP1, date)])
    elif fmt == Fmt.PNG:
        result.data = png_meta.with_exif(result.data, strip_exif_header(date))
    elif fmt == Fmt.WEBP:
        result.data = webp_meta.with_exif(result.data, strip_exif_header(date))
    elif fmt == Fmt.TIFF:
        result.data = tiff_meta.with_exif(result.data, strip_exif_header(date))
    result.date_exif = date
    result.notes.append(t("processor.date_kept", when=when))


# ------------------------------------------------------------------ converting to JPG


def _convert_to_jpeg(data: bytes, fmt: Fmt, opts: ContentOptions, edit: Edit | None = None) -> ContentResult:
    notes: list[str] = []
    im = _open_loaded(data)
    frames = getattr(im, "n_frames", 1)
    if frames > 1:
        notes.append(
            t("processor.multi_converted")
            if fmt == Fmt.HEIF
            else t("processor.animated_converted")
        )
    icc = _effective_icc(im, data, fmt, notes)
    exif_raw = xmp_raw = None
    if not opts.strip_metadata:
        exif_raw = _source_exif(im, fmt)
        xmp_raw = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
    had_alpha = im.has_transparency_data
    bit_depth = im.info.get("bit_depth", 8)

    px = edits.apply(to_color_mode(apply_orientation(im)), edit)
    target = _target_size(px.size, opts.max_side)  # None: within the size limit
    _check_jpeg_size(target or px.size)
    keep_icc = not opts.strip_metadata and colors.profile_matches(icc, _base_mode(px.mode))
    kept = None
    if opts.strip_metadata and icc and colors.needs_conversion(icc, px.mode):
        kept = _kept_profile(icc, px.mode, opts.keep_color_profile)
    if kept is not None:
        notes.append(_kept_note(kept))
    elif not keep_icc and icc:
        converted = colors.needs_conversion(icc, px.mode)
        px, warning = colors.to_srgb(px, icc)
        if warning:
            notes.append(warning)
        elif converted:
            notes.append(_color_note(icc, opts.keep_color_profile))
    elif px.mode in ("CMYK", "LAB"):
        px, _ = colors.to_srgb(px, None)
    px = _shrink(flatten(_tone(px, edit)), opts.max_side, notes)
    if had_alpha:
        notes.append(t("processor.flattened"))
    if isinstance(bit_depth, int) and bit_depth > 8:
        notes.append(t("processor.depth_to_jpeg", bits=bit_depth))
    clean = clean_copy(px)

    kwargs = {
        "quality": opts.jpeg_quality,
        "optimize": True,
        "subsampling": _chroma_for(fmt, data, im),
    }
    if kept is not None:
        kwargs["icc_profile"] = kept.icc
    if not opts.strip_metadata:
        if keep_icc:
            kwargs["icc_profile"] = icc
        exif = _prepare_exif(exif_raw, notes, clean.size if edit or target else None)
        if exif:
            kwargs["exif"] = exif
        xmp = _prepare_xmp(xmp_raw, notes)
        if xmp:
            kwargs["xmp"] = xmp
    out = _save(clean, "JPEG", **kwargs)
    return ContentResult(out, Fmt.JPEG, CONVERTED, notes, clean.size, kept.icc if kept is not None else None)


def _chroma_for(fmt: Fmt, data: bytes, im: Image.Image) -> str:
    """Chooses the chroma subsampling that preserves the source's color resolution.

    Lossless sources (PNG, GIF, BMP, TIFF, lossless WebP) mostly hold screenshots or
    graphics, where 4:2:0 blurs the edges of colored text. HEIC/AVIF/lossy WebP photos are
    usually stored as 4:2:0 already, so using 4:2:0 loses nothing and makes the file smaller.
    """
    if fmt in (Fmt.PNG, Fmt.GIF, Fmt.BMP, Fmt.TIFF):
        return "4:4:4"
    if fmt == Fmt.WEBP:
        return "4:4:4" if webp_meta.is_lossless(data) else "4:2:0"
    if fmt == Fmt.HEIF and im.info.get("chroma") == 444:
        return "4:4:4"
    return "4:2:0"


def _bmp_icc(data: bytes, fmt: Fmt) -> bytes | None:
    if fmt != Fmt.BMP:
        return None
    cstype, profile = bmp_meta.color_profile(data)
    return profile if cstype == bmp_meta.LCS_EMBEDDED else None


def _effective_icc(im: Image.Image, data: bytes, fmt: Fmt, notes: list[str]) -> bytes | None:
    """Profile describing the image's color space: the embedded ICC profile, otherwise an
    equivalent profile generated from HEIC's 'nclx' codes."""
    icc = im.info.get("icc_profile") or _bmp_icc(data, fmt)
    if icc:
        return icc
    nclx = im.info.get("nclx_profile")
    if isinstance(nclx, dict):
        transfer = nclx.get("transfer_characteristics") or 2
        if transfer in iccgen.HDR_TRANSFERS:
            notes.append(t("processor.hdr_image"))
        return iccgen.nclx_profile(nclx.get("color_primaries") or 2, transfer)
    return None


def source_profile(im: Image.Image, data: bytes, fmt: Fmt) -> bytes | None:
    """Profile describing the image's color space (embedded ICC, HEIC 'nclx' or, for JPEG,
    the Adobe RGB marker in EXIF). The large preview converts colors to sRGB with it too, so
    it looks the same as the processed result."""
    icc = _effective_icc(im, data, fmt, [])
    if icc is None and fmt == Fmt.JPEG:
        try:
            icc = _implicit_jpeg_profile(jpeg_meta.parse(data))
        except Exception:  # noqa: BLE001 - broken EXIF: treated as no profile
            icc = None
    return icc


def _implicit_jpeg_profile(layout) -> bytes | None:
    """Cameras shooting in Adobe RGB embed no profile; they only put an "R03" (Adobe RGB)
    marker in EXIF. Stripping EXIF loses this information."""
    raw = layout.exif()
    ex = load_exif(raw) if raw else None
    if ex is None:
        return None
    try:
        interop = ex.get_ifd(ExifTags.IFD.Interop)
        color_space = ex.get_ifd(ExifTags.IFD.Exif).get(0xA001)
    except Exception:  # noqa: BLE001
        return None
    index = interop.get(0x0001)
    if isinstance(index, bytes):
        index = index.decode("ascii", "ignore")
    if isinstance(index, str) and index.strip("\x00 ").upper() == "R03" and color_space != 1:
        return iccgen.adobe_rgb_profile()
    return None


def _source_exif(im: Image.Image, fmt: Fmt) -> bytes | None:
    if fmt == Fmt.TIFF:
        ex = im.getexif()
        out = Image.Exif()
        skip = tiff_meta.STRUCTURAL | {0x8769, 0x8825, 0xA005, 700, 33723, 34377, 34675, 37724}
        for tag, value in ex.items():
            if tag not in skip:
                out[tag] = value
        for ifd_tag in (0x8769, 0x8825):
            sub = ex.get_ifd(ifd_tag)
            if sub:
                out[ifd_tag] = sub
        return out.tobytes() if len(out) else None
    raw = im.info.get("exif")
    return bytes(raw) if isinstance(raw, (bytes, bytearray)) and raw else None


def _prepare_exif(raw: bytes | None, notes: list[str], edited_size: tuple[int, int] | None = None) -> bytes | None:
    """Prepares the source EXIF for the JPEG: the orientation is set to 1 (the rotation was
    applied to the pixels); all other bytes, MakerNote offsets included, are kept.
    `edited_size`: the new size if the photo was edited or shrunk; the old thumbnail is removed too."""
    if not raw:
        return None
    exif = with_exif_header(raw)
    exif = for_edited(exif, edited_size) if edited_size else patch_orientation(exif, 1)
    if len(exif) > MAX_APP_PAYLOAD:
        notes.append(t("processor.exif_too_big"))
        return None
    return exif


def _xmp_upright(raw) -> bytes:
    """Sets the orientation in XMP to 1 (the byte length does not change)."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    raw = re.sub(rb'tiff:Orientation="\d"', b'tiff:Orientation="1"', bytes(raw))
    return re.sub(rb"<tiff:Orientation>\d</tiff:Orientation>", b"<tiff:Orientation>1</tiff:Orientation>", raw)


def _prepare_xmp(raw, notes: list[str]) -> bytes | None:
    if not raw:
        return None
    raw = _xmp_upright(raw)
    if len(raw) > MAX_APP_PAYLOAD - 29:
        notes.append(t("processor.xmp_too_big"))
        return None
    return raw


# ----------------------------------------------------------------------- RAW → JPG


def _convert_raw(data: bytes, opts: ContentOptions, edit: Edit | None = None) -> ContentResult:
    """Converts a RAW to JPG: either the camera's embedded full-size JPEG (stripped losslessly,
    or re-encoded with its own quantization tables if there is an edit or it is shrunk) or a
    full-resolution development of the RAW data with LibRaw (always, if the exposure was adjusted)."""
    parts = raw_meta.exif_parts(data)
    rendering = rawdev.render(data, opts.raw_mode, edit.exposure if edit is not None else 0.0)
    notes = list(rendering.notes)
    exif = xmp = None
    if not opts.strip_metadata:
        exif, had_makernote = raw_meta.exif_for_jpeg(data)
        if exif and len(exif) > MAX_APP_PAYLOAD:
            notes.append(t("processor.exif_too_big"))
            exif = None
        if had_makernote:
            notes.append(t("processor.no_makernote"))
        xmp = _prepare_xmp(raw_meta.xmp_of(data), notes)

    if rendering.jpeg is not None:
        preview = rendering.jpeg
        layout = jpeg_meta.parse(preview)
        # Trust the preview's own orientation if it has one (e.g. Fuji RAF), otherwise the RAW's.
        orientation = None if layout.orientation() else rendering.orientation
        icc = None
        if raw_meta.is_adobe_rgb(parts) and not layout.icc_profile():
            icc = iccgen.adobe_rgb_profile()
        stripped = _strip_jpeg(preview, orientation=orientation, icc=icc, edit=edit, keep_profile=opts.keep_color_profile,
                               max_side=opts.max_side)
        _verify(preview, stripped, ContentOptions(strip_metadata=True))
        notes += [n for n in stripped.notes if n not in notes]
        out = stripped.data
        frame = layout.frame()
        shrunk = frame is not None and _target_size((frame.width, frame.height), opts.max_side) is not None
        segments = []
        if exif:
            if edit is not None or shrunk:
                exif = for_edited(exif, stripped.expected_size)
            segments.append((jpeg_meta.APP1, exif))
        if xmp:
            segments.append((jpeg_meta.APP1, jpeg_meta.XMP_ID + xmp))
        if segments:
            out = jpeg_meta.insert_segments(out, segments)
        return ContentResult(out, Fmt.JPEG, CONVERTED, notes, stripped.expected_size, stripped.profile)

    edited = edits.apply(rendering.image, edit)
    image = _shrink(edited, opts.max_side, notes)
    _check_jpeg_size(image.size)
    shrunk = image.size != edited.size
    clean = clean_copy(image)
    # An image developed from RAW has full color resolution: 4:4:4 keeps it.
    kwargs = {"quality": opts.jpeg_quality, "optimize": True, "subsampling": "4:4:4"}
    if exif:
        kwargs["exif"] = for_edited(exif, clean.size) if edit is not None or shrunk else exif
    if xmp:
        kwargs["xmp"] = xmp
    out = _save(clean, "JPEG", **kwargs)
    return ContentResult(out, Fmt.JPEG, CONVERTED, notes, clean.size)


# ------------------------------------------------------- stripping in the same format


def _strip(data: bytes, fmt: Fmt, edit: Edit | None = None, keep_profile: bool = False, max_side: int = 0,
           date: bytes | None = None) -> ContentResult:
    """`date`: the date-only EXIF to keep; only HEIC/AVIF take it here (from their encoder), the other
    formats get it afterwards (`_add_date`)."""
    if fmt == Fmt.JPEG:
        return _strip_jpeg(data, edit=edit, keep_profile=keep_profile, max_side=max_side)
    if fmt == Fmt.PNG:
        return _strip_png(data, edit, keep_profile, max_side)
    if fmt == Fmt.WEBP:
        return _strip_webp(data, edit, keep_profile, max_side)
    if fmt == Fmt.GIF:
        if edit is not None or not _within(data, max_side):
            return _resave(data, fmt, edit, keep=False, max_side=max_side)
        return ContentResult(gif_meta.strip(data), Fmt.GIF, LOSSLESS)
    if fmt == Fmt.TIFF:
        return _strip_tiff(data, edit, keep_profile, max_side)
    if fmt == Fmt.BMP:
        return _strip_bmp(data, edit, max_side)
    return _strip_bmff(data, fmt, edit, keep_profile, max_side, date)


def _jpeg_icc_segment(icc: bytes) -> tuple[int, bytes]:
    """An APP2 ICC_PROFILE segment (one chunk: the clean profiles are a few KB)."""
    return jpeg_meta.APP2, jpeg_meta.ICC_ID + b"\x01\x01" + icc


def _strip_jpeg(data: bytes, orientation: int | None = None, icc: bytes | None = None,
                edit: Edit | None = None, keep_profile: bool = False, max_side: int = 0) -> ContentResult:
    """`orientation`/`icc`: values to use instead of the file's own (for a camera JPEG taken
    from a RAW, the orientation and color space are recorded in the RAW)."""
    layout = jpeg_meta.parse(data)
    frame = layout.frame()
    if frame is None:
        raise ProcessError(t("processor.jpeg_no_frame"))
    if orientation is None:
        orientation = layout.orientation() or 1
    if icc is None:
        icc = layout.icc_profile() or _implicit_jpeg_profile(layout)
    mode = {1: "L", 3: "RGB", 4: "CMYK"}.get(frame.components, "RGB")
    convert_colors = colors.needs_conversion(icc, mode)
    kept = _kept_profile(icc, mode, keep_profile) if convert_colors else None
    if kept is not None:
        convert_colors = False
    notes: list[str] = [_kept_note(kept)] if kept is not None else []
    fits = _target_size((frame.width, frame.height), max_side) is None
    if orientation == 1 and not convert_colors and edit is None and fits:
        out = jpeg_meta.strip(data)
        if kept is not None:  # the pixels stay untouched: only the clean profile is added
            out = jpeg_meta.insert_segments(out, [_jpeg_icc_segment(kept.icc)])
        return ContentResult(out, Fmt.JPEG, LOSSLESS, notes, profile=kept.icc if kept is not None else None)

    # The orientation lives in EXIF, so without EXIF the photo would appear sideways or
    # upside down; that is why the rotation is applied to the pixels.
    if orientation != 1:
        notes.append(t(ORIENTED_NOTE))
    if convert_colors and keep_profile and mode == "RGB":
        notes.append(_color_note(icc, keep=True))
        convert_note = False
    else:
        convert_note = True
    out, size = _reencode_jpeg(data, mode, frame.progressive, orientation, edit, icc if convert_colors else None,
                               notes, embed_icc=kept.icc if kept is not None else None, convert_note=convert_note,
                               max_side=max_side)
    return ContentResult(out, Fmt.JPEG, REENCODED, notes, size, kept.icc if kept is not None else None)


# Effect of the PIL transposes on the axes: (axes swapped?, new x flipped?, new y flipped?)
_AXES = {
    Image.Transpose.FLIP_LEFT_RIGHT: (False, True, False),
    Image.Transpose.FLIP_TOP_BOTTOM: (False, False, True),
    Image.Transpose.ROTATE_180: (False, True, True),
    Image.Transpose.ROTATE_90: (True, False, True),
    Image.Transpose.ROTATE_270: (True, True, False),
    Image.Transpose.TRANSPOSE: (True, False, False),
    Image.Transpose.TRANSVERSE: (True, True, True),
}


def _block_grid(im: Image.Image, orientation: int, turn: Image.Transpose | None) -> list[tuple[int, int]]:
    """Where the source's JPEG block (MCU) grid lies after the orientation and the edit's flip and 90°
    rotation (`turn`, see `Edit.turn`; a flip mirrors the x axis before the rotation): (offset, step)
    for each axis. Grid lines are at x ≡ offset (mod step)."""
    layers = getattr(im, "layer", None) or []
    steps = (8 * max((layer[1] for layer in layers), default=1), 8 * max((layer[2] for layer in layers), default=1))
    axes = [(0, False), (1, False)]  # output axis ← (source axis, flipped?)
    for op in (ORIENTATION_TRANSPOSE.get(orientation), turn):
        if op is None:
            continue
        swap, flip_x, flip_y = _AXES[op]
        ax, ay = (axes[1], axes[0]) if swap else (axes[0], axes[1])
        axes = [(ax[0], ax[1] != flip_x), (ay[0], ay[1] != flip_y)]
    dims = im.size
    return [((dims[a] % steps[a]) if flipped else 0, steps[a]) for a, flipped in axes]


def _snap(pos: int, size: int, limit: int, grid: tuple[int, int]) -> int:
    """Aligns a crop edge to the block grid (to the preceding grid line when possible)."""
    offset, step = grid
    snapped = pos - (pos - offset) % step
    if snapped < 0:
        snapped += step
    return snapped if 0 <= snapped and snapped + size <= limit else pos


def _crop_on_blocks(im: Image.Image, px: Image.Image, orientation: int, edit: Edit) -> Image.Image:
    """For a crop without straightening, aligns the box's top-left corner to the source's block
    grid (it shifts by at most one block, usually within 16 pixels; the size stays the same).
    Blocks re-encoded with the same quantization tables then coincide with the source's, and
    quality is preserved almost losslessly; with an unaligned crop every block is requantized."""
    px = edits.apply(px, Edit(quarter=edit.quarter, mirror=edit.mirror))
    left, top, w, h = edit.pixels_on_canvas(*px.size)
    gx, gy = _block_grid(im, orientation, edit.turn())
    left = _snap(int(left), w, px.width, gx)
    top = _snap(int(top), h, px.height, gy)
    return px.crop((left, top, left + w, top + h))


def _reencode_jpeg(data: bytes, mode: str, progressive: bool, orientation: int, edit: Edit | None,
                   convert_icc: bytes | None, notes: list[str], keep_cmyk: bool = False,
                   embed_icc: bytes | None = None, convert_note: bool = True,
                   max_side: int = 0) -> tuple[bytes, tuple[int, int]]:
    """Re-encodes a JPEG with the orientation, edit and color conversion applied (and shrunk to
    `max_side`). To preserve quality, the source's own quantization tables (transposed if the axes
    swapped) and chroma subsampling are reused; the crop is aligned to the block grid. `embed_icc`: a
    clean profile to embed (the colors are then not converted)."""
    im = open_image(io.BytesIO(data))
    qtables = getattr(im, "quantization", None) or {}
    sampling = JpegImagePlugin.get_sampling(im)
    full = _draft(im, orientation, edit, max_side)
    im.load()
    px = orient(im, orientation)
    if edit is not None and edit.crop is not None and not edit.angle:
        px = _crop_on_blocks(im, px, orientation, edit)
    else:
        px = edits.apply(px, edit)
    if convert_icc is not None:
        px, warning = colors.to_srgb(to_color_mode(px), convert_icc)
        if warning or convert_note:
            notes.append(warning or _color_note(convert_icc))
    px = _tone(px, edit)
    if not (keep_cmyk and px.mode == "CMYK"):
        px = flatten(px)
    clean = clean_copy(_shrink(px, max_side, notes, full))

    kwargs: dict = {"optimize": True, "progressive": progressive}
    if embed_icc is not None:
        kwargs["icc_profile"] = embed_icc
    same_model = (mode, clean.mode) in (("L", "L"), ("RGB", "RGB"))
    if qtables and same_model:
        tables = [list(qtables[k]) for k in sorted(qtables)]
        swapped = (orientation in (5, 6, 7, 8)) != (edit is not None and edit.swaps_axes())
        if swapped:
            tables = [transpose_qtable(t) for t in tables]
            if sampling == 1:  # rotated 4:2:2 becomes 4:4:0; use 4:4:4 so nothing is lost
                sampling = 0
        kwargs["qtables"] = tables
        if clean.mode == "RGB":
            kwargs["subsampling"] = sampling if sampling in (0, 1, 2) else 0
    else:
        kwargs["quality"] = FALLBACK_JPEG_QUALITY
        if clean.mode != "CMYK":
            kwargs["subsampling"] = 0
    return _save(clean, "JPEG", **kwargs), clean.size


def _png_mode_hint(data: bytes) -> str:
    color_type = data[25] if len(data) > 25 else 2
    return "L" if color_type in (0, 4) else "RGB"


def _strip_png(data: bytes, edit: Edit | None = None, keep_profile: bool = False, max_side: int = 0) -> ContentResult:
    icc = png_meta.icc_profile(data)
    orientation = png_meta.orientation(data) or 1
    mode = _png_mode_hint(data)
    convert_colors = colors.needs_conversion(icc, mode)
    kept = _kept_profile(icc, mode, keep_profile) if convert_colors else None
    hidden = png_meta.hidden_idat_data(data)
    if orientation == 1 and not convert_colors and not hidden and edit is None and _within(data, max_side):
        return ContentResult(png_meta.strip(data), Fmt.PNG, LOSSLESS)
    result = _reencode_same(data, Fmt.PNG, icc, orientation, convert_colors and kept is None, edit,
                            kept=kept, keep_requested=keep_profile, max_side=max_side)
    if hidden:
        result.notes.append(t("processor.hidden_idat"))
    return result


def _strip_webp(data: bytes, edit: Edit | None = None, keep_profile: bool = False, max_side: int = 0) -> ContentResult:
    icc = webp_meta.icc_profile(data)
    orientation = webp_meta.orientation(data) or 1
    convert_colors = colors.needs_conversion(icc, "RGB")
    kept = _kept_profile(icc, "RGB", keep_profile) if convert_colors else None
    if orientation == 1 and not convert_colors and edit is None and _within(data, max_side):
        return ContentResult(webp_meta.strip(data), Fmt.WEBP, LOSSLESS)
    return _reencode_same(data, Fmt.WEBP, icc, orientation, convert_colors and kept is None, edit,
                          kept=kept, keep_requested=keep_profile, max_side=max_side)


def _reencode_same(data: bytes, fmt: Fmt, icc, orientation: int, convert_colors: bool,
                   edit: Edit | None = None, kept: colors.StandardProfile | None = None,
                   keep_requested: bool = False, max_side: int = 0) -> ContentResult:
    """Re-encodes a PNG/WebP in the same format (when the orientation, color profile, an edit or
    shrinking requires it). For PNG and lossless WebP this is lossless. For animated images the
    edit is applied to every frame. `kept`: a clean standard profile embedded instead of converting."""
    notes: list[str] = []
    im = _open_loaded(data)
    if orientation != 1:
        notes.append(t(ORIENTED_NOTE))
    if kept is not None:
        notes.append(_kept_note(kept))
    elif convert_colors:
        notes.append(_color_note(icc, keep_requested))

    def prepare(frame: Image.Image) -> Image.Image:
        px = apply_orientation(frame) if orientation != 1 else frame.copy()
        px = edits.apply(px, edit)
        if convert_colors:
            px, warning = colors.to_srgb(to_color_mode(px), icc)
            if warning and warning not in notes:
                notes.append(warning)
        return clean_copy(_shrink(_tone(px, edit), max_side, notes))

    lossless_webp = fmt == Fmt.WEBP and webp_meta.is_lossless(data)
    params: dict = {}
    if fmt == Fmt.WEBP:
        params = {"lossless": True, "quality": 100, "exact": True} if lossless_webp else {"quality": WEBP_QUALITY}
        if not lossless_webp:
            notes.append(t("processor.webp_recompressed", quality=percent(WEBP_QUALITY)))
    if kept is not None:
        params["icc_profile"] = kept.icc
    target = "PNG" if fmt == Fmt.PNG else "WEBP"
    profile = kept.icc if kept is not None else None

    if getattr(im, "is_animated", False):
        frames, durations = [], []
        for frame in ImageSequence.Iterator(im):
            durations.append(frame.info.get("duration", 100))
            frames.append(prepare(frame.convert("RGBA")))
        out = _save(
            frames[0], target, save_all=True, append_images=frames[1:],
            duration=durations, loop=im.info.get("loop", 0), **params,
        )
        return ContentResult(out, fmt, REENCODED, notes, frames[0].size, profile)
    clean = prepare(im)
    out = _save(clean, target, **params)
    return ContentResult(out, fmt, REENCODED, notes, clean.size, profile)


def _strip_tiff(data: bytes, edit: Edit | None = None, keep_profile: bool = False, max_side: int = 0) -> ContentResult:
    notes: list[str] = []
    im = open_image(io.BytesIO(data))
    compression = im.info.get("compression", "raw")
    loaded: list[tuple[Image.Image, bytes | None]] = []
    for index in range(getattr(im, "n_frames", 1)):
        im.seek(index)
        im.load()  # for TIFF, Pillow applies the orientation to the pixels while loading
        loaded.append((edits.apply(apply_orientation(im), edit), im.info.get("icc_profile")))
    # A clean profile is kept only if every page has the same standard wide color space (one profile for the file).
    standards = {_kept_profile(icc, px.mode, keep_profile) if colors.needs_conversion(icc, px.mode) else None
                 for px, icc in loaded}
    kept = standards.pop() if len(standards) == 1 else None
    if kept is not None:
        notes.append(_kept_note(kept))
    pages: list[Image.Image] = []
    for px, icc in loaded:
        if kept is None and colors.needs_conversion(icc, px.mode):
            px, warning = colors.to_srgb(to_color_mode(px), icc)
            note = warning or _color_note(icc, keep_profile)
            if note not in notes:
                notes.append(note)
        pages.append(clean_copy(_shrink(_tone(px, edit), max_side, notes)))
    target = _tiff_compression(compression, pages, notes)
    extra = {"icc_profile": kept.icc} if kept is not None else {}
    out = _save(pages[0], "TIFF", compression=target, save_all=len(pages) > 1, append_images=pages[1:], **extra)
    return ContentResult(out, Fmt.TIFF, REENCODED, notes, pages[0].size, kept.icc if kept is not None else None)


def _strip_bmp(data: bytes, edit: Edit | None = None, max_side: int = 0) -> ContentResult:
    cstype, profile = bmp_meta.color_profile(data)
    profiled = cstype in (bmp_meta.LCS_LINKED, bmp_meta.LCS_EMBEDDED)
    if not profiled and edit is None and _within(data, max_side):
        return ContentResult(data[: bmp_meta.content_end(data)], Fmt.BMP, LOSSLESS)
    notes: list[str] = []
    im = _open_loaded(data)
    px = edits.apply(im, edit)
    if profiled:
        px = to_color_mode(px)
    if cstype == bmp_meta.LCS_EMBEDDED and colors.needs_conversion(profile, px.mode):
        px, warning = colors.to_srgb(px, profile)
        notes.append(warning or _color_note(profile))
    clean = clean_copy(_shrink(_tone(px, edit), max_side, notes))
    return ContentResult(_save(clean, "BMP"), Fmt.BMP, REENCODED, notes, clean.size)


def _strip_bmff(data: bytes, fmt: Fmt, edit: Edit | None = None, keep_profile: bool = False, max_side: int = 0,
                date: bytes | None = None) -> ContentResult:
    """`date`: the date-only EXIF to keep (`keep_date`), handed to the encoder."""
    notes: list[str] = []
    im = _open_loaded(data)
    if getattr(im, "n_frames", 1) > 1:
        notes.append(t("processor.multi_kept"))
    icc = _effective_icc(im, data, fmt, notes)
    bit_depth = im.info.get("bit_depth", 8)
    px = edits.apply(to_color_mode(apply_orientation(im)), edit)
    kept = _kept_profile(icc, px.mode, keep_profile) if colors.needs_conversion(icc, px.mode) else None
    if kept is not None:
        notes.append(_kept_note(kept))
    elif colors.needs_conversion(icc, px.mode):
        px, warning = colors.to_srgb(px, icc)
        notes.append(warning or _color_note(icc, keep_profile))
    elif px.mode in ("CMYK", "LAB"):
        px, _ = colors.to_srgb(px, None)
    clean = clean_copy(_shrink(_tone(px, edit), max_side, notes))
    extra: dict = {"icc_profile": kept.icc} if kept is not None else {}
    if date is not None:
        extra["exif"] = date
    kinds = scan_bytes(data, fmt, enrich=False).kinds()
    if Kind.EXTRA_IMAGE in kinds:
        notes.append(t("processor.aux_removed"))
    if isinstance(bit_depth, int) and bit_depth > 8:
        notes.append(t("processor.depth_8", bits=bit_depth))
    name = DISPLAY_NAME[fmt]
    if fmt == Fmt.HEIF:
        chroma = im.info.get("chroma", 420)
        # x265 "medium": ~2.3x faster than the default (slow), same quality, file ~1.5% larger (measured).
        out = _save(
            clean, "HEIF", quality=HEIF_QUALITY, chroma=chroma if chroma in (420, 422, 444) else 420,
            enc_params={"preset": "medium"}, **extra,
        )
        quality = HEIF_QUALITY
    else:
        # libaom speed 8: ~3.5x faster than speed 6, a 0.2 dB difference (measured).
        out = _save(clean, "AVIF", quality=AVIF_QUALITY, speed=8, **extra)
        quality = AVIF_QUALITY
    notes.append(
        t("processor.heavy_recompressed", name=name, quality=percent(quality))
    )
    return ContentResult(out, fmt, REENCODED, notes, clean.size, kept.icc if kept is not None else None)


# --------------------------------------------------- editing with metadata kept


def _edit_keeping_metadata(data: bytes, fmt: Fmt, edit: Edit | None, max_side: int = 0) -> ContentResult:
    """A photo edited or shrunk while metadata stripping is off: saved again in the same format with
    its metadata kept (the orientation becomes 1; thumbnails of the old image are removed)."""
    if fmt == Fmt.JPEG:
        return _edit_jpeg_keep(data, edit, max_side)
    return _resave(data, fmt, edit, keep=True, max_side=max_side)


def _edit_jpeg_keep(data: bytes, edit: Edit | None, max_side: int = 0) -> ContentResult:
    """JPEG: the pixels are re-encoded with the source's quantization tables; EXIF, XMP, IPTC,
    the color profile and comments are added back as segments. Colors are not converted (the
    profile is kept too, so the look is the same). Thumbnails (EXIF IFD1, JFXX, Photoshop),
    extra images (MPF, trailing data) and content credentials (C2PA) belong to the old image
    and are removed."""
    layout = jpeg_meta.parse(data)
    frame = layout.frame()
    if frame is None:
        raise ProcessError(t("processor.jpeg_no_frame"))
    orientation = layout.orientation() or 1
    mode = {1: "L", 3: "RGB", 4: "CMYK"}.get(frame.components, "RGB")
    notes: list[str] = []
    out, size = _reencode_jpeg(data, mode, frame.progressive, orientation, edit, None, notes, keep_cmyk=True,
                               max_side=max_side)
    segments = _kept_jpeg_segments(layout, size, notes)
    if segments:
        out = jpeg_meta.insert_segments(out, segments)
    return ContentResult(out, Fmt.JPEG, REENCODED, notes, size)


def _kept_jpeg_segments(layout, size: tuple[int, int], notes: list[str]) -> list[tuple[int, bytes]]:
    kept: list[tuple[int, bytes]] = []
    for seg in layout.segments:
        m = seg.marker
        p = layout.payload(seg)
        if m == jpeg_meta.APP1 and p.startswith(b"Exif\x00"):
            exif = for_edited(p, size)
            if len(exif) <= MAX_APP_PAYLOAD:
                kept.append((m, exif))
        elif m == jpeg_meta.APP1 and p.startswith(jpeg_meta.XMP_ID):
            kept.append((m, jpeg_meta.XMP_ID + _xmp_upright(p[len(jpeg_meta.XMP_ID):])))
        elif m == jpeg_meta.APP1 and p.startswith(jpeg_meta.XMP_EXT_ID):
            kept.append((m, p))
        elif m == jpeg_meta.APP2 and p.startswith(jpeg_meta.ICC_ID):
            kept.append((m, p))
        elif m == jpeg_meta.APP13 and p.startswith(b"Photoshop 3.0\x00"):
            kept.append((m, jpeg_meta.irb_without_thumbnails(p)))
        elif m == jpeg_meta.COM:
            kept.append((m, p))
    return kept


def _png_texts(im: Image.Image) -> PngInfo:
    """PNG text chunks (tEXt/zTXt/iTXt); the orientation in XMP is set to 1."""
    info = PngInfo()
    for key, value in (getattr(im, "text", None) or {}).items():
        if key == "XML:com.adobe.xmp":
            value = iTXt(_xmp_upright(value).decode("utf-8", "replace"), getattr(value, "lang", ""),
                         getattr(value, "tkey", ""))
        if isinstance(value, iTXt):
            info.add_itxt(key, value, value.lang or "", value.tkey or "")
        else:
            info.add_text(key, value)
    return info


def _resave(data: bytes, fmt: Fmt, edit: Edit | None, keep: bool, max_side: int = 0) -> ContentResult:
    """Saves the edited (or shrunk) image again in the same format; for animated/multi-page files
    the edit is applied to every frame.

    `keep`: keep the metadata — EXIF (orientation 1, old thumbnail removed), XMP, the color
    profile, PNG text chunks, the GIF comment. If the format cannot carry the color profile
    (GIF, BMP), colors are converted to sRGB so that the look does not change.
    """
    notes: list[str] = []
    im = _open_loaded(data)
    info = dict(im.info)
    frames = getattr(im, "n_frames", 1)
    multi = frames > 1 and fmt in (Fmt.PNG, Fmt.WEBP, Fmt.GIF, Fmt.TIFF)
    if frames > 1 and not multi:
        notes.append(t("processor.multi_kept"))
    if fmt in (Fmt.HEIF, Fmt.AVIF):
        icc = _effective_icc(im, data, fmt, notes)  # HEIC's 'nclx' color info is carried as an equivalent profile
    else:
        icc = info.get("icc_profile") or _bmp_icc(data, fmt)
    carry_icc = bool(keep and icc and fmt not in (Fmt.GIF, Fmt.BMP))
    exif_raw = _source_exif(im, fmt) if keep else None
    xmp_raw = (info.get("xmp") or info.get("XML:com.adobe.xmp")) if keep else None
    texts = _png_texts(im) if keep and fmt == Fmt.PNG else None
    bit_depth = info.get("bit_depth", 8)

    def prepare(frame: Image.Image) -> Image.Image:
        px = apply_orientation(frame)
        if multi and fmt != Fmt.TIFF:
            px = px.convert("RGBA")  # frames as composited
        px = edits.apply(px, edit)
        if fmt in (Fmt.HEIF, Fmt.AVIF):
            px = to_color_mode(px)
        if icc and not carry_icc and colors.needs_conversion(icc, px.mode):
            px, warning = colors.to_srgb(to_color_mode(px), icc)
            note = warning or _color_note(icc)
            if note not in notes:
                notes.append(note)
        return clean_copy(_shrink(_tone(px, edit), max_side, notes))

    pages: list[Image.Image] = []
    durations: list[int] = []
    if multi:
        for frame in ImageSequence.Iterator(im):
            durations.append(frame.info.get("duration", 100))
            pages.append(prepare(frame))
    else:
        pages.append(prepare(im))
    first = pages[0]

    params: dict = {}
    if multi:
        params.update(save_all=True, append_images=pages[1:])
        if fmt != Fmt.TIFF:
            params["duration"] = durations
            if "loop" in info:
                params["loop"] = info["loop"]
    exif = for_edited(with_exif_header(exif_raw), first.size) if exif_raw else None
    xmp = _xmp_upright(xmp_raw) if xmp_raw else None
    if carry_icc:
        params["icc_profile"] = icc
    if fmt == Fmt.PNG:
        target = "PNG"
        if keep:
            params["pnginfo"] = texts
            if exif:
                params["exif"] = exif
            if "dpi" in info:
                params["dpi"] = info["dpi"]
    elif fmt == Fmt.WEBP:
        target = "WEBP"
        if webp_meta.is_lossless(data):
            params.update(lossless=True, quality=100, exact=True)
        else:
            params["quality"] = WEBP_QUALITY
            notes.append(t("processor.webp_recompressed", quality=percent(WEBP_QUALITY)))
        if exif:
            params["exif"] = exif
        if xmp:
            params["xmp"] = xmp
    elif fmt == Fmt.GIF:
        target = "GIF"
        if keep and info.get("comment"):
            params["comment"] = info["comment"]
        if multi:
            params["disposal"] = 2  # frames are full images; each one is drawn on a cleared background
    elif fmt == Fmt.TIFF:
        target = "TIFF"
        params["compression"] = _tiff_compression(info.get("compression", "raw"), pages, notes)
        if keep:
            tags = Image.Exif()
            if exif:
                tags.load(exif)
            if xmp:
                tags[700] = xmp
            if len(tags):
                params["exif"] = tags
            if "dpi" in info:
                params["dpi"] = info["dpi"]
    elif fmt == Fmt.BMP:
        target = "BMP"
    else:
        target = "HEIF" if fmt == Fmt.HEIF else "AVIF"
        if exif:
            params["exif"] = exif
        if xmp:
            params["xmp"] = xmp
        if fmt == Fmt.HEIF:
            chroma = info.get("chroma", 420)
            params.update(quality=HEIF_QUALITY, chroma=chroma if chroma in (420, 422, 444) else 420,
                          enc_params={"preset": "medium"})
        else:
            params.update(quality=AVIF_QUALITY, speed=8)
        quality = HEIF_QUALITY if fmt == Fmt.HEIF else AVIF_QUALITY
        notes.append(t("processor.recompressed", name=DISPLAY_NAME[fmt], quality=percent(quality)))
        if isinstance(bit_depth, int) and bit_depth > 8:
            notes.append(t("processor.depth_8", bits=bit_depth))
    out = _save(first, target, **params)
    return ContentResult(out, fmt, REENCODED, notes, first.size)


# ------------------------------------------------------------------ verification


def _verify(src: bytes, result: ContentResult, opts: ContentOptions) -> None:
    out = result.data
    if out is None:
        return
    if sniff(out[:64]) != result.fmt:
        raise ProcessError(t("processor.verify_format"))
    if opts.strip_metadata:
        leftovers = scan_bytes(out, result.fmt, enrich=False)
        signatures = residual_signatures(out)
        if result.profile is not None:
            # The clean standard profile was embedded on purpose; it must be exactly that one.
            if _embedded_icc(out, result.fmt) != result.profile or not colors.is_clean_profile(result.profile):
                raise ProcessError(t("processor.verify_profile"))
            leftovers.findings = [f for f in leftovers.findings if f.kind != Kind.ICC]
            signatures = [s for s in signatures if s != "ICC"]
        if result.date_exif is not None:
            # The date-only EXIF was embedded on purpose; it must be exactly that one, and the only one.
            if sum(f.kind == Kind.EXIF for f in leftovers.findings) != 1 or not _date_matches(out, result):
                raise ProcessError(t("processor.verify_date"))
            leftovers.findings = [f for f in leftovers.findings if f.kind != Kind.EXIF]
            signatures = [s for s in signatures if s != "EXIF"]
        if leftovers.findings or signatures:
            found = leftovers.describe() or ", ".join(signatures)
            raise ProcessError(t("processor.verify_leftover", found=found))
    try:
        decoded = _open_loaded(out)
    except Exception as exc:  # noqa: BLE001 - any decoding error is a verification failure
        raise ProcessError(t("processor.verify_decode", error=exc)) from exc
    if result.expected_size and decoded.size != result.expected_size:
        raise ProcessError(t("processor.verify_size"))
    if result.method == LOSSLESS and out != src:
        _assert_same_pixels(src, out)


_EXIF_SIGNATURES = (b"Exif\x00\x00II*\x00", b"Exif\x00\x00MM\x00*")


def _date_matches(data: bytes, result: ContentResult) -> bool:
    """Is the EXIF the output carries exactly the date-only EXIF embedded on purpose? The "Exif\\0\\0"
    signature may appear only where that EXIF is stored with it (once in JPEG, HEIC and AVIF)."""
    fmt = result.fmt
    expected = strip_exif_header(result.date_exif)
    headers = sum(data.count(sig) for sig in _EXIF_SIGNATURES)
    if headers != (1 if fmt in (Fmt.JPEG, Fmt.HEIF, Fmt.AVIF) else 0):
        return False
    if fmt == Fmt.TIFF:  # the Exif IFD is part of the file: its entries are compared (first page only)
        return (tiff_meta.count_tag(data, TAG_EXIF_IFD) == 1
                and tiff_meta.exif_entries(data) == tiff_meta.exif_entries(expected))
    if fmt == Fmt.JPEG:
        found = jpeg_meta.parse(data).exif()
    elif fmt == Fmt.PNG:
        found = png_meta.exif_block(data)
    elif fmt == Fmt.WEBP:
        found = webp_meta.exif_block(data)
    else:
        with open_image(io.BytesIO(data)) as im:
            found = im.info.get("exif")
    return found is not None and strip_exif_header(bytes(found)) == expected


def _embedded_icc(data: bytes, fmt: Fmt) -> bytes | None:
    """The ICC profile the file carries (read back for verification)."""
    if fmt == Fmt.JPEG:
        return jpeg_meta.parse(data).icc_profile()
    if fmt == Fmt.PNG:
        return png_meta.icc_profile(data)
    if fmt == Fmt.WEBP:
        return webp_meta.icc_profile(data)
    with open_image(io.BytesIO(data)) as im:
        return im.info.get("icc_profile")


def _pixels(im: Image.Image) -> bytes:
    if im.mode in ("P", "PA"):
        return im.convert("RGBA").tobytes()
    return im.tobytes()


def _assert_same_pixels(src: bytes, out: bytes) -> None:
    a = open_image(io.BytesIO(src))
    b = open_image(io.BytesIO(out))
    frames_a = getattr(a, "n_frames", 1)
    if frames_a != getattr(b, "n_frames", 1):
        raise ProcessError(t("processor.verify_frames"))
    for index in range(min(frames_a, 50)):
        if index:
            a.seek(index)
            b.seek(index)
        a.load()
        b.load()
        if a.size != b.size or a.mode != b.mode or _pixels(a) != _pixels(b):
            raise ProcessError(t("processor.verify_pixels"))
