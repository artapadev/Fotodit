"""Video: format detection, info reading, metadata stripping, conversion to MP4, rotation/cropping."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction

import av
import numpy as np
import pytest
from builders import dominant, marker_color
from video_builders import (
    ANDROID_TAGS,
    APPLE_TAGS,
    make_video,
    mdpm_payload,
    sei_nal,
    upright_frame,
)

from fotodit.core.edits import Edit
from fotodit.core.formats import (
    EXTENSIONS,
    VIDEO_EXTENSIONS,
    Fmt,
    is_video_name,
    sniff_file,
)
from fotodit.core.video import bitstream, hwenc, tonemap
from fotodit.core.video.boxes import read_structure
from fotodit.core.video.probe import parse_date, parse_iso6709, probe
from fotodit.core.video.process import (
    VIDEO_COMPATIBLE,
    VIDEO_EDIT_NEEDS_MP4,
    VIDEO_KEEP,
    VideoCancelled,
    VideoError,
    _Window,
    edit_reason,
    geometry,
    plan_video,
    process_video,
)
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


def _frames(path: str, n: int = 3) -> list[np.ndarray]:
    out = []
    with av.open(path) as c:
        for frame in c.decode(video=0):
            out.append(frame.to_ndarray(format="rgb24"))
            if len(out) >= n:
                break
    return out


def _corners(im) -> dict[str, str]:
    return {c: dominant(marker_color(im, c)) for c in ("tl", "tr", "bl")}


# -------------------------------------------------------------- format detection


@pytest.mark.parametrize("name,fmt,kw", [
    ("a.mp4", Fmt.MP4, {}),
    ("a.mov", Fmt.MOV, {"fmt": "mov"}),
    ("a.mkv", Fmt.MKV, {"fmt": "matroska"}),
    ("a.webm", Fmt.WEBM, {"fmt": "webm", "vcodec": "libvpx-vp9", "audio": "libopus"}),
    ("a.avi", Fmt.AVI, {"fmt": "avi", "vcodec": "mjpeg", "pix_fmt": "yuvj420p", "audio": None}),
    ("a.mts", Fmt.MTS, {"fmt": "mpegts", "audio": "ac3"}),
    ("a.wmv", Fmt.WMV, {"fmt": "asf", "vcodec": "wmv2", "audio": "wmav2"}),
    ("a.3gp", Fmt.THREEGP, {"fmt": "3gp", "vcodec": "mpeg4", "audio": "libopencore_amrnb", "size": (176, 144)}),
])
def test_sniff_video_containers(tmp_path, name, fmt, kw):
    path = make_video(str(tmp_path / name), frames_n=6, **kw)
    assert sniff_file(path) == fmt
    assert probe(path).fmt == fmt


def test_video_extensions():
    assert is_video_name("IMG_0001.MOV") and is_video_name("00001.MTS") and is_video_name("clip.m2ts")
    assert ".ts" not in VIDEO_EXTENSIONS  # must not be confused with TypeScript files
    assert all(ext in EXTENSIONS for ext in VIDEO_EXTENSIONS)


# ------------------------------------------------------------ bitstream / SEI


def test_sei_scrub_keeps_other_messages():
    user = bitstream.MDPM_UUID + b"MDPM" + b"\x00\x00\x03\x00\x00\x01" * 5
    recovery = b"\x80"  # recovery point (type 6)
    nal = bitstream.build_sei(b"\x06", [(6, recovery), (5, user), (1, b"\x12\x34")])
    assert bitstream.unescape(nal) != nal  # emulation prevention bytes were inserted
    scrubber = bitstream.SeiScrubber("h264", None, collect=True)
    out = scrubber.scrub_nals([nal, b"\x65\x88\x84"])
    assert out is not None and out[1] == b"\x65\x88\x84"
    msgs = bitstream.sei_messages(bitstream.unescape(out[0]), 1)
    assert [t for t, _ in msgs] == [6, 1]
    assert scrubber.removed == 1 and scrubber.mdpm == [user]
    # An SEI that carries only user data is dropped entirely.
    only = bitstream.build_sei(b"\x06", [(5, user)])
    assert bitstream.SeiScrubber("h264", None).scrub_nals([only, b"\x65\x00"]) == [b"\x65\x00"]


def test_escape_roundtrip():
    rbsp = bytes([0, 0, 0, 0, 0, 1, 0, 0, 2, 0, 0, 3, 7, 0, 0])
    assert bitstream.unescape(bitstream.escape(rbsp)) == rbsp


def test_mdpm_fields():
    tags = bitstream.mdpm_tags(mdpm_payload())
    when, offset = bitstream.mdpm_datetime(tags)
    assert when == datetime(2019, 8, 3, 14, 5, 9) and offset == timedelta(hours=3)
    lat, lon = bitstream.mdpm_gps(tags)
    assert lat == pytest.approx(41.0082, abs=1e-4) and lon == pytest.approx(28.9784, abs=1e-4)
    assert bitstream.mdpm_camera(tags) == "Sony"
    assert not bitstream.mdpm_has_gps(bitstream.mdpm_tags(mdpm_payload(gps=False)))


def test_mjpeg_frame_scrub():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, "JPEG", exif=b"Exif\x00\x00MM\x00*\x00\x00\x00\x08\x00\x00")
    data = buf.getvalue()
    assert b"Exif" in data
    clean = bitstream.strip_mjpeg(data)
    assert clean is not None and b"Exif" not in clean
    assert Image.open(io.BytesIO(clean)).getpixel((5, 5)) == Image.open(io.BytesIO(data)).getpixel((5, 5))


def test_parsers():
    assert parse_iso6709("+41.0082+028.9784+050.000/") == pytest.approx((41.0082, 28.9784))
    assert parse_iso6709("-33.8688+151.2093/") == pytest.approx((-33.8688, 151.2093))
    assert parse_date("2024-07-12T18:30:10+0300") == (datetime(2024, 7, 12, 18, 30, 10), timedelta(hours=3))
    assert parse_date("2024-07-12T15:30:10.000000Z")[1] == timedelta(0)
    assert parse_date("abc") is None


# --------------------------------------------------------------------- info


def test_probe_iphone(tmp_path):
    path = make_video(str(tmp_path / "IMG_0001.MOV"), fmt="mov", tags=APPLE_TAGS, apple_keys=True, rotation=90)
    info = probe(path)
    assert info.fmt == Fmt.MOV and info.rotation == 90 and info.display_size == (240, 320)
    assert info.taken == datetime(2024, 7, 12, 18, 30, 10)  # local time, written with its time zone
    assert info.camera == "Apple iPhone 15 Pro"
    assert info.gps == pytest.approx((41.0082, 28.9784)) and info.has_gps
    assert info.duration == pytest.approx(1.0, abs=0.1) and info.vcodec == "h264" and info.acodec == "aac"


def test_probe_android_utc(tmp_path):
    named = make_video(str(tmp_path / "VID_20240712_183010.mp4"), tags=ANDROID_TAGS)
    info = probe(named)
    assert info.taken == datetime(2024, 7, 12, 18, 30, 10)  # UTC + 3 h: the local time in the file name
    assert "dosya adı" in info.taken_note and info.has_gps
    plain = make_video(str(tmp_path / "clip.mp4"), tags=ANDROID_TAGS)
    local = datetime(2024, 7, 12, 15, 30, 10).replace(tzinfo=__import__("datetime").timezone.utc).astimezone()
    assert probe(plain).taken == local.replace(tzinfo=None)


def test_probe_camera_local_time(tmp_path):
    path = make_video(str(tmp_path / "GX010001.MP4"), tags={"creation_time": "2024-07-12T18:30:10Z",
                                                            "make": "GoPro", "model": "HERO12"})
    info = probe(path)
    # The MP4 muxer does not write make/model; recognizing a GoPro by its track (handler) name has no
    # separate test: only the rule is exercised here.
    from fotodit.core.video.probe import _date

    info.camera = "GoPro HERO12"
    assert _date(path, info)[0] == datetime(2024, 7, 12, 18, 30, 10)


def test_probe_mdpm_and_mkv_rotation(tmp_path):
    mts = make_video(str(tmp_path / "00001.MTS"), fmt="mpegts", audio="ac3", sei=[mdpm_payload()])
    info = probe(mts)
    assert info.taken == datetime(2019, 8, 3, 14, 5, 9) and info.camera == "Sony"
    assert info.has_gps and info.gps == pytest.approx((41.0082, 28.9784), abs=1e-4)
    assert any("MDPM" in s for s in info.sei)
    mkv = make_video(str(tmp_path / "portrait.mkv"), fmt="matroska", rotation=270)
    assert probe(mkv).rotation == 270


def test_probe_hdr_and_tracks(tmp_path):
    path = make_video(str(tmp_path / "GX010001.MP4"), pix_fmt="yuv420p10le", hlg=True, gpmd=True,
                      subtitle="41.0082N 28.9784E")
    info = probe(path)
    assert info.hdr == "HLG" and info.bit_depth == 10
    assert any("GoPro" in t for t in info.data_tracks) and len(info.subtitle_tracks) == 1 and info.has_gps


def test_probe_rejects_broken(tmp_path):
    path = make_video(str(tmp_path / "a.mp4"))
    data = _raw(path)
    broken = tmp_path / "truncated.mp4"
    broken.write_bytes(data[:len(data) // 2])  # 'moov' is at the end, so the truncated file lacks it
    with pytest.raises(VideoError):
        process_video(str(broken), str(tmp_path / "out.mp4"), Opts())


# -------------------------------------------------------- metadata stripping


def test_strip_removes_everything_losslessly(tmp_path):
    src = make_video(str(tmp_path / "IMG_0002.MOV"), fmt="mov", tags=APPLE_TAGS, apple_keys=True, rotation=90,
                     subtitle="41.0082N 28.9784E", chapters=True, sei=[mdpm_payload()])
    dst = str(tmp_path / "out.mov")
    result = process_video(src, dst, Opts())
    assert result.method == "lossless" and result.out_fmt == Fmt.MOV
    assert any("altyazı" in n for n in result.notes)
    info = probe(dst)
    assert set(info.tags) <= {"major_brand", "minor_version", "compatible_brands"}
    assert not info.has_gps and not info.data_tracks and not info.subtitle_tracks and not info.chapters
    assert info.sei == [] and info.taken is None and info.camera is None
    assert info.rotation == 90  # orientation is kept (the video does not turn sideways)
    st = read_structure(dst)
    assert not st.keys and not st.items and st.created == 0 and all(t.created == 0 for t in st.tracks)
    raw = _raw(dst)
    for secret in (b"com.apple.quicktime", b"41.0082", b"x264 - core", bitstream.MDPM_MAGIC, b"iPhone"):
        assert secret not in raw
    # The video was not re-encoded: the frames are identical.
    for a, b in zip(_frames(src), _frames(dst), strict=True):
        assert np.array_equal(a, b)


@pytest.mark.parametrize("fmt,name,kw", [
    ("matroska", "a.mkv", {}),
    ("mpegts", "a.mts", {"audio": "ac3", "sei": None}),
    ("avi", "a.avi", {"vcodec": "mjpeg", "pix_fmt": "yuvj420p", "audio": "pcm_s16le"}),
])
def test_strip_other_containers(tmp_path, fmt, name, kw):
    kw = dict(kw)
    sei = kw.pop("sei", [mdpm_payload()]) if fmt == "mpegts" else []
    if fmt == "mpegts":
        sei = [mdpm_payload()]
    # The person's name in the "comment" tag is non-ASCII on purpose (its UTF-8 bytes are checked below).
    src = make_video(str(tmp_path / name), fmt=fmt, tags={"title": "Holiday", "comment": "Ayşe's camera"},
                     sei=sei, **kw)
    dst = str(tmp_path / ("out" + os.path.splitext(name)[1]))
    process_video(src, dst, Opts())
    info = probe(dst)
    assert info.fmt == probe(src).fmt
    assert "title" not in info.tags and "comment" not in info.tags and not info.sei
    assert b"Ay\xc5\x9fe" not in _raw(dst) and bitstream.MDPM_MAGIC not in _raw(dst)


def test_strip_removes_gopro_telemetry(tmp_path):
    src = make_video(str(tmp_path / "GX010002.MP4"), gpmd=True, tags={"location": "+41.0082+028.9784/"})
    assert probe(src).has_gps
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts())
    assert any("veri izi" in n for n in result.notes)
    info = probe(dst)
    assert not info.data_tracks and not info.has_gps and b"GPS5" not in _raw(dst)


# ------------------------------------------------------------------- subtitles

# Turkish movie cues on purpose: non-ASCII letters and ASS styling must survive the round trip.
MOVIE_CUES = [(0, 300, "Merhaba dünya"), (400, 700, "{\\i1}İyi{\\i0} seyirler"), (800, 950, "Son satır")]
DJI_CUES = [(i * 300, i * 300 + 290, f"F/2.8, SS 500, ISO 100, EV 0, DZOOM 1.000, GPS (28.9784, 41.0082, 19), "
             f"D {12.3 + i:.2f}m, H 5.67m") for i in range(3)]


def test_subtitle_telemetry_detection():
    from fotodit.core.video.subtitles import plain_text, telemetry

    assert telemetry(["Hello", "Turn off the GPS!", "Where are we going?", "I don't know"]) == (None, False)
    assert telemetry([t for _, _, t in DJI_CUES]) == ("konum ve kamera ayarları kaydı", True)
    new_dji = "[iso : 100] [shutter : 1/500.0] [fnum : 280] [latitude: 41.008200] [longitude: 28.978400]"
    assert telemetry([new_dji] * 5)[1]
    assert telemetry(["41.0082N 28.9784E"]) == ("konum kaydı", True)
    dashcam = [f"2024/05/12 14:30:{s:02d}  {50 + s} km/h" for s in range(6)]
    assert telemetry(dashcam) == ("tarih/saat ve hız kaydı", False)
    assert telemetry(["Birthday 2024-05-12"]) == (None, False)  # a single line is not a log
    # ASS event line with styling and UTF-8 non-ASCII text (intentional).
    assert plain_text(b"3,0,Default,,0,0,0,,{\\i1}e\xc4\x9fik{\\i0}\\Nyaz\xc4\xb1") == "eğik\nyazı"


def test_strip_keeps_real_subtitles(tmp_path):
    from video_builders import subtitle_lines

    src = make_video(str(tmp_path / "film.mp4"), tags=ANDROID_TAGS, subtitle=MOVIE_CUES, subtitle_language="tur")
    info = probe(src)
    assert len(info.subtitle_tracks) == 1 and info.subtitle_tracks[0].telemetry is None
    assert info.has_gps and not any("ltyazı" in s for s in info.gps_sources)  # movie subtitles are not a GPS source
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts())
    assert result.method == "lossless" and not any("altyazı" in n for n in result.notes)
    assert subtitle_lines(dst) == [("mov_text", "tur", ["Merhaba dünya", "İyi seyirler", "Son satır"])]
    out = probe(dst)
    assert not out.has_gps and not out.tags.keys() - {"major_brand", "minor_version", "compatible_brands"}


def test_strip_removes_telemetry_subtitles(tmp_path):
    src = make_video(str(tmp_path / "DJI_0001.MP4"), subtitle=DJI_CUES,
                     extra_subtitles=((MOVIE_CUES, "mov_text", "eng"),))
    info = probe(src)
    assert info.has_gps and "Altyazı izinde konum kaydı" in info.gps_sources
    assert [t.telemetry for t in info.subtitle_tracks] == ["konum ve kamera ayarları kaydı", None]
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts())
    assert any("altyazı izindeki konum ve kamera ayarları kaydı" in n for n in result.notes)
    from video_builders import subtitle_lines

    assert [(codec, lang) for codec, lang, _ in subtitle_lines(dst)] == [("mov_text", "eng")]
    assert b"28.9784" not in _raw(dst) and not probe(dst).has_gps


@pytest.mark.parametrize("strip", [True, False])
def test_mkv_subtitles_become_mp4_subtitles(tmp_path, strip):
    from video_builders import subtitle_lines

    src = make_video(str(tmp_path / "series.mkv"), fmt="matroska", subtitle=MOVIE_CUES, subtitle_codec="subrip",
                     subtitle_language="tur", extra_subtitles=((MOVIE_CUES[:2], "ass", "eng"),))
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts(strip_metadata=strip, convert_video=True))
    assert result.out_fmt == Fmt.MP4 and not any("altyazı" in n for n in result.notes)
    assert subtitle_lines(dst) == [
        ("mov_text", "tur", ["Merhaba dünya", "İyi seyirler", "Son satır"]),
        ("mov_text", "eng", ["Merhaba dünya", "İyi seyirler"]),
    ]


def test_keep_metadata_when_not_stripping(tmp_path):
    src = make_video(str(tmp_path / "IMG_0003.MP4"), tags=APPLE_TAGS, apple_keys=True, gpmd=True,
                     subtitle="a subtitle")
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts(strip_metadata=False, convert_video=True, video_codec=VIDEO_KEEP))
    assert result.out_fmt == Fmt.MP4 and result.method == "lossless"
    info = probe(dst)
    assert info.camera == "Apple iPhone 15 Pro" and info.has_gps
    assert info.taken == datetime(2024, 7, 12, 18, 30, 10)
    assert len(info.subtitle_tracks) == 1 and info.data_tracks


# -------------------------------------------------------------- conversion


def test_convert_hevc_to_h264_upright(tmp_path):
    src = make_video(str(tmp_path / "IMG_0004.MOV"), fmt="mov", vcodec="libx265", rotation=90, frames_n=12)
    plan = plan_video(probe(src), Opts(convert_video=True))
    assert plan.encode_video and "HEVC → H.264" in plan.reasons[0]
    dst = str(tmp_path / "out.mp4")
    process_video(src, dst, Opts(convert_video=True))
    info = probe(dst)
    assert (info.fmt, info.vcodec, info.acodec) == (Fmt.MP4, "h264", "aac")
    assert info.rotation == 0 and info.display_size == (240, 320)  # orientation applied to the pixels
    assert _corners(upright_frame(dst)) == _corners(upright_frame(src))


def test_convert_keep_copies_hevc_with_hvc1(tmp_path):
    src = make_video(str(tmp_path / "a.mkv"), fmt="matroska", vcodec="libx265", frames_n=8)
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts(convert_video=True, video_codec=VIDEO_KEEP))
    assert result.method == "lossless"
    assert read_structure(dst).video.entry == b"hvc1"  # so it opens on Apple devices too


@pytest.mark.parametrize("kw,codecs", [
    ({"fmt": "avi", "vcodec": "mjpeg", "pix_fmt": "yuvj420p", "audio": "pcm_s16le"}, ("h264", "aac")),
    ({"fmt": "asf", "vcodec": "wmv2", "audio": "wmav2"}, ("h264", "aac")),
    ({"fmt": "webm", "vcodec": "libvpx-vp9", "audio": "libopus"}, ("h264", "aac")),
    ({"fmt": "3gp", "vcodec": "mpeg4", "audio": "libopencore_amrnb", "size": (176, 144)}, ("h264", "aac")),
])
def test_convert_legacy_formats(tmp_path, kw, codecs):
    src = make_video(str(tmp_path / "src.bin"), frames_n=10, **kw)
    dst = str(tmp_path / "out.mp4")
    process_video(src, dst, Opts(convert_video=True))
    info = probe(dst)
    assert (info.vcodec, info.acodec) == codecs and info.fmt == Fmt.MP4
    assert _corners(upright_frame(dst)) == {"tl": "red", "tr": "green", "bl": "blue"}


def test_hdr_is_tone_mapped(tmp_path):
    ref_white = 0.75  # HLG reference white (203 cd/m²)
    level = int(round(ref_white * 255))
    path = str(tmp_path / "hdr.mov")
    out = av.open(path, "w", format="mov")
    v = out.add_stream("libx264", rate=24)
    v.width, v.height, v.pix_fmt = 64, 48, "yuv420p10le"
    v.codec_context.color_primaries, v.codec_context.color_trc, v.codec_context.colorspace = 9, 18, 9
    for _ in range(6):
        frame = av.VideoFrame.from_ndarray(np.full((48, 64, 3), level, np.uint8), format="rgb24")
        for p in v.encode(frame.reformat(format="yuv420p10le")):
            out.mux(p)
    for p in v.encode():
        out.mux(p)
    out.close()
    dst = str(tmp_path / "sdr.mp4")
    result = process_video(path, dst, Opts(convert_video=True))
    assert any("HDR" in n for n in result.notes)
    with av.open(dst) as c:
        cc = c.streams.video[0].codec_context
        assert (cc.color_primaries, cc.color_trc, cc.colorspace) == (1, 1, 1)
        frame = next(c.decode(video=0))
        value = frame.to_ndarray(format="rgb24").mean()
    assert value == pytest.approx(255 * 0.9606, abs=8)  # reference white becomes bright white in SDR, not dim


# ------------------------------------------------------------- GPU (hardware) encoder

X264_AS_GPU = hwenc.Encoder("libx264", "Test encoder", "h264", (("preset", "ultrafast"), ("crf", "20")), "yuv420p")


@pytest.fixture
def fresh_hwenc():
    hwenc.reset()
    yield hwenc
    hwenc.reset()


def test_gpu_encoder_failure_falls_back_to_cpu(tmp_path, monkeypatch, fresh_hwenc):
    broken = hwenc.Encoder("h264_no_such_encoder", "Nonexistent GPU", "h264")
    monkeypatch.setattr(hwenc, "find", lambda codec="h264": broken if codec == "h264" else None)
    src = make_video(str(tmp_path / "a.mov"), fmt="mov", vcodec="libx265", frames_n=8)
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts(convert_video=True, video_hw=True))
    assert any("Nonexistent GPU" in n and "işlemciyle kodlandı" in n for n in result.notes)
    info = probe(dst)
    assert info.vcodec == "h264" and _frames(dst, 1)


def test_gpu_encoder_bad_output_is_rejected(tmp_path, monkeypatch, fresh_hwenc):
    from fotodit.core.video import process

    monkeypatch.setattr(hwenc, "find", lambda codec="h264": X264_AS_GPU if codec == "h264" else None)
    check = process._check_frame

    def corrupt_on_gpu(job, frame, first):  # simulates a frame corrupted by a driver bug
        if first and job.plan.encoder is not None:
            # Same user-facing (Turkish) message as the app's own check; it ends up in the note.
            raise process._BadOutput("Kodlanan görüntü bozuk çıktı.")
        check(job, frame, first)

    monkeypatch.setattr(process, "_check_frame", corrupt_on_gpu)
    src = make_video(str(tmp_path / "a.mov"), fmt="mov", vcodec="libx265", frames_n=8)
    result = process_video(src, str(tmp_path / "out.mp4"), Opts(convert_video=True, video_hw=True))
    assert any("bozuk" in n and "işlemciyle kodlandı" in n for n in result.notes)
    assert "libx264" in hwenc._broken  # no longer used in this session


def test_encoder_output_similarity_check(tmp_path):
    """The first frame fed to the encoder is compared with the first frame in the file (against broken encoders)."""
    from fotodit.core.video import process

    src = make_video(str(tmp_path / "a.mov"), fmt="mov", vcodec="libx265", frames_n=8)
    job = process._Job(src, str(tmp_path / "out.mp4"), probe(src),
                       plan_video(probe(src), Opts(convert_video=True)), None, None)
    job.run()
    assert job.reference is not None
    process._verify(job)  # a correct encode passes
    job.reference = 255 - job.reference  # negative image: must count as corrupted
    with pytest.raises(process._BadOutput):
        process._verify(job)


def test_gpu_hevc_keeps_hdr(tmp_path, monkeypatch, fresh_hwenc):
    """With a 10-bit HEVC GPU encoder, an HDR video cropped under the "keep codec" option stays HDR.
    (This test uses the CPU's x265 in place of the GPU encoder.)"""
    x265 = hwenc.Encoder("libx265", "HEVC test", "hevc", (("preset", "ultrafast"), ("crf", "24")), "yuv420p10le")
    monkeypatch.setattr(hwenc, "find", lambda codec="h264": x265 if codec == "hevc" else None)
    src = make_video(str(tmp_path / "IMG_0001.MOV"), fmt="mov", vcodec="libx265", pix_fmt="yuv420p10le", hlg=True,
                     frames_n=8)
    edit = Edit(crop=(0.1, 0.1, 0.8, 0.8))
    plan = plan_video(probe(src), Opts(convert_video=True, video_codec=VIDEO_KEEP, video_hw=True), edit, detect=True)
    assert plan.encode_video and plan.codec == "hevc" and plan.tonemap is None
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts(convert_video=True, video_codec=VIDEO_KEEP, video_hw=True), edit)
    assert not any("SDR" in n for n in result.notes) and any("HEVC test" in n for n in result.notes)
    info = probe(dst)
    assert info.vcodec == "hevc" and info.hdr == "HLG" and info.bit_depth == 10
    assert b"hvc1" in _raw(dst) and b"x265 (build" not in _raw(dst)  # the encoder's settings string (SEI) is gone


