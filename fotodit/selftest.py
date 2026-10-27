"""Installation verification: `Fotodit.exe --self-test report.txt`

Checks with samples generated in memory that all decoders (JPEG, PNG, WebP, HEIC, AVIF,
TIFF, GIF, BMP) and color management work in the packaged app and that metadata really is
stripped; RAW (LibRaw, also with an exposure), the exposure of other photos, keeping a wide color
space, keeping only the capture date, shrinking for sharing, folder scanning, the large preview and
rotate/crop/flip are covered too. For video, FFmpeg
(PyAV), x264, trimming without re-encoding, removing a part (re-encoded), Qt's playback plugin and
the backup player are exercised; and the license files must be in the bundle. Exit code 0 = passed.
"""

from __future__ import annotations

import io
import traceback

from PIL import Image, ImageDraw, features

from .core.imaging import ensure_plugins, open_image
from .core.meta import Kind, residual_signatures, scan_bytes
from .core.processor import ContentOptions, process_bytes

_XMP = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/></x:xmpmeta>'


def _sample() -> tuple[Image.Image, bytes]:
    im = Image.new("RGB", (96, 64), (230, 230, 230))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 20, 20), fill=(255, 0, 0))
    d.ellipse((40, 10, 90, 60), fill=(20, 90, 200))
    ex = Image.Exif()
    ex[0x010F] = "SelfTest"
    ex[0x0110] = "Camera"
    ex[0x0112] = 6
    gps = ex.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (41.0, 0.0, 30.0), "E", (29.0, 0.0, 5.0)
    return im, ex.tobytes()


def _raw_checks(im: Image.Image, lines: list[str]) -> bool:
    """LibRaw with a synthetic DNG: the develop-from-RAW-data and camera-JPEG paths."""
    import rawpy

    from .core.dngwriter import make_dng
    from .core.rawdev import RAW_AUTO, RAW_DEVELOP

    lines.append(f"LibRaw {'.'.join(map(str, rawpy.libraw_version))}")
    ok = True
    stored = im.transpose(Image.Transpose.ROTATE_90)  # as if the phone was held upright (orientation 6)
    for label, preview, mode in (("develop RAW", None, RAW_DEVELOP), ("camera JPEG", "full", RAW_AUTO)):
        data = make_dng(stored, orientation=6, preview=preview)
        result = process_bytes(data, ContentOptions(strip_metadata=True, convert_to_jpeg=True, raw_mode=mode), name="x.dng")
        out = result.data
        with open_image(io.BytesIO(out)) as check:
            check.load()
            size = check.size
            corner = check.convert("RGB").getpixel((4, 4))
        good = (
            scan_bytes(out, result.fmt, enrich=False).clean and not residual_signatures(out)
            and size == im.size and corner[0] > 150 > corner[2]
        )
        ok &= good
        lines.append(f"DNG   → {label:11s}: {'OK' if good else 'FAIL'} ({size[0]}x{size[1]})")
    # An exposure: developed from the RAW data (not the camera JPEG) and brighter.
    from .core.edits import Edit

    data = make_dng(stored, orientation=6, preview="full")
    opts = ContentOptions(strip_metadata=True, convert_to_jpeg=True, raw_mode=RAW_AUTO)
    plain, bright = (_decode(process_bytes(data, opts, name="x.dng", edit=edit).data)
                     for edit in (None, Edit(exposure=1.0)))
    gain = sum(bright.convert("L").getdata()) / max(1, sum(plain.convert("L").getdata()))
    good = bright.size == im.size and gain > 1.1
    ok &= good
    lines.append(f"DNG   → exposure +1  : {'OK' if good else 'FAIL'} (brightness ×{gain:.2f})")
    return ok


