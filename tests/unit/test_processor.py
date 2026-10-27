"""End-to-end tests of the metadata stripping and JPG conversion engine."""

from __future__ import annotations

import io
import struct
import zlib

import builders as b
import pytest
from helpers import assert_clean, assert_corners, decode, stored_for
from oracle import exiv2_metadata, psnr
from PIL import Image, ImageOps, JpegImagePlugin

from fotodit.core.formats import Fmt
from fotodit.core.imaging import open_image
from fotodit.core.meta import Kind, scan_bytes
from fotodit.core.meta import jpeg as jpeg_meta
from fotodit.core.meta import png as png_meta
from fotodit.core.meta import webp as webp_meta
from fotodit.core.meta.exif import read_orientation
from fotodit.core.processor import (
    CONVERTED,
    LOSSLESS,
    REENCODED,
    ContentOptions,
    ProcessError,
    process_bytes,
)

STRIP = ContentOptions(strip_metadata=True, convert_to_jpeg=False)
TO_JPEG = ContentOptions(strip_metadata=True, convert_to_jpeg=True, jpeg_quality=95)
TO_JPEG_KEEP = ContentOptions(strip_metadata=False, convert_to_jpeg=True, jpeg_quality=95)

def scan_segments(data: bytes) -> list[bytes]:
    layout = jpeg_meta.parse(data)
    return [data[s.start:s.end] for s in layout.segments if s.marker == jpeg_meta.SOS]


# ============================================================================ JPEG


def test_jpeg_all_metadata_found_then_removed_losslessly():
    src = b.jpeg()
    kinds = scan_bytes(src).kinds()
    expected = {
        Kind.EXIF, Kind.GPS, Kind.MAKERNOTE, Kind.THUMBNAIL, Kind.XMP, Kind.IPTC, Kind.PHOTOSHOP,
        Kind.EXTRA_IMAGE, Kind.COMMENT, Kind.C2PA, Kind.TRAILER, Kind.OTHER,
    }
    assert expected <= kinds, expected - kinds
    meta = exiv2_metadata(src)
    assert meta["exif"] and meta["iptc"] and meta["xmp"]

    r = process_bytes(src, STRIP)
    assert r.method == LOSSLESS
    assert_clean(r.data, Fmt.JPEG)
    assert scan_segments(src) == scan_segments(r.data), "compressed image data must not change"
    assert decode(src).tobytes() == decode(r.data).tobytes()
    assert len(r.data) < len(src)
    assert b"SEFT" not in r.data and b"ftyp" not in r.data


def test_jpeg_summary_extracts_camera_date_gps():
    res = scan_bytes(b.jpeg())
    s = res.summary
    assert s.camera == "Apple iPhone 15 Pro"
    assert s.taken is not None and (s.taken.year, s.taken.month, s.taken.day, s.taken.hour) == (2024, 5, 12, 14)
    assert s.gps is not None and abs(s.gps[0] - 41.00836) < 1e-3 and abs(s.gps[1] - 28.97833) < 1e-3
    exif_detail = next(f.detail for f in res.findings if f.kind == Kind.EXIF)
    assert "gövde seri no." in exif_detail and "Windows yazar bilgisi" in exif_detail


@pytest.mark.parametrize("orientation", range(1, 9))
def test_jpeg_orientation_is_baked_into_pixels(orientation):
    displayed = b.photo(320, 240)
    src = b.jpeg(stored_for(displayed, orientation), orientation=orientation, everything=False)
    viewer = ImageOps.exif_transpose(decode(src))
    r = process_bytes(src, STRIP)
    out = decode(r.data)
    assert_clean(r.data, Fmt.JPEG)
    assert out.size == displayed.size
    assert_corners(out)
    assert psnr(out, viewer) > 42
    assert r.method == (LOSSLESS if orientation == 1 else REENCODED)