@pytest.mark.skipif(hwenc.find("h264") is None, reason="no GPU encoder on this computer")
def test_real_gpu_encoder(tmp_path):
    encoder = hwenc.find("h264")
    src = make_video(str(tmp_path / "a.mov"), fmt="mov", vcodec="libx265", size=(640, 360), frames_n=30)
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts(convert_video=True, video_hw=True))
    assert any(encoder.label in n and "kodlandı" in n for n in result.notes)
    assert not any("işlemciyle" in n for n in result.notes)
    assert probe(dst).vcodec == "h264" and len(_frames(dst, 30)) == 30


def test_tonemap_curve():
    grey = np.array([[0.0, 0.0, 0.0], [0.5, 0.5, 0.5], [0.75, 0.75, 0.75], [1.0, 1.0, 1.0]])
    out = tonemap.tonemap(grey, tonemap.HLG)[:, 0]
    assert out[0] == 0 and np.all(np.diff(out) > 0) and out[-1] <= 1.0
    assert out[2] == pytest.approx(0.9606, abs=0.005)
    assert tonemap.tonemap(np.array([[0.58, 0.58, 0.58]]), tonemap.PQ)[0, 0] == pytest.approx(0.9606, abs=0.02)


# ---------------------------------------------------------- rotation/cropping


