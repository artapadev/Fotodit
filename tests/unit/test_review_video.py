"""Video fixes after review: audio tracks FFmpeg cannot decode, lossless trims next to a keyframe and
between milliseconds at the end, camera EXIF in an AVI's stream header and DV recording dates, the
output file left locked after a failed header, location tracks named only in the boxes, mirrored
display matrices, MPEG-TS start times, non-square pixels, MKV B-frames and late trims of MKVs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
import pytest
from builders import dominant, marker_color
from video_builders import (
    HFLIP,
    add_strd,
    avif_exif,
    make_dv,
    make_video,
    set_matrix,
    set_sample_entry,
    upright_frame,
)

from fotodit.core.edits import Edit
from fotodit.core.formats import Fmt
from fotodit.core.video import bitstream, process
from fotodit.core.video.boxes import read_structure
from fotodit.core.video.probe import probe, scan_result
from fotodit.core.video.process import (
    VIDEO_COMPATIBLE,
    VIDEO_KEEP,
    VideoError,
    process_video,
)
from fotodit.core.video.thumb import poster
from fotodit.i18n import t


@dataclass
class Opts:
    strip_metadata: bool = True
    convert_video: bool = False
    video_codec: str = VIDEO_COMPATIBLE
    video_hw: bool = False


def _raw(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _bars(path: str) -> list[int]:
    """Where the moving bar of the test frames is, frame by frame in display order (it moves right)."""
    shown = []
    with av.open(path) as c:
        for frame in c.decode(video=0):
            row = frame.to_ndarray(format="rgb24")[frame.height // 2]
            shown.append((frame.pts, int(np.flatnonzero(row.min(axis=1) > 240)[0])))
    return [x for _, x in sorted(shown)]


# ------------------------------------------------------------ audio FFmpeg cannot decode


def test_audio_track_without_a_decoder_is_left_out(tmp_path):
    """An iPhone's spatial audio track ('apac') next to its stereo AAC one: FFmpeg has no decoder for it, so it
    cannot be copied. The video is still processed (copied, re-encoded or with a part removed), without it."""
    src = make_video(str(tmp_path / "IMG_0016.MOV"), fmt="mov", frames_n=48, audio_tracks=2)
    set_sample_entry(src, 2, b"apac")
    info = probe(src)
    assert (info.acodec, info.audio_streams, info.unsupported_audio) == ("aac", 1, 1)
    for name, opts, edit in (("strip.mov", Opts(), None), ("cut.mov", Opts(), Edit(cuts=((0.5, 1.0),))),
                             ("convert.mp4", Opts(convert_video=True), None)):
        dst = str(tmp_path / name)
        result = process_video(src, dst, opts, edit, info=info)
        assert t("video.audio_unsupported", n=1) in result.notes
        with av.open(dst) as c:
            assert [s.codec_context.name for s in c.streams.audio] == ["aac"]
    first = make_video(str(tmp_path / "IMG_0017.MOV"), fmt="mov", frames_n=24, audio_tracks=2)
    set_sample_entry(first, 1, b"apac")  # the undecodable track listed first
    info = probe(first)
    assert (info.acodec, info.audio_streams, info.unsupported_audio) == ("aac", 1, 1)


# ------------------------------------------------------------ lossless trims


@pytest.mark.parametrize("name,kw,start", [
    ("alli.mp4", {"gop": 1}, 0.51),  # every frame a keyframe
    ("MVI_0001.MOV", {"fmt": "mov", "vcodec": "mjpeg", "pix_fmt": "yuvj420p"}, 0.77),
    ("gop.mp4", {"gop": 12}, 0.49),  # the keyframe at 0.5 s is the first kept frame
])
def test_lossless_trim_starting_just_before_a_keyframe(tmp_path, name, kw, start):
    """Copying starts at the first kept frame when that is a keyframe: starting at the one before would copy a
    whole group of frames that players, and FFmpeg reading the file back, leave out (and the result was
    rejected: the packets did not add up)."""
    src = make_video(str(tmp_path / name), frames_n=48, fps=24, **kw)
    result = process_video(src, str(tmp_path / ("out" + os.path.splitext(name)[1])), Opts(),
                           Edit(trim=(start, None)))
    assert result.method == "lossless"
    assert not any("gizli kaldı" in n for n in result.notes)  # nothing hidden before the start


@pytest.mark.parametrize("edit", [
    Edit(trim=(0.52, 2.134)),  # copied: an audio packet starts in the last fraction of a millisecond
    Edit(trim=(0.52, 2.155)),
    Edit(trim=(0.52, 2.219), mirror=True),  # re-encoded: the last AAC packet began past the edit list's end
])
def test_trim_end_between_milliseconds(tmp_path, edit):
    """The edit list ends on the header's millisecond grid; nothing that starts after it is written, so FFmpeg
    reads back every packet written (it leaves out packets past the end of the edit list)."""
    src = make_video(str(tmp_path / "a.mp4"), frames_n=120, fps=30, gop=15)
    dst = str(tmp_path / "out.mp4")
    process_video(src, dst, Opts(), edit)
    assert probe(dst).duration == pytest.approx(edit.trim[1] - 16 / 30, abs=0.05)  # from frame 16 (0.533 s)


# ------------------------------------------------------------ camera data that has to go


def test_camera_exif_in_the_avi_stream_header_is_removed(tmp_path):
    """Camera AVIs (MJPEG) keep EXIF in the stream header ('strd'); FFmpeg passes it on as the track's codec
    configuration, which a copy wrote back into its own header."""
    src = make_video(str(tmp_path / "DSCF0001.AVI"), fmt="avi", vcodec="mjpeg", pix_fmt="yuvj420p", audio=None,
                     frames_n=12)
    add_strd(src, avif_exif({0x010F: "FUJIFILM", 0x0110: "FinePix F31fd", 0x0132: "2007:08:15 10:20:30"}))
    info = probe(src)
    assert t("probe.sei.stream_header") in [f.detail for f in scan_result(info).findings]
    dst = str(tmp_path / "out.avi")
    assert process_video(src, dst, Opts(), info=info).method == "lossless"
    raw = _raw(dst)
    assert b"FUJIFILM" not in raw and b"FinePix" not in raw and b"2007:08:15" not in raw
    assert len(_frames(dst)) == 12
    # Should it survive, the verification finds it (a Casio header: FFmpeg shows nothing of it as tags).
    casio = make_video(str(tmp_path / "CIMG0001.AVI"), fmt="avi", vcodec="mjpeg", pix_fmt="yuvj420p", audio=None,
                       frames_n=6)
    add_strd(casio, b"CASI" + bytes(4) + b"EX-Z750 2007:08:15")
    job = SimpleNamespace(created=None, plan=SimpleNamespace(out_fmt=Fmt.AVI), dst=casio)
    with av.open(casio) as c, pytest.raises(VideoError, match=t("video.sig.exif")):
        process._verify_clean(c, job)


def test_dv_recording_date_is_removed(tmp_path, monkeypatch):
    """DV camcorders write the recording date and time into every frame: stripped, the frames keep their
    picture and sound but no date; should one survive, the verification finds it."""
    src = make_dv(str(tmp_path / "tape.avi"))
    info = probe(src)
    assert t("probe.sei.dv_date") in info.sei
    dst = str(tmp_path / "out.avi")
    assert process_video(src, dst, Opts(), info=info).method == "lossless"
    with av.open(src) as a, av.open(dst) as b:
        before = [bytes(p) for p in a.demux(video=0) if p.size]
        after = [bytes(p) for p in b.demux(video=0) if p.size]
    assert all(bitstream.dv_has_dates(p) for p in before) and not any(bitstream.dv_has_dates(p) for p in after)
    assert [len(p) for p in after] == [len(p) for p in before]
    assert all(np.array_equal(x, y) for x, y in zip(_frames(src), _frames(dst), strict=True))
    monkeypatch.setattr(bitstream, "strip_dv", lambda data: None)
    with pytest.raises(VideoError, match=t("video.sig.dv_date")):
        process_video(src, str(tmp_path / "kept.avi"), Opts(), info=info)


def _frames(path: str) -> list[np.ndarray]:
    with av.open(path) as c:
        return [f.to_ndarray(format="rgb24") for f in c.decode(video=0)]


# ------------------------------------------------------------ a header that cannot be written


def test_output_is_not_left_locked_after_a_failed_header(tmp_path, monkeypatch):
    """If FFmpeg cannot write the header (here: a timecode track forced into an MP4), the job is redone without
    the extra tracks; the first attempt's file must not stay open, or the runner cannot move the result."""
    monkeypatch.setattr(process._Job, "_can_keep", lambda self, s: s.type == "data")
    src = make_video(str(tmp_path / "GX010001.MP4"), frames_n=24, timecode="10:20:30:00")
    dst = str(tmp_path / "out.tmp")
    process_video(src, dst, Opts(strip_metadata=False), Edit(quarter=1))
    os.replace(dst, str(tmp_path / "moved.mp4"))  # PermissionError (WinError 32) while a handle is open