def _folder_checks(lines: list[str]) -> bool:
    """Folder selection: subfolder scan, skip rules and a plan that keeps the folder structure."""
    import os
    import tempfile

    from .core.folderscan import walk
    from .core.planner import GROUP_SOURCE, JobOptions, OutputOptions, Photo, build_plan
    from .winutil import recycle_bin_capacity, volume_root

    with tempfile.TemporaryDirectory(prefix="fotodit-selftest-") as tmp:
        root = os.path.join(tmp, "Ayşe")  # non-ASCII on purpose: exercises Unicode paths
        for rel in ("2019/Holiday/a.jpg", "2019/b.jpg", "c.jpg", ".thumbnails/t.jpg"):
            path = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xd9")
        scan = walk(root)
        photos = [Photo(p, size, mtime, i, root=root) for i, (p, size, mtime) in enumerate(scan.photos)]
        out = os.path.join(tmp, "out")
        plan = build_plan(photos, JobOptions(output=OutputOptions(folder=out, group_by=GROUP_SOURCE)))
        targets = sorted(os.path.relpath(e.target_path, out) for e in plan)
        good = targets == [os.path.join("Ayşe", "2019", "Holiday", "a.jpg"), os.path.join("Ayşe", "2019", "b.jpg"),
                           os.path.join("Ayşe", "c.jpg")]
        vol = volume_root(tmp)
        capacity = recycle_bin_capacity(tmp)
    lines.append(f"Folder scan + structure preservation: {'OK' if good else 'FAIL'} ({len(scan.photos)} photos)")
    lines.append(f"Volume {vol}: Recycle Bin capacity {capacity // 1048576 if capacity else capacity} MB")
    return good


def _preview_checks(im: Image.Image, exif: bytes, lines: list[str]) -> bool:
    """Large preview: orientation and size (JPEG, orientation 6) and the RAW's embedded preview."""
    import os
    import tempfile

    from .core.dngwriter import make_dng
    from .core.preview import render

    ok = True
    with tempfile.TemporaryDirectory(prefix="fotodit-selftest-") as tmp:
        stored = im.transpose(Image.Transpose.ROTATE_90)  # EXIF orientation 6: the viewer rotates it back
        # Non-ASCII file names on purpose: the preview must open Unicode paths (LibRaw included).
        cases = {"Ayşe.jpg": None, "Ayşe.dng": make_dng(stored, orientation=6, preview="full")}
        buf = io.BytesIO()
        stored.save(buf, "JPEG", exif=exif)
        cases["Ayşe.jpg"] = buf.getvalue()
        for name, data in cases.items():
            path = os.path.join(tmp, name)
            with open(path, "wb") as fh:
                fh.write(data)
            shown = render(path, 1920).convert("RGB")
            corner = shown.getpixel((4, 4))
            good = shown.size == im.size and corner[0] > 150 > corner[2]
            ok &= good
            lines.append(f"Preview {name.split('.')[-1].upper():4s}: {'OK' if good else 'FAIL'} "
                         f"({shown.size[0]}x{shown.size[1]})")
    return ok


def _edit_checks(im: Image.Image, lines: list[str]) -> bool:
    """Rotate and crop: 90° + crop on a JPEG (re-encoding instead of the lossless stripping
    path), no empty corners after straightening, and 180° while converting HEIC to JPG. The
    horizontal flip: the red top-left corner of a JPEG ends up top right."""
    from .core.edits import Edit

    def red(px) -> bool:
        return px[0] > 150 > px[2]

    ok = True
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    jpeg = buf.getvalue()
    opts = ContentOptions(strip_metadata=True)
    out = _decode(process_bytes(jpeg, opts, edit=Edit(quarter=1, crop=(0, 0, 1, 0.5))).data)
    good = out.size == (64, 48) and red(out.getpixel((out.width - 4, 4)))  # top-left corner is now top right
    straight = _decode(process_bytes(jpeg, opts, edit=Edit(angle=5)).data)
    corners = [straight.getpixel(p) for p in ((0, 0), (straight.width - 1, 0), (0, straight.height - 1),
                                              (straight.width - 1, straight.height - 1))]
    good &= straight.size == Edit(angle=5).output_size(*im.size) and all(sum(c) > 150 for c in corners)
    buf = io.BytesIO()
    im.save(buf, "HEIF", quality=90)
    heic = _decode(process_bytes(buf.getvalue(), ContentOptions(strip_metadata=True, convert_to_jpeg=True),
                                 edit=Edit(quarter=2)).data)
    good &= heic.size == im.size and red(heic.getpixel((heic.width - 4, heic.height - 4)))
    ok &= good
    lines.append(f"Rotate/crop/straighten: {'OK' if good else 'FAIL'} ({out.size[0]}x{out.size[1]}, "
                 f"{straight.size[0]}x{straight.size[1]})")
    flipped = _decode(process_bytes(jpeg, opts, edit=Edit(mirror=True)).data)
    moved = red(flipped.getpixel((flipped.width - 4, 4))) and not red(flipped.getpixel((4, 4)))
    good = flipped.size == im.size and moved
    ok &= good
    lines.append(f"JPEG  → mirror       : {'OK' if good else 'FAIL'} ({flipped.size[0]}x{flipped.size[1]}, "
                 f"red corner {'top right' if moved else 'not moved'})")
    return ok