def test_quarter_rotation_is_lossless(tmp_path):
    src = make_video(str(tmp_path / "a.mp4"))
    dst = str(tmp_path / "out.mp4")
    edit = Edit(quarter=1)
    assert not plan_video(probe(src), Opts(), edit).encode_video
    result = process_video(src, dst, Opts(), edit)
    assert result.method == "lossless"
    assert probe(dst).rotation == 90
    # 90° clockwise: the red top-left corner moves to the top right, the blue bottom-left one to the top left.
    assert _corners(upright_frame(dst)) == {"tl": "blue", "tr": "red", "bl": "other"}
    for a, b in zip(_frames(src), _frames(dst), strict=True):
        assert np.array_equal(a, b)


def test_rotation_adds_to_existing_orientation(tmp_path):
    src = make_video(str(tmp_path / "portrait.mov"), fmt="mov", rotation=90)
    dst = str(tmp_path / "out.mov")
    process_video(src, dst, Opts(strip_metadata=False), Edit(quarter=3))  # rotate left: now upright
    assert probe(dst).rotation == 0


def test_crop_and_straighten_reencode(tmp_path):
    src = make_video(str(tmp_path / "a.mp4"))
    crop = Edit(crop=(0.5, 0.0, 1.0, 0.5))  # top-right quarter
    dst = str(tmp_path / "crop.mp4")
    plan = plan_video(probe(src), Opts(), crop)
    assert plan.encode_video and "kırpma" in plan.reasons[0]
    process_video(src, dst, Opts(), crop)
    info = probe(dst)
    assert info.display_size == (160, 120) and info.vcodec == "h264"
    im = upright_frame(dst)
    assert dominant(marker_color(im, "tr")) == "green"
    tilted = Edit(angle=4.0)
    dst2 = str(tmp_path / "tilt.mp4")
    process_video(src, dst2, Opts(), tilted)
    steps, size = geometry(probe(src), tilted)
    assert probe(dst2).display_size == size and size[0] % 2 == 0 and size[1] % 2 == 0
    corner = np.asarray(upright_frame(dst2))[:4, :4].mean()
    assert corner > 20  # no black gaps in the corners (the box shrank with the tilt)


