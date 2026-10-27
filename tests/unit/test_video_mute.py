"""Removing a video's audio (Edit.mute): the edit model, the plan, the lossless copy and the re-encode
paths, verification and the runner."""

from __future__ import annotations

import os
from dataclasses import dataclass

import av
import numpy as np
import pytest
from helpers import photos_of
from video_builders import APPLE_TAGS, make_video

from fotodit.core.edits import Edit
from fotodit.core.formats import Fmt
from fotodit.core.planner import JobOptions, OutputOptions, build_plan
from fotodit.core.processor import ContentOptions
from fotodit.core.runner import Runner
from fotodit.core.video import process as video_process
from fotodit.core.video.probe import probe
from fotodit.core.video.process import (
    VideoError,
    edit_reason,
    plan_video,
    process_video,
)


@dataclass
class Opts:
    strip_metadata: bool = True
    convert_video: bool = False
    video_codec: str = "h264"
    video_hw: bool = False


MUTE = Edit(mute=True)


def audio_streams(path: str) -> int:
    with av.open(path) as c:
        return len(c.streams.audio)


def frames(path: str, n: int = 3) -> list[np.ndarray]:
    out = []
    with av.open(path) as c:
        for frame in c.decode(video=0):
            out.append(frame.to_ndarray(format="rgb24"))
            if len(out) >= n:
                break
    return out


# ========================================================================= the edit


def test_mute_in_the_edit_model():
    assert not MUTE.is_identity() and Edit(mute=False).is_identity()
    assert MUTE.photo_only() is None  # photos have no sound
    assert Edit(quarter=1, mute=True).photo_only() == Edit(quarter=1)
    assert Edit(quarter=1, mute=True).geometry_only() == Edit(quarter=1)
    assert Edit(quarter=1, mute=True).rotated(1).mute and MUTE.tidied(10.0).mute
    assert MUTE.describe() == "sesi kaldırıldı" and MUTE.describe(future=True) == "sesi kaldırılacak"
    assert Edit(quarter=1, mute=True).describe() == "sağa 90° döndürüldü · sesi kaldırıldı"
    assert MUTE.short() == "sessiz" and Edit(trim=(1.0, None), mute=True).short() == "✂ 0:01 – son · sessiz"


def test_mute_works_in_every_container():
    assert edit_reason(Fmt.MKV, Opts(), MUTE) is None  # no MP4 needed to leave the audio out
    assert edit_reason(Fmt.AVI, Opts(), Edit(quarter=1, mute=True)) is not None  # the rotation still needs one


def test_plan(tmp_path):
    info = probe(make_video(str(tmp_path / "a.mp4")))
    plan = plan_video(info, Opts(strip_metadata=False), MUTE)
    assert plan.mute and plan.lossless and not plan.encode_video and not plan.noop(info)
    removing = plan_video(info, Opts(), Edit(cuts=((0.2, 0.4),), mute=True))
    assert removing.encode_video and not removing.audio_encode  # no audio left to encode
    silent = probe(make_video(str(tmp_path / "silent.mp4"), audio=None))
    plan = plan_video(silent, Opts(strip_metadata=False), MUTE)
    assert not plan.mute and plan.noop(silent)  # nothing to remove: the file stays as it is


# ======================================================================== processing


@pytest.mark.parametrize("strip", [False, True])
def test_copy_path_stays_lossless(tmp_path, strip):
    src = make_video(str(tmp_path / "IMG_0001.MOV"), fmt="mov", tags=APPLE_TAGS, apple_keys=True, rotation=90)
    dst = str(tmp_path / "out.mov")
    result = process_video(src, dst, Opts(strip_metadata=strip), MUTE)
    assert result.method == "lossless" and "Ses kaldırıldı." in result.notes
    assert not any("ses izi" in n for n in result.notes)
    assert audio_streams(src) == 1 and audio_streams(dst) == 0
    for a, b in zip(frames(src), frames(dst), strict=True):
        assert np.array_equal(a, b)  # the video was not re-encoded
    info = probe(dst)
    assert info.rotation == 90 and info.duration == pytest.approx(probe(src).duration, abs=0.1)
    assert info.has_gps != strip  # the metadata is kept or stripped as asked, independently


def test_reencode_path(tmp_path):
    src = make_video(str(tmp_path / "a.mp4"))
    dst = str(tmp_path / "crop.mp4")
    result = process_video(src, dst, Opts(), Edit(crop=(0.0, 0.0, 0.5, 1.0), mute=True))
    assert result.method == "reencoded" and audio_streams(dst) == 0 and probe(dst).display_size == (160, 240)
    cut = str(tmp_path / "cut.mp4")
    result = process_video(src, cut, Opts(), Edit(cuts=((0.25, 0.5),), mute=True))
    assert audio_streams(cut) == 0 and probe(cut).duration == pytest.approx(0.75, abs=0.06)


def test_trim_and_conversion(tmp_path):
    src = make_video(str(tmp_path / "a.mp4"), frames_n=48, gop=12)
    dst = str(tmp_path / "trim.mp4")
    result = process_video(src, dst, Opts(), Edit(trim=(0.5, None), mute=True))
    assert result.method == "lossless" and audio_streams(dst) == 0
    assert probe(dst).duration == pytest.approx(1.5, abs=0.06)
    pcm = make_video(str(tmp_path / "b.avi"), fmt="avi", vcodec="mjpeg", pix_fmt="yuvj420p", audio="pcm_s16le")
    mp4 = str(tmp_path / "b.mp4")
    process_video(pcm, mp4, Opts(convert_video=True), MUTE)  # the PCM audio would need encoding: it is dropped
    assert probe(mp4).acodec is None and audio_streams(mp4) == 0
    mkv = make_video(str(tmp_path / "c.mkv"), fmt="matroska")
    out = str(tmp_path / "c_out.mkv")
    assert process_video(mkv, out, Opts(), MUTE).method == "lossless"  # stays MKV
    assert probe(out).fmt == Fmt.MKV and audio_streams(out) == 0


def test_verification_expects_no_audio(tmp_path):
    src = make_video(str(tmp_path / "a.mp4"))
    info = probe(src)
    job = video_process._Job(src, str(tmp_path / "out.mp4"), info, plan_video(info, Opts(), MUTE), None, None)
    job.run()
    video_process._verify(job)
    job.expected_streams.append("audio")  # had the audio stayed, the count would not match
    with pytest.raises(VideoError, match="izleri eksik"):
        video_process._verify(job)


def test_runner_mutes(tmp_path):
    src_dir = tmp_path / "src"
    os.makedirs(src_dir)
    src = make_video(str(src_dir / "clip.mp4"))
    photos = photos_of([src])
    photos[0].edit = MUTE
    out = tmp_path / "out"
    opts = JobOptions(content=ContentOptions(), output=OutputOptions(folder=str(out)))
    plan = build_plan(photos, opts)
    assert plan[0].changes_content(opts) and plan[0].video_plan.mute
    report = Runner(plan, opts).run()
    assert not report.failed and report.results[0].content_changed
    assert audio_streams(str(out / "clip.mp4")) == 0 and audio_streams(src) == 1
