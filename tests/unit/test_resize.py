"""Shrinking photos for sharing (`max_side`): every output path, losslessness below the limit, the notes
and the plan."""

from __future__ import annotations

import io
import os

import builders as b
import pytest
from helpers import FakeBin, photos_of, write
from oracle import exiv2_metadata, psnr
from PIL import Image, ImageSequence, JpegImagePlugin

from fotodit.core import processor
from fotodit.core.dngwriter import make_dng
from fotodit.core.edits import Edit
from fotodit.core.formats import Fmt
from fotodit.core.imaging import open_image
from fotodit.core.meta import jpeg as jpeg_meta
from fotodit.core.meta import scan_bytes
from fotodit.core.meta import webp as webp_meta
from fotodit.core.meta.exif import read_orientation
from fotodit.core.photoinfo import PhotoInfo
from fotodit.core.planner import JobOptions, OutputOptions, Photo, PlanEntry, build_plan
from fotodit.core.processor import (
    CONVERTED,
    LOSSLESS,
    RAW_RESIZE_NEEDS_JPEG,
    REENCODED,
    UNCHANGED,
    ContentOptions,
    ProcessError,
    process_bytes,
)
from fotodit.core.runner import Runner
from fotodit.i18n import t

SMALL = 160  # the long side for the 320 × 240 test photos


def decode(data: bytes) -> Image.Image:
    im = open_image(io.BytesIO(data))
    im.load()
    return im


def gif() -> bytes:
    """A still GIF of the test photo's size, with a comment."""
    buf = io.BytesIO()
    b.photo().quantize(64).save(buf, "GIF", comment="Ayşe tarafından çekildi".encode())
    return buf.getvalue()


def note(before: tuple[int, int], after: tuple[int, int]) -> str:
    return f"Boyutu küçültüldü: {before[0]} × {before[1]} → {after[0]} × {after[1]}."


def test_target_size():
    target = processor._target_size
    assert target((4032, 3024), 2048) == (2048, 1536)
    assert target((3024, 4032), 2048) == (1536, 2048)
    assert target((2048, 1000), 2048) is None and target((4032, 3024), 0) is None
    assert target((10000, 10), 2048) == (2048, 2) and target((10, 10000), 100) == (1, 100)
    assert target((4000, 2251), 1000) == (1000, 563)  # rounded to the nearest pixel


# ============================================================================ JPEG


def test_jpeg_within_the_limit_stays_lossless():
    src = b.jpeg()
    for max_side in (320, 4096):
        r = process_bytes(src, ContentOptions(strip_metadata=True, max_side=max_side))
        assert r.method == LOSSLESS and decode(r.data).tobytes() == decode(src).tobytes()
        assert not any("Boyutu" in n for n in r.notes)
    assert process_bytes(src, ContentOptions(max_side=320)).data is None  # metadata kept: untouched


def test_jpeg_shrinks_with_its_own_tables():
    src = b.jpeg(b.photo(640, 480), everything=False, quality=77)
    r = process_bytes(src, ContentOptions(strip_metadata=True, max_side=320))
    out = decode(r.data)
    assert r.method == REENCODED and out.size == (320, 240) and r.expected_size == (320, 240)
    assert note((640, 480), (320, 240)) in r.notes
    assert out.quantization == decode(src).quantization  # the source's own quality, not a new one
    reference = decode(src).resize((320, 240), Image.Resampling.LANCZOS)
    assert psnr(out, reference) > 32
    assert scan_bytes(r.data).clean


def test_jpeg_large_reduction_decodes_smaller_first(monkeypatch):
    """A big reduction lets libjpeg decode at 1/2, 1/4 or 1/8 size (draft); the result is the same size and
    looks the same, and the note gives the full size."""
    src = b.jpeg(b.photo(1600, 1200), everything=False, quality=90)
    drafts = []
    real = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        drafts.append(size)
        result = real(self, mode, size)
        drafts.append(self.size)
        return result

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    r = process_bytes(src, ContentOptions(strip_metadata=True, max_side=300))
    assert drafts == [(300, 225), (400, 300)]  # decoded at a quarter of its size, still larger than the result
    assert decode(r.data).size == (300, 225) and note((1600, 1200), (300, 225)) in r.notes
    reference = decode(src).resize((300, 225), Image.Resampling.LANCZOS)
    assert psnr(decode(r.data), reference) > 33
    drafts.clear()  # a crop is measured on the full image: decoded at full size
    cropped = process_bytes(src, ContentOptions(strip_metadata=True, max_side=300), edit=Edit(crop=(0, 0, 0.5, 0.5)))
    assert not drafts and decode(cropped.data).size == (300, 225)