def test_edit_needs_mp4_for_other_containers():
    assert edit_reason(Fmt.MKV, Opts(), Edit(quarter=1)) == t(VIDEO_EDIT_NEEDS_MP4)
    assert edit_reason(Fmt.MKV, Opts(convert_video=True), Edit(quarter=1)) is None
    assert edit_reason(Fmt.MOV, Opts(), Edit(crop=(0, 0, 0.5, 0.5))) is None
    assert edit_reason(Fmt.AVI, Opts(), None) is None
    assert edit_reason(Fmt.AVI, Opts(), Edit(trim=(1, None))) == t(VIDEO_EDIT_NEEDS_MP4)


# ------------------------------------------------------------------ trimming

CUES = [(0, 900, "one"), (1000, 1900, "two"), (2000, 2900, "three"), (3000, 3900, "four")]


def _gray(path: str) -> list[np.ndarray]:
    with av.open(path) as c:
        return [f.to_ndarray(format="gray").astype(np.float32) for f in c.decode(video=0)]


def _source_frames(src: list[np.ndarray], out: list[np.ndarray], box=None) -> list[int]:
    """Which source frame each output frame is (the test frames differ by a moving bar)."""
    if box is not None:
        y0, y1, x0, x1 = box
        src = [s[y0:y1, x0:x1] for s in src]
    return [int(np.argmin([((o - s) ** 2).mean() for s in src])) for o in out]