def test_mp4_keeps_gopro_telemetry_but_not_a_timecode_track(tmp_path):
    """With metadata kept, a GoPro MP4's telemetry ('gpmd') fits in the new MP4, its timecode track does not:
    only that one is left out (the header failed and every extra track went)."""
    src = make_video(str(tmp_path / "GX010002.MP4"), frames_n=24, gpmd=True, timecode="10:20:30:00")
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts(strip_metadata=False), Edit(quarter=1))
    assert result.method == "lossless" and any("veri izi" in n for n in result.notes)
    info = probe(dst)
    assert info.data_tracks == [t("boxes.timed.gopro")] and info.has_gps


@pytest.mark.parametrize("entry", [b"gpmd", b"djmd", b"rtmd", b"camm"])
def test_location_tracks_exposed_by_ffmpeg_are_recognized(tmp_path, entry):
    """GoPro, DJI, Sony and camera-motion tracks log the location; FFmpeg shows them as data streams without
    their sample entry, so the boxes tell which they are."""
    src = make_video(str(tmp_path / "a.mp4"), frames_n=12, gpmd=True)
    set_sample_entry(src, 2, entry)
    info = probe(src)
    assert info.has_gps and info.location_tracks and info.data_tracks == info.location_tracks


# ------------------------------------------------------------ orientation, timing, pixels