def test_jpeg_rotation_reuses_quantization_tables():
    src = b.jpeg(stored_for(b.photo(640, 480), 6), orientation=6, everything=False, quality=77)
    r = process_bytes(src, STRIP)
    q_src, q_out = decode(src).quantization, decode(r.data).quantization
    for k in q_src:
        t = list(q_src[k])
        transposed = [t[(i % 8) * 8 + i // 8] for i in range(64)]
        assert list(q_out[k]) == transposed
    # size must stay close to the source (quality neither lowered nor inflated)
    assert 0.8 < len(r.data) / len(src) < 1.25


def test_jpeg_p3_profile_converted_to_srgb():
    src = b.jpeg(b.photo(), icc=b.P3_ICC, everything=False)
    r = process_bytes(src, STRIP)
    assert r.method == REENCODED
    assert_clean(r.data, Fmt.JPEG)
    expected = b.srgb_convert(decode(src), b.P3_ICC)
    assert psnr(decode(r.data), expected) > 38
    assert psnr(decode(r.data), decode(src)) < psnr(decode(r.data), expected)
    assert any("sRGB" in n for n in r.notes)


def test_jpeg_srgb_profile_dropped_losslessly():
    src = b.jpeg(b.photo(), icc=b.SRGB_ICC, everything=False)
    r = process_bytes(src, STRIP)
    assert r.method == LOSSLESS
    assert_clean(r.data, Fmt.JPEG)


def test_cmyk_jpeg_with_profile_converted_correctly():
    buf = io.BytesIO()
    b.photo().convert("CMYK").save(buf, "JPEG", quality=92, icc_profile=b.cmyk_icc())
    src = buf.getvalue()
    r = process_bytes(src, STRIP)
    out = decode(r.data)
    assert out.mode == "RGB"
    assert_clean(r.data, Fmt.JPEG)
    expected = b.srgb_convert(decode(src), b.cmyk_icc())
    assert psnr(out, expected) > 35


def test_cmyk_jpeg_without_profile_keeps_adobe_marker():
    buf = io.BytesIO()
    b.photo().convert("CMYK").save(buf, "JPEG", quality=92)
    src = b.insert_after_soi(buf.getvalue(), b.seg(0xE1, b"Exif\x00\x00" + b.rich_exif(1)))
    assert jpeg_meta.parse(src).has_adobe_marker()
    r = process_bytes(src, STRIP)
    assert r.method == LOSSLESS
    assert jpeg_meta.parse(r.data).has_adobe_marker()
    assert decode(r.data).tobytes() == decode(src).tobytes()
    assert_clean(r.data, Fmt.JPEG)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"progressive": True},
        {"restart_marker_blocks": 3},
        {"subsampling": "4:4:4"},
        {"subsampling": "4:2:2"},
    ],
)
def test_jpeg_variants_lossless(kwargs):
    src = b.jpeg(b.photo(333, 211), **kwargs)
    r = process_bytes(src, STRIP)
    assert r.method == LOSSLESS
    assert decode(r.data).tobytes() == decode(src).tobytes()
    assert_clean(r.data, Fmt.JPEG)


def test_grayscale_jpeg_lossless():
    src = b.jpeg(b.photo().convert("L"))
    r = process_bytes(src, STRIP)
    assert r.method == LOSSLESS and decode(r.data).mode == "L"
    assert_clean(r.data, Fmt.JPEG)


def test_jfif_thumbnail_removed():
    src = b.jpeg_with_jfif_thumbnail()
    assert Kind.THUMBNAIL in scan_bytes(src).kinds()
    r = process_bytes(src, STRIP)
    assert_clean(r.data, Fmt.JPEG)
    assert r.data[2:4] == b"\xff\xe0" and struct.unpack(">H", r.data[4:6])[0] == 16


def test_422_rotated_uses_444_to_keep_chroma():
    src = b.jpeg(stored_for(b.photo(), 6), orientation=6, everything=False, subsampling="4:2:2")
    r = process_bytes(src, STRIP)
    assert JpegImagePlugin.get_sampling(decode(r.data)) == 0


