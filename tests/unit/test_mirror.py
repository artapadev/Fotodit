"""Horizontal flip ("Yatay çevir"): the edit model, `Edit.mirrored`, every photo path (the JPEG block grid
included), RAW, video, the self-test checks and the reworded messages."""

from __future__ import annotations

import io
import random
from dataclasses import dataclass

import builders as b
import pytest
from helpers import assert_clean, stored_for
from oracle import psnr
from PIL import Image, ImageChops, ImageOps, ImageStat
from video_builders import make_video, upright_frame

from fotodit import selftest
from fotodit.core import processor
from fotodit.core.dngwriter import make_dng
from fotodit.core.edits import MIRRORED_QUARTER, QUARTER, Edit, apply
from fotodit.core.formats import Fmt
from fotodit.core.imaging import ensure_plugins, open_image
from fotodit.core.meta import jpeg as jpeg_meta
from fotodit.core.meta import webp as webp_meta
from fotodit.core.meta.exif import read_orientation
from fotodit.core.meta.model import LABELS, Kind
from fotodit.core.processor import (
    RAW_EDIT_NEEDS_JPEG,
    RAW_NEEDS_JPEG,
    RAW_RESIZE_NEEDS_JPEG,
    REENCODED,
    ContentOptions,
    process_bytes,
)
from fotodit.core.rawdev import RAW_AUTO, RAW_DEVELOP
from fotodit.core.video.probe import probe
from fotodit.core.video.process import (
    VIDEO_EDIT_NEEDS_MP4,
    edit_reason,
    plan_video,
    process_video,
)
from fotodit.i18n import t

STRIP = ContentOptions(strip_metadata=True)
MIRROR = Edit(mirror=True)
PHOTO = b.photo(320, 240)


def decode(data: bytes) -> Image.Image:
    im = open_image(io.BytesIO(data))
    im.load()
    return im


def corners(im: Image.Image) -> dict[str, str]:
    return {c: b.dominant(b.marker_color(im, c)) for c in ("tl", "tr", "bl", "br")}


# Red top left, green top right, blue bottom left (see builders.photo); flipped left-right:
FLIPPED = {"tl": "green", "tr": "red", "bl": "other", "br": "blue"}


# ========================================================================= the edit


def test_mirror_in_the_edit_model():
    assert not MIRROR.is_identity() and MIRROR.has_geometry()
    assert MIRROR.photo_only() == MIRROR and MIRROR.geometry_only() == MIRROR
    assert Edit(mirror=True, exposure=1.0, trim=(1.0, None)).geometry_only() == MIRROR
    assert MIRROR.turn() == Image.Transpose.FLIP_LEFT_RIGHT and Edit().turn() is None
    assert Edit(quarter=1, mirror=True).rotated(1) == Edit(quarter=2, mirror=True)
    assert MIRROR.describe() == "yatay çevrildi" and MIRROR.describe(future=True) == "yatay çevrilecek"
    assert Edit(quarter=1, mirror=True).describe(future=True) == "yatay çevrilecek · sağa 90° döndürülecek"
    assert MIRROR.short() == "yatay çevirme" and Edit(quarter=1, mirror=True).short() == "yatay çevirme · ↻ 90°"
    assert MIRROR.output_size(320, 240) == (320, 240) and Edit(quarter=1, mirror=True).output_size(320, 240) == (240, 320)


def test_mirrored_maps_every_field():
    edit = Edit(quarter=1, angle=3.5, crop=(0.1, 0.2, 0.5, 0.6), exposure=0.7, trim=(1.0, 2.0), cuts=((1.2, 1.4),),
                mute=True)
    assert edit.mirrored() == Edit(quarter=3, angle=-3.5, crop=(0.5, 0.2, 0.9, 0.6), exposure=0.7, trim=(1.0, 2.0),
                                   cuts=((1.2, 1.4),), mute=True, mirror=True)
    assert edit.mirrored().mirrored() == edit and Edit().mirrored() == MIRROR and MIRROR.mirrored() == Edit()


def test_one_transpose_for_the_flip_and_the_turn():
    im = b.photo(37, 23)
    for quarter in range(4):
        two = im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if quarter:
            two = two.transpose(QUARTER[quarter])
        assert im.transpose(MIRRORED_QUARTER[quarter]).tobytes() == two.tobytes()
        assert apply(im, Edit(quarter=quarter, mirror=True)).tobytes() == two.tobytes()  # the flip comes first


