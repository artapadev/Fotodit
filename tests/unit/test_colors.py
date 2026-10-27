"""Color space tests: generated ICC profiles, nclx, EXIF R03, the safe image opener."""

from __future__ import annotations

import io

import builders as b
import pytest
from helpers import assert_clean, decode
from oracle import psnr
from PIL import Image, UnidentifiedImageError

from fotodit.core import colors, iccgen
from fotodit.core.formats import Fmt
from fotodit.core.imaging import ensure_plugins, open_image
from fotodit.core.meta import scan_bytes
from fotodit.core.processor import (
    LOSSLESS,
    REENCODED,
    UNKNOWN_PROFILE,
    ContentOptions,
    ProcessError,
    process_bytes,
)
from fotodit.i18n import t

STRIP = ContentOptions(strip_metadata=True)
TO_JPEG = ContentOptions(strip_metadata=True, convert_to_jpeg=True)
KEEP_WIDE = ContentOptions(strip_metadata=True, keep_color_profile=True)
KEEP_WIDE_JPEG = ContentOptions(strip_metadata=True, convert_to_jpeg=True, keep_color_profile=True)
P3_COLORANTS = ((0.515102, 0.241196, -0.001053), (0.291965, 0.692236, 0.041882), (0.157153, 0.066568, 0.784073))


def close(a, b_, tol=2e-4):
    return all(abs(x - y) <= tol for x, y in zip(a, b_))


def test_generated_colorants_match_published_values():
    r, g, bl = iccgen.colorants_d50((0.64, 0.33), (0.30, 0.60), (0.15, 0.06), iccgen.D65)
    assert close(r, (0.4361, 0.2225, 0.0139), 5e-4)
    assert close(g, (0.3851, 0.7169, 0.0971), 5e-4)
    assert close(bl, (0.1431, 0.0606, 0.7141), 5e-4)
    r, g, bl = iccgen.colorants_d50(*iccgen.PRIMARIES[12])
    assert close(r, (0.515102, 0.241196, -0.001053), 5e-4)


def test_generated_profiles_behave():
    srgb_like = iccgen.rgb_profile("sRGB test", ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06), iccgen.D65), "srgb")
    assert colors.analyze_icc(srgb_like).srgb_equivalent
    p3 = iccgen.nclx_profile(12, 13)
    info = colors.analyze_icc(p3)
    assert info.valid and not info.srgb_equivalent and info.description == "Display P3 (nclx)"
    assert iccgen.nclx_profile(1, 13) is None and iccgen.nclx_profile(2, 2) is None
    ours = b.srgb_convert(b.photo(), p3)
    reference = b.srgb_convert(b.photo(), b.P3_ICC)
    assert psnr(ours, reference) > 50
    assert not colors.analyze_icc(iccgen.adobe_rgb_profile()).srgb_equivalent


@pytest.mark.parametrize("opts", [STRIP, TO_JPEG], ids=["strip", "jpeg"])
def test_heic_with_only_nclx_p3_is_color_converted(opts):
    ensure_plugins()
    buf = io.BytesIO()
    b.photo().save(buf, "HEIF", quality=95, color_primaries=12, transfer_characteristics=13,
                   matrix_coefficients=6, full_range_flag=1)
    src = buf.getvalue()
    im = decode(src)
    assert "icc_profile" not in im.info and im.info["nclx_profile"]["color_primaries"] == 12
    r = process_bytes(src, opts)
    assert any("Display P3" in n for n in r.notes)
    assert_clean(r.data, r.fmt)
    expected = b.srgb_convert(b.photo(), b.P3_ICC)
    converted, unconverted = psnr(decode(r.data), expected), psnr(decode(r.data), b.photo())
    assert converted > unconverted + 3, (converted, unconverted)
    assert converted > 30  # HEIC is lossy: two generations of compression


def test_camera_adobe_rgb_r03_is_converted():
    e = "<"
    ifd0 = b.Ifd(e).ascii(0x010F, "NIKON CORPORATION").ascii(0x0110, "NIKON D850").pointer(0x8769, "exif")
    exif = b.Ifd(e).short(0xA001, 0xFFFF).pointer(0xA005, "interop")
    interop = b.Ifd(e).ascii(0x0001, "R03")
    raw = b.build_tiff([("ifd0", ifd0), ("exif", exif), ("interop", interop)])
    buf = io.BytesIO()
    b.photo().save(buf, "JPEG", quality=95, exif=b"Exif\x00\x00" + raw)
    r = process_bytes(buf.getvalue(), STRIP)
    assert r.method == REENCODED
    assert any("Adobe RGB" in n for n in r.notes)
    assert_clean(r.data, Fmt.JPEG)
    expected = b.srgb_convert(decode(buf.getvalue()), iccgen.adobe_rgb_profile())
    assert psnr(decode(r.data), expected) > 38

    # If the same camera shot in sRGB mode (R98), colors are left alone and the file is cleaned losslessly
    interop_srgb = b.Ifd(e).ascii(0x0001, "R98")
    exif_srgb = b.Ifd(e).short(0xA001, 1).pointer(0xA005, "interop")
    raw = b.build_tiff([("ifd0", ifd0), ("exif", exif_srgb), ("interop", interop_srgb)])
    buf = io.BytesIO()
    b.photo().save(buf, "JPEG", quality=95, exif=b"Exif\x00\x00" + raw)
    assert process_bytes(buf.getvalue(), STRIP).method == "lossless"


