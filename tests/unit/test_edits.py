"""Rotation, straightening and cropping: geometry, applying to pixels and processing in every format.
Also the non-geometric edits: RAW exposure and the video trim's model."""

from __future__ import annotations

import io
import math
import os

import builders as b
import numpy as np
import pytest
from oracle import exiv2_metadata, psnr
from PIL import (
    ExifTags,
    Image,
    ImageDraw,
    ImageFilter,
    ImageOps,
    ImageSequence,
    ImageStat,
)

from fotodit.core.dngwriter import make_dng
from fotodit.core.edits import (
    MAX_EXPOSURE,
    Edit,
    apply,
    apply_exposure,
    box_fits,
    clock,
    ev_text,
    exposure_curve,
    largest_box,
    max_half_height,
)
from fotodit.core.formats import Fmt
from fotodit.core.imaging import open_image
from fotodit.core.meta import Kind, residual_signatures, scan_bytes
from fotodit.core.meta import jpeg as jpeg_meta
from fotodit.core.meta.exif import drop_thumbnail, for_edited, read_orientation
from fotodit.core.photoinfo import read_info
from fotodit.core.planner import JobOptions, OutputOptions, Photo, build_plan
from fotodit.core.processor import (
    RAW_EDIT_NEEDS_JPEG,
    REENCODED,
    ContentOptions,
    ProcessError,
    process_bytes,
)
from fotodit.core.rawdev import RAW_DEVELOP
from fotodit.core.runner import Runner
from fotodit.i18n import t

STRIP = ContentOptions(strip_metadata=True)
TO_JPEG = ContentOptions(strip_metadata=True, convert_to_jpeg=True)
KEEP = ContentOptions()  # metadata stripping and conversion off: the edit only
KEEP_TO_JPEG = ContentOptions(convert_to_jpeg=True)

PHOTO = b.photo(320, 240)


def decode(data: bytes) -> Image.Image:
    im = open_image(io.BytesIO(data))
    im.load()
    return im


def corners(im: Image.Image) -> dict[str, str]:
    return {c: b.dominant(b.marker_color(im, c)) for c in ("tl", "tr", "bl", "br")}


def assert_clean(data: bytes, fmt: Fmt) -> None:
    res = scan_bytes(data, fmt, enrich=False)
    assert res.clean, res.describe()
    assert residual_signatures(data) == []


# ================================================================ edit model


def test_edit_normalization_and_identity():
    assert Edit().is_identity()
    assert Edit(quarter=4).is_identity() and Edit(quarter=-1).quarter == 3
    assert Edit(angle=0.02).angle == 0.0 and Edit(angle=90).angle == 45.0
    assert Edit(crop=(0, 0, 1, 1)).crop is None  # full box = no crop
    assert Edit(crop=(0.5, 0.5, 0.5004, 0.9)).crop is None  # too thin to count
    assert Edit(crop=(0.8, 0.9, 0.2, 0.1)).crop == (0.2, 0.1, 0.8, 0.9)  # gets sorted
    assert not Edit(quarter=1).is_identity() and Edit(quarter=1).swaps_axes()


def test_edit_rotation_moves_crop_with_image():
    edit = Edit(quarter=0, angle=3, crop=(0.1, 0.2, 0.5, 0.6))
    right = edit.rotated(1)
    assert right.quarter == 1 and right.angle == 3
    assert right.crop == pytest.approx((0.4, 0.1, 0.8, 0.5))
    assert edit.rotated(1).rotated(-1) == edit
    assert edit.rotated(4) == edit and edit.rotated(2) == edit.rotated(1).rotated(1)


def test_describe_and_short():
    assert Edit(quarter=1).describe() == "sağa 90° döndürüldü"
    assert Edit(quarter=3, angle=-2.5, crop=(0, 0, 0.5, 0.5)).describe(future=True) == (
        "sola 90° döndürülecek · eğikliği 2,5° düzeltilecek · kırpılacak"
    )
    assert Edit(quarter=2, crop=(0, 0, 0.5, 0.5)).short() == "↻ 180° · kırpma"
    assert Edit(angle=1).short() == "∡ 1°"


@pytest.mark.parametrize(
    "quarter,expected",
    [
        (1, {"tl": "blue", "tr": "red", "br": "green"}),
        (2, {"tr": "blue", "br": "red", "bl": "green"}),
        (3, {"tl": "green", "br": "blue", "bl": "red"}),
    ],
)
def test_quarter_turns_are_clockwise_and_lossless(quarter, expected):
    out = apply(PHOTO, Edit(quarter=quarter))
    assert out.size == ((240, 320) if quarter % 2 else (320, 240))
    got = corners(out)
    assert {k: got[k] for k in expected} == expected
    back = out.transpose({1: Image.Transpose.ROTATE_90, 2: Image.Transpose.ROTATE_180,
                          3: Image.Transpose.ROTATE_270}[quarter])
    assert back.tobytes() == PHOTO.tobytes()