def test_truncated_jpeg_rejected():
    src = b.jpeg(everything=False)
    with pytest.raises(ProcessError):
        process_bytes(src[: len(src) // 2], STRIP)
    with pytest.raises(ProcessError):
        process_bytes(src[:40], STRIP)


def test_strip_is_idempotent():
    once = process_bytes(b.jpeg(), STRIP).data
    twice = process_bytes(once, STRIP).data
    assert once == twice


# ============================================================================= PNG


def test_png_all_metadata_removed_losslessly():
    src = b.png()
    kinds = scan_bytes(src).kinds()
    assert {Kind.TEXT, Kind.XMP, Kind.EXIF, Kind.GPS, Kind.TIMESTAMP, Kind.C2PA, Kind.OTHER, Kind.TRAILER} <= kinds
    text = next(f.detail for f in scan_bytes(src).findings if f.kind == Kind.TEXT)
    assert "yapay zekâ" in text
    r = process_bytes(src, STRIP)
    assert r.method == LOSSLESS
    assert_clean(r.data, Fmt.PNG)
    chunks = {c for c, *_ in png_meta.iter_chunks(r.data)}
    assert chunks <= png_meta.KEEP
    assert decode(r.data).tobytes() == decode(src).tobytes()


def test_png_palette_transparency_and_16bit_preserved():
    pal = b.photo(64, 48).quantize(16)
    pal.info["transparency"] = 3
    buf = io.BytesIO()
    pal.save(buf, "PNG", transparency=3)
    src = png_meta_insert_text(buf.getvalue())
    r = process_bytes(src, STRIP)
    assert r.method == LOSSLESS
    assert decode(r.data).convert("RGBA").tobytes() == decode(src).convert("RGBA").tobytes()

    g16 = Image.linear_gradient("L").resize((64, 64)).convert("I").point(lambda v: v * 257).convert("I;16")
    buf = io.BytesIO()
    g16.save(buf, "PNG")
    src16 = png_meta_insert_text(buf.getvalue())
    r16 = process_bytes(src16, STRIP)
    out = decode(r16.data)
    assert out.mode == "I;16" and out.tobytes() == decode(src16).tobytes()


def png_meta_insert_text(data: bytes) -> bytes:
    return b.insert_png_chunks(data, b.png_chunk(b"tEXt", b"Author\x00Ayse"))


def test_png_p3_converted_and_orientation_applied():
    r = process_bytes(b.png(b.photo(), icc=b.P3_ICC), STRIP)
    assert r.method == REENCODED
    assert_clean(r.data, Fmt.PNG)
    expected = b.srgb_convert(b.photo(), b.P3_ICC)
    assert psnr(decode(r.data), expected) > 45

    displayed = b.photo()
    r = process_bytes(b.png(stored_for(displayed, 6), orientation=6), STRIP)
    out = decode(r.data)
    assert out.size == displayed.size
    assert out.tobytes() == displayed.tobytes()  # PNG: rotation is lossless


def test_apng_animation_kept():
    frames = [b.photo(80, 60), b.photo(80, 60).rotate(180)]
    buf = io.BytesIO()
    frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:], duration=100, loop=0)
    src = png_meta_insert_text(buf.getvalue())
    r = process_bytes(src, STRIP)
    assert r.method == LOSSLESS
    assert decode(r.data).n_frames == 2


# ============================================================================ WebP


@pytest.mark.parametrize("lossless", [False, True])
def test_webp_all_metadata_removed_losslessly(lossless):
    src = b.webp(lossless=lossless)
    assert {Kind.EXIF, Kind.XMP, Kind.OTHER, Kind.TRAILER} <= scan_bytes(src).kinds()
    r = process_bytes(src, STRIP)
    assert r.method == LOSSLESS
    assert_clean(r.data, Fmt.WEBP)
    assert decode(r.data).tobytes() == decode(src).tobytes()
    assert webp_meta.is_lossless(r.data) == lossless


def test_webp_p3_reencoded_and_animated_kept():
    r = process_bytes(b.webp(icc=b.P3_ICC, lossless=True), STRIP)
    assert r.method == REENCODED and webp_meta.is_lossless(r.data)
    assert_clean(r.data, Fmt.WEBP)
    assert psnr(decode(r.data), b.srgb_convert(b.photo(), b.P3_ICC)) > 45

    anim = b.animated_webp()
    r = process_bytes(anim, STRIP)
    assert r.method == LOSSLESS
    assert decode(r.data).n_frames == 3
    assert_clean(r.data, Fmt.WEBP)


# ======================================================================= GIF / BMP


def test_gif_comments_and_xmp_removed_animation_kept():
    src = b.gif()
    kinds = scan_bytes(src).kinds()
    assert {Kind.COMMENT, Kind.XMP, Kind.IPTC, Kind.TRAILER} <= kinds
    r = process_bytes(src, STRIP)
    assert r.method == LOSSLESS
    assert_clean(r.data, Fmt.GIF, oracle=False)
    out = decode(r.data)
    assert out.n_frames == 2 and out.info.get("loop") == 0
    assert b"NETSCAPE2.0" in r.data


def test_bmp_plain_unchanged_and_profiles_removed():
    src = b.bmp()
    r = process_bytes(src, STRIP)
    assert r.data == src

    for linked in (False, True):
        src = b.bmp_v5_with_profile(b.P3_ICC, linked=linked)
        assert scan_bytes(src).findings
        r = process_bytes(src, STRIP)
        assert r.method == REENCODED
        assert_clean(r.data, Fmt.BMP, oracle=False)
        assert b"ayse" not in r.data


# =========================================================================== TIFF


