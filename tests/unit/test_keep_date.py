"""Keeping the capture date ("Çekim tarihini koru"): a stripped photo gets a date-only EXIF (exactly one
tag), the written file gets the capture date as its modified time, and an MP4/MOV gets it as its header
dates; verification accepts exactly that and nothing else."""

from __future__ import annotations

import io
import os
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import builders as b
import pytest
from helpers import FakeBin, jpeg_taken, photos_of, write
from oracle import exiv2_metadata
from PIL import Image
from video_builders import APPLE_TAGS, make_video

from fotodit.core import processor
from fotodit.core.dngwriter import make_dng
from fotodit.core.edits import Edit
from fotodit.core.formats import Fmt
from fotodit.core.imaging import open_image
from fotodit.core.meta import Kind, residual_signatures, scan_bytes
from fotodit.core.meta import jpeg as jpeg_meta
from fotodit.core.meta import png as png_meta
from fotodit.core.meta import tiff as tiff_meta
from fotodit.core.meta import webp as webp_meta
from fotodit.core.meta.exif import date_only_exif, strip_exif_header
from fotodit.core.planner import JobOptions, OutputOptions, RenameOptions, build_plan
from fotodit.core.processor import LOSSLESS, ContentOptions, ProcessError, process_bytes
from fotodit.core.runner import Runner
from fotodit.core.video import process as video_process
from fotodit.core.video.boxes import EPOCH_1904, read_structure
from fotodit.core.video.probe import probe
from fotodit.core.video.process import VideoError, plan_video, process_video

TAKEN = datetime(2024, 5, 12, 14, 30, 22, 123000)  # sub-seconds are not kept
TEXT = "2024:05:12 14:30:22"
KEEP = ContentOptions(strip_metadata=True, keep_date=True)
KEEP_WIDE = ContentOptions(strip_metadata=True, keep_date=True, keep_color_profile=True)
KEEP_JPEG = ContentOptions(strip_metadata=True, keep_date=True, convert_to_jpeg=True)
NOTE = "Çekim tarihi korundu (12.05.2024 14:30); diğer bütün bilgiler silindi."

# The structural tags exiv2 lists for a TIFF as "Exif.Image.*" (see oracle.py), and the profile kept on purpose.
_STRUCTURE = {"ImageWidth", "ImageLength", "BitsPerSample", "Compression", "PhotometricInterpretation",
              "StripOffsets", "SamplesPerPixel", "RowsPerStrip", "StripByteCounts", "PlanarConfiguration",
              "XResolution", "YResolution", "ResolutionUnit", "Predictor", "InterColorProfile"}


def decode(data: bytes) -> Image.Image:
    im = open_image(io.BytesIO(data))
    im.load()
    return im


def assert_only_date(data: bytes, fmt: Fmt, profile: bytes | None = None, text: str = TEXT) -> None:
    """Exactly one tag (DateTimeOriginal, reached from IFD0 by the ExifIFD pointer) and nothing else, by
    Fotodit's own scanner, the signature scan, Pillow and exiv2 (independent of Fotodit)."""
    res = scan_bytes(data, fmt, enrich=False)
    kinds = [f.kind for f in res.findings]
    assert kinds.count(Kind.EXIF) == 1, res.describe()
    assert set(kinds) <= {Kind.EXIF} | ({Kind.ICC} if profile is not None else set()), res.describe()
    assert set(residual_signatures(data)) <= {"EXIF"} | ({"ICC"} if profile is not None else set())
    with open_image(io.BytesIO(data)) as im:
        exif = im.getexif()
        assert set(exif) - tiff_meta.STRUCTURAL - {34675} == {0x8769}
        assert dict(exif.get_ifd(0x8769)) == {0x9003: text}  # (no Interop pointer either)
        assert not exif.get_ifd(0x8825)
    meta = exiv2_metadata(data)
    found = {k: v for k, v in (meta.get("exif") or {}).items()
             if not (k.startswith(("Exif.Image.", "Exif.Thumbnail.")) and k.split(".")[-1] in _STRUCTURE)}
    assert set(found) == {"Exif.Image.ExifTag", "Exif.Photo.DateTimeOriginal"}, found
    assert found["Exif.Photo.DateTimeOriginal"] == text
    assert not meta.get("iptc") and not meta.get("xmp") and not meta.get("comment")
    assert bool(meta.get("icc")) == (profile is not None)


