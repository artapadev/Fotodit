"""Tests for the planner (sorting/naming/grouping) and the runner (disk operations)."""

from __future__ import annotations

import io
import os
import stat
import threading
import time

import builders as b
import pytest
from helpers import FakeBin, jpeg_taken, photos_of, write
from oracle import assert_exiv2_clean
from PIL import Image

from fotodit.core.meta import scan_file
from fotodit.core.photoinfo import (
    DATE_EXIF,
    DATE_FILENAME,
    DATE_MTIME,
    filename_date,
)
from fotodit.core.planner import (
    GROUP_MONTH,
    GROUP_SOURCE,
    SORT_DATE,
    SORT_MANUAL,
    SORT_NAME,
    SORT_SIZE,
    JobOptions,
    OutputOptions,
    Photo,
    RenameOptions,
    build_plan,
    undated_count,
    undated_folder,
)
from fotodit.core.processor import ContentOptions
from fotodit.core.runner import TEMP_PREFIX, Runner


def no_temp_files(*roots) -> bool:
    for root in roots:
        for _, _, files in os.walk(root):
            if any(f.startswith(TEMP_PREFIX) for f in files):
                return False
    return True


# ================================================================= date extraction


@pytest.mark.parametrize(
    "name,expected",
    [
        ("IMG_20240512_143022.jpg", (2024, 5, 12, 14, 30, 22)),
        ("PXL_20240512_143022123.jpg", (2024, 5, 12, 14, 30, 22)),
        ("WhatsApp Image 2024-05-12 at 14.30.22.jpeg", (2024, 5, 12, 14, 30, 22)),
        ("Screenshot_2024-05-12-14-30-22-123_com.app.png", (2024, 5, 12, 14, 30, 22)),
        ("Ekran görüntüsü 2024-05-12 143022.png", (2024, 5, 12, 14, 30, 22)),  # Turkish Windows screenshot name
        ("signal-2024-05-12-143022_002.jpeg", (2024, 5, 12, 14, 30, 22)),
        ("IMG-20240512-WA0003.jpg", (2024, 5, 12, 0, 0, 0)),
        ("DSC01234.JPG", None),
        ("IMG_1234.HEIC", None),
        ("20241399_123456.jpg", None),
    ],
)
def test_filename_dates(name, expected):
    got = filename_date(name)
    if expected is None:
        assert got is None
    else:
        assert got is not None and (got.year, got.month, got.day, got.hour, got.minute, got.second) == expected


def test_date_sources(tmp_path):
    a = write(tmp_path / "a.jpg", jpeg_taken("2023:01:02 03:04:05"))
    buf = io.BytesIO()
    b.photo(32, 32).save(buf, "PNG")
    c = write(tmp_path / "Screenshot_2022-07-08-09-10-11.png", buf.getvalue())
    d = write(tmp_path / "plain.png", buf.getvalue(), mtime=time.mktime((2021, 6, 1, 12, 0, 0, 0, 0, -1)))
    plan = build_plan(photos_of([a, c, d]), JobOptions())
    src = {os.path.basename(e.photo.path): e.date_source for e in plan}
    assert src == {"a.jpg": DATE_EXIF, "Screenshot_2022-07-08-09-10-11.png": DATE_FILENAME, "plain.png": DATE_MTIME}
    # The modification time (2021) is not a capture date: the undated file goes last.
    assert [os.path.basename(e.photo.path) for e in plan] == ["Screenshot_2022-07-08-09-10-11.png", "a.jpg", "plain.png"]


