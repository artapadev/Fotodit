"""Fixes from the 1.3 review of the metadata parsers: a TIFF walk that could run for hours, chunks
hidden inside WebP animation frames, odd EXIF text that made a photo unreadable, compressed PNG chunks
that inflate to gigabytes, RAW and TIFF directories read without the image data, bytes hidden in the
chunks the strippers keep, damaged HEIF boxes, GPS on a TIFF's later pages and a WebP frame without
a size."""

from __future__ import annotations

import io
import mmap
import struct
import time
import tracemalloc
import zlib
from datetime import datetime

import builders as b
import pytest
from PIL import Image, ImageSequence
from PIL.TiffImagePlugin import ImageFileDirectory_v2

from fotodit.core import meta
from fotodit.core.dngwriter import make_dng
from fotodit.core.formats import Fmt
from fotodit.core.meta import (
    Kind,
    gif,
    jpeg,
    png,
    raw,
    scan_bytes,
    scan_file,
    tiff,
    webp,
)
from fotodit.core.meta import exif as exif_meta
from fotodit.core.meta.model import FormatError
from fotodit.core.photoinfo import read_info

SECRET = b"GPS 41.0085N 28.9784E serial SN-123456789"
TAKEN = datetime(2024, 5, 12, 14, 30, 22)


def _frames(data: bytes) -> list[bytes]:
    with Image.open(io.BytesIO(data)) as im:
        return [frame.convert("RGBA").tobytes() for frame in ImageSequence.Iterator(im)]


def _animated_webp(**kw) -> bytes:
    frames = [Image.new("RGB", (16, 16), color) for color in ("red", "green", "blue")]
    buf = io.BytesIO()
    frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:], duration=100, loop=3, lossless=True, **kw)
    return buf.getvalue()


def _grow_chunk(data: bytes, fourcc: bytes, extra: bytes) -> bytes:
    """The WebP with `extra` appended to the payload of its first `fourcc` chunk (sizes fixed)."""
    pos = data.find(fourcc, 12)
    size = struct.unpack_from("<I", data, pos + 4)[0]
    end = pos + 8 + size + (size & 1)
    payload = data[pos + 8:end] + extra + b"\x00" * (len(extra) & 1)
    body = data[12:pos] + fourcc + struct.pack("<I", len(payload)) + payload + data[end:]
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def _jpeg_with_exif(blob: bytes) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "gray").save(buf, "JPEG")
    return jpeg.insert_segments(buf.getvalue(), [(jpeg.APP1, b"Exif\x00\x00" + blob)])


def _png_with(*chunks: bytes) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "gray").save(buf, "PNG")
    data = buf.getvalue()
    at = data.find(b"IDAT") - 4
    return data[:at] + b"".join(chunks) + data[at:]


def _tiff_pages(*colors: str) -> bytes:
    pages = [Image.new("RGB", (32, 32), color) for color in colors]
    buf = io.BytesIO()
    pages[0].save(buf, "TIFF", save_all=len(pages) > 1, append_images=pages[1:])
    data = buf.getvalue()
    return data + b"\x00" * (len(data) & 1)


def _rewrite(data: bytes, page: int, add) -> bytes:
    """The TIFF with the directory of page `page` (0: the first) written again at its end with one more entry;
    `add(offset)` gives that entry and the bytes written after the directory, at `offset`."""
    data = bytearray(data)
    link = 4  # where the pointer to the directory is: the header, then each directory's next pointer
    for _ in range(page):
        ifd = struct.unpack_from("<I", data, link)[0]
        link = ifd + 2 + 12 * struct.unpack_from("<H", data, ifd)[0]
    ifd = struct.unpack_from("<I", data, link)[0]
    count = struct.unpack_from("<H", data, ifd)[0]
    at = len(data)
    entry, tail = add(at + 2 + 12 * (count + 1) + 4)
    entries = [bytes(data[ifd + 2 + 12 * i:ifd + 14 + 12 * i]) for i in range(count)] + [entry]
    entries.sort(key=lambda e: struct.unpack_from("<H", e)[0])
    following = data[ifd + 2 + 12 * count:ifd + 6 + 12 * count]
    data += struct.pack("<H", len(entries)) + b"".join(entries) + following + tail
    struct.pack_into("<I", data, link, at)
    return bytes(data)


def _box(typ: bytes, body: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(body)) + typ + body


# ------------------------------------------------------------------ 1: the TIFF walk has limits