def _audio_seconds(path: str) -> float:
    with av.open(path) as c:
        stream = c.streams.audio[0]
        return sum(f.samples for f in c.decode(stream)) / stream.sample_rate


@pytest.mark.parametrize("trim,first,last,texts", [
    ((1.30, 3.10), 32, 74, ["two", "three", "four"]),  # 1.30 s → the first frame at or after it is the 32nd
    ((2.5, None), 60, 95, ["three", "four"]),
    ((0, 1.0), 0, 23, ["one"]),
])
def test_trim_without_reencoding(tmp_path, trim, first, last, texts):
    """B-frames, a keyframe every 12 frames: the copy starts at the keyframe before the start, the edit list
    hides the frames before the start (and the references kept past the end): exactly the kept frames play."""
    from video_builders import subtitle_lines

    src = make_video(str(tmp_path / "a.mp4"), frames_n=96, fps=24, gop=12, subtitle=CUES)
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts(convert_video=True, video_codec=VIDEO_KEEP), Edit(trim=trim))
    assert result.method == "lossless" and any(n.startswith("Video kesildi") for n in result.notes)
    hidden = (first % 12) / 24  # from the keyframe before the start
    assert any("gizli kaldı" in n for n in result.notes) == (hidden >= 0.05)
    assert _source_frames(_gray(src), _gray(dst)) == list(range(first, last + 1))
    seconds = (last + 1 - first) / 24
    assert probe(dst).duration == pytest.approx(seconds, abs=0.05)
    assert _audio_seconds(dst) == pytest.approx(seconds, abs=0.05)
    assert subtitle_lines(dst)[0][2] == texts