def _random_edit(rng: random.Random) -> Edit:
    crop = None
    if rng.random() < 0.7:
        x0, y0 = rng.uniform(0, 0.6), rng.uniform(0, 0.6)
        crop = (x0, y0, rng.uniform(x0 + 0.15, 1), rng.uniform(y0 + 0.15, 1))
    angle = rng.uniform(-30, 30) if rng.random() < 0.4 else 0.0
    return Edit(quarter=rng.randrange(4), angle=angle, crop=crop, mirror=rng.random() < 0.5, exposure=rng.choice((0, 1)))


@pytest.mark.parametrize("size", [(64, 48), (63, 47), (40, 70)])
def test_mirrored_flips_the_result_on_screen(size):
    """For any edit: applying `mirrored()` gives the same pixels as flipping the edited result."""
    rng = random.Random(size[0] * 1000 + size[1])
    image = b.photo(*size)
    for _ in range(150):
        edit = _random_edit(rng)
        expected = ImageOps.mirror(apply(image, edit))
        got = apply(image, edit.mirrored())
        assert got.size == expected.size, edit
        diff = ImageChops.difference(got, expected)
        if not edit.angle:
            assert diff.getbbox() is None, edit  # flips, quarter turns and crops are exact
        else:  # straightening resamples: within interpolation tolerance
            assert max(high for _, high in diff.getextrema()) <= 8 and max(ImageStat.Stat(diff).mean) < 0.5, edit
        assert edit.mirrored().mirrored() == edit


# ==================================================================== photo formats


def test_jpeg_flip_keeps_the_tables():
    src = b.jpeg(PHOTO, everything=False, quality=85)
    r = process_bytes(src, STRIP, edit=MIRROR)
    out = decode(r.data)
    assert r.method == REENCODED and out.size == (320, 240) and corners(out) == FLIPPED
    assert out.quantization == decode(src).quantization
    assert psnr(out, ImageOps.mirror(decode(src))) > 40
    assert_clean(r.data, Fmt.JPEG)


@pytest.mark.parametrize("orientation", [1, 3, 6, 8])
@pytest.mark.parametrize("quarter", [0, 1])
def test_jpeg_flip_acts_on_the_upright_photo(orientation, quarter):
    src = b.jpeg(stored_for(PHOTO, orientation), orientation=orientation, everything=False)
    edit = Edit(quarter=quarter, mirror=True)
    out = decode(process_bytes(src, STRIP, edit=edit).data)
    expected = apply(ImageOps.exif_transpose(decode(src)), edit)  # the edit acts on the photo as displayed
    assert out.size == expected.size and corners(out) == corners(apply(PHOTO, edit))
    assert psnr(out, expected) > 40


@pytest.mark.parametrize("edit", [
    Edit(mirror=True, crop=(0.1, 0.15, 0.7, 0.8)),
    Edit(mirror=True, quarter=1, crop=(0.2, 0.1, 0.9, 0.6)),
    Edit(mirror=True, quarter=3, crop=(0.05, 0.3, 0.55, 0.95)),
], ids=["flip", "flip-right", "flip-left"])
def test_jpeg_flipped_crop_lands_on_the_source_blocks(edit):
    """A crop is moved onto the source's 8/16-pixel block grid; after a flip that grid starts at the other
    edge. Aligned blocks are re-encoded almost losslessly with the source's own tables."""
    im = b.photo(330, 250)  # not a multiple of the 16-pixel block: the flipped grid is shifted
    src = b.jpeg(im, everything=False, quality=90)
    source = decode(src)
    whole = apply(source, Edit(quarter=edit.quarter, mirror=True))
    left, top, w, h = edit.pixels_on_canvas(*whole.size)
    gx, gy = processor._block_grid(source, 1, edit.turn())
    left, top = processor._snap(int(left), w, whole.width, gx), processor._snap(int(top), h, whole.height, gy)
    out = decode(process_bytes(src, STRIP, edit=edit).data)
    region = whole.crop((left, top, left + w, top + h))
    assert out.size == edit.output_size(330, 250) == region.size
    # The same blocks again: about 44 dB here, where a crop off the grid requantizes every block (about 36 dB).
    assert psnr(out, region) > 42


