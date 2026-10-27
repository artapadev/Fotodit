"""Names from the capture date (date and time, day and number) and file names that give away the capture
date or the device/app when they are kept."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import pytest
from helpers import write

from fotodit.core.formats import Fmt
from fotodit.core.photoinfo import DATE_EXIF, DATE_VIDEO, PhotoInfo, name_reveals
from fotodit.core.planner import (
    GROUP_MONTH,
    NAME_DATE_NUMBER,
    NAME_DATE_TIME,
    NAME_NUMBER,
    SORT_DATE,
    SORT_NAME,
    JobOptions,
    OutputOptions,
    Photo,
    RenameOptions,
    build_plan,
)
from fotodit.core.processor import ContentOptions

OLD = time.mktime((2021, 6, 1, 12, 0, 0, 0, 0, -1))  # a modification time: never a capture date


def shot(folder, name: str, when: datetime | None = None, order: int = 0, video: bool = False) -> Photo:
    """A photo (or video) in the selection with the given capture date (None: undated). Nothing is read
    from disk: the plan only needs the info."""
    fmt = Fmt.MP4 if video else {".png": Fmt.PNG, ".nef": Fmt.RAW}.get(os.path.splitext(name)[1].lower(), Fmt.JPEG)
    info = PhotoInfo(fmt=fmt, width=4000, height=3000, taken=when,
                     taken_source=(DATE_VIDEO if video else DATE_EXIF) if when else None)
    return Photo(os.path.join(str(folder), name), 1000, OLD, order, info)


def plan_names(photos, out, rename: RenameOptions, group_by: str = "none") -> list[tuple[str, str, str]]:
    opts = JobOptions(rename=rename, output=OutputOptions(folder=str(out), group_by=group_by))
    return [(os.path.basename(e.target_dir), e.photo.name, e.target_name) for e in build_plan(photos, opts)]


T = datetime(2024, 5, 12, 14, 30, 22)


# ======================================================================= date and time


def test_date_time_names_with_the_same_second(tmp_path):
    src = tmp_path / "src"
    photos = [
        shot(src, "b.jpg", T.replace(microsecond=500000), 0),
        shot(src, "a.jpg", T.replace(microsecond=100000), 1),
        shot(src, "c.jpg", T + timedelta(seconds=1), 2),
        shot(src, "d.jpg", T.replace(microsecond=900000), 3),
        shot(src, "DSC_0001.NEF", T.replace(microsecond=100000), 4),  # the RAW of a.jpg
        shot(src, "z.png", None, 5),
        shot(src, "y.png", None, 6),
    ]
    got = plan_names(photos, tmp_path / "out", RenameOptions(enabled=True, pattern=NAME_DATE_TIME))
    assert [(name, target) for _, name, target in got] == [
        ("a.jpg", "2024-05-12_14-30-22.jpg"),  # in sort order (by the capture date with sub-seconds)
        ("DSC_0001.NEF", "2024-05-12_14-30-22.nef"),  # another extension: no clash, the pair keeps its name
        ("b.jpg", "2024-05-12_14-30-22_2.jpg"),
        ("d.jpg", "2024-05-12_14-30-22_3.jpg"),
        ("c.jpg", "2024-05-12_14-30-23.jpg"),
        ("z.png", "Tarihsiz_1.png"),  # undated: numbered in selection order
        ("y.png", "Tarihsiz_2.png"),
    ]


def test_date_time_names_with_prefix_padding_and_descending_order(tmp_path):
    src = tmp_path / "src"
    photos = [shot(src, f"{i}.jpg", T, i) for i in range(2)] + [shot(src, f"u{i}.png", None, 10 + i) for i in range(3)]
    rename = RenameOptions(enabled=True, pattern=NAME_DATE_TIME, prefix="Tatil:_", zero_pad=True, descending=True)
    got = [target for _, _, target in plan_names(photos, tmp_path / "out", rename)]
    assert got == ["Tatil_2024-05-12_14-30-22.jpg", "Tatil_2024-05-12_14-30-22_2.jpg",
                   "Tatil_Tarihsiz_01.png", "Tatil_Tarihsiz_02.png", "Tatil_Tarihsiz_03.png"]


def test_same_second_in_different_folders_stays_plain(tmp_path):
    photos = [shot(tmp_path / "x", "a.jpg", T, 0), shot(tmp_path / "y", "b.jpg", T, 1)]
    rename = RenameOptions(enabled=True, pattern=NAME_DATE_TIME)
    got = plan_names(photos, tmp_path / "out", rename, group_by="source")
    assert sorted(got) == [("x", "a.jpg", "2024-05-12_14-30-22.jpg"), ("y", "b.jpg", "2024-05-12_14-30-22.jpg")]


def test_existing_files_still_get_a_suffix(tmp_path):
    out = tmp_path / "out"
    write(out / "2024-05-12_14-30-22.jpg", b"existing")
    got = plan_names([shot(tmp_path / "src", "a.jpg", T)], out, RenameOptions(enabled=True, pattern=NAME_DATE_TIME))
    assert got[0][2] == "2024-05-12_14-30-22 (2).jpg"


# ====================================================================== day and number


def test_day_numbers_restart_every_day(tmp_path):
    src = tmp_path / "src"
    day2 = T + timedelta(days=1)
    photos = [
        shot(src, "a.jpg", T, 0), shot(src, "b.jpg", day2, 1), shot(src, "c.jpg", T + timedelta(hours=2), 2),
        shot(src, "u.png", None, 3), shot(src, "d.jpg", day2 + timedelta(minutes=1), 4), shot(src, "e.jpg", T, 5),
    ]
    got = plan_names(photos, tmp_path / "out", RenameOptions(enabled=True, pattern=NAME_DATE_NUMBER, prefix="Foto_"))
    assert [(name, target) for _, name, target in got] == [
        ("a.jpg", "Foto_2024-05-12_001.jpg"), ("e.jpg", "Foto_2024-05-12_002.jpg"),
        ("c.jpg", "Foto_2024-05-12_003.jpg"), ("b.jpg", "Foto_2024-05-13_001.jpg"),
        ("d.jpg", "Foto_2024-05-13_002.jpg"), ("u.png", "Foto_Tarihsiz_001.png"),
    ]


def test_day_numbers_widen_only_when_needed(tmp_path):
    src = tmp_path / "src"
    photos = [shot(src, f"{i:05d}.jpg", T + timedelta(seconds=i), i) for i in range(1001)]
    got = [target for _, _, target in plan_names(photos, tmp_path / "out",
                                                 RenameOptions(enabled=True, pattern=NAME_DATE_NUMBER))]
    assert got[0] == "2024-05-12_001.jpg" and got[998] == "2024-05-12_999.jpg" and got[-1] == "2024-05-12_1001.jpg"


def test_day_numbers_follow_the_sort_order_and_the_folders(tmp_path):
    src = tmp_path / "src"
    photos = [shot(src, "b.jpg", T.replace(month=6), 0), shot(src, "a.jpg", T.replace(hour=20), 1),
              shot(src, "c.jpg", T, 2)]
    rename = RenameOptions(enabled=True, pattern=NAME_DATE_NUMBER, sort_by=SORT_NAME)
    assert [t for _, _, t in plan_names(photos, tmp_path / "out", rename)] == [
        "2024-05-12_001.jpg", "2024-06-12_001.jpg", "2024-05-12_002.jpg"]  # a, b, c by name
    got = plan_names(photos, tmp_path / "out", RenameOptions(enabled=True, pattern=NAME_DATE_NUMBER),
                     group_by=GROUP_MONTH)
    assert got == [("2024-05 Mayıs", "c.jpg", "2024-05-12_001.jpg"), ("2024-05 Mayıs", "a.jpg", "2024-05-12_002.jpg"),
                   ("2024-06 Haziran", "b.jpg", "2024-06-12_001.jpg")]


def test_photos_and_videos_are_named_and_counted_separately(tmp_path):
    src = tmp_path / "src"
    photos = [shot(src, "IMG_1.jpg", T, 0), shot(src, "VID_1.mp4", T + timedelta(minutes=1), 1, video=True),
              shot(src, "IMG_2.jpg", T + timedelta(minutes=2), 2),
              shot(src, "VID_2.mp4", T + timedelta(minutes=3), 3, video=True)]
    rename = RenameOptions(enabled=True, pattern=NAME_DATE_NUMBER, prefix="Foto_", video_prefix="Video_",
                           start=7, video_start=9)
    assert {n: t for _, n, t in plan_names(photos, tmp_path / "out", rename)} == {
        "IMG_1.jpg": "Foto_2024-05-12_001.jpg", "IMG_2.jpg": "Foto_2024-05-12_002.jpg",
        "VID_1.mp4": "Video_2024-05-12_001.mp4", "VID_2.mp4": "Video_2024-05-12_002.mp4",
    }
    rename.pattern = NAME_DATE_TIME
    assert {n: t for _, n, t in plan_names(photos, tmp_path / "out", rename)} == {
        "IMG_1.jpg": "Foto_2024-05-12_14-30-22.jpg", "IMG_2.jpg": "Foto_2024-05-12_14-32-22.jpg",
        "VID_1.mp4": "Video_2024-05-12_14-31-22.mp4", "VID_2.mp4": "Video_2024-05-12_14-33-22.mp4",
    }


def test_numbers_stay_the_default(tmp_path):
    assert RenameOptions().pattern == NAME_NUMBER
    src = tmp_path / "src"
    photos = [shot(src, "b.jpg", T, 0), shot(src, "a.jpg", T - timedelta(days=1), 1)]
    got = [t for _, _, t in plan_names(photos, tmp_path / "out", RenameOptions(enabled=True, prefix="X_", start=5))]
    assert got == ["X_5.jpg", "X_6.jpg"]


def test_date_patterns_need_dates():
    assert not JobOptions(rename=RenameOptions(enabled=True, sort_by=SORT_NAME)).needs_dates()
    assert JobOptions(rename=RenameOptions(enabled=True, sort_by=SORT_NAME, pattern=NAME_DATE_TIME)).needs_dates()
    assert JobOptions(rename=RenameOptions(enabled=True, sort_by=SORT_NAME, pattern=NAME_DATE_NUMBER)).needs_dates()
    assert not JobOptions(rename=RenameOptions(enabled=False, sort_by=SORT_NAME, pattern=NAME_DATE_TIME)).needs_dates()
    assert JobOptions(rename=RenameOptions(enabled=True, sort_by=SORT_DATE)).needs_dates()


def test_plan_stays_linear_for_large_selections(tmp_path):
    """The plan is recomputed on every option change: 20,000 files must stay quick."""
    src = tmp_path / "src"
    photos = [shot(src, f"IMG_{i:05d}.jpg", T + timedelta(seconds=i // 3), i) for i in range(20000)]
    for pattern in (NAME_DATE_TIME, NAME_DATE_NUMBER):
        start = time.perf_counter()
        plan = build_plan(photos, JobOptions(rename=RenameOptions(enabled=True, pattern=pattern),
                                             output=OutputOptions(folder=str(tmp_path / "out"), group_by=GROUP_MONTH)))
        assert time.perf_counter() - start < 10
        assert len({e.target_name for e in plan}) == 20000


# ================================================================ names that give away


@pytest.mark.parametrize("name,reason", [
    ("IMG_20240512_143022.jpg", "photoinfo.name_date"),
    ("PXL_20240512_143022123.jpg", "photoinfo.name_date"),
    ("WhatsApp Image 2024-05-12 at 14.30.22.jpeg", "photoinfo.name_date"),
    ("Ekran görüntüsü 2024-05-12 143022.png", "photoinfo.name_date"),
    ("20240512_143022.mp4", "photoinfo.name_date"),
    ("DJI_0001.JPG", "photoinfo.name_device"),
    ("GOPR0001.JPG", "photoinfo.name_device"),
    ("GH010001.MP4", "photoinfo.name_device"),
    ("GX010001.MP4", "photoinfo.name_device"),
    ("DSC_0001.NEF", "photoinfo.name_device"),
    ("DSCN0001.JPG", "photoinfo.name_device"),
    ("DSCF0001.RAF", "photoinfo.name_device"),
    ("_DSC0001.ARW", "photoinfo.name_device"),
    ("DSC01234.JPG", "photoinfo.name_device"),
    ("PXL_0001.jpg", "photoinfo.name_device"),
    ("WhatsApp Video (2).mp4", "photoinfo.name_device"),
    ("signal-attachment.jpg", "photoinfo.name_device"),
    ("Snapchat-123456789.jpg", "photoinfo.name_device"),
    ("Screenshot (12).png", "photoinfo.name_device"),
    ("Ekran görüntüsü (3).png", "photoinfo.name_device"),
    ("EKRAN GÖRÜNTÜSÜ.png", "photoinfo.name_device"),
    ("tatil.jpg", None),
    ("IMG_1234.HEIC", None),
    ("Holiday_1.jpg", None),
    ("2024-05-12_001.jpg", "photoinfo.name_date"),  # Fotodit's own date names give the date away too
])
def test_name_reveals(name, reason):
    assert name_reveals(name) == reason


def test_plan_flags_kept_names_that_give_away(tmp_path):
    src = tmp_path / "src"
    photos = [shot(src, "IMG_20240512_143022.jpg", T, 0), shot(src, "DJI_0001.JPG", T, 1),
              shot(src, "tatil.jpg", T, 2), shot(src, "broken.jpg", T, 3)]
    photos[3].info.error = "Dosya açılamadı."
    strip = JobOptions(content=ContentOptions(strip_metadata=True), output=OutputOptions(folder=str(tmp_path / "o")))
    leaks = {e.photo.name: e.name_leak for e in build_plan(photos, strip)}
    assert leaks == {"IMG_20240512_143022.jpg": "photoinfo.name_date",
                     "DJI_0001.JPG": "photoinfo.name_device", "tatil.jpg": None, "broken.jpg": None}
    strip.rename.enabled = True  # renamed: the old names go away
    assert all(e.name_leak is None for e in build_plan(photos, strip))
    keep = JobOptions(output=OutputOptions(folder=str(tmp_path / "o")))  # metadata kept anyway
    assert all(e.name_leak is None for e in build_plan(photos, keep))