def test_tiff_subifd_count_cannot_stall_the_scan():
    # SubIFDs: a count of 4 billion offsets, all zero. Pillow opens the file normally; the walk used to
    # loop over all of them (about 8 minutes).
    data = _rewrite(_tiff_pages("gray"), 0, lambda at: (struct.pack("<HHII", 330, 4, 0xFFFFFFFF, at), bytes(64)))
    started = time.perf_counter()
    scan_bytes(data, Fmt.TIFF)
    assert time.perf_counter() - started < 2


# ------------------------------------------------------------------ 2: chunks inside animation frames


def test_chunks_inside_an_animation_frame_are_reported_and_dropped():
    blob = b.rich_exif(1, thumbnail=False)  # GPS, serial numbers, MakerNote
    src = _grow_chunk(_animated_webp(), b"ANMF", b"EXIF" + struct.pack("<I", len(blob)) + blob)
    assert {Kind.EXIF, Kind.GPS, Kind.MAKERNOTE} <= scan_bytes(src, Fmt.WEBP).kinds()
    out = webp.strip(src)
    assert blob not in out and b"SN-123456789" not in out
    assert scan_bytes(out, Fmt.WEBP).clean
    assert _frames(out) == _frames(src) and len(_frames(out)) == 3
    # Duplicates are found by the start of the kept image data: the same before and after stripping.
    assert webp.strip(out) == out and webp.image_prefix(src, 1 << 15) == webp.image_prefix(out, 1 << 15)


# ------------------------------------------------------------------ 3: odd EXIF text


def test_odd_exif_text_does_not_make_a_photo_unreadable(tmp_path):
    ifd0 = b.Ifd("<").ascii(0x010F, "\xa0").ascii(0x0110, "EOS R5").pointer(0x8769, "exif")  # Make: a no-break space
    sub = b.Ifd("<").ascii(0x9003, "2024:05:12 14:30:22").ascii(0x9291, "²")  # sub-seconds: a superscript two
    ifd1 = b.Ifd("<").short(0x0103, 6).pointer(0x0201, "thumb").ascii(0x0202, "abc")  # a thumbnail length as text
    blob = b.build_tiff([("ifd0", ifd0), ("exif", sub), ("ifd1", ifd1)], {"thumb": b"\xff\xd8\xff\xd9"}, "<",
                        {"ifd0": "ifd1"})
    data = _jpeg_with_exif(blob)
    res = scan_bytes(data, Fmt.JPEG)
    assert res.summary.camera == "EOS R5" and res.summary.taken == TAKEN and Kind.THUMBNAIL in res.kinds()
    path = tmp_path / "odd.jpg"
    path.write_bytes(data)
    info = read_info(str(path))
    assert info.error is None and info.camera == "EOS R5" and info.taken == TAKEN


def test_odd_exif_text_in_a_raw_does_not_break_reading_it(tmp_path):
    data = make_dng(b.photo(160, 120), make="\xa0")
    # SubSecTimeOriginal "²" in place of the serial number (same length, same place in the tag order).
    serial = struct.pack("<HHI", 0xA431, 2, 14)
    assert data.count(serial) == 1
    data = data.replace(serial, struct.pack("<HHI", 0x9291, 2, 14))
    data = data.replace(b"SN-DNG-000123\x00", "²".encode().ljust(14, b"\x00"))
    parts = raw.exif_parts(data)
    assert raw.camera_of(parts) == "Test Kamera" and raw.taken_of(parts) == TAKEN
    path = tmp_path / "odd.dng"
    path.write_bytes(data)
    info = read_info(str(path))
    assert info.error is None and info.camera == "Test Kamera" and info.taken == TAKEN


def test_a_raw_whose_camera_or_date_cannot_be_read_still_gets_its_info(tmp_path, monkeypatch):
    def unreadable(parts):
        raise ValueError("odd value")

    monkeypatch.setattr(raw, "camera_of", unreadable)
    monkeypatch.setattr(raw, "taken_of", unreadable)
    path = tmp_path / "plain.dng"
    path.write_bytes(make_dng(b.photo(160, 120)))
    info = read_info(str(path))
    assert info.error is None and info.camera is None and info.has_gps


# ------------------------------------------------------------------ 4: compressed PNG chunks