def test_block_grid_after_a_flip():
    buf = io.BytesIO()
    b.photo(330, 250).save(buf, "JPEG", quality=90)  # 4:2:0: 16-pixel blocks
    im = open_image(io.BytesIO(buf.getvalue()))
    grid = processor._block_grid
    assert grid(im, 1, None) == [(0, 16), (0, 16)]
    assert grid(im, 1, Edit(mirror=True).turn()) == [(330 % 16, 16), (0, 16)]  # x counted from the right edge
    assert grid(im, 1, Edit(mirror=True, quarter=1).turn()) == [(250 % 16, 16), (330 % 16, 16)]
    assert grid(im, 1, Edit(mirror=True, quarter=3).turn()) == [(0, 16), (0, 16)]
    assert grid(im, 1, Edit(mirror=True, quarter=2).turn()) == [(0, 16), (250 % 16, 16)]


@pytest.mark.parametrize("name,make,exact", [
    ("png", lambda: b.png(PHOTO), True), ("webp-lossless", lambda: b.webp(PHOTO, lossless=True), True),
    ("tiff", lambda: b.tiff(pages=1), True), ("bmp", lambda: b.bmp(PHOTO), True),
    ("webp", lambda: b.webp(PHOTO), False), ("heic", lambda: b.heic(PHOTO), False), ("avif", lambda: b.avif(PHOTO), False),
], ids=lambda v: v if isinstance(v, str) else "")
def test_other_formats(name, make, exact):
    ensure_plugins()
    src = make()
    r = process_bytes(src, STRIP, edit=MIRROR)
    out = decode(r.data).convert("RGB")
    expected = ImageOps.mirror(decode(src).convert("RGB"))
    assert out.size == expected.size and corners(out) == FLIPPED
    if exact:
        assert out.tobytes() == expected.tobytes()  # lossless formats: exactly the flipped pixels
    else:
        assert psnr(out, expected) > 30


def test_flip_while_converting_and_keeping_metadata():
    heic = decode(process_bytes(b.heic(PHOTO), ContentOptions(strip_metadata=True, convert_to_jpeg=True),
                                edit=Edit(mirror=True, quarter=2)).data)
    assert corners(heic) == corners(apply(PHOTO, Edit(mirror=True, quarter=2)))
    kept = process_bytes(b.jpeg(stored_for(PHOTO, 6), orientation=6), ContentOptions(), edit=MIRROR)
    assert corners(decode(kept.data)) == FLIPPED and read_orientation(jpeg_meta.parse(kept.data).exif()) == 1
    frames = process_bytes(b.animated_webp(), STRIP, edit=MIRROR)
    source = decode(b.animated_webp())
    out = decode(frames.data)
    assert out.n_frames == source.n_frames
    for index in range(out.n_frames):
        out.seek(index)
        source.seek(index)
        assert out.convert("RGBA").tobytes() == ImageOps.mirror(source.convert("RGBA")).tobytes()


def test_animated_lossless_webp_stays_lossless():
    """An animation's frames sit inside ANMF chunks: a lossless one is re-encoded losslessly (it used to
    be taken for lossy and re-compressed at 95 %)."""
    lossless = b.animated_webp()
    frames = [b.photo(80, 60), b.photo(80, 60).rotate(180)]
    buf = io.BytesIO()
    frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:], duration=100, lossless=False, quality=80)
    assert webp_meta.is_lossless(lossless) and not webp_meta.is_lossless(buf.getvalue())
    r = process_bytes(lossless, STRIP, edit=Edit(quarter=2))
    assert webp_meta.is_lossless(r.data) and not any("yeniden sıkıştırıldı" in n for n in r.notes)
    lossy = process_bytes(buf.getvalue(), STRIP, edit=Edit(quarter=2))
    assert not webp_meta.is_lossless(lossy.data) and "WebP yeniden sıkıştırıldı (kalite %95)." in lossy.notes


@pytest.mark.parametrize("mode,preview", [(RAW_DEVELOP, None), (RAW_AUTO, "full")])
def test_raw_to_jpeg(mode, preview):
    data = make_dng(stored_for(PHOTO, 6), orientation=6, preview=preview)
    opts = ContentOptions(strip_metadata=True, convert_to_jpeg=True, raw_mode=mode)
    out = decode(process_bytes(data, opts, name="a.dng", edit=MIRROR).data)
    assert out.size == (320, 240) and corners(out) == FLIPPED
    turned = decode(process_bytes(data, opts, name="a.dng", edit=Edit(mirror=True, quarter=1)).data)
    assert corners(turned) == corners(apply(PHOTO, Edit(mirror=True, quarter=1)))
    assert process_bytes(data, ContentOptions(), name="a.dng").data is None  # no edit: untouched


