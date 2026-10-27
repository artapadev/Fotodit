"""Large preview: preparing the image (orientation, color, size)."""

from __future__ import annotations

import io

import builders as b  # noqa: E402
import pytest
from helpers import write_in  # noqa: E402
from PIL import Image, ImageChops, ImageStat  # noqa: E402

from fotodit.core.dngwriter import make_dng  # noqa: E402
from fotodit.core.preview import PreviewError, render  # noqa: E402

DISPLAY = b.photo(640, 480)


def mean_diff(a: Image.Image, c: Image.Image) -> float:
    diff = ImageChops.difference(a.convert("RGB"), c.convert("RGB").resize(a.size))
    return sum(ImageStat.Stat(diff).mean) / 3


# ------------------------------------------------------------ preparing the image


@pytest.mark.parametrize("orientation", range(1, 9))
def test_render_applies_orientation(tmp_path, orientation):
    from fotodit.core.imaging import orient

    inverse = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 8, 7: 7, 8: 6}[orientation]
    stored = orient(DISPLAY, inverse)  # pixels written to the file; a viewer applies the orientation
    path = write_in(tmp_path, "orientation.jpg", b.jpeg(stored, orientation=orientation, everything=False))
    im = render(path, 1920)
    assert im.size == DISPLAY.size
    assert [b.dominant(b.marker_color(im, c)) for c in ("tl", "tr", "bl")] == ["red", "green", "blue"]


def test_render_converts_colors_like_jpg_conversion(tmp_path):
    """Display P3 (JPEG and HEIC) and CMYK colors are converted to sRGB, as in JPG conversion."""
    from fotodit.core.imaging import open_image

    cmyk = io.BytesIO()
    DISPLAY.convert("CMYK").save(cmyk, "JPEG", quality=92, icc_profile=b.cmyk_icc())
    for name, data, icc in (("p3.jpg", b.jpeg(DISPLAY, icc=b.P3_ICC, everything=False), b.P3_ICC),
                            ("p3.heic", b.heic(DISPLAY, icc=b.P3_ICC), b.P3_ICC),
                            ("cmyk.jpg", cmyk.getvalue(), b.cmyk_icc())):
        path = write_in(tmp_path, name, data)
        with open_image(path) as src:
            src.load()
            decoded = src.copy()
        expected = b.srgb_convert(decoded, icc)  # reference: the same pixels converted to sRGB
        im = render(path, 1920)
        assert im.mode == "RGB" and mean_diff(im, expected) < 1.0, name
        assert mean_diff(decoded, expected) > 2.0, name  # the conversion really changed something


def test_render_sizes_alpha_and_errors(tmp_path):
    big = write_in(tmp_path, "large.jpg", b.jpeg(b.photo(2400, 1600), everything=False))
    assert max(render(big, 800).size) == 800  # scaled down to the screen size
    assert render(big, None).size == (2400, 1600)  # 100%: full resolution
    png = render(write_in(tmp_path, "transparent.png", b.png(DISPLAY.convert("RGBA"))), 1920)
    assert png.mode == "RGBA"  # transparency is kept (not flattened onto white)
    with pytest.raises(PreviewError, match="bozuk"):  # "bozuk" (corrupt) comes from the app's Turkish message
        render(write_in(tmp_path, "broken.jpg", b.jpeg(DISPLAY, everything=False)[:500]), 1920)
    with pytest.raises(PreviewError):
        render(write_in(tmp_path, "text.jpg", b"bu bir fotograf degil"), 1920)  # Turkish: "this is not a photo"


def test_render_raw_uses_camera_preview_or_develops(tmp_path):
    full = write_in(tmp_path, "full.dng", make_dng(DISPLAY.transpose(Image.Transpose.ROTATE_90), orientation=6,
                                                preview="full"))
    im = render(full, 1920)
    assert im.size == DISPLAY.size and b.dominant(b.marker_color(im, "tl")) == "red"
    # If only a thumbnail is embedded, the large preview is developed from the RAW data (no upscaled thumbnail).
    small = write_in(tmp_path, "small.dng", make_dng(DISPLAY, preview="small"))
    assert max(render(small, 1920).size) >= 320
    assert max(render(small, 200).size) <= 200