def test_compressed_png_chunks_are_inflated_only_up_to_a_limit(monkeypatch):
    monkeypatch.setattr(png, "ICC_LIMIT", 1 << 20, raising=False)
    monkeypatch.setattr(png, "TEXT_LIMIT", 1 << 20, raising=False)
    bomb = zlib.compress(bytes(32 << 20), 9)  # 32 MB of zeros in about 32 KB
    data = _png_with(
        b.png_chunk(b"iCCP", b"evil\x00\x00" + bomb),
        b.png_chunk(b"zTXt", b"Comment\x00\x00" + bomb),
        b.png_chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x01\x00\x00\x00" + bomb),
    )
    tracemalloc.start()
    try:
        icc = png.icc_profile(data)
        kinds = scan_bytes(data, Fmt.PNG).kinds()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 << 20
    assert icc is None  # too large: treated as an unreadable profile
    assert {Kind.ICC, Kind.TEXT, Kind.XMP} <= kinds  # still found
    out = png.strip(data)
    assert b"iCCP" not in out and b"zTXt" not in out and b"iTXt" not in out


def test_hidden_png_image_data_is_not_inflated_to_the_end(monkeypatch):
    stream = zlib.compress(bytes(4) + bytes(32 << 20), 9)  # one RGB pixel, then 32 MB more
    ihdr = b.png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    data = png.SIGNATURE + ihdr + b.png_chunk(b"IDAT", stream) + b.png_chunk(b"IEND", b"")
    inflated = 0
    real = zlib.decompressobj

    class Counting:
        def __init__(self):
            self.dec = real()

        def decompress(self, chunk, max_length=0):
            nonlocal inflated
            out = self.dec.decompress(chunk, max_length)
            inflated += len(out)
            return out

        def __getattr__(self, name):
            return getattr(self.dec, name)

    monkeypatch.setattr(zlib, "decompressobj", Counting)
    assert png.hidden_idat_data(data) > 0
    assert inflated < 4 << 20


# ------------------------------------------------------------------ 5 and 10: only the directories are read


def test_raw_exif_is_read_wherever_its_directories_lie(monkeypatch):
    data = make_dng(b.photo(160, 120))
    monkeypatch.setattr(raw, "HEAD_LIMIT", 64)  # the old fixed head does not reach the Exif and GPS IFDs
    parts = raw.exif_parts(data)
    assert raw.gps_of(parts) is not None and raw.taken_of(parts) == TAKEN
    res = scan_bytes(data, Fmt.RAW)
    assert res.has_gps() and res.summary.taken == TAKEN
    # A span over SPAN_LIMIT is read as before: only the first HEAD_LIMIT bytes.
    monkeypatch.setattr(exif_meta, "SPAN_LIMIT", 64)
    parts = raw.exif_parts(data)
    assert parts is None or not parts.gps


def test_tiff_scan_reads_only_the_first_directories(monkeypatch, tmp_path):
    ifd = ImageFileDirectory_v2()
    ifd[306] = "2024:05:12 14:30:22"
    ifd[315] = "Ayşe Yılmaz"
    buf = io.BytesIO()
    Image.new("RGB", (1024, 1024)).save(buf, "TIFF", tiffinfo=ifd)  # uncompressed: 3 MB, directories first
    data = buf.getvalue()
    handed: list[int] = []
    analyze = tiff.analyze_exif

    def recording(blob, res, **kw):
        handed.append(len(blob))
        return analyze(blob, res, **kw)

    monkeypatch.setattr(tiff, "analyze_exif", recording)
    assert scan_bytes(data, Fmt.TIFF).summary.taken == TAKEN
    assert len(handed) == 1 and handed[0] < len(data) // 100
    # A file is scanned through a memory map, not read whole.
    mapped: list[type] = []

    def through(contents, fmt=None, **kw):
        mapped.append(type(contents))
        return scan_bytes(contents, fmt, **kw)

    monkeypatch.setattr(meta, "scan_bytes", through)
    path = tmp_path / "large.tif"
    path.write_bytes(data)
    assert scan_file(str(path)).summary.taken == TAKEN
    assert mapped == [mmap.mmap]


# ------------------------------------------------------------------ 6: kept chunks in their defined form


def test_bytes_hidden_in_kept_jpeg_and_png_chunks_do_not_survive():
    adobe = b"Adobe\x00\x64\x00\x00\x00\x00\x01"
    src = b.insert_after_soi(b.jpeg(b.photo(64, 48), everything=False), b.seg(0xEE, adobe + SECRET))
    out = jpeg.strip(src)
    assert SECRET not in out and b.seg(0xEE, adobe) in out and _frames(out) == _frames(src)
    plain = b.png(b.photo(64, 48), everything=False)
    src = plain[:plain.rfind(b"IEND") - 4] + b.png_chunk(b"IEND", SECRET)
    out = png.strip(src)
    assert SECRET not in out and out.endswith(png.IEND) and _frames(out) == _frames(src)
    assert png.image_prefix(src, 1 << 15) == png.image_prefix(out, 1 << 15)  # duplicates: the same picture