def test_undated_files_keep_selection_order(tmp_path):
    """Files without a capture date are not slotted in by mtime: they stay after the dated ones in
    selection order (descending too); equal keys also follow selection order, not name order."""
    buf = io.BytesIO()
    b.photo(32, 32).save(buf, "PNG")
    png = buf.getvalue()

    def year(y):
        return time.mktime((y, 6, 1, 12, 0, 0, 0, 0, -1))

    selection = [
        write(tmp_path / "zz.png", png, year(2020)),
        write(tmp_path / "new.jpg", jpeg_taken("2024:05:02 10:00:00")),
        write(tmp_path / "aa.png", png, year(2030)),
        write(tmp_path / "b_same.jpg", jpeg_taken("2024:05:01 10:00:00")),
        write(tmp_path / "a_same.jpg", jpeg_taken("2024:05:01 10:00:00")),
        write(tmp_path / "mm.png", png, year(2022)),
    ]
    opts = JobOptions(rename=RenameOptions(enabled=True, sort_by=SORT_DATE),
                      output=OutputOptions(folder=str(tmp_path / "o")))
    plan = build_plan(photos_of(selection), opts)
    assert [e.photo.name for e in plan] == ["b_same.jpg", "a_same.jpg", "new.jpg", "zz.png", "aa.png", "mm.png"]
    assert [e.target_name for e in plan] == ["1.jpg", "2.jpg", "3.jpg", "4.png", "5.png", "6.png"]
    assert undated_count(plan) == 3
    opts.rename.descending = True
    plan = build_plan(photos_of(selection), opts)
    assert [e.photo.name for e in plan] == ["new.jpg", "b_same.jpg", "a_same.jpg", "zz.png", "aa.png", "mm.png"]
    # A manually changed selection order applies too.
    reordered = [selection[i] for i in (5, 0, 1, 2, 4, 3)]  # mm, zz, new, aa, a_same, b_same
    names = [e.photo.name for e in build_plan(photos_of(reordered), opts)]
    assert names == ["new.jpg", "a_same.jpg", "b_same.jpg", "mm.png", "zz.png", "aa.png"]
    # By size: files of equal size stay in selection order.
    opts.rename.sort_by, opts.rename.descending = SORT_SIZE, False
    names = [e.photo.name for e in build_plan(photos_of(selection), opts)]
    assert [n for n in names if n.endswith(".png")] == ["zz.png", "aa.png", "mm.png"]
    assert [n for n in names if n.endswith(".jpg")] == ["new.jpg", "b_same.jpg", "a_same.jpg"]
    assert undated_count(build_plan(photos_of(selection), opts)) == 3  # independent of the sort key
    # Files whose info is not read yet are not counted (they move into place once read).
    opts.rename.sort_by = SORT_DATE
    unread = [Photo.from_path(p, i) for i, p in enumerate(selection)]
    plan = build_plan(unread, opts)
    assert [e.photo.path for e in plan] == selection and undated_count(plan) == 0


# ======================================================================= planning


def make_set(tmp_path) -> list[str]:
    """Photos in different folders, taken in mixed order."""
    return [
        write(tmp_path / "A" / "IMG_0010.jpg", jpeg_taken("2024:05:20 10:00:00")),
        write(tmp_path / "A" / "IMG_0002.jpg", jpeg_taken("2024:05:01 09:00:00")),
        write(tmp_path / "B" / "photo.jpg", jpeg_taken("2024:06:15 18:30:00")),
        write(tmp_path / "B" / "IMG_0001.jpg", jpeg_taken("2024:04:30 23:59:59")),
    ]


def test_rename_by_date_numbers_in_order(tmp_path):
    paths = make_set(tmp_path)
    out = str(tmp_path / "out")
    opts = JobOptions(rename=RenameOptions(enabled=True, sort_by=SORT_DATE), output=OutputOptions(folder=out))
    plan = build_plan(photos_of(paths), opts)
    assert [(os.path.basename(e.photo.path), e.target_name) for e in plan] == [
        ("IMG_0001.jpg", "1.jpg"),
        ("IMG_0002.jpg", "2.jpg"),
        ("IMG_0010.jpg", "3.jpg"),
        ("photo.jpg", "4.jpg"),
    ]
    opts.rename.descending = True
    assert [e.target_name for e in build_plan(photos_of(paths), opts)] == ["1.jpg", "2.jpg", "3.jpg", "4.jpg"]
    assert os.path.basename(build_plan(photos_of(paths), opts)[0].photo.path) == "photo.jpg"