# ============================================================================ the EXIF block


def test_date_only_exif_is_minimal_and_deterministic():
    exif = date_only_exif(TAKEN)
    assert exif == date_only_exif(TAKEN.replace(microsecond=0)) and len(exif) == 70
    assert exif.startswith(b"Exif\x00\x00II*\x00\x08\x00\x00\x00")
    ex = Image.Exif()
    ex.load(exif)
    assert dict(ex) == {0x8769: 26} and dict(ex.get_ifd(0x8769)) == {0x9003: TEXT}
    assert date_only_exif(datetime(2001, 2, 3, 4, 5, 6)).endswith(b"2001:02:03 04:05:06\x00")


# ================================================================ every photo format


@pytest.mark.parametrize("name,make,fmt,lossless", [
    ("jpeg", lambda: b.jpeg(), Fmt.JPEG, True),
    ("jpeg-rotated", lambda: b.jpeg(orientation=6, everything=False), Fmt.JPEG, False),
    ("png", lambda: b.png(), Fmt.PNG, True),
    ("png-palette", lambda: b.png(b.photo().quantize(64)), Fmt.PNG, True),
    ("webp-lossy", lambda: b.webp(), Fmt.WEBP, True),
    ("webp-lossless", lambda: b.webp(lossless=True), Fmt.WEBP, True),
    ("webp-animated", b.animated_webp, Fmt.WEBP, True),
    ("tiff", lambda: b.tiff(pages=2), Fmt.TIFF, False),
    ("heic", lambda: b.heic(), Fmt.HEIF, False),
    ("avif", lambda: b.avif(), Fmt.AVIF, False),
], ids=lambda v: v if isinstance(v, str) else "")
def test_strip_keeps_only_the_date(name, make, fmt, lossless):
    src = make()
    r = process_bytes(src, KEEP, taken=TAKEN)
    assert r.fmt == fmt and (r.method == LOSSLESS) == lossless
    assert r.date_exif == date_only_exif(TAKEN) and NOTE in r.notes
    assert_only_date(r.data, fmt)
    out, plain = decode(r.data), decode(process_bytes(src, ContentOptions(strip_metadata=True)).data)
    assert out.size == plain.size
    if fmt not in (Fmt.HEIF, Fmt.AVIF):  # the same picture as without the date (HEIC/AVIF: re-encoded each time)
        assert out.convert("RGBA").tobytes() == plain.convert("RGBA").tobytes()
    if fmt == Fmt.TIFF:
        assert decode(r.data).n_frames == 2 and tiff_meta.count_tag(r.data, 34665) == 1  # first page only
    if fmt == Fmt.WEBP:
        chunks = [fourcc for fourcc, *_ in webp_meta.iter_chunks(r.data)]
        assert chunks[0] == b"VP8X" and chunks.count(b"EXIF") == 1 and r.data[20] & webp_meta.FLAG_EXIF
        assert decode(r.data).n_frames == decode(src).n_frames


def test_simple_webp_becomes_extended_with_its_canvas():
    for lossless, mode in ((False, "RGB"), (True, "RGBA"), (True, "RGB")):
        im = b.photo(123, 45).convert(mode)
        if mode == "RGBA":
            im.putalpha(Image.linear_gradient("L").resize(im.size))
        buf = io.BytesIO()
        im.save(buf, "WEBP", lossless=lossless, quality=90)
        assert [fourcc for fourcc, *_ in webp_meta.iter_chunks(buf.getvalue())] == [b"VP8L" if lossless else b"VP8 "]
        r = process_bytes(buf.getvalue(), KEEP, taken=TAKEN)
        flags, = struct.unpack_from("<B", r.data, 20)
        width = int.from_bytes(r.data[24:27], "little") + 1
        height = int.from_bytes(r.data[27:30], "little") + 1
        assert (width, height) == (123, 45) and flags & webp_meta.FLAG_EXIF
        assert bool(flags & webp_meta.FLAG_ALPHA) == (mode == "RGBA")
        assert decode(r.data).mode == decode(buf.getvalue()).mode
        assert decode(r.data).tobytes() == decode(buf.getvalue()).tobytes()  # verified pixel for pixel too
        assert_only_date(r.data, Fmt.WEBP)