def test_jpeg_orientation_and_edit_come_first():
    displayed = b.photo(320, 240)
    src = b.jpeg(displayed.transpose(Image.Transpose.ROTATE_90), orientation=6, everything=False)
    r = process_bytes(src, ContentOptions(strip_metadata=True, max_side=SMALL))
    out = decode(r.data)
    assert out.size == (160, 120) and note((320, 240), (160, 120)) in r.notes
    assert b.dominant(b.marker_color(out, "tl")) == "red"
    crop = Edit(crop=(0.0, 0.0, 0.5, 1.0))  # 160 × 240, then shrunk to 107 × 160
    r = process_bytes(b.jpeg(), ContentOptions(strip_metadata=True, max_side=SMALL), edit=crop)
    assert decode(r.data).size == (107, 160) and note((160, 240), (107, 160)) in r.notes


def test_jpeg_keeping_metadata_updates_the_pixel_size():
    ex = Image.Exif()
    ex[0x010F], ex[0x0110], ex[0x0112] = "Canon", "EOS R5", 1
    ex[0x8769] = {0x9003: "2024:05:12 14:30:22", 0xA002: 640, 0xA003: 480}
    buf = io.BytesIO()
    b.photo(640, 480).save(buf, "JPEG", exif=ex.tobytes(), quality=90)
    r = process_bytes(buf.getvalue(), ContentOptions(max_side=320))
    assert r.method == REENCODED and decode(r.data).size == (320, 240)
    kept = decode(r.data).getexif()
    assert kept[0x0110] == "EOS R5" and read_orientation(jpeg_meta.parse(r.data).exif()) == 1
    assert kept.get_ifd(0x8769)[0xA002] == 320 and kept.get_ifd(0x8769)[0xA003] == 240
    assert kept.get_ifd(0x8769)[0x9003] == "2024:05:12 14:30:22"
    assert exiv2_metadata(r.data)["exif"]["Exif.Photo.PixelXDimension"] == "320"


# ===================================================================== other formats


@pytest.mark.parametrize("name,make,fmt", [
    ("png", lambda: b.png(), Fmt.PNG), ("webp-lossy", lambda: b.webp(), Fmt.WEBP),
    ("webp-lossless", lambda: b.webp(lossless=True), Fmt.WEBP), ("tiff", lambda: b.tiff(pages=1), Fmt.TIFF),
    ("bmp", lambda: b.bmp(), Fmt.BMP), ("gif", gif, Fmt.GIF),
    ("heic", lambda: b.heic(), Fmt.HEIF), ("avif", lambda: b.avif(), Fmt.AVIF),
], ids=lambda v: v if isinstance(v, str) else "")
@pytest.mark.parametrize("strip", [True, False], ids=["strip", "keep"])
def test_every_format_shrinks(name, make, fmt, strip):
    src = make()
    size = decode(src).size
    r = process_bytes(src, ContentOptions(strip_metadata=strip, max_side=SMALL))
    out = decode(r.data)
    expected = (160, 120) if size == (320, 240) else processor._target_size(size, SMALL)
    assert r.fmt == fmt and r.method == REENCODED and out.size == expected and r.expected_size == expected
    assert note(size, expected) in r.notes
    if strip:
        assert scan_bytes(r.data, fmt, enrich=False).clean
    if name == "webp-lossless":
        assert webp_meta.is_lossless(r.data)
    if name == "webp-lossy":
        assert not webp_meta.is_lossless(r.data)
    if fmt in (Fmt.PNG, Fmt.TIFF, Fmt.BMP) or name == "webp-lossless":
        # Lossless formats stay lossless: exactly Pillow's Lanczos result, nothing lost on top of it.
        reference = decode(src).convert("RGB").resize(expected, Image.Resampling.LANCZOS, reducing_gap=3.0)
        assert out.convert("RGB").tobytes() == reference.tobytes()
    else:
        assert psnr(out, decode(src).resize(expected, Image.Resampling.LANCZOS)) > 25