def test_name_sort_uses_explorer_order(tmp_path):
    paths = [write(tmp_path / n, jpeg_taken("2024:01:01 00:00:00")) for n in ("img10.jpg", "img2.jpg", "img1.jpg")]
    opts = JobOptions(rename=RenameOptions(enabled=True, sort_by=SORT_NAME), output=OutputOptions(folder=str(tmp_path / "o")))
    assert [os.path.basename(e.photo.path) for e in build_plan(photos_of(paths), opts)] == ["img1.jpg", "img2.jpg", "img10.jpg"]


def test_prefix_padding_and_start(tmp_path):
    paths = [write(tmp_path / f"{i}.jpg", jpeg_taken(f"2024:01:{i + 1:02d} 00:00:00")) for i in range(12)]
    opts = JobOptions(
        rename=RenameOptions(enabled=True, prefix='Holiday:_', start=1, zero_pad=True),
        output=OutputOptions(folder=str(tmp_path / "o")),
    )
    names = [e.target_name for e in build_plan(photos_of(paths), opts)]
    assert names[0] == "Holiday_01.jpg" and names[-1] == "Holiday_12.jpg"  # the invalid ':' was removed
    opts.rename.start = 95
    names = [e.target_name for e in build_plan(photos_of(paths), opts)]
    assert names[0] == "Holiday_095.jpg" and names[-1] == "Holiday_106.jpg"
    # When padding is requested, at least two digits even for a few photos
    opts.rename.start = 1
    names = [e.target_name for e in build_plan(photos_of(paths[:3]), opts)]
    assert names == ["Holiday_01.jpg", "Holiday_02.jpg", "Holiday_03.jpg"]


def test_extensions(tmp_path):
    buf = io.BytesIO()
    b.photo(32, 32).save(buf, "PNG")
    fake_jpg = write(tmp_path / "really_png.jpg", buf.getvalue())
    jfif = write(tmp_path / "download.jfif", jpeg_taken("2024:01:01 00:00:00"))
    heic = write(tmp_path / "IMG_1.HEIC", b.heic())
    photos = photos_of([fake_jpg, jfif, heic])
    out = OutputOptions(folder=str(tmp_path / "o"))
    names = {e.photo.name: e.target_name for e in build_plan(photos, JobOptions(content=ContentOptions(convert_to_jpeg=True), output=out))}
    assert names == {"really_png.jpg": "really_png.jpg", "download.jfif": "download.jpg", "IMG_1.HEIC": "IMG_1.jpg"}
    names = {e.photo.name: e.target_name for e in build_plan(photos, JobOptions(content=ContentOptions(), output=out))}
    assert names == {"really_png.jpg": "really_png.jpg", "download.jfif": "download.jfif", "IMG_1.HEIC": "IMG_1.HEIC"}
    opts = JobOptions(content=ContentOptions(), rename=RenameOptions(enabled=True, sort_by=SORT_MANUAL), output=out)
    assert [e.target_name for e in build_plan(photos, opts)] == ["1.png", "2.jpg", "3.heic"]


def test_group_by_month_restarts_numbering(tmp_path):
    paths = make_set(tmp_path)
    opts = JobOptions(rename=RenameOptions(enabled=True), output=OutputOptions(folder=str(tmp_path / "o"), group_by=GROUP_MONTH))
    plan = build_plan(photos_of(paths), opts)
    got = [(os.path.basename(e.target_dir), e.target_name) for e in plan]
    assert got == [("2024-04 Nisan", "1.jpg"), ("2024-05 Mayıs", "1.jpg"), ("2024-05 Mayıs", "2.jpg"), ("2024-06 Haziran", "1.jpg")]
    opts.output.group_by = GROUP_SOURCE
    assert {os.path.basename(e.target_dir) for e in build_plan(photos_of(paths), opts)} == {"A", "B"}