def test_png_exif_comes_before_the_image_data():
    r = process_bytes(b.png(), KEEP, taken=TAKEN)
    chunks = [ctype for ctype, *_ in png_meta.iter_chunks(r.data)]
    assert chunks.index(b"eXIf") < chunks.index(b"IDAT") and chunks.count(b"eXIf") == 1
    assert png_meta.exif_block(r.data) == strip_exif_header(date_only_exif(TAKEN))  # PNG: no "Exif\0\0"
    frames = [b.photo(80, 60), b.photo(80, 60).rotate(180)]
    buf = io.BytesIO()
    frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:], duration=100, loop=0)
    apng = process_bytes(buf.getvalue(), KEEP, taken=TAKEN)
    chunks = [ctype for ctype, *_ in png_meta.iter_chunks(apng.data)]
    assert chunks.index(b"eXIf") < chunks.index(b"acTL") and decode(apng.data).n_frames == 2


def test_jpeg_exif_follows_the_jfif_header():
    r = process_bytes(b.jpeg(), KEEP, taken=TAKEN)
    layout = jpeg_meta.parse(r.data)
    markers = [seg.marker for seg in layout.segments]
    assert markers[:3] == [jpeg_meta.SOI, jpeg_meta.APP0, jpeg_meta.APP1]
    assert layout.exif() == date_only_exif(TAKEN)


@pytest.mark.parametrize("name,make", [
    ("jpeg", lambda: b.jpeg(b.photo(), icc=b.P3_ICC)), ("png", lambda: b.png(b.photo(), icc=b.P3_ICC)),
    ("webp", lambda: b.webp(b.photo(), icc=b.P3_ICC)), ("tiff", lambda: b.tiff(pages=1, icc=b.P3_ICC)),
    ("heic", lambda: b.heic(b.photo(), icc=b.P3_ICC)),
], ids=lambda v: v if isinstance(v, str) else "")
def test_date_together_with_the_clean_color_profile(name, make):
    r = process_bytes(make(), KEEP_WIDE, taken=TAKEN)
    assert r.profile is not None and r.date_exif is not None
    assert decode(r.data).info.get("icc_profile") == r.profile
    assert_only_date(r.data, r.fmt, profile=r.profile)
    if name == "jpeg":
        assert r.method == LOSSLESS  # the profile swapped and the date added: the pixels untouched


@pytest.mark.parametrize("name,make", [
    ("png", lambda: b.png()), ("webp", lambda: b.webp()), ("heic", lambda: b.heic()), ("avif", lambda: b.avif()),
    ("tiff", lambda: b.tiff(pages=1)), ("bmp", lambda: b.bmp()), ("gif", lambda: b.gif()),
], ids=lambda v: v if isinstance(v, str) else "")
def test_date_when_converting_to_jpeg(name, make):
    r = process_bytes(make(), KEEP_JPEG, taken=TAKEN)
    assert r.fmt == Fmt.JPEG and NOTE in r.notes
    assert_only_date(r.data, Fmt.JPEG)


@pytest.mark.parametrize("fmt", ["jpeg", "png", "heic"])
def test_date_with_an_edit(fmt):
    src = {"jpeg": lambda: b.jpeg(), "png": lambda: b.png(), "heic": lambda: b.heic()}[fmt]()
    edit = Edit(quarter=1, crop=(0.1, 0.1, 0.9, 0.6), exposure=0.5)
    r = process_bytes(src, KEEP, edit=edit, taken=TAKEN)
    assert decode(r.data).size == edit.output_size(320, 240)
    assert_only_date(r.data, r.fmt)
    assert process_bytes(b.jpeg(), KEEP, edit=Edit(quarter=2), taken=TAKEN).method == "reencoded"


@pytest.mark.parametrize("preview", [None, "full"])
def test_raw_to_jpeg_keeps_the_date(preview):
    data = make_dng(b.photo(320, 240), orientation=1, preview=preview)
    r = process_bytes(data, KEEP_JPEG, name="IMG_0001.dng", taken=TAKEN)
    assert r.fmt == Fmt.JPEG and decode(r.data).size == (320, 240)
    assert_only_date(r.data, Fmt.JPEG)