def test_crop_is_pixel_exact_and_commutes_with_rotation():
    edit = Edit(crop=(0.1, 0.2, 0.6, 0.7))
    assert apply(PHOTO, edit).tobytes() == PHOTO.crop((32, 48, 192, 168)).tobytes()
    assert edit.output_size(320, 240) == (160, 120)
    # Rotating the cropped photo = cropping the rotated photo (with the rotated box).
    rotated = apply(PHOTO, edit.rotated(1))
    assert rotated.tobytes() == apply(PHOTO, edit).transpose(Image.Transpose.ROTATE_270).tobytes()
    assert edit.rotated(1).output_size(320, 240) == (120, 160)


def test_geometry_largest_box_inside_rotated_image():
    # A square rotated by 45°: the largest square that fits inside has side / √2
    t = max_half_height(100, 100, 45, 1.0, 50, 50)
    assert 2 * t == pytest.approx(100 / math.sqrt(2), rel=1e-6)
    for angle in (-30, -7.5, 2, 12, 44):
        box = largest_box(400, 300, angle, 4 / 3)
        assert box_fits(400, 300, angle, box)
        grown = (box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1)
        assert not box_fits(400, 300, angle, grown)  # really the largest
    # A box centered near the edge stays small; zero if the center is outside the image.
    assert max_half_height(400, 300, 10, 1.0, 5, 150) < 10
    assert max_half_height(400, 300, 10, 1.0, 1, 1) == 0.0


def test_straighten_has_no_empty_corners():
    solid = Image.new("RGB", (400, 300), (200, 120, 40))
    for edit in (Edit(angle=7), Edit(angle=-20, crop=(0, 0, 0.3, 0.3)), Edit(quarter=1, angle=44)):
        out = apply(solid, edit)
        assert out.size == edit.output_size(400, 300)
        assert out.getextrema() == ((200, 200), (120, 120), (40, 40)), edit  # no (black) fill
    # The automatic crop keeps the image's aspect ratio.
    w, h = Edit(angle=7).output_size(400, 300)
    assert abs(w / h - 4 / 3) < 0.01 and w < 400


