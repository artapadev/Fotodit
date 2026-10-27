"""Videos in the plan and on disk: separate naming, MP4 extension, skip rule, end-to-end processing."""

from __future__ import annotations

import os
import threading
from datetime import datetime

import pytest
from helpers import FakeBin, jpeg_taken, photos_of, write
from video_builders import APPLE_TAGS, make_video

from fotodit.core.edits import Edit
from fotodit.core.meta import Kind, scan_file
from fotodit.core.photoinfo import DATE_VIDEO, read_info
from fotodit.core.planner import (
    GROUP_MONTH,
    SORT_DATE,
    JobOptions,
    OutputOptions,
    RenameOptions,
    build_plan,
)
from fotodit.core.processor import ContentOptions
from fotodit.core.runner import STAGE_FILE, TEMP_PREFIX, Runner
from fotodit.core.video.probe import probe
from fotodit.core.video.process import VIDEO_EDIT_NEEDS_MP4
from fotodit.i18n import t


@pytest.fixture
def mixed(tmp_path):
    """Two photos and two videos (an iPhone MOV and an Android MP4) with capture dates in mixed order."""
    src = tmp_path / "Holiday"
    os.makedirs(src)
    paths = [
        write(src / "IMG_0001.JPG", jpeg_taken("2024:07:12 09:00:00")),
        make_video(str(src / "IMG_0002.MOV"), fmt="mov", tags=APPLE_TAGS, apple_keys=True),  # 18:30
        write(src / "IMG_0003.JPG", jpeg_taken("2024:07:12 20:00:00")),
        make_video(str(src / "VID_20240712_120000.mp4"),
                   tags={"creation_time": "2024-07-12T09:00:00Z", "location": "+41.0+028.9/"}),  # 12:00
    ]
    return tmp_path, paths


def test_video_info_in_selection(mixed):
    _, paths = mixed
    info = read_info(paths[1])
    assert info.is_video and info.taken == datetime(2024, 7, 12, 18, 30, 10) and info.taken_source == DATE_VIDEO
    assert info.has_gps and info.camera == "Apple iPhone 15 Pro" and info.video.duration > 0.5
    result = scan_file(paths[1])
    assert Kind.GPS in result.kinds() and result.summary.camera == "Apple iPhone 15 Pro"


def test_separate_naming_for_photos_and_videos(mixed):
    tmp, paths = mixed
    opts = JobOptions(
        content=ContentOptions(convert_video=True),
        rename=RenameOptions(enabled=True, sort_by=SORT_DATE, prefix="Holiday_", video_prefix="Video_", video_start=5),
        output=OutputOptions(folder=str(tmp / "out")),
    )
    plan = build_plan(photos_of(paths), opts)
    names = {os.path.basename(e.photo.path): e.target_name for e in plan}
    assert names == {
        "IMG_0001.JPG": "Holiday_1.jpg", "IMG_0003.JPG": "Holiday_2.jpg",  # photos: their own sequence
        "VID_20240712_120000.mp4": "Video_5.mp4", "IMG_0002.MOV": "Video_6.mp4",  # videos: separate sequence, .mp4
    }


def test_video_extension_without_rename(mixed):
    tmp, paths = mixed
    keep = JobOptions(output=OutputOptions(folder=str(tmp / "out")))
    assert build_plan(photos_of(paths[1:2]), keep)[0].target_name == "IMG_0002.MOV"
    convert = JobOptions(content=ContentOptions(convert_video=True), output=OutputOptions(folder=str(tmp / "o")))
    assert build_plan(photos_of(paths[1:2]), convert)[0].target_name == "IMG_0002.mp4"


def test_group_videos_by_capture_month(mixed):
    tmp, paths = mixed
    opts = JobOptions(output=OutputOptions(folder=str(tmp / "out"), group_by=GROUP_MONTH))
    plan = build_plan(photos_of(paths), opts)
    assert {os.path.basename(e.target_dir) for e in plan} == {"2024-07 Temmuz"}