def test_trim_late_start_reads_from_the_keyframe(tmp_path):
    src = make_video(str(tmp_path / "a.mp4"), frames_n=240, fps=24, gop=12)  # 10 s
    dst = str(tmp_path / "out.mp4")
    process_video(src, dst, Opts(), Edit(trim=(6.2, 8.0)))
    assert _source_frames(_gray(src), _gray(dst)) == list(range(149, 192))


def test_trim_with_reencoding_is_frame_exact(tmp_path):
    src = make_video(str(tmp_path / "a.mp4"), frames_n=96, fps=24, gop=12)
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts(), Edit(trim=(1.30, 3.10), crop=(0.0, 0.1, 1.0, 0.9)))
    assert result.method == "reencoded" and probe(dst).display_size == (320, 192)
    # (The crop keeps the full width, so the moving bar that tells the frames apart stays in view.)
    assert _source_frames(_gray(src), _gray(dst), box=(24, 216, 0, 320)) == list(range(32, 75))
    # Audio converted to AAC (PCM in AVI) is cut sample-exactly.
    avi = make_video(str(tmp_path / "b.avi"), fmt="avi", vcodec="mjpeg", pix_fmt="yuvj420p", audio="pcm_s16le",
                     frames_n=48)
    out = str(tmp_path / "b.mp4")
    process_video(avi, out, Opts(convert_video=True), Edit(trim=(0.5, 1.5)))
    assert probe(out).duration == pytest.approx(1.0, abs=0.05)
    assert _audio_seconds(out) == pytest.approx(1.0, abs=0.03)


