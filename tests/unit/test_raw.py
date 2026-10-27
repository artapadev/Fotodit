"""RAW support tests: detection, metadata reading, conversion to JPG, planning.

Instead of real camera files, the tests use synthetic files that are structurally
equivalent to real RAWs: DNGs that LibRaw processes like a real RAW (Bayer mosaic +
DNG color tags + EXIF/GPS/XMP + embedded preview) and the metadata structures of
CR3 / RAF containers.
"""

from __future__ import annotations

import io
import os
import struct

import builders as b
import pytest
import rawpy
from helpers import INVERSE, assert_corners, decode
from oracle import assert_exiv2_clean
from PIL import Image

from fotodit.core.dngwriter import make_dng
from fotodit.core.formats import Fmt, detect, sniff, sniff_file
from fotodit.core.meta import Kind, scan_bytes, scan_file
from fotodit.core.meta import jpeg as jpeg_meta
from fotodit.core.meta import raw as raw_meta
from fotodit.core.meta.exif import read_orientation
from fotodit.core.photoinfo import DATE_EXIF, read_info
from fotodit.core.planner import (
    JobOptions,
    OutputOptions,
    Photo,
    RenameOptions,
    build_plan,
)
from fotodit.core.processor import (
    RAW_NEEDS_JPEG,
    ContentOptions,
    ProcessError,
    process_bytes,
)
from fotodit.core.rawdev import RAW_AUTO, RAW_DEVELOP
from fotodit.core.runner import Runner
from fotodit.core.tiffwriter import Ifd, build_tiff
from fotodit.i18n import t

DEVELOP = ContentOptions(strip_metadata=True, convert_to_jpeg=True, raw_mode=RAW_DEVELOP)
AUTO = ContentOptions(strip_metadata=True, convert_to_jpeg=True, raw_mode=RAW_AUTO)


def stored_for(displayed: Image.Image, orientation: int) -> Image.Image:
    t = INVERSE[orientation]
    return displayed.transpose(t) if t is not None else displayed


def dng(orientation=1, preview=None, **kw) -> bytes:
    return make_dng(stored_for(b.photo(320, 240), orientation), orientation=orientation, preview=preview, **kw)


# ===================================================================== detection


@pytest.mark.parametrize(
    "head",
    [
        b"IIRO\x08\x00\x00\x00",  # Olympus ORF
        b"MMOR\x00\x00\x00\x08",
        b"IIU\x00\x08\x00\x00\x00",  # Panasonic RW2
        b"FUJIFILMCCD-RAW 0201FF383501",  # Fujifilm RAF
        b"FOVb\x00\x00\x00\x00",  # Sigma X3F
        b"\x00MRM\x00\x00\x00\x00",  # Minolta MRW
        b"II\x1a\x00\x00\x00HEAPCCDR",  # Canon CRW
        b"II*\x00\x10\x00\x00\x00CR\x02\x00",  # Canon CR2
        b"\x00\x00\x00\x18ftypcrx \x00\x00\x00\x01crx isom",  # Canon CR3
    ],
)
def test_raw_signatures(head):
    assert sniff(head + b"\x00" * 64) == Fmt.RAW


def test_tiff_based_raw_detection(tmp_path):
    assert detect(dng()) == Fmt.RAW  # DNG version tag
    # TIFF without DNG tags but carrying CFA sensor data (like NEF/ARW/PEF)
    cfa = Ifd().long(256, 4).long(257, 4).short(258, 16).short(259, 1).short(262, 32803).ascii(271, "NIKON")
    nef_like = build_tiff([("ifd0", Ifd().long(254, 1).short(262, 2).pointer(330, "raw")), ("raw", cfa)])
    assert sniff(nef_like) == Fmt.TIFF and detect(nef_like) == Fmt.RAW
    # A plain TIFF stays TIFF, but with a .nef extension it counts as RAW
    plain = b.tiff(pages=1)
    assert detect(plain) == Fmt.TIFF and detect(plain, "photo.NEF") == Fmt.RAW
    p = tmp_path / "x.dng"
    p.write_bytes(dng())
    assert sniff_file(str(p)) == Fmt.RAW
    q = tmp_path / "plain.tif"
    q.write_bytes(plain)
    assert sniff_file(str(q)) == Fmt.TIFF


# ============================================================== metadata reading


