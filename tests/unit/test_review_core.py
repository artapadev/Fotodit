"""Fixes from the 1.3 review of the core: the capture date kept even for files read late, file
times before 1970, a TIFF's turned size, RAWs LibRaw cannot open, folders left by a cancelled job,
the 400 MP limit, a Lab TIFF without a profile, and the folder listing cache."""

from __future__ import annotations

import os
import struct
import threading

import builders as b
import pytest
from helpers import FakeBin, photos_of, write
from PIL import Image

from fotodit.core import planner
from fotodit.core.formats import Fmt
from fotodit.core.imaging import open_image
from fotodit.core.photoinfo import (
    DATE_MTIME,
    PhotoInfo,
    best_date,
    filename_date,
    local_time,
    read_info,
)
from fotodit.core.planner import (
    GROUP_YEAR,
    JobOptions,
    OutputOptions,
    Photo,
    build_plan,
    skip_reason,
)
from fotodit.core.processor import ContentOptions, process_bytes
from fotodit.core.runner import Runner


def test_keeping_the_date_waits_for_the_dates():
    keep = JobOptions(content=ContentOptions(strip_metadata=True, keep_date=True))
    assert keep.needs_dates()  # otherwise a file read late would lose its date
    assert not JobOptions(content=ContentOptions(strip_metadata=True)).needs_dates()


def test_file_times_before_1970_do_not_stop_the_plan(tmp_path):
    old = -3.15e8  # 1960
    when, source = best_date(None, "scan.jpg", old)
    assert source == DATE_MTIME and when.year in (1960, 1959)
    assert local_time(0).year in (1969, 1970)
    p = write(tmp_path / "scan.jpg", b.jpeg(everything=False))
    photo = Photo(p, os.path.getsize(p), old, 0, read_info(p))
    plan = build_plan([photo], JobOptions(output=OutputOptions(folder=str(tmp_path / "out"), group_by=GROUP_YEAR)))
    assert len(plan) == 1 and plan[0].target_dir.endswith(str(plan[0].date.year))


def test_a_turned_tiff_reports_the_size_it_shows(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # turned a quarter: 40 × 30 stored, 30 × 40 shown
    path = str(tmp_path / "turned.tif")
    Image.new("RGB", (40, 30), (200, 10, 10)).save(path, "TIFF", exif=exif)
    info = read_info(path)
    assert (info.width, info.height) == (30, 40)
    jpeg = str(tmp_path / "turned.jpg")
    Image.new("RGB", (40, 30), (200, 10, 10)).save(jpeg, "JPEG", exif=exif)
    assert (read_info(jpeg).width, read_info(jpeg).height) == (30, 40)


def test_a_raw_libraw_cannot_open_is_skipped_only_when_converted(tmp_path, monkeypatch):
    import rawpy

    from fotodit.core.dngwriter import make_dng

    path = write(tmp_path / "new_camera.dng", make_dng(b.photo(64, 48)))

    def refuse(*_args, **_kw):
        raise rawpy.LibRawFileUnsupportedError("unsupported")

    monkeypatch.setattr(rawpy, "imread", refuse)
    info = read_info(path)
    assert info.error is None and info.raw_error and info.taken is not None  # the date is read without LibRaw
    photo = Photo(path, 1, 1.0, 0, info)
    assert skip_reason(photo, ContentOptions()) is None  # copying, renaming, grouping work
    assert skip_reason(photo, ContentOptions(convert_to_jpeg=True)) == info.raw_error
    assert skip_reason(Photo(path, 1, 1.0, 0, PhotoInfo(fmt=Fmt.RAW, raw_error="x")),
                       ContentOptions(convert_to_jpeg=True)) == "x"


def test_a_cancelled_job_leaves_no_folders(tmp_path):
    paths = [write(tmp_path / "in" / f"{i}.jpg", b.jpeg(b.photo(64, 48))) for i in range(3)]
    out = tmp_path / "out"
    opts = JobOptions(content=ContentOptions(strip_metadata=True),
                      output=OutputOptions(folder=str(out), group_by=GROUP_YEAR))
    cancel = threading.Event()

    def progress(stage, done, total, name):
        if done >= 1:
            cancel.set()  # after the first file was prepared in its year folder

    report = Runner(build_plan(photos_of(paths), opts), opts, on_progress=progress, cancel_event=cancel,
                    workers=1, recycle_func=FakeBin(str(tmp_path / "_bin"))).run()
    assert report.cancelled and not out.exists()


def test_a_folder_whose_files_all_failed_is_removed(tmp_path):
    good = write(tmp_path / "in" / "good.jpg", b.jpeg(b.photo(64, 48)))
    bad = write(tmp_path / "in" / "bad.jpg", b.jpeg(everything=False)[:500])
    out = tmp_path / "out"
    opts = JobOptions(output=OutputOptions(folder=str(out), group_by=GROUP_YEAR))
    plan = build_plan(photos_of([good, bad]), opts)
    os.remove(good)  # it cannot be copied any more
    report = Runner(plan, opts, workers=1, recycle_func=FakeBin(str(tmp_path / "_bin"))).run()
    done = {os.path.basename(r.entry.photo.path) for r in report.succeeded}
    assert "good.jpg" not in done
    for folder, _, files in os.walk(out):
        assert files or os.listdir(folder), folder  # no empty folder left behind


def _bmp_header(width: int, height: int) -> bytes:
    """Only the headers of a 24-bit BMP: enough for its size to be read (the pixels are never loaded)."""
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, 0, 2835, 2835, 0, 0)
    return struct.pack("<2sIHHI", b"BM", 54 + width * height * 3, 0, 0, 54) + info


def test_images_over_400_megapixels_are_refused(tmp_path):
    big = tmp_path / "pano.bmp"
    big.write_bytes(_bmp_header(25_000, 20_000))  # 500 MP
    with pytest.raises(Image.DecompressionBombError):
        open_image(str(big))
    fine = tmp_path / "large.bmp"
    fine.write_bytes(_bmp_header(18_000, 15_000))  # 270 MP: allowed
    with open_image(str(fine)) as im:
        assert im.size == (18_000, 15_000)


def test_a_lab_tiff_without_a_profile_is_cleaned(tmp_path):
    import io

    buf = io.BytesIO()
    Image.new("LAB", (16, 12), (60, 120, 140)).save(buf, "TIFF")
    result = process_bytes(buf.getvalue(), ContentOptions(strip_metadata=True))
    assert result.data and any("CIELAB" in note for note in result.notes)


def test_a_folder_checked_a_moment_ago_is_not_asked_again(tmp_path, monkeypatch):
    planner.forget_listings()
    stats = []
    real_stat = os.stat
    monkeypatch.setattr(planner.os, "stat", lambda p, *a, **k: stats.append(p) or real_stat(p, *a, **k))
    planner._listdir(str(tmp_path))
    planner._listdir(str(tmp_path))
    assert len(stats) == 1
    planner.forget_listings()


def test_the_name_caches_hold_a_large_selection():
    assert filename_date.cache_info().maxsize >= 200_000