@pytest.mark.parametrize("name,make", [
    ("png", lambda: b.png(everything=False)), ("webp", lambda: b.webp(lossless=True)),
    ("gif", lambda: b.gif(animated=False)), ("bmp", lambda: b.bmp()),
], ids=lambda v: v if isinstance(v, str) else "")
def test_lossless_strip_below_the_limit(name, make):
    src = make()
    r = process_bytes(src, ContentOptions(strip_metadata=True, max_side=2048))
    assert r.method == LOSSLESS and not any("Boyutu" in n for n in r.notes)


def test_animations_shrink_every_frame():
    for src, fmt in ((b.animated_webp(), "WEBP"), (b.gif(animated=True), "GIF")):
        r = process_bytes(src, ContentOptions(strip_metadata=True, max_side=60))
        out = decode(r.data)
        assert out.format == fmt and out.n_frames == decode(src).n_frames
        assert all(frame.size == (60, 45) for frame in ImageSequence.Iterator(out))
        assert out.info.get("loop") == 0
    apng = io.BytesIO()
    frames = [b.photo(80, 60), b.photo(80, 60).rotate(180)]
    frames[0].save(apng, "PNG", save_all=True, append_images=frames[1:], duration=100, loop=0)
    out = decode(process_bytes(apng.getvalue(), ContentOptions(strip_metadata=True, max_side=40)).data)
    assert out.n_frames == 2 and out.size == (40, 30)


def test_tiff_pages_shrink_and_stay_lzw():
    r = process_bytes(b.tiff(pages=2), ContentOptions(strip_metadata=True, max_side=SMALL))
    out = decode(r.data)
    assert out.n_frames == 2 and out.info.get("compression") == "tiff_lzw"
    sizes = []
    for frame in ImageSequence.Iterator(out):
        sizes.append(frame.size)
    assert sizes == [(160, 120), (120, 160)]


def test_high_bit_depth_and_palette_modes():
    g16 = Image.linear_gradient("L").resize((300, 200)).convert("I").point(lambda v: v * 257).convert("I;16")
    buf = io.BytesIO()
    g16.save(buf, "PNG")
    out = decode(process_bytes(buf.getvalue(), ContentOptions(strip_metadata=True, max_side=150)).data)
    assert out.mode == "I;16" and out.size == (150, 100) and out.getextrema()[1] > 60000
    pal = b.photo(300, 200).quantize(16)
    buf = io.BytesIO()
    pal.save(buf, "PNG")
    out = decode(process_bytes(buf.getvalue(), ContentOptions(strip_metadata=True, max_side=150)).data)
    assert out.mode == "RGB" and out.size == (150, 100)  # resampled smoothly, not with the nearest pixel


def test_convert_to_jpeg_shrinks_and_keeps_metadata_in_step():
    src = b.heic(b.photo(320, 240), orientation=1)
    r = process_bytes(src, ContentOptions(strip_metadata=True, convert_to_jpeg=True, max_side=SMALL))
    assert r.method == CONVERTED and decode(r.data).size == (160, 120) and note((320, 240), (160, 120)) in r.notes
    kept = process_bytes(b.png(), ContentOptions(convert_to_jpeg=True, max_side=SMALL))
    assert decode(kept.data).size == (160, 120) and scan_bytes(kept.data).summary.camera == "Apple iPhone 15 Pro"
    assert scan_bytes(kept.data).summary.gps is not None  # metadata kept, as without shrinking