def test_mirrored_display_matrix(tmp_path):
    """QuickTime's "Flip Horizontal" mirrors a video by its matrix: it is not a half turn. A copy keeps the
    matrix; a re-encode, the thumbnail and the preview flip the picture."""
    src = make_video(str(tmp_path / "flip.mov"), fmt="mov", frames_n=24)
    set_matrix(src, HFLIP)
    info = probe(src)
    assert (info.rotation, info.mirrored) == (0, True)
    dst = str(tmp_path / "out.mov")
    assert process_video(src, dst, Opts(), info=info).method == "lossless"
    track = read_structure(dst).video
    assert (track.rotation, track.mirrored) == (0, True)
    top = str(tmp_path / "top.mov")
    process_video(src, top, Opts(), Edit(crop=(0.0, 0.0, 1.0, 0.5)), info=info)  # the top half, as shown
    for im in (upright_frame(top), poster(src)):
        assert (dominant(marker_color(im, "tl")), dominant(marker_color(im, "tr"))) == ("green", "red")


@pytest.mark.parametrize("vcodec", ["libx264", "mpeg2video"])  # copied / re-encoded
def test_mpegts_converted_to_mp4_starts_at_zero(tmp_path, vcodec):
    """An MPEG-TS clock starting at 10 s became an empty edit list entry: 10 s of nothing before the first
    frame in players."""
    src = make_video(str(tmp_path / "00001.MTS"), fmt="mpegts", vcodec=vcodec, frames_n=24,
                     options={"output_ts_offset": "10"})
    dst = str(tmp_path / "out.mp4")
    process_video(src, dst, Opts(convert_video=True))
    with av.open(dst) as c:
        assert (c.start_time or 0) / 1_000_000 < 0.1
        assert c.duration / 1_000_000 == pytest.approx(1.0, abs=0.1)


def test_reencoding_keeps_non_square_pixels(tmp_path):
    """HDV/AVCHD 1440×1080 (and DV) have wide pixels; re-encoded, they stay so (turned with a quarter turn)."""
    src = make_video(str(tmp_path / "a.mp4"), frames_n=12, sar=Fraction(4, 3))
    for edit, sar in ((Edit(mirror=True), Fraction(4, 3)), (Edit(quarter=1, mirror=True), Fraction(3, 4))):
        dst = str(tmp_path / f"out{edit.quarter}.mp4")
        assert process_video(src, dst, Opts(), edit).method == "reencoded"
        with av.open(dst) as c:
            assert c.streams.video[0].sample_aspect_ratio == sar


@pytest.mark.parametrize("name,opts", [("out.mkv", Opts()), ("out.mp4", Opts(convert_video=True))])
def test_mkv_with_b_frames_stays_in_order(tmp_path, name, opts):
    """The first packets of an MKV with B-frames have no decoding time; given their presentation time instead,
    the second frame was shown after the third."""
    src = make_video(str(tmp_path / "obs.mkv"), fmt="matroska", frames_n=24, fps=24, gop=12)
    dst = str(tmp_path / name)
    process_video(src, dst, opts)
    assert _bars(dst) == _bars(src) == sorted(_bars(src))


def test_late_trim_of_an_mkv_decodes_from_its_keyframe(tmp_path, monkeypatch):
    """Without an index to jump with, an MKV is read from the beginning, but only the packets from the keyframe
    before the start on are decoded."""
    src = make_video(str(tmp_path / "a.mkv"), fmt="matroska", frames_n=96, fps=24, gop=12)  # 4 s
    decoded = []
    frame_of = process._VideoEncoder._frame

    def counted(self, frame):
        decoded.append(frame.pts)
        frame_of(self, frame)

    monkeypatch.setattr(process._VideoEncoder, "_frame", counted)
    dst = str(tmp_path / "out.mp4")
    process_video(src, dst, Opts(convert_video=True, video_codec=VIDEO_KEEP), Edit(trim=(3.0, 3.5), mirror=True))
    assert len(decoded) < 30  # from the keyframe at 3 s on (not the 72 frames before it)
    assert len(_frames(dst)) == 12