def test_edit_on_mkv_needs_conversion(tmp_path):
    path = make_video(str(tmp_path / "clip.mkv"), fmt="matroska")
    photo = photos_of([path])[0]
    photo.edit = Edit(quarter=1)
    plan = build_plan([photo], JobOptions(output=OutputOptions(folder=str(tmp_path / "o"))))
    assert plan[0].skip_reason == t(VIDEO_EDIT_NEEDS_MP4)
    ok = build_plan([photo], JobOptions(content=ContentOptions(convert_video=True),
                                        output=OutputOptions(folder=str(tmp_path / "o"))))
    assert ok[0].skip_reason is None and ok[0].target_name == "clip.mp4"


def test_run_mixed_selection(mixed):
    tmp, paths = mixed
    out = tmp / "out"
    opts = JobOptions(
        content=ContentOptions(strip_metadata=True, convert_to_jpeg=True, convert_video=True),
        rename=RenameOptions(enabled=True, prefix="Photo_", video_prefix="Video_"),
        output=OutputOptions(folder=str(out)),
    )
    photos = photos_of(paths)
    photos[1].edit = Edit(quarter=1)  # rotate the iPhone video right
    events = []
    report = Runner(build_plan(photos, opts), opts, on_progress=lambda *a: events.append(a)).run()
    assert not report.failed and len(report.succeeded) == 4
    files = sorted(os.listdir(out))
    assert files == ["Photo_1.jpg", "Photo_2.jpg", "Video_1.mp4", "Video_2.mp4"]
    for name in ("Video_1.mp4", "Video_2.mp4"):
        info = probe(str(out / name))
        assert not info.has_gps and info.camera is None and set(info.tags) <= {
            "major_brand", "minor_version", "compatible_brands"}
    assert probe(str(out / "Video_2.mp4")).rotation == 90  # second in order: the iPhone video from 18:30
    assert any(stage == STAGE_FILE for stage, *_ in events)
    assert os.path.exists(paths[1])  # originals stay unchanged


def test_run_in_place_replaces_video(mixed):
    tmp, paths = mixed
    bin_ = FakeBin(str(tmp / "bin"))
    opts = JobOptions(content=ContentOptions(strip_metadata=True), output=OutputOptions(in_place=True))
    report = Runner(build_plan(photos_of(paths[1:2]), opts), opts, recycle_func=bin_).run()
    assert not report.failed and report.recycled == 1
    cleaned = probe(paths[1])
    assert cleaned.fmt.value == "MOV" and not cleaned.has_gps
    assert probe(bin_.items[0]).has_gps  # the original is in the Recycle Bin
    assert not [f for f in os.listdir(os.path.dirname(paths[1])) if f.startswith(TEMP_PREFIX)]


def test_already_mp4_is_copied_without_processing(tmp_path):
    path = make_video(str(tmp_path / "a.mp4"), tags={"title": "keep me"})
    opts = JobOptions(content=ContentOptions(convert_video=True, video_codec="keep"),
                      output=OutputOptions(folder=str(tmp_path / "out")))
    report = Runner(build_plan(photos_of([path]), opts), opts).run()
    result = report.succeeded[0]
    assert not result.content_changed
    with open(path, "rb") as a, open(result.final_path, "rb") as b:
        assert a.read() == b.read()


def test_cancel_during_video(tmp_path):
    path = make_video(str(tmp_path / "a.avi"), fmt="avi", vcodec="mjpeg", pix_fmt="yuvj420p", frames_n=48,
                      audio=None)
    cancel = threading.Event()

    def progress(stage, done, total, name):
        if stage == STAGE_FILE and done > 0:
            cancel.set()

    opts = JobOptions(content=ContentOptions(convert_video=True), output=OutputOptions(folder=str(tmp_path / "out")))
    report = Runner(build_plan(photos_of([path]), opts), opts, on_progress=progress, cancel_event=cancel).run()
    assert report.cancelled
    assert not os.path.exists(tmp_path / "out") or not os.listdir(tmp_path / "out")