def test_undated_files_go_to_undated_folder(tmp_path):
    """When grouping by date, files without a capture date go to their own folder (selection order, numbered from 1)."""
    buf = io.BytesIO()
    b.photo(32, 32).save(buf, "PNG")
    paths = make_set(tmp_path) + [write(tmp_path / "C" / name, buf.getvalue()) for name in ("z.png", "a.png")]
    opts = JobOptions(rename=RenameOptions(enabled=True),
                      output=OutputOptions(folder=str(tmp_path / "o"), group_by=GROUP_MONTH))
    got = [(os.path.basename(e.target_dir), e.target_name, e.photo.name) for e in build_plan(photos_of(paths), opts)]
    assert got[0] == ("2024-04 Nisan", "1.jpg", "IMG_0001.jpg")
    assert got[-2:] == [(undated_folder(), "1.png", "z.png"), (undated_folder(), "2.png", "a.png")]
    opts.output.group_by = GROUP_SOURCE  # not by date: undated files also go to their source folders
    assert {os.path.basename(e.target_dir) for e in build_plan(photos_of(paths), opts)} == {"A", "B", "C"}


def test_conflicts_never_overwrite(tmp_path):
    paths = make_set(tmp_path)
    out = tmp_path / "o"
    write(out / "1.jpg", b"existing")
    opts = JobOptions(rename=RenameOptions(enabled=True), output=OutputOptions(folder=str(out)))
    plan = build_plan(photos_of(paths), opts)
    assert plan[0].target_name == "1 (2).jpg" and plan[0].renamed_for_conflict
    # Files with the same name collected into one folder
    same = [write(tmp_path / d / "IMG.jpg", jpeg_taken("2024:01:01 00:00:00")) for d in ("x", "y", "z")]
    names = sorted(e.target_name for e in build_plan(photos_of(same), JobOptions(output=OutputOptions(folder=str(out)))))
    assert names == ["IMG (2).jpg", "IMG (3).jpg", "IMG.jpg"]
    # If the output folder is the source folder, a source file is never used as a target
    plan = build_plan(photos_of(same[:1]), JobOptions(output=OutputOptions(folder=str(tmp_path / "x"))))
    assert plan[0].target_name == "IMG (2).jpg"


def test_unreadable_files_are_skipped_without_number_gaps(tmp_path):
    paths = make_set(tmp_path)
    fake = write(tmp_path / "A" / "000_fake.jpg", b"bu bir fotograf degil")
    opts = JobOptions(rename=RenameOptions(enabled=True, sort_by=SORT_NAME), output=OutputOptions(folder=str(tmp_path / "o")))
    plan = build_plan(photos_of(paths + [fake]), opts)
    assert [e.target_name for e in plan[:4]] == ["1.jpg", "2.jpg", "3.jpg", "4.jpg"]
    assert plan[-1].photo.name == "000_fake.jpg" and plan[-1].skip_reason
    report, _ = run(plan, opts, tmp_path)
    assert len(report.succeeded) == 4
    assert [r.entry.photo.name for r in report.failed] == ["000_fake.jpg"]
    assert sorted(os.listdir(tmp_path / "o")) == ["1.jpg", "2.jpg", "3.jpg", "4.jpg"]


def test_in_place_swap_needs_no_suffix(tmp_path):
    one = write(tmp_path / "1.jpg", jpeg_taken("2024:02:02 00:00:00"))
    two = write(tmp_path / "2.jpg", jpeg_taken("2024:01:01 00:00:00"))
    opts = JobOptions(rename=RenameOptions(enabled=True), output=OutputOptions(in_place=True))
    plan = build_plan(photos_of([one, two]), opts)
    assert [(e.photo.name, e.target_name) for e in plan] == [("2.jpg", "1.jpg"), ("1.jpg", "2.jpg")]
    assert not any(e.renamed_for_conflict for e in plan)


def test_in_place_convert_does_not_steal_unchanged_name(tmp_path):
    png_buf = io.BytesIO()
    b.photo(32, 32).save(png_buf, "PNG")
    a_png = write(tmp_path / "a.png", png_buf.getvalue())
    a_jpg = write(tmp_path / "a.jpg", jpeg_taken("2024:01:01 00:00:00"))
    opts = JobOptions(content=ContentOptions(strip_metadata=True, convert_to_jpeg=True), output=OutputOptions(in_place=True))
    plan = {e.photo.name: e.target_name for e in build_plan(photos_of([a_png, a_jpg]), opts)}
    assert plan == {"a.jpg": "a.jpg", "a.png": "a (2).jpg"}