def test_unexpected_decoders_are_refused(tmp_path):
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
    with pytest.raises(ProcessError):
        process_bytes(eps, STRIP)
    path = tmp_path / "innocent.jpg"
    path.write_bytes(eps)
    with pytest.raises(UnidentifiedImageError):
        open_image(str(path))
    # Allowed formats do open
    assert Image.open(io.BytesIO(b.jpeg(everything=False))).format == "JPEG"
    assert scan_bytes(eps).clean


# ------------------------------------------------------------- keeping wide color spaces


def test_standard_wide_profiles_are_recognized():
    p3 = colors.standard_profile(b.P3_ICC)
    assert p3 is not None and p3.name == "Display P3" and p3.icc != b.P3_ICC and colors.is_clean_profile(p3.icc)
    assert colors.standard_profile(iccgen.nclx_profile(12, 13)) == p3
    assert colors.standard_profile(iccgen.adobe_rgb_profile()).name == "Adobe RGB"
    assert colors.standard_profile(iccgen.rgb_profile("x", iccgen.PROPHOTO_RGB, 1.8)).name == "ProPhoto RGB"
    assert colors.standard_profile(b.SRGB_ICC) is None and colors.standard_profile(None) is None
    assert colors.standard_profile(b.make_icc("Linear P3", *P3_COLORANTS, gamma=1.0)) is None  # not a standard
    assert colors.standard_profile(b.cmyk_icc()) is None and colors.standard_profile(b"not a profile") is None
    assert not colors.is_clean_profile(b.P3_ICC) and not colors.is_clean_profile(None)


def _wide(fmt: str) -> bytes:
    return {"jpeg": lambda: b.jpeg(b.photo(), icc=b.P3_ICC), "png": lambda: b.png(b.photo(), icc=b.P3_ICC),
            "webp": lambda: b.webp(b.photo(), icc=b.P3_ICC), "tiff": lambda: b.tiff(pages=1, icc=b.P3_ICC),
            "heic": lambda: b.heic(b.photo(), icc=b.P3_ICC)}[fmt]()


@pytest.mark.parametrize("fmt", ["jpeg", "png", "webp", "tiff", "heic"])
def test_keeping_wide_color_embeds_a_clean_profile(fmt):
    """The pixels stay in Display P3 and a clean standard profile replaces the source's (which can name
    the device); without the option the colors are converted to sRGB."""
    ensure_plugins()
    src = _wide(fmt)
    clean = colors.standard_profile(b.P3_ICC).icc
    r = process_bytes(src, KEEP_WIDE)
    assert r.profile == clean
    assert "Geniş renk alanı korundu (Display P3; temiz, standart bir renk profili eklendi)." in r.notes
    assert_clean(r.data, r.fmt, profile=clean)
    out, source = decode(r.data).convert("RGB"), decode(src).convert("RGB")
    assert psnr(out, source) > psnr(out, b.srgb_convert(source, b.P3_ICC)) + 3  # not converted
    if fmt == "jpeg":
        assert r.method == LOSSLESS  # only the profile was swapped
    plain = process_bytes(src, STRIP)
    assert plain.profile is None and decode(plain.data).info.get("icc_profile") is None


def test_keeping_wide_color_when_converting_to_jpeg():
    ensure_plugins()
    clean = colors.standard_profile(b.P3_ICC).icc
    for src in (b.png(b.photo(), icc=b.P3_ICC), b.heic(b.photo(), icc=b.P3_ICC)):
        r = process_bytes(src, KEEP_WIDE_JPEG)
        assert r.fmt == Fmt.JPEG and r.profile == clean
        assert_clean(r.data, Fmt.JPEG, profile=clean)


def test_keeping_wide_color_converts_profiles_that_are_not_standard():
    odd = b.make_icc("Linear P3", *P3_COLORANTS, gamma=1.0)
    r = process_bytes(b.jpeg(b.photo(), icc=odd, everything=False), KEEP_WIDE)
    assert r.profile is None and r.method == REENCODED
    assert t(UNKNOWN_PROFILE, source="Linear P3") in r.notes
    assert_clean(r.data, Fmt.JPEG)