@pytest.mark.parametrize("make,label", [(b.gif, "GIF"), (b.bmp, "BMP")])
def test_gif_and_bmp_keep_it_only_as_the_file_date(make, label):
    r = process_bytes(make(), KEEP, taken=TAKEN)
    assert r.date_exif is None and r.fmt.value == label
    assert f"Çekim tarihi (12.05.2024 14:30) yalnızca dosya tarihi olarak korundu: {label} dosyası EXIF bilgisi " \
           "taşıyamaz." in r.notes
    assert scan_bytes(r.data).clean and b"2024:05:12" not in r.data


def test_no_date_without_stripping_or_without_a_date():
    plain = process_bytes(b.jpeg(), ContentOptions(strip_metadata=True), taken=TAKEN)  # keep_date off
    assert plain.date_exif is None and scan_bytes(plain.data).clean
    undated = process_bytes(b.jpeg(), KEEP, taken=None)
    assert undated.date_exif is None and scan_bytes(undated.data).clean and not any("Çekim" in n for n in undated.notes)
    kept = process_bytes(b.jpeg(), ContentOptions(keep_date=True), taken=TAKEN)  # metadata kept anyway
    assert kept.data is None


def test_tiff_exif_is_added_to_big_endian_files_too():
    pixels = bytes(range(8 * 4))
    ifd = (b.Ifd(">").long(256, 8).long(257, 4).short(258, 8).short(259, 1).short(262, 1)
           .pointer(273, "pixels").short(277, 1).long(278, 4).long(279, len(pixels)))
    data = b.build_tiff([("ifd0", ifd)], {"pixels": pixels}, e=">")
    assert data[:2] == b"MM" and tiff_meta.exif_entries(data) is None
    dated = tiff_meta.with_exif(data, strip_exif_header(date_only_exif(TAKEN)))
    assert tiff_meta.exif_entries(dated) == [(0x9003, 2, 20, (TEXT + "\x00").encode())]
    assert decode(dated).tobytes() == pixels  # the image data did not move
    assert dict(decode(dated).getexif().get_ifd(0x8769)) == {0x9003: TEXT}
    assert exiv2_metadata(dated)["exif"]["Exif.Photo.DateTimeOriginal"] == TEXT
    assert tiff_meta.count_tag(dated, 34665) == 1


# ====================================================================== verification


def _sneaky(monkeypatch, extra):
    real = processor._add_date

    def add(result, date, taken):
        real(result, date, taken)
        extra(result)

    monkeypatch.setattr(processor, "_add_date", add)


def test_verification_rejects_a_second_exif(monkeypatch):
    _sneaky(monkeypatch, lambda r: setattr(r, "data", jpeg_meta.insert_segments(
        r.data, [(jpeg_meta.APP1, b"Exif\x00\x00" + b.rich_exif(thumbnail=False))])))
    with pytest.raises(ProcessError, match="tarih bilgisi beklenenden farklı"):
        process_bytes(b.jpeg(), KEEP, taken=TAKEN)


def test_verification_rejects_a_different_exif(monkeypatch):
    """The same date, but with the camera's make next to it: not exactly the date-only EXIF."""
    other = b.build_tiff([("ifd0", b.Ifd().ascii(0x010F, "Apple").pointer(0x8769, "exif")),
                          ("exif", b.Ifd().ascii(0x9003, TEXT))])

    def swap(r):
        r.data = png_meta.with_exif(png_meta.strip(r.data), other)

    _sneaky(monkeypatch, swap)
    with pytest.raises(ProcessError, match="tarih bilgisi beklenenden farklı"):
        process_bytes(b.png(), KEEP, taken=TAKEN)


def test_verification_rejects_another_date(monkeypatch):
    _sneaky(monkeypatch, lambda r: setattr(r, "date_exif", date_only_exif(datetime(2020, 1, 1))))
    for src in (b.jpeg(), b.webp(), b.tiff(pages=1), b.heic()):
        with pytest.raises(ProcessError, match="tarih bilgisi"):
            process_bytes(src, KEEP, taken=TAKEN)


def test_verification_still_rejects_other_metadata(monkeypatch):
    def add_text(r):
        r.data = b.insert_png_chunks(r.data, b.png_chunk(b"tEXt", b"Author\x00Ayse"))

    _sneaky(monkeypatch, add_text)
    with pytest.raises(ProcessError, match="gizli bilgi kaldı"):
        process_bytes(b.png(), KEEP, taken=TAKEN)


# ============================================================================= runner