def test_dng_metadata_scan_and_info(tmp_path):
    data = dng(orientation=6, preview="small", xmp=b.XMP)
    res = scan_bytes(data)
    assert {Kind.EXIF, Kind.GPS, Kind.XMP, Kind.THUMBNAIL} <= res.kinds(), res.describe()
    s = res.summary
    assert s.camera == "Fotodit Test Kamera" and s.gps and abs(s.gps[0] - 41.00833) < 1e-3
    assert s.software == "Fotodit DNG yazıcısı"  # UTF-8 in an ASCII field decodes correctly (non-ASCII on purpose)
    assert (s.taken.year, s.taken.month, s.taken.day) == (2024, 5, 12)
    exif_detail = next(f.detail for f in res.findings if f.kind == Kind.EXIF)
    assert "gövde seri no." in exif_detail

    p = tmp_path / "IMG_0001.DNG"
    p.write_bytes(data)
    info = read_info(str(p))
    assert info.fmt == Fmt.RAW and info.error is None
    assert (info.width, info.height) == (320, 240)  # size with orientation applied (as displayed)
    assert info.taken_source == DATE_EXIF and info.camera == "Fotodit Test Kamera" and info.has_gps
    assert scan_file(str(p)).kinds() == res.kinds()  # the memory-mapped scan gives the same result