# ===================================================================== execution


def run(plan, opts, tmp_path, **kw):
    bin_ = FakeBin(str(tmp_path / "_bin"))
    report = Runner(plan, opts, recycle_func=bin_, workers=kw.pop("workers", 3), **kw).run()
    return report, bin_


def test_new_folder_run_leaves_sources_untouched(tmp_path):
    paths = make_set(tmp_path)
    before = {p: open(p, "rb").read() for p in paths}
    out = str(tmp_path / "out")
    opts = JobOptions(
        content=ContentOptions(strip_metadata=True, convert_to_jpeg=True),
        rename=RenameOptions(enabled=True),
        output=OutputOptions(folder=out),
    )
    report, bin_ = run(build_plan(photos_of(paths), opts), opts, tmp_path)
    assert len(report.succeeded) == 4 and not report.failed
    assert sorted(os.listdir(out)) == ["1.jpg", "2.jpg", "3.jpg", "4.jpg"]
    for p, data in before.items():
        assert open(p, "rb").read() == data
    for name in os.listdir(out):
        data = open(os.path.join(out, name), "rb").read()
        assert scan_file(os.path.join(out, name)).clean
        assert_exiv2_clean(data)
    assert not bin_.items and no_temp_files(tmp_path)


def test_in_place_strip_recycles_originals(tmp_path):
    paths = make_set(tmp_path)
    opts = JobOptions(content=ContentOptions(strip_metadata=True), output=OutputOptions(in_place=True))
    report, bin_ = run(build_plan(photos_of(paths), opts), opts, tmp_path)
    assert len(report.succeeded) == 4 and report.recycled == 4
    for p in paths:
        assert os.path.exists(p) and scan_file(p).clean
    assert len(bin_.items) == 4 and all(not scan_file(i).clean for i in bin_.items)
    assert no_temp_files(tmp_path)


def test_in_place_permanent_replace(tmp_path):
    p = write(tmp_path / "x.jpg", b.jpeg())
    opts = JobOptions(content=ContentOptions(strip_metadata=True), output=OutputOptions(in_place=True, recycle=False))
    report, bin_ = run(build_plan(photos_of([p]), opts), opts, tmp_path)
    assert report.succeeded and scan_file(p).clean and not bin_.items


def test_in_place_rename_swap_and_convert(tmp_path):
    one = write(tmp_path / "1.jpg", jpeg_taken("2024:02:02 00:00:00"))
    two = write(tmp_path / "2.jpg", jpeg_taken("2024:01:01 00:00:00"))
    one_data, two_data = open(one, "rb").read(), open(two, "rb").read()
    opts = JobOptions(rename=RenameOptions(enabled=True), output=OutputOptions(in_place=True))
    report, bin_ = run(build_plan(photos_of([one, two]), opts), opts, tmp_path)
    assert len(report.succeeded) == 2 and not bin_.items
    assert open(one, "rb").read() == two_data and open(two, "rb").read() == one_data

    png_path = write(tmp_path / "c" / "shot.png", b.png())
    opts = JobOptions(content=ContentOptions(strip_metadata=True, convert_to_jpeg=True), output=OutputOptions(in_place=True))
    report, bin_ = run(build_plan(photos_of([png_path]), opts), opts, tmp_path)
    assert os.listdir(tmp_path / "c") == ["shot.jpg"]
    assert Image.open(tmp_path / "c" / "shot.jpg").format == "JPEG"
    assert len(bin_.items) == 1


def test_in_place_group_moves_into_subfolders(tmp_path):
    paths = make_set(tmp_path)
    opts = JobOptions(rename=RenameOptions(enabled=True), output=OutputOptions(in_place=True, group_by=GROUP_MONTH))
    report, _ = run(build_plan(photos_of(paths), opts), opts, tmp_path)
    assert len(report.succeeded) == 4
    assert sorted(os.listdir(tmp_path / "A")) == ["2024-05 Mayıs"]
    assert sorted(os.listdir(tmp_path / "A" / "2024-05 Mayıs")) == ["1.jpg", "2.jpg"]
    assert sorted(os.listdir(tmp_path / "B")) == ["2024-04 Nisan", "2024-06 Haziran"]