def _run(paths, opts, tmp_path):
    plan = build_plan(photos_of(paths), opts)
    return Runner(plan, opts, recycle_func=FakeBin(str(tmp_path / "_bin")), workers=2).run()


def test_written_file_gets_the_capture_date(tmp_path):
    buf = io.BytesIO()
    b.photo(32, 32).save(buf, "PNG")  # no EXIF: the date comes from the name, or (plain.png) from nowhere
    dated = write(tmp_path / "src" / "a.jpg", jpeg_taken("2023:01:02 03:04:05"))
    named = write(tmp_path / "src" / "Screenshot_2022-07-08-09-10-11.png", buf.getvalue())
    old = time.mktime((2021, 6, 1, 12, 0, 0, 0, 0, -1))
    undated = write(tmp_path / "src" / "plain.png", buf.getvalue(), mtime=old)
    out = tmp_path / "out"
    opts = JobOptions(content=KEEP, output=OutputOptions(folder=str(out)))
    report = _run([dated, named, undated], opts, tmp_path)
    assert not report.failed
    a, shot, plain = (out / "a.jpg").read_bytes(), (out / "Screenshot_2022-07-08-09-10-11.png").read_bytes(), \
        (out / "plain.png").read_bytes()
    assert os.stat(out / "a.jpg").st_mtime == pytest.approx(datetime(2023, 1, 2, 3, 4, 5).timestamp(), abs=1)
    assert_only_date(a, Fmt.JPEG, text="2023:01:02 03:04:05")
    # A date from the file name is a capture date too.
    assert os.stat(out / "Screenshot_2022-07-08-09-10-11.png").st_mtime == pytest.approx(
        datetime(2022, 7, 8, 9, 10, 11).timestamp(), abs=1)
    assert_only_date(shot, Fmt.PNG, text="2022:07:08 09:10:11")
    # The modified time is not a capture date: never written, and the file is new.
    assert scan_bytes(plain).clean and abs(os.stat(out / "plain.png").st_mtime - time.time()) < 120
    by_name = {os.path.basename(r.entry.photo.path): r for r in report.results}
    assert any("Çekim tarihi korundu" in n for n in by_name["a.jpg"].notes)
    assert not any("Çekim" in n for n in by_name["plain.png"].notes)


def test_without_keep_date_the_times_stay_as_before(tmp_path):
    p = write(tmp_path / "src" / "a.jpg", jpeg_taken("2023:01:02 03:04:05"))
    out = tmp_path / "out"
    _run([p], JobOptions(content=ContentOptions(strip_metadata=True), output=OutputOptions(folder=str(out))), tmp_path)
    assert abs(os.stat(out / "a.jpg").st_mtime - time.time()) < 120 and scan_bytes((out / "a.jpg").read_bytes()).clean


def test_in_place_with_renaming(tmp_path):
    p = write(tmp_path / "a.jpg", jpeg_taken("2023:01:02 03:04:05"))
    opts = JobOptions(content=KEEP, rename=RenameOptions(enabled=True, prefix="Tatil_"),
                      output=OutputOptions(in_place=True))
    report = _run([p], opts, tmp_path)
    assert not report.failed and os.listdir(tmp_path / "_bin")
    final = tmp_path / "Tatil_1.jpg"
    assert os.stat(final).st_mtime == pytest.approx(datetime(2023, 1, 2, 3, 4, 5).timestamp(), abs=1)
    assert_only_date(final.read_bytes(), Fmt.JPEG, text="2023:01:02 03:04:05")


# ============================================================================== video


@dataclass
class Opts:
    strip_metadata: bool = True
    convert_video: bool = False
    video_codec: str = "h264"
    video_hw: bool = False
    keep_date: bool = True


VIDEO_TAKEN = datetime(2024, 7, 12, 18, 30, 10)


def _header(when: datetime) -> int:
    utc = when.astimezone(timezone.utc).replace(tzinfo=None)  # local time on this computer → UTC
    return int((utc - EPOCH_1904).total_seconds())


def _times(path: str) -> set[int]:
    st = read_structure(path)
    return {st.created, st.modified, *(v for t in st.tracks for v in (t.created, t.modified, t.media_created,
                                                                      t.media_modified))}