def test_tiff_tags_removed_pages_kept():
    src = b.tiff(pages=2)
    kinds = scan_bytes(src).kinds()
    assert {Kind.EXIF, Kind.XMP, Kind.IPTC} <= kinds
    r = process_bytes(src, STRIP)
    assert_clean(r.data, Fmt.TIFF)
    a, o = open_image(io.BytesIO(src)), open_image(io.BytesIO(r.data))
    assert o.n_frames == 2
    for i in range(2):
        a.seek(i)
        o.seek(i)
        assert a.convert("RGB").tobytes() == o.convert("RGB").tobytes()


def test_tiff_jpeg_compression_becomes_lzw_and_p3_converted():
    r = process_bytes(b.tiff(pages=1, icc=b.P3_ICC, compression="jpeg"), STRIP)
    assert_clean(r.data, Fmt.TIFF)
    assert any("LZW" in n for n in r.notes) and any("sRGB" in n for n in r.notes)


# ====================================================================== HEIC / AVIF


@pytest.mark.parametrize("builder", [b.heic, b.avif])
def test_heif_family_reencoded_clean(builder):
    fmt = Fmt.HEIF if builder is b.heic else Fmt.AVIF
    src = builder(icc=b.P3_ICC)
    kinds = scan_bytes(src).kinds()
    assert {Kind.EXIF, Kind.XMP, Kind.ICC, Kind.GPS} <= kinds
    r = process_bytes(src, STRIP)
    assert r.method == REENCODED
    assert_clean(r.data, fmt)
    out = decode(r.data)
    assert out.size == (320, 240)
    assert psnr(out, b.srgb_convert(b.photo(), b.P3_ICC)) > 30


@pytest.mark.parametrize("builder", [b.heic, b.avif])
@pytest.mark.parametrize("orientation", [3, 6, 8])
def test_heif_family_orientation(builder, orientation):
    displayed = b.photo(320, 240)
    src = builder(stored_for(displayed, orientation), orientation=orientation)
    for opts in (STRIP, TO_JPEG):
        out = decode(process_bytes(src, opts).data)
        assert out.size == displayed.size
        assert_corners(out)


# ==================================================================== JPG conversion