def test_trim_keeps_metadata_tracks_in_the_window(tmp_path):
    """With metadata kept, the tags stay and the chapters are cut to the window; a MOV stays MOV."""
    src = make_video(str(tmp_path / "a.mp4"), frames_n=72, chapters=True, tags=ANDROID_TAGS)
    dst = str(tmp_path / "out.mp4")
    process_video(src, dst, Opts(strip_metadata=False), Edit(trim=(0.25, None)))
    info = probe(dst)
    assert info.duration == pytest.approx(2.75, abs=0.05) and info.has_gps  # the location tag was kept
    mov = make_video(str(tmp_path / "b.mov"), fmt="mov", frames_n=48, tags=APPLE_TAGS, apple_keys=True)
    process_video(mov, str(tmp_path / "out.mov"), Opts(strip_metadata=False), Edit(trim=(0, 1.5)))
    kept = probe(str(tmp_path / "out.mov"))
    assert kept.fmt == Fmt.MOV and kept.duration == pytest.approx(1.5, abs=0.05)
    assert kept.tags.get("com.apple.quicktime.model") == "iPhone 15 Pro"
    with av.open(dst) as c:
        chapters = [(float(ch["start"] * ch["time_base"]), float(ch["end"] * ch["time_base"])) for ch in c.chapters()]
    assert chapters == [(0.0, pytest.approx(0.25, abs=0.002))]  # 0–500 ms → 0–250 ms


def test_trim_plan_and_errors(tmp_path):
    src = make_video(str(tmp_path / "a.mp4"), frames_n=24)  # 1 s
    info = probe(src)
    plan = plan_video(info, Opts(strip_metadata=False), Edit(trim=(0.2, None)))
    assert plan.trim == (0.2, float("inf")) and plan.lossless and not plan.noop(info)  # to the very end
    with pytest.raises(VideoError, match="boş"):
        process_video(src, str(tmp_path / "out.mp4"), Opts(), Edit(trim=(5.0, None)))
    # A removed part is removed for good, at the start or the end too (re-encoded: nothing stays hidden).
    plan = plan_video(info, Opts(), Edit(cuts=((0.0, 0.3), (0.8, 2.0))))
    assert plan.trim == (0.3, 0.8) and not plan.cuts and plan.removed == 2 and plan.encode_video
    plan = plan_video(info, Opts(), Edit(cuts=((0.3, 0.5),)))
    assert plan.trim == (0.0, float("inf")) and plan.cuts == ((0.3, 0.5),) and plan.removed == 1
    assert plan.encode_video and plan.audio_encode and "bölüm çıkarma" in plan.reasons
    assert plan_video(info, Opts(), Edit(trim=(0.5, None), cuts=((0.1, 0.3),))).removed == 0  # outside the trim
    with pytest.raises(VideoError, match="hiçbir bölümü"):
        process_video(src, str(tmp_path / "none.mp4"), Opts(), Edit(trim=(0.2, 0.6), cuts=((0.1, 0.7),)))


def test_window_maps_times_across_removed_parts():
    w = _Window(start=1.0, end=9.0, base=0.0, key_pts=0, seek=False, cuts=((3.0, 4.0), (6.0, 7.5)))
    assert [w.kept(t) for t in (0.9, 1.0, 2.99, 3.0, 3.99, 4.0, 7.49, 7.5, 8.99, 9.0)] == \
        [False, True, True, False, False, True, False, True, True, False]
    assert w.out_time(2.0) == 1.0 and w.out_time(4.0) == 2.0 and w.out_time(7.5) == 4.0
    assert w.out_time(3.5) == 2.0  # inside a removed part: where it was cut
    assert w.pieces(2.5, 8.0) == [(2.5, 3.0), (4.0, 6.0), (7.5, 8.0)]
    assert w.span(2.5, 8.0) == (1.5, 3.0) and w.span(3.2, 3.8) is None
    chapters = [{"start": 0, "end": 3500, "time_base": Fraction(1, 1000)},
                {"start": 3500, "end": 6500, "time_base": Fraction(1, 1000)},
                {"start": 6500, "end": 7000, "time_base": Fraction(1, 1000)}]
    assert [(c["start"], c["end"]) for c in w.chapters(chapters)] == [(0, 2000), (2000, 4000)]