def test_bytes_hidden_in_kept_gif_extensions_do_not_survive():
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=150, loop=5)
    data = buf.getvalue()
    hidden = bytes([len(SECRET)]) + SECRET
    loop = data.find(b"NETSCAPE2.0") - 3 + 18  # the terminator after the 3-byte loop sub-block
    gce = data.find(b"\x21\xf9\x04", loop) + 7  # the terminator after the 4-byte block
    assert data[loop] == 0 and data[gce] == 0
    src = data[:loop] + hidden + data[loop:gce] + hidden + data[gce:]
    out = gif.strip(src)
    assert SECRET not in out and _frames(out) == _frames(src) and len(_frames(out)) == 2
    with Image.open(io.BytesIO(out)) as im, Image.open(io.BytesIO(src)) as before:
        assert im.info["loop"] == before.info["loop"] == 5 and im.info["duration"] == before.info["duration"]


def test_bytes_hidden_in_webp_headers_do_not_survive():
    plain = _animated_webp(background=(1, 2, 3, 255))
    src = _grow_chunk(plain, b"ANIM", SECRET)
    out = webp.strip(src)
    assert SECRET not in out and _frames(out) == _frames(src)
    with Image.open(io.BytesIO(out)) as im, Image.open(io.BytesIO(src)) as before:
        assert im.info["loop"] == before.info["loop"] == 3 and im.info["background"] == before.info["background"]
    # libwebp refuses a longer VP8X header; cut back to its 10 bytes, the file decodes again.
    out = webp.strip(_grow_chunk(plain, b"VP8X", SECRET))
    assert SECRET not in out and _frames(out) == _frames(plain)


# ------------------------------------------------------------------ 7: damaged HEIF boxes


@pytest.mark.parametrize("box", [
    _box(b"pitm"),
    _box(b"iref", bytes(4) + _box(b"thmb")),
    _box(b"iinf", bytes(6) + _box(b"infe")),
    _box(b"iinf", bytes(6) + _box(b"infe", b"\x02\x00\x00\x00\x00")),
], ids=["pitm", "iref", "infe", "short infe"])
def test_damaged_heif_boxes_give_the_format_error(box):
    data = _box(b"ftyp", b"heic\x00\x00\x00\x00mif1heic") + _box(b"meta", bytes(4) + box)
    with pytest.raises(FormatError):
        scan_bytes(data, Fmt.HEIF, enrich=False)


# ------------------------------------------------------------------ 8: GPS on a later TIFF page


def _gps_ifd(at: int) -> bytes:
    """A GPS IFD at offset `at`: 41°0'30.12" N, 28°58'42" E."""
    values = at + 2 + 12 * 4 + 4
    return (struct.pack("<H", 4) + struct.pack("<HHI", 1, 2, 2) + b"N\x00\x00\x00"
            + struct.pack("<HHII", 2, 5, 3, values) + struct.pack("<HHI", 3, 2, 2) + b"E\x00\x00\x00"
            + struct.pack("<HHII", 4, 5, 3, values + 24) + struct.pack("<I", 0)
            + struct.pack("<6I", 41, 1, 0, 1, 3012, 100) + struct.pack("<6I", 28, 1, 58, 1, 4200, 100))


def test_gps_on_a_later_tiff_page_is_reported():
    data = _rewrite(_tiff_pages("red", "blue"), 1, lambda at: (struct.pack("<HHII", 34853, 4, 1, at), _gps_ifd(at)))
    res = scan_bytes(data, Fmt.TIFF)
    assert res.has_gps() and res.summary.gps == (41.008367, 28.978333)


# ------------------------------------------------------------------ 9: a WebP frame without a size


def test_webp_frame_without_a_size_gives_the_format_error():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(buf, "WEBP", quality=80)
    data = bytearray(buf.getvalue())
    frame = data.find(b"VP8 ") + 8
    assert data[frame + 3:frame + 6] == b"\x9d\x01\x2a"
    data[frame + 6:frame + 8] = b"\x00\x00"  # width 0
    with pytest.raises(webp.WebpError):
        webp.with_exif(bytes(data), b.rich_exif(1, thumbnail=False))