@pytest.mark.parametrize(
    "name,make,sampling",
    [
        ("png", lambda: b.png(), 0),
        ("webp-lossy", lambda: b.webp(), 2),
        ("webp-lossless", lambda: b.webp(lossless=True), 0),
        ("heic", lambda: b.heic(), 2),
        ("avif", lambda: b.avif(), 2),
        ("tiff", lambda: b.tiff(pages=1), 0),
        ("bmp", lambda: b.bmp(), 0),
        ("gif", lambda: b.gif(animated=False), 0),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_convert_to_jpeg_clean(name, make, sampling):
    src = make()
    r = process_bytes(src, TO_JPEG)
    assert r.method == CONVERTED and r.fmt == Fmt.JPEG
    assert_clean(r.data, Fmt.JPEG)
    out = decode(r.data)
    assert out.format == "JPEG" and out.mode == "RGB"
    assert JpegImagePlugin.get_sampling(out) == sampling
    if name not in ("gif",):
        assert psnr(out, b.photo()) > 30


def test_convert_transparency_to_white():
    im = b.photo().convert("RGBA")
    im.putalpha(Image.new("L", im.size, 255))
    im.paste((0, 0, 0, 0), (100, 100, 200, 200))  # fully transparent, black underneath
    buf = io.BytesIO()
    im.save(buf, "PNG")
    r = process_bytes(buf.getvalue(), TO_JPEG)
    px = decode(r.data).getpixel((150, 150))
    assert all(c >= 250 for c in px), px
    assert any("beyaz" in n for n in r.notes)


def test_convert_16bit_gray_scaled_not_clipped():
    g16 = Image.new("I;16", (64, 64), 32768)
    buf = io.BytesIO()
    g16.save(buf, "PNG")
    out = decode(process_bytes(buf.getvalue(), TO_JPEG).data)
    assert out.mode == "L" and abs(out.getpixel((10, 10)) - 128) <= 2


def test_convert_cmyk_tiff_uses_profile():
    buf = io.BytesIO()
    b.photo().convert("CMYK").save(buf, "TIFF", icc_profile=b.cmyk_icc())
    r = process_bytes(buf.getvalue(), TO_JPEG)
    expected = b.srgb_convert(b.photo().convert("CMYK"), b.cmyk_icc())
    assert psnr(decode(r.data), expected) > 35


def test_convert_animated_gif_first_frame_with_note():
    r = process_bytes(b.gif(animated=True), TO_JPEG)
    assert any("ilk kare" in n for n in r.notes)


def test_convert_keeping_metadata():
    src = b.heic(stored_for(b.photo(), 6), orientation=6, icc=b.P3_ICC)
    r = process_bytes(src, TO_JPEG_KEEP)
    out = decode(r.data)
    assert_corners(out)
    exif_raw = jpeg_meta.parse(r.data).exif()
    assert exif_raw is not None and read_orientation(exif_raw) == 1
    res = scan_bytes(r.data)
    assert res.summary.camera == "Apple iPhone 15 Pro" and res.summary.gps is not None
    assert jpeg_meta.parse(r.data).icc_profile() == b.P3_ICC
    assert Kind.XMP in res.kinds()
    # colors are not converted: the P3 pixels travel with the P3 profile
    assert psnr(out, b.photo()) > 30


def test_jpeg_source_with_convert_option_is_just_stripped():
    r = process_bytes(b.jpeg(), TO_JPEG)
    assert r.method == LOSSLESS and r.fmt == Fmt.JPEG


# ============================================== hidden data locations from the research


def _png_from_raw(w: int, h_declared: int, raw_rows: bytes, extra_after_stream: bytes = b"") -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h_declared, 8, 2, 0, 0, 0)
    return (
        png_meta.SIGNATURE + b.png_chunk(b"IHDR", ihdr)
        + b.png_chunk(b"IDAT", zlib.compress(raw_rows) + extra_after_stream) + b.png_chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("variant", ["taller-stream", "after-stream"])
def test_png_hidden_idat_data_removed(variant):
    """The uncropped original of a cropped screenshot is still hidden inside IDAT (aCropalypse)."""
    full = b.photo(64, 96)
    rows = b"".join(b"\x00" + full.crop((0, y, 64, y + 1)).tobytes() for y in range(96))
    visible_rows = rows[: 64 * (1 + 64 * 3)]
    if variant == "taller-stream":
        src = _png_from_raw(64, 64, rows)
    else:
        src = _png_from_raw(64, 64, visible_rows, extra_after_stream=b"SECRET-ORIGINAL-SCREENSHOT" * 20)
    assert png_meta.hidden_idat_data(src) > 0
    assert Kind.TRAILER in scan_bytes(src).kinds()
    r = process_bytes(src, STRIP)
    assert png_meta.hidden_idat_data(r.data) == 0
    assert_clean(r.data, Fmt.PNG)
    assert decode(r.data).tobytes() == full.crop((0, 0, 64, 64)).tobytes()
    assert any("gizlenmiş" in n for n in r.notes)


def test_gif_plain_text_extension_removed():
    data = b.gif(animated=False)
    packed = data[10]
    pos = 13 + 3 * (2 << (packed & 7))
    plain = b"\x21\x01\x0c" + bytes(12) + b"\x0bgizli metin" + b"\x00"
    src = data[:pos] + plain + data[pos:]
    assert Kind.TEXT in scan_bytes(src).kinds()
    r = process_bytes(src, STRIP)
    assert b"gizli metin" not in r.data
    assert_clean(r.data, Fmt.GIF, oracle=False)


def test_bmp_trailing_data_removed():
    src = b.bmp() + b"hidden trailing bytes"
    assert Kind.TRAILER in scan_bytes(src).kinds()
    r = process_bytes(src, STRIP)
    assert r.data == b.bmp()


def test_webp_c2pa_chunk_detected_and_removed():
    data = b.webp()
    body = data[12:data.find(b"TRAILER")] + b"C2PA" + struct.pack("<I", 8) + b"manifest"
    src = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body
    assert Kind.C2PA in scan_bytes(src).kinds()
    r = process_bytes(src, STRIP)
    assert b"C2PA" not in r.data and b"manifest" not in r.data
    assert_clean(r.data, Fmt.WEBP)


def test_heic_motion_photo_box_detected_and_removed():
    src = b.heic() + struct.pack(">I", 24) + b"mpvd" + b"\x00\x00\x00\x10ftypmp42" + b"\x00" * 4
    assert Kind.TRAILER in scan_bytes(src).kinds()
    r = process_bytes(src, STRIP)
    assert b"mpvd" not in r.data
    assert_clean(r.data, Fmt.HEIF)


def test_garbage_rejected():
    with pytest.raises(ProcessError):
        process_bytes(b"this is not an image at all", STRIP)
    png = b.png(everything=False)
    with pytest.raises(ProcessError):
        process_bytes(png[: len(png) - 40], STRIP)