@pytest.mark.parametrize("name,kw,opts,edit", [
    ("IMG_0001.MOV", {"fmt": "mov", "tags": APPLE_TAGS, "apple_keys": True, "subtitle": "hello"}, Opts(), None),
    ("clip.mp4", {"tags": {"location": "+41.0082+028.9784/"}}, Opts(), Edit(quarter=1)),
    ("clip.mkv", {"fmt": "matroska"}, Opts(convert_video=True), Edit(crop=(0.0, 0.0, 0.5, 1.0))),
], ids=["mov-copy", "mp4-rotated", "mkv-converted-cropped"])
def test_video_header_dates_are_the_capture_date(tmp_path, name, kw, opts, edit):
    src = make_video(str(tmp_path / name), **kw)
    dst = str(tmp_path / ("out" + (".mp4" if opts.convert_video else os.path.splitext(name)[1])))
    result = process_video(src, dst, opts, edit, taken=VIDEO_TAKEN)
    assert "Çekim tarihi korundu (12.07.2024 18:30)." in result.notes
    assert _times(dst) == {_header(VIDEO_TAKEN)}  # mvhd, every tkhd and mdhd: creation and modification
    st = read_structure(dst)
    assert not st.keys and not st.items and not st.xmp and all(not t.udta for t in st.tracks)
    info = probe(dst)
    assert set(info.tags) <= {"major_brand", "minor_version", "compatible_brands", "creation_time"}
    assert info.taken == VIDEO_TAKEN and not info.has_gps and info.camera is None
    assert b"com.apple" not in open(dst, "rb").read()


def test_video_without_keep_date_has_zero_header_dates(tmp_path):
    src = make_video(str(tmp_path / "IMG_0001.MOV"), fmt="mov", tags=APPLE_TAGS, apple_keys=True)
    dst = str(tmp_path / "out.mov")
    result = process_video(src, dst, Opts(keep_date=False), taken=VIDEO_TAKEN)
    assert _times(dst) == {0} and not any("Çekim" in n for n in result.notes)
    assert probe(dst).taken is None
    kept = str(tmp_path / "kept.mov")  # metadata kept: the source's own dates, the option changes nothing
    process_video(src, kept, Opts(strip_metadata=False), Edit(quarter=1), taken=VIDEO_TAKEN)
    assert probe(kept).tags.get("com.apple.quicktime.model") == "iPhone 15 Pro"


def test_video_verification_checks_the_header_dates(tmp_path):
    src = make_video(str(tmp_path / "a.mp4"))
    info = probe(src)
    created = video_process._utc(VIDEO_TAKEN)
    job = video_process._Job(src, str(tmp_path / "out.mp4"), info, plan_video(info, Opts()), None, None, created)
    job.run()
    video_process._verify(job)  # exactly the requested date: accepted
    job.created = created.replace(year=2020)
    with pytest.raises(VideoError, match="tarih damgası|creation_time"):
        video_process._verify(job)
    job.created = None  # no date requested: the header dates must be zero
    with pytest.raises(VideoError, match="tarih damgası|creation_time"):
        video_process._verify(job)


def test_other_containers_keep_it_only_as_the_file_date(tmp_path):
    src = make_video(str(tmp_path / "a.mkv"), fmt="matroska", tags={"title": "Holiday"})
    dst = str(tmp_path / "out.mkv")
    result = process_video(src, dst, Opts(), taken=VIDEO_TAKEN)
    assert any(n.startswith("Çekim tarihi (12.07.2024 18:30) yalnızca dosya tarihi olarak korundu") for n in result.notes)
    info = probe(dst)
    assert info.taken is None and "creation_time" not in info.tags and "title" not in info.tags


def test_runner_dates_the_video(tmp_path):
    os.makedirs(tmp_path / "src")
    src = make_video(str(tmp_path / "src" / "IMG_0001.MOV"), fmt="mov", tags=APPLE_TAGS, apple_keys=True)
    out = tmp_path / "out"
    opts = JobOptions(content=ContentOptions(strip_metadata=True, keep_date=True), output=OutputOptions(folder=str(out)))
    report = _run([src], opts, tmp_path)
    assert not report.failed
    dst = str(out / "IMG_0001.MOV")
    assert os.stat(dst).st_mtime == pytest.approx(VIDEO_TAKEN.timestamp(), abs=1)  # the iPhone's local time
    assert _times(dst) == {_header(VIDEO_TAKEN)}