def _tone_checks(im: Image.Image, lines: list[str]) -> bool:
    """The exposure of a JPEG (in linear light), and keeping a wide color space: a Display P3 photo
    keeps its pixels and gets a clean standard profile (Little CMS measures the equivalence)."""
    from PIL import ImageStat

    from .core import colors, iccgen
    from .core.edits import Edit

    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    bright = process_bytes(buf.getvalue(), ContentOptions(strip_metadata=True), edit=Edit(exposure=1.0))
    gain = (ImageStat.Stat(_decode(bright.data).convert("L")).mean[0]
            / max(1.0, ImageStat.Stat(im.convert("L")).mean[0]))
    good1 = gain > 1.05
    lines.append(f"JPEG  → exposure +1  : {'OK' if good1 else 'FAIL'} (brightness ×{gain:.2f})")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92, icc_profile=iccgen.nclx_profile(12, 13))
    kept = process_bytes(buf.getvalue(), ContentOptions(strip_metadata=True, keep_color_profile=True))
    with open_image(io.BytesIO(kept.data)) as check:
        embedded = check.info.get("icc_profile")
    good2 = kept.method == "lossless" and colors.is_clean_profile(kept.profile) and embedded == kept.profile
    lines.append(f"JPEG  → keep Display P3: {'OK' if good2 else 'FAIL'} ({kept.method})")
    return good1 and good2


def _sharing_checks(im: Image.Image, exif: bytes, lines: list[str]) -> bool:
    """Keeping only the capture date: a JPEG with camera and location EXIF comes out with exactly one
    tag, its DateTimeOriginal. Shrinking for sharing: the long side becomes the limit."""
    from datetime import datetime

    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92, exif=exif)
    dated = process_bytes(buf.getvalue(), ContentOptions(strip_metadata=True, keep_date=True),
                          taken=datetime(2024, 5, 12, 14, 30, 22))
    with open_image(io.BytesIO(dated.data)) as check:
        tags = check.getexif()
        kept = dict(tags.get_ifd(0x8769))
        ifd0 = set(tags)
    found = [f.kind for f in scan_bytes(dated.data, dated.fmt, enrich=False).findings]
    good1 = (kept == {0x9003: "2024:05:12 14:30:22"} and ifd0 == {0x8769} and found == [Kind.EXIF]
             and residual_signatures(dated.data) == ["EXIF"])
    lines.append(f"JPEG  → keep date    : {'OK' if good1 else 'FAIL'} ({kept.get(0x9003)}, {len(ifd0)} IFD0 tag)")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    small = _decode(process_bytes(buf.getvalue(), ContentOptions(strip_metadata=True, max_side=48)).data)
    good2 = small.size == (48, 32) and small.getpixel((3, 3))[0] > 150 > small.getpixel((3, 3))[2]
    lines.append(f"JPEG  → max 48 px    : {'OK' if good2 else 'FAIL'} ({small.size[0]}x{small.size[1]})")
    return good1 and good2


def _legal_check(lines: list[str]) -> bool:
    """Fotodit's license, the notices and the license texts are in the bundle (the About window)."""
    import os

    from .core.legal import LICENSE, NOTICES, legal_folder, read_text

    folder = legal_folder()
    texts = len(os.listdir(os.path.join(folder, "licenses"))) if folder is not None else 0
    good = ("GNU GENERAL PUBLIC LICENSE" in (read_text(LICENSE) or "")[:200]
            and "Artapa" in (read_text(NOTICES) or "") and texts >= 15)
    lines.append(f"License files: {'OK' if good else 'FAIL'} ({texts} entries in licenses)")
    return good