def test_errors_are_isolated(tmp_path):
    good = make_set(tmp_path)
    bad = write(tmp_path / "A" / "broken.jpg", b.jpeg(everything=False)[:500])
    missing = write(tmp_path / "A" / "missing.jpg", jpeg_taken("2024:01:01 00:00:00"))
    opts = JobOptions(content=ContentOptions(strip_metadata=True), output=OutputOptions(in_place=True))
    plan = build_plan(photos_of(good + [bad, missing]), opts)
    os.remove(missing)
    report, bin_ = run(plan, opts, tmp_path)
    assert len(report.succeeded) == 4
    errors = {os.path.basename(r.entry.photo.path): r.error for r in report.failed}
    assert set(errors) == {"broken.jpg", "missing.jpg"}
    assert "bulunamadı" in errors["missing.jpg"]
    assert open(bad, "rb").read() == b.jpeg(everything=False)[:500]  # the broken file was left alone
    assert no_temp_files(tmp_path)


def test_cancel_changes_nothing(tmp_path):
    paths = make_set(tmp_path)
    before = {p: open(p, "rb").read() for p in paths}
    opts = JobOptions(content=ContentOptions(strip_metadata=True), rename=RenameOptions(enabled=True), output=OutputOptions(in_place=True))
    ev = threading.Event()
    calls = []

    def progress(stage, done, total, name):
        calls.append(stage)
        if done >= 2:
            ev.set()

    report, bin_ = run(build_plan(photos_of(paths), opts), opts, tmp_path, on_progress=progress, cancel_event=ev, workers=1)
    assert report.cancelled and not report.succeeded and not bin_.items
    for p, data in before.items():
        assert open(p, "rb").read() == data
    assert no_temp_files(tmp_path)


def test_read_only_file_left_alone(tmp_path):
    p = write(tmp_path / "ro.jpg", b.jpeg())
    os.chmod(p, stat.S_IREAD)
    try:
        opts = JobOptions(content=ContentOptions(strip_metadata=True), output=OutputOptions(in_place=True))
        report, bin_ = run(build_plan(photos_of([p]), opts), opts, tmp_path)
        assert report.failed and "salt okunur" in report.failed[0].error
        assert not scan_file(p).clean and not bin_.items
    finally:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)


def test_timestamps(tmp_path):
    old = time.mktime((2020, 1, 1, 12, 0, 0, 0, 0, -1))
    p = write(tmp_path / "src" / "a.png", b.png(), mtime=old)
    out = str(tmp_path / "out")
    keep = JobOptions(content=ContentOptions(strip_metadata=False, convert_to_jpeg=True), output=OutputOptions(folder=out))
    run(build_plan(photos_of([p]), keep), keep, tmp_path)
    assert abs(os.stat(os.path.join(out, "a.jpg")).st_mtime - old) < 2  # metadata kept, so the mtime is kept too

    out2 = str(tmp_path / "out2")
    strip = JobOptions(content=ContentOptions(strip_metadata=True, convert_to_jpeg=True), output=OutputOptions(folder=out2))
    run(build_plan(photos_of([p]), strip), strip, tmp_path)
    assert abs(os.stat(os.path.join(out2, "a.jpg")).st_mtime - time.time()) < 120  # new file, new mtime


def test_copy_only_when_no_operation(tmp_path):
    p = write(tmp_path / "src" / "a.jpg", b.jpeg())
    out = str(tmp_path / "out")
    opts = JobOptions(content=ContentOptions(strip_metadata=False), output=OutputOptions(folder=out))
    report, _ = run(build_plan(photos_of([p]), opts), opts, tmp_path)
    assert report.succeeded
    assert open(os.path.join(out, "a.jpg"), "rb").read() == open(p, "rb").read()