def test_cuts_remove_parts_from_the_middle(tmp_path):
    """Removing a part re-encodes: exactly the frames outside it play, joined; the audio and the subtitles
    follow (a cue cut by the removed part gets shorter)."""
    from video_builders import subtitle_lines

    src = make_video(str(tmp_path / "a.mp4"), frames_n=96, fps=24, gop=12, subtitle=CUES)
    dst = str(tmp_path / "out.mp4")
    result = process_video(src, dst, Opts(), Edit(cuts=((1.0, 2.0),)))
    assert result.method == "reencoded"
    assert "Video kesildi; 1 bölüm çıkarıldı (kalan: 0:00 – 0:01, 0:02 – 0:04)." in result.notes
    assert _source_frames(_gray(src), _gray(dst)) == [*range(0, 24), *range(48, 96)]
    assert probe(dst).duration == pytest.approx(3.0, abs=0.05)
    assert _audio_seconds(dst) == pytest.approx(3.0, abs=0.03)
    assert subtitle_lines(dst)[0][2] == ["one", "three", "four"]
    # Two parts removed, one of them in the middle of a cue; a start trim as well.
    dst2 = str(tmp_path / "out2.mp4")
    process_video(src, dst2, Opts(), Edit(trim=(0.25, None), cuts=((0.5, 1.5), (2.5, 3.0))))
    assert _source_frames(_gray(src), _gray(dst2)) == [*range(6, 12), *range(36, 60), *range(72, 96)]
    assert probe(dst2).duration == pytest.approx(2.25, abs=0.05)
    assert _audio_seconds(dst2) == pytest.approx(2.25, abs=0.03)
    with av.open(dst2) as c:
        cues = [(round(float(p.pts * p.time_base), 2), round(float(p.duration * p.time_base), 2))
                for p in c.demux(c.streams.subtitles[0]) if p.size > 2]
    # Kept: 0.25–0.5 → 0–0.25, 1.5–2.5 → 0.25–1.25, 3–4 → 1.25–2.25. "one" (0–0.9) keeps 0.25–0.5, "two"
    # (1–1.9) keeps 1.5–1.9, "three" (2–2.9) keeps 2–2.5.
    assert cues == [(0.0, 0.25), (0.25, 0.4), (0.75, 0.5), (1.25, 0.9)]


def test_cuts_on_a_coarse_time_base_stay_in_order(tmp_path):
    """AVI counts time in frames: the frames on either side of a removed part that does not fall on frame
    boundaries must not get the same time."""
    avi = make_video(str(tmp_path / "b.avi"), fmt="avi", vcodec="mjpeg", pix_fmt="yuvj420p", audio="pcm_s16le",
                     frames_n=48)
    out = str(tmp_path / "b.mp4")
    process_video(avi, out, Opts(convert_video=True), Edit(cuts=((0.51, 0.99),)))
    assert len(_gray(out)) == 13 + 24  # frames 0–12 and 24–47
    assert _source_frames(_gray(avi), _gray(out)) == [*range(0, 13), *range(24, 48)]
    assert _audio_seconds(out) == pytest.approx(1.52, abs=0.03)


def test_cuts_shorten_chapters(tmp_path):
    src = make_video(str(tmp_path / "a.mp4"), frames_n=72, chapters=True)  # one chapter, 0–500 ms
    dst = str(tmp_path / "out.mp4")
    process_video(src, dst, Opts(strip_metadata=False), Edit(cuts=((0.25, 0.4),)))
    with av.open(dst) as c:
        chapters = [(float(ch["start"] * ch["time_base"]), float(ch["end"] * ch["time_base"])) for ch in c.chapters()]
    assert chapters == [(0.0, pytest.approx(0.35, abs=0.002))]


# ----------------------------------------------------------------- other


def test_progress_and_cancel(tmp_path):
    src = make_video(str(tmp_path / "a.mov"), fmt="mov", vcodec="mjpeg", pix_fmt="yuvj420p", frames_n=30)
    seen = []
    process_video(src, str(tmp_path / "out.mp4"), Opts(convert_video=True), progress=seen.append)
    assert seen and seen[-1] == 1.0 and all(0 <= x <= 1 for x in seen)
    dst = tmp_path / "cancelled.mp4"
    with pytest.raises(VideoCancelled):
        process_video(src, str(dst), Opts(convert_video=True), cancelled=lambda: True)
    assert not dst.exists()


def test_sei_payload_helper_is_valid_h264(tmp_path):
    """The SEI added by the test builder is a valid NAL unit that the decoder accepts."""
    nal = sei_nal(b"\x00" * 16 + b"hello")
    assert bitstream.sei_messages(bitstream.unescape(nal), 1) == [(5, b"\x00" * 16 + b"hello")]
    path = make_video(str(tmp_path / "a.mp4"), sei=[b"\x11" * 16 + b"deneme"])
    assert len(_frames(path, 50)) == 24