def _make_video(path: str, fmt: str, codec: str, pix_fmt: str, audio: str, rotation: int = 0,
                tags: dict | None = None) -> None:
    """A short 96×64 video of 12 frames (with audio) whose top-left corner is red."""
    import fractions

    import av
    import numpy as np

    out = av.open(path, "w", format=fmt, options={"movflags": "+use_metadata_tags"} if tags else {})
    v = out.add_stream(codec, rate=12)
    v.width, v.height, v.pix_fmt = 96, 64, pix_fmt
    if rotation:
        v.set_display_rotation((-rotation) % 360)
    a = out.add_stream(audio, rate=48000, layout="stereo")
    if tags:
        out.metadata.update(tags)
    img = np.full((64, 96, 3), 200, np.uint8)
    img[:20, :20] = (255, 0, 0)
    for i in range(12):
        img[40:44, i * 6:i * 6 + 10] = 255
        for packet in v.encode(av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format=pix_fmt)):
            out.mux(packet)
    for packet in v.encode():
        out.mux(packet)
    for i in range(48):
        if audio == "aac":
            frame = av.AudioFrame.from_ndarray(np.zeros((2, 1024), np.float32), format="fltp", layout="stereo")
        else:  # PCM (as in AVIs from older cameras): interleaved 16-bit
            frame = av.AudioFrame.from_ndarray(np.zeros((1, 2048), np.int16), format="s16", layout="stereo")
        frame.sample_rate, frame.pts, frame.time_base = 48000, i * 1024, fractions.Fraction(1, 48000)
        for packet in a.encode(frame):
            out.mux(packet)
    for packet in a.encode():
        out.mux(packet)
    out.close()


def _video_checks(lines: list[str]) -> bool:
    """Video: PyAV/FFmpeg and x264, metadata stripping (location tag removed, rotation kept),
    MJPEG AVI to MP4 conversion (H.264 + AAC, with a crop), the thumbnail, Qt's video playback
    plugin, the backup player (AV1) and the GPU encoder."""
    import os
    import tempfile

    import av

    from .core.edits import Edit
    from .core.video.probe import probe
    from .core.video.process import process_video
    from .core.video.thumb import poster

    lines.append(f"FFmpeg {av.ffmpeg_version_info} (PyAV {av.__version__})")
    with tempfile.TemporaryDirectory(prefix="fotodit-selftest-") as tmp:
        src = os.path.join(tmp, "iphone.mov")
        _make_video(src, "mov", "libx264", "yuv420p", "aac", rotation=90, tags={
            "com.apple.quicktime.location.ISO6709": "+41.0082+028.9784+050.000/",
            "com.apple.quicktime.make": "Apple", "com.apple.quicktime.model": "iPhone",
        })
        info = probe(src)
        clean = os.path.join(tmp, "clean.mov")
        r1 = process_video(src, clean, ContentOptions(strip_metadata=True))
        after = probe(clean)
        good1 = (info.has_gps and info.rotation == 90 and r1.method == "lossless" and not after.has_gps
                 and after.camera is None and after.rotation == 90 and not after.sei)
        lines.append(f"Video → strip metadata: {'OK' if good1 else 'FAIL'} (MOV, rotation {after.rotation}°, "
                     f"{'has location' if after.has_gps else 'no location'})")
        avi = os.path.join(tmp, "old.avi")
        _make_video(avi, "avi", "mjpeg", "yuvj420p", "pcm_s16le")
        mp4 = os.path.join(tmp, "new.mp4")
        process_video(avi, mp4, ContentOptions(strip_metadata=True, convert_video=True), Edit(crop=(0, 0, 0.5, 1)))
        conv = probe(mp4)
        good2 = (conv.vcodec, conv.acodec, conv.display_size) == ("h264", "aac", (48, 64))
        lines.append(f"Video → MP4 (H.264): {'OK' if good2 else 'FAIL'} ({conv.vcodec}/{conv.acodec}, "
                     f"{conv.display_size[0]}x{conv.display_size[1]})")
        thumb = poster(src, 64)
        good3 = thumb is not None and thumb.size == (43, 64) and thumb.getpixel((thumb.width - 4, 4))[0] > 150
        lines.append(f"Video thumbnail: {'OK' if good3 else 'FAIL'} ({thumb.size if thumb else None})")
        cut = os.path.join(tmp, "cut.mov")  # x264's defaults use B-frames: the edit lists are exercised
        r6 = process_video(src, cut, ContentOptions(strip_metadata=True), Edit(trim=(0.25, 0.75)))
        trimmed = probe(cut)
        good6 = r6.method == "lossless" and abs(trimmed.duration - 0.5) < 0.05 and trimmed.rotation == 90
        lines.append(f"Video → trim (lossless): {'OK' if good6 else 'FAIL'} ({trimmed.duration:.2f} s)")
        removed = os.path.join(tmp, "removed.mov")
        r7 = process_video(src, removed, ContentOptions(strip_metadata=True), Edit(cuts=((0.25, 0.5),)))
        shortened = probe(removed)
        good7 = r7.method == "reencoded" and abs(shortened.duration - 0.75) < 0.06
        lines.append(f"Video → remove a part (re-encoded): {'OK' if good7 else 'FAIL'} ({shortened.duration:.2f} s)")
        good4 = _player_check(clean, lines)
        av1 = os.path.join(tmp, "av1.mp4")
        _make_video(av1, "mp4", "libsvtav1", "yuv420p", "aac")
        good5 = _player_check(av1, lines, codec="av1")
    from .core.video import hwenc

    encoder = hwenc.find("h264")
    lines.append(f"GPU encoder: {encoder.label if encoder else 'none (videos are encoded on the CPU)'}")
    return good1 and good2 and good3 and good4 and good5 and good6 and good7