def test_with_the_date_and_the_wide_color_profile():
    from datetime import datetime

    src = b.jpeg(b.photo(), icc=b.P3_ICC)
    opts = ContentOptions(strip_metadata=True, keep_date=True, keep_color_profile=True, max_side=SMALL)
    r = process_bytes(src, opts, taken=datetime(2024, 5, 12, 14, 30, 22))
    assert decode(r.data).size == (160, 120) and r.profile is not None and r.date_exif is not None
    assert decode(r.data).info.get("icc_profile") == r.profile


# ============================================================================== RAW


@pytest.mark.parametrize("preview", [None, "full"])
def test_raw_to_jpeg_shrinks(preview):
    data = make_dng(b.photo(320, 240).transpose(Image.Transpose.ROTATE_90), orientation=6, preview=preview)
    r = process_bytes(data, ContentOptions(strip_metadata=True, convert_to_jpeg=True, max_side=SMALL), name="a.dng")
    out = decode(r.data)
    assert out.size == (160, 120) and note((320, 240), (160, 120)) in r.notes
    assert b.dominant(b.marker_color(out, "tl")) == "red" and scan_bytes(r.data).clean
    kept = process_bytes(data, ContentOptions(convert_to_jpeg=True, max_side=SMALL), name="a.dng")
    assert decode(kept.data).size == (160, 120) and scan_bytes(kept.data).summary.camera == "Fotodit Test Kamera"


def test_raw_cannot_shrink_without_converting(tmp_path):
    data = make_dng(b.photo(320, 240))
    with pytest.raises(ProcessError, match="küçültülemez"):
        process_bytes(data, ContentOptions(max_side=SMALL), name="a.dng")
    assert process_bytes(data, ContentOptions(max_side=4096), name="a.dng").method == UNCHANGED
    p = write(tmp_path / "DSC_0001.NEF", data)
    big = build_plan(photos_of([p]), JobOptions(content=ContentOptions(max_side=SMALL),
                                                output=OutputOptions(folder=str(tmp_path / "o"))))
    assert big[0].skip_reason == t(RAW_RESIZE_NEEDS_JPEG)
    fits = build_plan(photos_of([p]), JobOptions(content=ContentOptions(max_side=4096),
                                                 output=OutputOptions(folder=str(tmp_path / "o"))))
    assert fits[0].skip_reason is None


# ============================================================================= plan


def test_resizing_counts_as_a_content_change(tmp_path):
    opts = JobOptions(content=ContentOptions(max_side=2048))
    assert opts.changes_content() and not JobOptions().changes_content()

    def entry(width, height, fmt=Fmt.JPEG, info=True):
        photo = Photo(str(tmp_path / "a.jpg"), 1, 0.0, 0, PhotoInfo(fmt=fmt, width=width, height=height) if info else None)
        return PlanEntry(photo, 1, str(tmp_path), "a.jpg", fmt, None, "exif")

    assert entry(4032, 3024).changes_content(opts) and not entry(2048, 1536).changes_content(opts)
    assert entry(0, 0, info=False).changes_content(opts)  # not read yet: it may need shrinking
    assert not entry(4032, 3024, fmt=Fmt.MP4).changes_content(opts)  # videos are not resized


def test_runner_resize_only(tmp_path):
    big = write(tmp_path / "src" / "big.jpg", b.jpeg(b.photo(640, 480), everything=False), mtime=1_600_000_000)
    small = write(tmp_path / "src" / "small.jpg", b.jpeg(b.photo(200, 150), everything=False), mtime=1_600_000_000)
    out = tmp_path / "out"
    opts = JobOptions(content=ContentOptions(max_side=320), output=OutputOptions(folder=str(out)))
    report = Runner(build_plan(photos_of([big, small]), opts), opts, recycle_func=FakeBin(str(tmp_path / "bin"))).run()
    assert not report.failed
    assert decode((out / "big.jpg").read_bytes()).size == (320, 240)
    assert (out / "small.jpg").read_bytes() == open(small, "rb").read()  # within the limit: copied as it is
    assert scan_bytes((out / "big.jpg").read_bytes()).summary.camera == "Apple iPhone 15 Pro"  # metadata kept
    assert os.stat(out / "big.jpg").st_mtime == pytest.approx(1_600_000_000, abs=2)