def test_raw_flip_needs_conversion():
    with pytest.raises(processor.ProcessError, match="yatay çevirme"):
        process_bytes(make_dng(PHOTO), ContentOptions(), name="a.dng", edit=MIRROR)


# ============================================================================= video


@dataclass
class Opts:
    strip_metadata: bool = True
    convert_video: bool = False
    video_codec: str = "h264"
    video_hw: bool = False


def test_video_flip_is_always_reencoded(tmp_path):
    src = make_video(str(tmp_path / "a.mp4"))
    info = probe(src)
    plan = plan_video(info, Opts(), MIRROR)
    assert plan.encode_video and "yatay çevirme" in plan.reasons and plan.rotation == 0
    turned = plan_video(info, Opts(), Edit(quarter=1, mirror=True))
    assert turned.encode_video and turned.rotation == 0  # never a display matrix, not even for the turn
    assert not plan_video(info, Opts(), Edit(quarter=1)).encode_video  # (a plain turn still is one)
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts(), MIRROR)
    assert result.method == "reencoded"
    out = probe(dst)
    assert out.rotation == 0 and out.display_size == (320, 240)
    assert corners(upright_frame(dst)) == FLIPPED  # the red marker on the left is now on the right


def test_video_flip_with_turn_and_portrait_source(tmp_path):
    src = make_video(str(tmp_path / "portrait.mov"), fmt="mov", rotation=90)
    shown = upright_frame(src)
    dst = str(tmp_path / "out.mov")
    process_video(src, dst, Opts(), MIRROR)
    out = probe(dst)
    assert out.rotation == 0 and out.display_size == shown.size  # the flip acts on the upright picture
    assert corners(upright_frame(dst)) == corners(ImageOps.mirror(shown))
    both = str(tmp_path / "both.mp4")
    edit = Edit(quarter=1, mirror=True)
    process_video(make_video(str(tmp_path / "a.mp4")), both, Opts(), edit)
    assert probe(both).display_size == (240, 320) and probe(both).rotation == 0
    assert corners(upright_frame(both)) == corners(apply(b.photo(320, 240), edit))


def test_video_flip_needs_mp4_for_other_containers():
    assert edit_reason(Fmt.MKV, Opts(), MIRROR) == t(VIDEO_EDIT_NEEDS_MP4)
    assert edit_reason(Fmt.MKV, Opts(convert_video=True), MIRROR) is None
    assert edit_reason(Fmt.MOV, Opts(), MIRROR) is None


# ======================================================================= self-test


def test_self_test_checks():
    ensure_plugins()
    im, exif = selftest._sample()
    lines: list[str] = []
    assert selftest._edit_checks(im, lines) and selftest._sharing_checks(im, exif, lines)
    assert any(line.startswith("JPEG  → mirror") and "OK" in line for line in lines)
    assert any(line.startswith("JPEG  → keep date") and "OK" in line for line in lines)
    assert any(line.startswith("JPEG  → max 48 px") and "OK" in line for line in lines)


# ========================================================================= wording


def test_messages_say_turn_on_not_tick():
    for key in (RAW_NEEDS_JPEG, RAW_EDIT_NEEDS_JPEG, RAW_RESIZE_NEEDS_JPEG, VIDEO_EDIT_NEEDS_MP4):
        text = t(key)
        assert "açın" in text and "işaretleyin" not in text and "metadata" not in text.lower()
    assert t(LABELS[Kind.TRACK]) == "Zamanlı veri izi"
    assert t(processor.ORIENTED_NOTE) == "Fotoğraf yön bilgisine göre döndürülerek kaydedildi (yön bilgisi de silindiği için)."
    r = process_bytes(b.jpeg(orientation=6, everything=False), STRIP)
    assert t(processor.ORIENTED_NOTE) in r.notes
    heic = process_bytes(b.heic(), STRIP)
    assert any("gizli bilgiler kayıpsız silinemez" in n and "Dönüştür'ü açın" in n for n in heic.notes)
    gif = process_bytes(b.gif(animated=True), ContentOptions(strip_metadata=True, convert_to_jpeg=True))
    assert "Hareketli ya da çok sayfalı dosya: yalnızca ilk kare dönüştürüldü." in gif.notes