def _player_check(path: str, lines: list[str], codec: str | None = None) -> bool:
    """Can Qt Multimedia (FFmpeg backend) open the video and deliver a frame? If `codec` is "av1",
    the PyAV backup player (dav1d) is tested instead."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication(["fotodit-selftest"])
    from .ui.player import VideoPlayer

    player = VideoPlayer()
    got: list[tuple[int, int, int]] = []
    loop = QEventLoop()
    player.frameReady.connect(lambda image, rotation: (got.append((image.width(), image.height(), rotation)),
                                                       loop.quit()))
    player.failed.connect(lambda _message: loop.quit())
    player.set_muted(True)
    player.load(path, codec)
    player.play()
    QTimer.singleShot(8000, loop.quit)
    loop.exec()
    backup = player.uses_backup()
    player.shutdown()
    del app
    if codec:  # backup player: a landscape 96×64 frame, no rotation
        good = backup and bool(got) and got[0] == (96, 64, 0)
        label = "Video playback, backup player (AV1)"
    else:  # a portrait video (96×64, rotation 90°) must arrive as a portrait frame: Qt applies the rotation
        good = not backup and bool(got) and got[0][1] > got[0][0] and got[0][2] == 0
        label = "Video playback (Qt Multimedia)"
    lines.append(f"{label}: {'OK' if good else 'FAIL'} "
                 f"({f'frame received, {got[0][0]}x{got[0][1]}' if got else 'no frame received'})")
    return good


def _decode(data: bytes) -> Image.Image:
    with open_image(io.BytesIO(data)) as check:
        check.load()
        return check.convert("RGB")


def run(report_path: str | None = None) -> int:
    lines: list[str] = []
    ok = True
    try:
        ensure_plugins()
        import pillow_heif

        info = pillow_heif.libheif_info()
        lines.append(f"libheif {info.get('libheif')} · HEIF encoder: {bool(info.get('HEIF'))}")
        for feat in ("jpg", "webp", "avif", "littlecms2", "libtiff", "zlib"):
            lines.append(f"{feat}: {features.check(feat)}")
            ok &= bool(features.check(feat))
        im, exif = _sample()
        cases = {
            "JPEG": {"exif": exif, "comment": b"secret"},
            "PNG": {"exif": exif},
            "WEBP": {"exif": exif, "xmp": _XMP},
            "HEIF": {"exif": exif, "xmp": _XMP, "quality": 90},
            "AVIF": {"exif": exif, "xmp": _XMP, "quality": 90},
            "TIFF": {"exif": exif},
            "GIF": {"comment": b"secret"},
            "BMP": {},
        }
        for fmt, params in cases.items():
            buf = io.BytesIO()
            im.save(buf, fmt, **params)
            data = buf.getvalue()
            for label, opts in (
                ("strip", ContentOptions(strip_metadata=True)),
                ("jpg", ContentOptions(strip_metadata=True, convert_to_jpeg=True)),
            ):
                result = process_bytes(data, opts)
                out = result.data
                res = scan_bytes(out, result.fmt, enrich=False)
                with open_image(io.BytesIO(out)) as check:
                    check.load()
                    size = check.size
                good = res.clean and not residual_signatures(out)
                ok &= good
                lines.append(f"{fmt:5s} → {label:8s}: {'OK' if good else 'FAIL'} ({result.method}, {size[0]}x{size[1]})")
        ok &= _raw_checks(im, lines)
        ok &= _tone_checks(im, lines)
        ok &= _sharing_checks(im, exif, lines)
        ok &= _folder_checks(lines)
        ok &= _preview_checks(im, exif, lines)
        ok &= _edit_checks(im, lines)
        ok &= _video_checks(lines)
        ok &= _legal_check(lines)
    except Exception:  # noqa: BLE001
        ok = False
        lines.append(traceback.format_exc())
    lines.append("RESULT: " + ("PASSED" if ok else "FAILED"))
    text = "\n".join(lines)
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0 if ok else 1