def test_cr3_metadata_structure():
    """CR3: EXIF lives in the CMT1 (IFD0), CMT2 (EXIF) and CMT4 (GPS) ISO boxes."""
    cmt1 = build_tiff([("i", Ifd().ascii(271, "Canon").ascii(272, "Canon EOS R5").short(274, 1))])
    cmt2 = build_tiff([("i", Ifd().ascii(0x9003, "2023:08:01 10:20:30").ascii(0xA431, "0123456789"))])
    cmt3 = build_tiff([("i", Ifd().long(1, 7))])
    cmt4 = build_tiff([("i", Ifd().ascii(1, "N").rationals(2, [(39, 1), (54, 1), (0, 1)])
                        .ascii(3, "E").rationals(4, [(32, 1), (51, 1), (0, 1)]))])

    def box(typ: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", 8 + len(payload)) + typ + payload

    canon = box(b"uuid", raw_meta.CANON_UUID + box(b"CMT1", cmt1) + box(b"CMT2", cmt2)
                + box(b"CMT3", cmt3) + box(b"CMT4", cmt4) + box(b"THMB", b"\x00" * 16))
    data = (box(b"ftyp", b"crx \x00\x00\x00\x01crx isom") + box(b"moov", canon)
            + box(b"uuid", raw_meta.XMP_UUID + b.XMP) + box(b"mdat", b"\x00" * 32))
    assert sniff(data) == Fmt.RAW
    res = scan_bytes(data)
    assert {Kind.EXIF, Kind.GPS, Kind.MAKERNOTE, Kind.XMP, Kind.THUMBNAIL} <= res.kinds(), res.describe()
    assert res.summary.camera == "Canon EOS R5" and res.summary.taken.year == 2023
    exif, dropped_makernote = raw_meta.exif_for_jpeg(data)
    assert exif and dropped_makernote
    kept = scan_bytes(b.insert_after_soi(b.jpeg(everything=False, orientation=1)[:2] + b"\xff\xd9", b.seg(0xE1, exif)))
    assert kept.summary.camera == "Canon EOS R5" and kept.summary.gps is not None


def test_raf_metadata_structure():
    """RAF: the camera JPEG (with EXIF) embedded in the header."""
    jpeg = b.jpeg(b.photo(160, 120), everything=False)
    header = b"FUJIFILMCCD-RAW 0201FF383501" + b"X-T5".ljust(32, b"\x00")
    header = header.ljust(84, b"\x00") + struct.pack(">II", 100, len(jpeg)) + b"\x00" * 8
    data = header + jpeg + b"\x00" * 64
    assert sniff(data) == Fmt.RAW
    res = scan_bytes(data)
    assert {Kind.EXIF, Kind.GPS, Kind.THUMBNAIL, Kind.OTHER} <= res.kinds(), res.describe()
    assert res.summary.camera == "Apple iPhone 15 Pro"


# ============================================================ conversion to JPG


@pytest.mark.parametrize("orientation", [1, 2, 3, 4, 5, 6, 7, 8])
@pytest.mark.parametrize("mode,preview", [(RAW_DEVELOP, None), (RAW_AUTO, "full"), (RAW_AUTO, "small")])
def test_raw_to_jpeg(orientation, mode, preview):
    data = dng(orientation, preview)
    opts = ContentOptions(strip_metadata=True, convert_to_jpeg=True, raw_mode=mode)
    r = process_bytes(data, opts, name="IMG_0001.dng")
    assert r.fmt == Fmt.JPEG
    out = decode(r.data)
    assert out.size == (320, 240)
    assert_corners(out)
    assert scan_bytes(r.data).clean and not jpeg_meta.parse(r.data).exif()
    assert_exiv2_clean(r.data)
    used_camera = any("gömülü tam boyutlu JPEG" in n for n in r.notes)
    assert used_camera == (mode == RAW_AUTO and preview == "full")
    if mode == RAW_AUTO and preview == "small":
        assert any("küçük olduğu için" in n for n in r.notes)


@pytest.mark.parametrize("orientation", [1, 2, 6, 8])
@pytest.mark.parametrize("preview", [None, "small"])
def test_raw_thumbnail_orientation(tmp_path, orientation, preview):
    from fotodit.core.rawdev import thumbnail

    p = tmp_path / f"t{orientation}.dng"
    p.write_bytes(dng(orientation, preview))
    im = thumbnail(str(p), 200)
    assert im is not None and im.width > im.height
    assert b.dominant(b.marker_color(im, "tl")) == "red"


def test_camera_jpeg_is_used_losslessly():
    data = dng(1, "full")
    with rawpy.imread(io.BytesIO(data)) as raw:
        preview = bytes(raw.extract_thumb().data)
    r = process_bytes(data, AUTO, name="a.dng")
    assert decode(r.data).tobytes() == decode(preview).tobytes()  # the camera's JPEG, not recompressed


def test_develop_matches_scene():
    r = process_bytes(dng(1), DEVELOP, name="a.dng")
    out = decode(r.data)
    src = b.photo(320, 240)
    mean = lambda im: sum(im.convert("L").get_flattened_data()) / (im.width * im.height)  # noqa: E731
    assert abs(mean(out) - mean(src)) < 6  # correct exposure/tone (sRGB curve, camera white balance)


def test_raw_keep_metadata():
    data = dng(6, None, xmp=b.XMP)
    r = process_bytes(data, ContentOptions(strip_metadata=False, convert_to_jpeg=True, raw_mode=RAW_DEVELOP), name="a.dng")
    layout = jpeg_meta.parse(r.data)
    assert read_orientation(layout.exif()) == 1
    res = scan_bytes(r.data)
    assert res.summary.camera == "Fotodit Test Kamera" and res.summary.gps is not None
    assert Kind.XMP in res.kinds()
    assert_corners(decode(r.data))
    # metadata is added on the camera-JPEG path too
    r2 = process_bytes(dng(6, "full"), ContentOptions(strip_metadata=False, convert_to_jpeg=True), name="a.dng")
    assert scan_bytes(r2.data).summary.camera == "Fotodit Test Kamera"
    assert_corners(decode(r2.data))


def test_raw_strip_only_is_refused_and_rename_only_allowed():
    with pytest.raises(ProcessError, match="JPG'ye dönüştürülerek"):
        process_bytes(dng(), ContentOptions(strip_metadata=True), name="a.dng")
    assert process_bytes(dng(), ContentOptions(), name="a.dng").data is None


def test_corrupt_raw_reports_turkish_error():
    data = dng()
    with pytest.raises(ProcessError) as err:
        process_bytes(data[: len(data) // 3], DEVELOP, name="a.dng")
    assert "RAW" in str(err.value)


# =================================================================== planning


def test_plan_and_run_with_raw(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    paths = []
    for i, when in enumerate(("2024:05:12 09:00:00", "2024:05:11 09:00:00")):
        p = src / f"DSC_{i:04d}.NEF"
        p.write_bytes(make_dng(b.photo(160, 120), taken=when))
        paths.append(str(p))
    photos = [Photo.from_path(p, i, read_info(p)) for i, p in enumerate(paths)]

    strip_only = JobOptions(content=ContentOptions(strip_metadata=True), rename=RenameOptions(enabled=True),
                            output=OutputOptions(folder=str(tmp_path / "o1")))
    plan = build_plan(photos, strip_only)
    assert all(e.skip_reason == t(RAW_NEEDS_JPEG) for e in plan)

    rename_only = JobOptions(rename=RenameOptions(enabled=True), output=OutputOptions(folder=str(tmp_path / "o2")))
    assert [e.target_name for e in build_plan(photos, rename_only)] == ["1.nef", "2.nef"]

    convert = JobOptions(content=ContentOptions(strip_metadata=True, convert_to_jpeg=True),
                         rename=RenameOptions(enabled=True), output=OutputOptions(folder=str(tmp_path / "o3")))
    plan = build_plan(photos, convert)
    assert [(os.path.basename(e.photo.path), e.target_name) for e in plan] == [("DSC_0001.NEF", "1.jpg"), ("DSC_0000.NEF", "2.jpg")]
    report = Runner(plan, convert, workers=2).run()
    assert len(report.succeeded) == 2
    for name in ("1.jpg", "2.jpg"):
        out = (tmp_path / "o3" / name).read_bytes()
        assert scan_bytes(out).clean
        assert decode(out).size == (160, 120)