def test_straighten_levels_a_tilted_horizon():
    """A horizon shot with a 5° counterclockwise tilt becomes level with a +5° (clockwise) correction."""
    im = Image.new("L", (600, 400), 255)
    slope = math.tan(math.radians(5))
    ImageDraw.Draw(im).line([(0, 200 + 300 * slope), (600, 200 - 300 * slope)], fill=0, width=5)

    def tilt(img):
        w = img.width
        ys = []
        for x0 in (w // 6, 5 * w // 6):
            col = [y for y in range(img.height) for x in range(x0 - 3, x0 + 3) if img.getpixel((x, y)) < 128]
            ys.append(sum(col) / len(col))
        return ys[0] - ys[1]

    assert tilt(im) > 30
    assert abs(tilt(apply(im, Edit(angle=5)))) < 1.5
    assert tilt(apply(im, Edit(angle=-5))) > 60  # the opposite direction makes the tilt worse


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P", "1", "I;16", "CMYK", "L"])
def test_all_pixel_modes(mode):
    if mode == "I;16":
        src = PHOTO.convert("L").convert("I").point(lambda v: v * 257).convert("I;16")
    elif mode in ("RGBA", "LA"):
        src = PHOTO.convert("RGBA").convert(mode)
    else:
        src = PHOTO.convert(mode)
    for edit in (Edit(quarter=1, crop=(0.1, 0.1, 0.9, 0.8)), Edit(angle=-6)):
        out = apply(src, edit)
        assert out.size == edit.output_size(*src.size)
        if not edit.angle:
            assert out.mode == src.mode  # lossless: mode and palette are kept
    assert apply(src, Edit(angle=3)).mode == {"P": "RGB", "1": "L"}.get(mode, mode)


def test_transparency_is_resampled_premultiplied():
    im = Image.new("RGBA", (200, 200), (255, 0, 0, 0))  # fully transparent red background
    im.paste((0, 0, 255, 255), (40, 40, 160, 160))
    out = apply(im, Edit(angle=10))
    edge = [p for p in out.get_flattened_data() if 0 < p[3] < 255]
    assert edge and max(p[0] for p in edge) == 0  # the transparent background's color does not bleed into the edge


# ====================================================================== EXIF


def _exif_with_pixel_size(e: str) -> bytes:
    ifd0 = b.Ifd(e).ascii(0x010F, "Canon").short(0x0112, 6).pointer(0x8769, "exif")
    exif = b.Ifd(e).short(0xA002, 4000).long(0xA003, 3000).ascii(0x9003, "2024:05:12 14:30:22")
    return b.build_tiff([("ifd0", ifd0), ("exif", exif)], {}, e)


@pytest.mark.parametrize("e", ["<", ">"])
def test_exif_for_edited_updates_size_and_orientation(e):
    raw = b"Exif\x00\x00" + _exif_with_pixel_size(e)
    out = for_edited(raw, (1200, 900))
    assert out.startswith(b"Exif\x00\x00") and len(out) == len(raw)
    ex = Image.Exif()
    ex.load(out)
    sub = ex.get_ifd(ExifTags.IFD.Exif)
    assert ex[0x0112] == 1 and sub[0xA002] == 1200 and sub[0xA003] == 900 and ex[0x010F] == "Canon"


def test_exif_thumbnail_dropped_in_place():
    raw = b"Exif\x00\x00" + b.rich_exif(6)
    ex = Image.Exif()
    ex.load(raw)
    thumb = ex.get_ifd(ExifTags.IFD.IFD1)
    start, length = thumb[0x0201], thumb[0x0202]
    blob = raw[6 + start:6 + start + length]
    assert blob.startswith(b"\xff\xd8")
    out = drop_thumbnail(raw)
    assert len(out) == len(raw) and blob not in out
    ex = Image.Exif()
    ex.load(out)
    assert not ex.get_ifd(ExifTags.IFD.IFD1)
    assert ex[0x010F] == "Apple" and ex.get_ifd(ExifTags.IFD.GPSInfo)  # the rest stays in place
    assert read_orientation(for_edited(raw)) == 1


# ================================================================ processing: JPEG


def _qtables(data: bytes) -> list[list[int]]:
    q = decode(data).quantization
    return [list(q[k]) for k in sorted(q)]


def _transposed(t: list[int]) -> list[int]:
    return [t[(i % 8) * 8 + i // 8] for i in range(64)]


def test_jpeg_strip_with_rotation_reuses_tables():
    src = b.jpeg(PHOTO, quality=81)
    r = process_bytes(src, STRIP, edit=Edit(quarter=1))
    assert r.method == REENCODED and r.expected_size == (240, 320)
    out = decode(r.data)
    assert out.size == (240, 320) and corners(out)["tl"] == "blue" and corners(out)["tr"] == "red"
    assert_clean(r.data, Fmt.JPEG)
    assert _qtables(r.data) == [_transposed(t) for t in _qtables(src)]
    # The re-encoding loss is small: compared with the rotated decoded source.
    assert psnr(out, decode(src).transpose(Image.Transpose.ROTATE_270)) > 38


def test_jpeg_orientation_and_edit_compose():
    """A photo stored with orientation 6, rotated left, is back in its sensor layout: tables are not transposed."""
    stored = PHOTO.transpose(Image.Transpose.ROTATE_90)
    src = b.jpeg(stored, orientation=6, everything=False, quality=85)
    r = process_bytes(src, STRIP, edit=Edit(quarter=3))
    out = decode(r.data)
    assert out.size == stored.size
    assert _qtables(r.data) == _qtables(src)
    assert psnr(out, decode(src)) > 40  # same block grid, same tables: nearly lossless


def _located(out: Image.Image, canvas: Image.Image, left: int, top: int) -> tuple[float, int, int]:
    """Where the output best matches the source within ±16 px of the requested crop position: (PSNR, dx, dy)."""
    import numpy as np

    o = np.asarray(out.convert("RGB"), dtype=np.float64)
    c = np.asarray(canvas.convert("RGB"), dtype=np.float64)
    h, w = o.shape[:2]
    best = (float("inf"), 0, 0)
    for dy in range(-16, 17):
        for dx in range(-16, 17):
            x, y = left + dx, top + dy
            if 0 <= x and 0 <= y and x + w <= c.shape[1] and y + h <= c.shape[0]:
                best = min(best, (float(np.mean((o - c[y:y + h, x:x + w]) ** 2)), dx, dy))
    mse, dx, dy = best
    return (float("inf") if mse == 0 else 10 * math.log10(255 ** 2 / mse)), dx, dy


def test_jpeg_crop_snaps_to_blocks_and_straighten():
    """Without straightening, the crop box snaps to the source's 16 px block grid: blocks re-encoded with
    the same quantization tables line up with the source blocks, so the quality is nearly lossless."""
    textured = Image.blend(b.photo(640, 480), b._noise(640, 480), 0.1).filter(ImageFilter.GaussianBlur(1))
    src = b.jpeg(textured, everything=False, quality=90, subsampling="4:2:0")
    viewer = decode(src)
    for edit in (Edit(crop=(0.25, 0.26, 0.75, 0.75)), Edit(quarter=1, crop=(0.13, 0.27, 0.8, 0.9))):
        r = process_bytes(src, STRIP, edit=edit)
        out = decode(r.data)
        canvas = apply(viewer, Edit(quarter=edit.quarter))
        left, top, w, h = edit.pixels_on_canvas(*canvas.size)
        assert out.size == (w, h) == r.expected_size
        quality, dx, dy = _located(out, canvas, left, top)
        assert quality > 44 and -16 < dx <= 0 and -16 < dy <= 0, (edit, quality, dx, dy)
        assert (left + dx) % 16 == 0 and (top + dy) % 16 == 0  # on a block boundary
    r = process_bytes(src, TO_JPEG, edit=Edit(angle=4))  # JPEG source: the same path when converting too
    assert decode(r.data).size == Edit(angle=4).output_size(640, 480)
    assert_clean(r.data, Fmt.JPEG)


def test_jpeg_keeping_metadata():
    """Metadata stripping off: EXIF/XMP/IPTC/comment stay; old thumbnails, extra images, the content
    signature (C2PA) and trailing data are removed because they no longer match the edited image."""
    src = b.jpeg(b.photo(320, 240))
    edit = Edit(quarter=1, crop=(0.1, 0.1, 0.9, 0.9))
    r = process_bytes(src, KEEP, edit=edit)
    out = decode(r.data)
    assert out.size == edit.output_size(320, 240) == r.expected_size
    res = scan_bytes(r.data)
    kinds = res.kinds()
    assert {Kind.EXIF, Kind.GPS, Kind.MAKERNOTE, Kind.XMP, Kind.IPTC, Kind.COMMENT} <= kinds
    assert not kinds & {Kind.THUMBNAIL, Kind.EXTRA_IMAGE, Kind.C2PA, Kind.TRAILER}, kinds
    assert res.summary.camera == "Apple iPhone 15 Pro" and res.summary.gps is not None
    assert read_orientation(jpeg_meta.parse(r.data).exif()) == 1
    meta = exiv2_metadata(r.data)
    assert meta["exif"]["Exif.Image.Make"] == "Apple"
    assert not [k for k in meta["exif"] if k.startswith("Exif.Thumbnail.")]
    assert meta["iptc"] and meta["comment"]


def test_jpeg_keep_cmyk_stays_cmyk_with_profile():
    buf = io.BytesIO()
    PHOTO.convert("CMYK").save(buf, "JPEG", quality=92, icc_profile=b.cmyk_icc())
    r = process_bytes(buf.getvalue(), KEEP, edit=Edit(quarter=2))
    out = decode(r.data)
    assert out.mode == "CMYK" and jpeg_meta.parse(r.data).icc_profile() == b.cmyk_icc()
    assert psnr(out.convert("RGB"), PHOTO.convert("CMYK").rotate(180).convert("RGB")) > 30


# =========================================================== processing: other formats


def test_png_edit_is_lossless_and_keeps_text_when_asked():
    edit = Edit(quarter=3, crop=(0.25, 0, 0.75, 1))
    expected = apply(PHOTO, edit)
    r = process_bytes(b.png(PHOTO), STRIP, edit=edit)
    assert decode(r.data).tobytes() == expected.tobytes()
    assert_clean(r.data, Fmt.PNG)
    r = process_bytes(b.png(PHOTO), KEEP, edit=edit)
    out = decode(r.data)
    assert out.tobytes() == expected.tobytes()
    assert out.text["Author"] == "Ayşe Yılmaz" and "XML:com.adobe.xmp" in out.text
    assert out.getexif()[0x010F] == "Apple" and out.getexif().get(0x0112) == 1


@pytest.mark.parametrize("lossless", [True, False])
def test_webp_edit(lossless):
    src = b.webp(PHOTO, lossless=lossless)
    edit = Edit(quarter=1)
    r = process_bytes(src, STRIP, edit=edit)
    assert_clean(r.data, Fmt.WEBP)
    if lossless:
        assert decode(r.data).convert("RGB").tobytes() == apply(PHOTO, edit).tobytes()
    r = process_bytes(src, KEEP, edit=edit)
    kinds = scan_bytes(r.data).kinds()
    assert {Kind.EXIF, Kind.XMP} <= kinds and decode(r.data).size == (240, 320)


def test_animated_edit_applies_to_every_frame():
    for src, fmt in ((b.animated_webp(), Fmt.WEBP), (b.gif(animated=True), Fmt.GIF)):
        r = process_bytes(src, STRIP, edit=Edit(quarter=1, crop=(0, 0, 0.5, 1)))
        out = decode(r.data)
        w, h = out.size
        assert out.n_frames == (3 if fmt == Fmt.WEBP else 2)
        for frame in ImageSequence.Iterator(out):
            assert frame.size == (w, h)
        assert_clean(r.data, fmt)
    r = process_bytes(b.gif(), KEEP, edit=Edit(quarter=2))
    assert decode(r.data).info.get("comment") == "Ayşe tarafından çekildi".encode()


def test_tiff_bmp_edit():
    r = process_bytes(b.tiff(pages=2), STRIP, edit=Edit(quarter=1))
    out = decode(r.data)
    assert out.n_frames == 2
    assert_clean(r.data, Fmt.TIFF)
    r = process_bytes(b.tiff(pages=1), KEEP, edit=Edit(crop=(0, 0, 0.5, 0.5)))
    out = decode(r.data)
    assert out.size == (160, 120) and out.tag_v2.get(271) == "Canon" and out.tag_v2.get(700)
    r = process_bytes(b.bmp(PHOTO), STRIP, edit=Edit(quarter=2))
    assert decode(r.data).tobytes() == PHOTO.rotate(180).tobytes()


@pytest.mark.parametrize("builder,fmt", [(b.heic, Fmt.HEIF), (b.avif, Fmt.AVIF)])
def test_heif_family_edit(builder, fmt):
    displayed = b.photo(320, 240)
    src = builder(displayed.transpose(Image.Transpose.ROTATE_90), orientation=6, icc=b.P3_ICC)
    edit = Edit(quarter=1)
    r = process_bytes(src, STRIP, edit=edit)
    assert decode(r.data).size == (240, 320) and corners(decode(r.data))["tr"] == "red"
    assert_clean(r.data, fmt)
    r = process_bytes(src, KEEP, edit=edit)
    out = decode(r.data)
    assert out.size == (240, 320) and corners(out)["tr"] == "red"
    assert out.info.get("icc_profile") == b.P3_ICC  # colors not converted, the profile was carried over
    res = scan_bytes(r.data)
    assert res.summary.camera == "Apple iPhone 15 Pro" and Kind.XMP in res.kinds()


def test_convert_to_jpeg_keeping_metadata_with_edit():
    src = b.heic(b.photo(320, 240), icc=b.P3_ICC)
    r = process_bytes(src, KEEP_TO_JPEG, edit=Edit(crop=(0, 0, 0.5, 0.5)))
    out = decode(r.data)
    assert out.size == (160, 120)
    assert read_orientation(jpeg_meta.parse(r.data).exif()) == 1
    assert scan_bytes(r.data).summary.camera == "Apple iPhone 15 Pro"


# ====================================================================== RAW


def test_raw_edit_needs_jpeg_conversion():
    data = make_dng(b.photo(320, 240), preview="full")
    with pytest.raises(ProcessError, match="JPG"):
        process_bytes(data, KEEP, name="x.dng", edit=Edit(quarter=1))
    assert process_bytes(data, KEEP, name="x.dng").data is None  # without an edit it is left untouched


@pytest.mark.parametrize("raw_mode", ["auto", RAW_DEVELOP])
def test_raw_edit_when_converting(raw_mode):
    displayed = b.photo(320, 240)
    data = make_dng(displayed.transpose(Image.Transpose.ROTATE_90), orientation=6, preview="full")
    opts = ContentOptions(strip_metadata=True, convert_to_jpeg=True, raw_mode=raw_mode)
    r = process_bytes(data, opts, name="x.dng", edit=Edit(quarter=1, crop=(0, 0, 1, 0.5)))
    out = decode(r.data)
    assert out.size == (240, 160) and b.dominant(b.marker_color(out, "tl")) == "blue"
    assert_clean(r.data, Fmt.JPEG)
    keep = ContentOptions(convert_to_jpeg=True, raw_mode=raw_mode)
    r = process_bytes(data, keep, name="x.dng", edit=Edit(quarter=1, crop=(0, 0, 1, 0.5)))
    assert decode(r.data).size == (240, 160)
    assert read_orientation(jpeg_meta.parse(r.data).exif()) == 1


# ====================================================== RAW exposure, video trim


def test_exposure_and_trim_normalization():
    assert Edit(exposure=0.004, trim=(0, None)).is_identity()
    e = Edit(exposure=7, trim=(-1, 3.25))
    assert e.exposure == MAX_EXPOSURE and e.trim == (0.0, 3.25)
    assert not e.is_identity() and not e.has_geometry() and e.geometry_only() is None
    assert Edit(trim=(5, 2)).trim == (5.0, None)  # an end before the start is dropped
    assert Edit(quarter=1, exposure=0.7, trim=(1, 2)).geometry_only() == Edit(quarter=1)
    assert Edit(exposure=1.0, trim=(1, None)).rotated(1) == Edit(quarter=1, exposure=1.0, trim=(1, None))
    assert Edit(trim=(2.5, None)).trim_window(10) == (2.5, 10) and Edit(trim=(2.5, None)).kept_duration(10) == 7.5
    assert Edit(trim=(1, 30)).trim_window(10) == (1, 10) and Edit().kept_duration(10) == 10
    im = b.photo(64, 48)
    assert apply(im, Edit(exposure=1.0, trim=(1, 2))) is im  # not geometric: the pixels stay as they are


def test_exposure_and_trim_texts():
    assert ev_text(0.7) == "+0,7 EV" and ev_text(-1.3) == "−1,3 EV" and ev_text(0) == "0 EV"
    assert clock(5) == "0:05" and clock(65.3) == "1:05,3" and clock(3725) == "1:02:05"
    assert Edit(exposure=-0.5).short() == "☀ −0,5 EV"
    assert Edit(exposure=1.0).describe(future=True) == "pozlaması +1,0 EV ayarlanacak"
    assert Edit(trim=(5, 20)).describe(future=True) == "başı ve sonu kesilecek (kalan: 0:05 – 0:20)"
    assert Edit(trim=(5, None)).describe() == "başı kesildi (kalan: 0:05 – son)"
    assert Edit(trim=(0, 20.5)).short() == "✂ 0:00 – 0:20,5"
    assert Edit(quarter=1, trim=(0, 20)).describe() == "sağa 90° döndürüldü · sonu kesildi (kalan: 0:00 – 0:20)"


def test_removed_parts():
    # Sorted, rounded, merged when they overlap or touch; too short ones dropped.
    e = Edit(cuts=((8, 9), (2.0004, 3), (2.5, 4), (4, 5), (6, 6.02)))
    assert e.cuts == ((2.0, 5.0), (8.0, 9.0)) and not e.is_identity() and e.photo_only() is None
    assert Edit(quarter=1, cuts=((1, 2),)).photo_only() == Edit(quarter=1)
    assert Edit(quarter=1, cuts=((1, 2),)).geometry_only() == Edit(quarter=1)
    assert e.segments(10) == [(0.0, 2.0), (5.0, 8.0), (9.0, 10)] and e.kept_duration(10) == 6
    assert e.segments(0) == [(0.0, 2.0), (5.0, 8.0), (9.0, None)]  # duration not known
    trimmed = Edit(trim=(3, 8.5), cuts=e.cuts)
    assert trimmed.segments(10) == [(5.0, 8.0)] and trimmed.kept_duration(10) == 3
    assert e.describe(future=True) == "2 bölüm çıkarılacak (kalan: 0:00 – 0:02, 0:05 – 0:08, 0:09 – son)"
    assert Edit(trim=(1, None), cuts=((3, 4),)).describe() == "başı kesildi, 1 bölüm çıkarıldı (kalan: 0:01 – 0:03, 0:04 – son)"
    assert e.short() == "✂ 3 parça" and Edit(cuts=((3, 4),)).short() == "✂ 0:00 – 0:03, 0:04 – son"
    assert Edit(cuts=((0, 10),)).segments(10) == []  # (the texts do not know the duration: "0:10 – son")


def test_tidied_keeps_removed_parts_inside_the_trim():
    # Removed parts are cut to the trim (outside ones dropped); one at an edge stays a removed part.
    e = Edit(trim=(2, 9), cuts=((0, 1), (1.5, 3), (5, 6), (8.5, 12)))
    assert e.tidied(10) == Edit(trim=(2, 9), cuts=((2, 3), (5, 6), (8.5, 9)))
    assert Edit(cuts=((0, 2),)).tidied(10) == Edit(cuts=((0, 2),))
    # Kept parts shorter than `shortest` go too; nothing left: None.
    assert Edit(cuts=((0.2, 5), (5.1, 7))).tidied(10, shortest=0.3) == Edit(cuts=((0, 7),))
    assert Edit(cuts=((0, 9.9),)).tidied(10, shortest=0.3) is None
    assert Edit(trim=(1.1, 1.4)).tidied(10, shortest=0.3) == Edit(trim=(1.1, 1.4))  # 0,3 s exactly is enough
    assert Edit(cuts=((8, 10),)).tidied(0) == Edit(cuts=((8, 10),))  # duration not known


def test_exposure_curve_works_in_linear_light():
    up, down = exposure_curve(1.0), exposure_curve(-1.0)
    assert exposure_curve(0.0) == tuple(range(256))
    assert all(a <= b_ for a, b_ in zip(up, up[1:])) and all(a <= b_ for a, b_ in zip(down, down[1:]))
    assert up[0] == down[0] == 0 and up[255] == 255  # white rolls off softly into white
    # 18 % gray (118) doubles to 36 % (162) or halves to 9 % (85) of the light.
    assert abs(up[118] - 162) <= 1 and abs(down[118] - 85) <= 1
    deep = exposure_curve(1.0, 65535)
    assert len(deep) == 65536 and abs(deep[118 * 257] / 257 - up[118]) < 1


def test_exposure_of_other_photos_in_every_mode():
    up = exposure_curve(1.0)
    rgba = Image.new("RGBA", (4, 4), (118, 60, 200, 77))
    assert apply_exposure(rgba, 0.0) is rgba
    assert apply_exposure(rgba, 1.0).getpixel((0, 0)) == (up[118], up[60], up[200], 77)  # the alpha stays
    assert apply_exposure(Image.new("LA", (2, 2), (118, 9)), 1.0).getpixel((0, 0)) == (up[118], 9)
    pal = Image.new("RGB", (4, 4), (118, 60, 200)).convert("P", palette=Image.Palette.ADAPTIVE)
    out = apply_exposure(pal, 1.0)
    assert out.mode == "P" and out.tobytes() == pal.tobytes()  # same indices, new palette
    assert out.convert("RGB").getpixel((0, 0)) == tuple(up[v] for v in pal.convert("RGB").getpixel((0, 0)))
    see_through = pal.copy()
    see_through.putpalette([118, 60, 200, 40] * 256, "RGBA")
    kept = apply_exposure(see_through, 1.0)
    assert kept.palette.mode == "RGBA" and kept.getpalette("RGBA")[:4] == [up[118], up[60], up[200], 40]
    ink = apply_exposure(Image.new("CMYK", (2, 2), (0, 0, 0, 128)), 1.0).getpixel((0, 0))
    assert ink[3] < 128  # brighter: less black ink
    assert apply_exposure(Image.new("I;16", (2, 2), 30000), 1.0).getpixel((0, 0)) > 30000
    bw = Image.new("1", (2, 2), 1)
    assert apply_exposure(bw, 1.0) is bw  # black and white only


@pytest.mark.parametrize("fmt", ["jpeg", "png", "webp", "heic", "tiff", "gif", "bmp"])
def test_exposure_of_other_photos(fmt):
    from fotodit.core.imaging import ensure_plugins

    ensure_plugins()
    src = {"jpeg": lambda: b.jpeg(b.photo()), "png": lambda: b.png(b.photo()), "webp": lambda: b.webp(b.photo()),
           "heic": lambda: b.heic(b.photo()), "tiff": lambda: b.tiff(pages=2), "gif": lambda: b.gif(animated=True),
           "bmp": lambda: b.bmp(b.photo())}[fmt]()
    gray = lambda data: ImageStat.Stat(decode(data).convert("L")).mean[0]  # noqa: E731
    before = gray(src)
    for ev in (1.0, -1.0):
        r = process_bytes(src, STRIP, name=f"a.{fmt}", edit=Edit(exposure=ev))
        after = gray(r.data)
        assert (after > before + 10) if ev > 0 else (after < before - 10), (fmt, ev, before, after)
        assert_clean(r.data, r.fmt)
    # Along with a rotation, and when converting to JPG.
    r = process_bytes(src, TO_JPEG, name=f"a.{fmt}", edit=Edit(quarter=1, exposure=1.0))
    assert r.fmt == Fmt.JPEG and gray(r.data) > before + 10
    assert decode(r.data).size == decode(src).size[::-1]


def test_exposure_development_matches_libraw_at_zero():
    """0 EV reproduces LibRaw's own 8-bit development (automatic brightening, sRGB curve)."""
    import rawpy

    from fotodit.core import rawdev

    data = make_dng(b.photo(320, 240))
    with rawpy.imread(io.BytesIO(data)) as raw:
        reference = raw.postprocess(use_camera_wb=True, output_color=rawpy.ColorSpace.sRGB, output_bps=8,
                                    gamma=(2.4, 12.92), highlight_mode=rawpy.HighlightMode.Clip)
    with rawpy.imread(io.BytesIO(data)) as raw:
        linear = rawdev.develop_linear(raw)
    white = rawdev.white_point(linear)
    zero = np.asarray(rawdev.tone(linear, white, 0.0)).astype(int)
    assert np.abs(zero - reference.astype(int)).max() <= 1
    darker, brighter = (np.asarray(rawdev.tone(linear, white, ev)).mean() for ev in (-1.0, 1.0))
    assert darker < zero.mean() - 15 and brighter > zero.mean() + 15
    lut = rawdev.exposure_lut(white, 2.0)
    assert (np.diff(lut.astype(int)) >= 0).all() and lut[0] == 0 and lut[-1] == 255


def test_raw_exposure_is_developed_from_raw_data(tmp_path):
    from fotodit.core import rawdev

    data = make_dng(b.photo(320, 240), preview="full")
    plain = process_bytes(data, TO_JPEG, name="x.dng")
    assert any("gömülü" in n for n in plain.notes)  # without an exposure: the camera's JPEG
    bright = process_bytes(data, TO_JPEG, name="x.dng", edit=Edit(exposure=1.0))
    assert any("Pozlama +1,0 EV ayarlandı" in n for n in bright.notes)
    gray = lambda r: np.asarray(decode(r.data).convert("L"), dtype=float).mean()  # noqa: E731
    assert gray(bright) > gray(plain) + 15
    assert_clean(bright.data, Fmt.JPEG)
    turned = process_bytes(data, TO_JPEG, name="x.dng", edit=Edit(quarter=1, exposure=-0.5))
    assert decode(turned.data).size == (240, 320) and gray(turned) < gray(plain)
    with pytest.raises(ProcessError, match="pozlama"):
        process_bytes(data, KEEP, name="x.dng", edit=Edit(exposure=1.0))
    # Other photos get the exposure too (see test_exposure_of_other_photos).
    jpeg = process_bytes(b.jpeg(b.photo(64, 48)), KEEP, name="a.jpg", edit=Edit(exposure=1.0))
    assert jpeg.method == "reencoded" and jpeg.data is not None  # other photos: adjusted in linear light
    # The live preview: a half-size development, reduced to the requested size.
    path = _write(tmp_path / "x.dng", data)
    preview = rawdev.linear_preview(path, 100)
    assert preview is not None and max(preview.pixels.shape[:2]) <= 100
    assert np.asarray(preview.image(1.0)).mean() > np.asarray(preview.image(0.0)).mean() + 15


# ================================================================ plan and run


def _write(path, data: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return str(path)


def test_plan_skips_edited_raw_without_conversion(tmp_path):
    raw = _write(tmp_path / "a.dng", make_dng(b.photo(320, 240), preview="full"))
    photo = Photo.from_path(raw, 0, read_info(raw))
    out = OutputOptions(folder=str(tmp_path / "o"))
    assert build_plan([photo], JobOptions(output=out))[0].skip_reason is None
    photo.edit = Edit(quarter=1)
    entry = build_plan([photo], JobOptions(output=out))[0]
    assert entry.skip_reason == t(RAW_EDIT_NEEDS_JPEG) and entry.edit == Edit(quarter=1)
    assert build_plan([photo], JobOptions(content=ContentOptions(convert_to_jpeg=True), output=out))[0].skip_reason is None


def test_runner_applies_edits_in_place_and_to_new_folder(tmp_path):
    from helpers import FakeBin

    src = _write(tmp_path / "A" / "sideways.jpg", b.jpeg(PHOTO))
    before = open(src, "rb").read()
    photo = Photo.from_path(src, 0, read_info(src))
    photo.edit = Edit(quarter=1)
    # New folder with no option enabled: the edited photo is still processed (not just copied).
    opts = JobOptions(output=OutputOptions(folder=str(tmp_path / "out")))
    plan = build_plan([photo], opts)
    report = Runner(plan, opts, workers=1).run()
    assert report.succeeded and open(src, "rb").read() == before
    out = tmp_path / "out" / "sideways.jpg"
    assert decode(out.read_bytes()).size == (240, 320)
    assert scan_bytes(out.read_bytes()).summary.camera == "Apple iPhone 15 Pro"  # metadata kept
    # Apply to the original: the original goes to the Recycle Bin, the rotated version takes its place.
    opts = JobOptions(output=OutputOptions(in_place=True))
    bin_ = FakeBin(str(tmp_path / "_bin"))
    report = Runner(build_plan([photo], opts), opts, workers=1, recycle_func=bin_).run()
    assert report.succeeded and report.recycled == 1
    assert decode(open(src, "rb").read()).size == (240, 320)
    assert open(bin_.items[0], "rb").read() == before


def test_copy_without_edit_is_byte_identical(tmp_path):
    src = _write(tmp_path / "a.jpg", b.jpeg(PHOTO))
    photo = Photo.from_path(src, 0, read_info(src))
    photo.edit = Edit()  # a no-op edit = no edit
    opts = JobOptions(output=OutputOptions(folder=str(tmp_path / "o")))
    entry = build_plan([photo], opts)[0]
    report = Runner([entry], opts, workers=1).run()
    assert report.succeeded and (tmp_path / "o" / "a.jpg").read_bytes() == open(src, "rb").read()


def test_viewer_orientation_matches_edit_semantics():
    """An edit is relative to the photo as displayed (with the EXIF orientation applied)."""
    stored = PHOTO.transpose(Image.Transpose.ROTATE_90)
    src = b.jpeg(stored, orientation=6, everything=False)
    viewer = ImageOps.exif_transpose(decode(src))
    r = process_bytes(src, STRIP, edit=Edit(crop=(0, 0, 0.5, 0.5)))
    assert psnr(decode(r.data), viewer.crop((0, 0, 160, 120))) > 35
