"""Helpers that build test videos with metadata (using PyAV).

They mimic the structures real devices write: the iPhone's date with time zone,
location and device tags (QuickTime 'mdta' keys), Android's ©xyz location,
GoPro's GPS telemetry track ('gpmd'), DJI's subtitle track carrying location, the
MDPM record that AVCHD cameras embed in the video stream (date, time zone, GPS),
the orientation of portrait footage (display matrix) and HDR (HLG) tags; and,
patched into a built file, other timed metadata and audio sample entries
(`set_sample_entry`), mirrored display matrices (`set_matrix`), the EXIF camera
AVIs keep in their stream header (`add_strd`) and DV frames with the camcorder's
recording date (`make_dv`).

As in the photo tests, frames carry corner markers (red top left, green top
right, blue bottom left), so the orientation can be checked after rotating/cropping.
"""

from __future__ import annotations

import fractions
import os
import struct

import av
import numpy as np
from builders import photo

from fotodit.core.video import bitstream
from fotodit.core.video.boxes import _boxes
from fotodit.core.video.subtitles import DEFAULT_ASS_HEADER

APPLE_TAGS = {
    "com.apple.quicktime.location.ISO6709": "+41.0082+028.9784+050.000/",
    "com.apple.quicktime.make": "Apple",
    "com.apple.quicktime.model": "iPhone 15 Pro",
    "com.apple.quicktime.software": "17.5.1",
    "com.apple.quicktime.creationdate": "2024-07-12T18:30:10+0300",
    "creation_time": "2024-07-12T15:30:10Z",
}
ANDROID_TAGS = {"location": "+41.0082+028.9784/", "creation_time": "2024-07-12T15:30:10Z"}


def mdpm_payload(year=2019, month=8, day=3, hour=14, minute=5, second=9, tz_hours=3, gps=True) -> bytes:
    """An AVCHD MDPM block (SEI user data payload): date, time zone, GPS, maker."""
    def bcd(v: int) -> int:
        return (v // 10) << 4 | v % 10

    tags = [
        (0x18, bytes([tz_hours << 1, bcd(year // 100), bcd(year % 100), bcd(month)])),
        (0x19, bytes([bcd(day), bcd(hour), bcd(minute), bcd(second)])),
        (0xE0, struct.pack(">HH", 0x0108, 0)),  # Sony
    ]
    if gps:
        tags += [
            (0xB1, b"N\x00\x00\x00"), (0xB2, struct.pack(">HH", 41, 1)), (0xB3, struct.pack(">HH", 0, 1)),
            (0xB4, struct.pack(">HH", 2952, 100)),
            (0xB5, b"E\x00\x00\x00"), (0xB6, struct.pack(">HH", 28, 1)), (0xB7, struct.pack(">HH", 58, 1)),
            (0xB8, struct.pack(">HH", 4224, 100)),
        ]
    return bitstream.MDPM_MAGIC + bytes([len(tags)]) + b"".join(bytes([t]) + v for t, v in tags)


def sei_nal(payload: bytes, codec: str = "h264", ptype: int = bitstream.USER_DATA_UNREGISTERED) -> bytes:
    header = bytes([0x4E, 0x01]) if codec == "hevc" else bytes([0x06])
    return bitstream.build_sei(header, [(ptype, payload)])


def frames(w: int, h: int, n: int):
    base = np.asarray(photo(w, h).convert("RGB"))
    for i in range(n):
        img = base.copy()
        x = (i * 7) % max(1, w - 20)
        img[h // 2 - 4:h // 2 + 4, x:x + 20] = 255  # a moving bar makes every frame different
        yield img


def make_video(path: str, *, fmt: str = "mp4", vcodec: str = "libx264", size=(320, 240), frames_n: int = 24,
               fps: int = 24, pix_fmt: str = "yuv420p", audio: str | None = "aac", rotation: int = 0,
               tags: dict | None = None, apple_keys: bool = False, sei: list[bytes] = (), hlg: bool = False,
               gpmd: bool = False, subtitle=None, subtitle_codec: str = "mov_text",
               subtitle_language: str | None = None, extra_subtitles: tuple = (), chapters: bool = False,
               options: dict | None = None, gop: int | None = None, audio_tracks: int = 1,
               timecode: str | None = None, sar: fractions.Fraction | None = None) -> str:
    """`rotation`: clockwise rotation on display (like a phone video shot in portrait).
    `sei`: SEI user data payloads to add to the first frame (e.g. `mdpm_payload()`).
    `subtitle`: a single line of text or [(start ms, end ms, text), …] (may contain ASS style codes);
    `extra_subtitles`: additional tracks as (cues, codec, language) triples.
    `gop`: H.264 with a keyframe every `gop` frames and B-frames (like phones and cameras), instead
    of the fastest settings. `audio_tracks`: how many audio tracks (like an iPhone's stereo and
    spatial ones). `timecode`: a timecode track ('tmcd', as cameras and GoPro write) starting at it.
    `sar`: the pixel aspect ratio (non-square pixels, like HDV/AVCHD 1440×1080 or DV)."""
    w, h = size
    mux_opts = dict(options or {})
    if apple_keys:
        mux_opts["movflags"] = "+use_metadata_tags"
    if timecode and fmt == "mp4":
        mux_opts["write_tmcd"] = "1"  # (MOV writes one by itself)
    out = av.open(path, "w", format=fmt, options=mux_opts)
    v = out.add_stream(vcodec, rate=fps)
    v.width, v.height, v.pix_fmt = w, h, pix_fmt
    if vcodec in ("libx264", "libx265"):
        v.options = {"preset": "ultrafast", "crf": "20"}
    if gop:
        v.options = {"preset": "medium", "crf": "20",
                     "x264-params": f"keyint={gop}:min-keyint={gop}:bframes=3:scenecut=0"}
    if hlg:
        v.codec_context.color_primaries = 9
        v.codec_context.color_trc = 18
        v.codec_context.colorspace = 9
    if sar:
        v.codec_context.sample_aspect_ratio = sar
    if timecode:
        v.metadata["timecode"] = timecode
    if rotation:
        v.set_display_rotation((-rotation) % 360)
    sounds = []
    for _ in range(audio_tracks if audio else 0):
        a = out.add_stream(audio, rate=48000 if audio != "libopencore_amrnb" else 8000,
                           layout="stereo" if audio != "libopencore_amrnb" else "mono")
        if audio in ("wmav2", "libopencore_amrnb"):
            a.bit_rate = 12200 if audio == "libopencore_amrnb" else 128000
        sounds.append(a)
    data = None
    if gpmd:
        data = out.add_data_stream("bin_data")
        data.codec_tag = "gpmd"
    tracks = []
    if subtitle is not None:
        tracks.append((subtitle, subtitle_codec, subtitle_language))
    tracks.extend(extra_subtitles)
    subs = []
    for cues, codec, language in tracks:
        sub = out.add_stream(codec)
        sub.codec_context.subtitle_header = DEFAULT_ASS_HEADER
        sub.time_base = fractions.Fraction(1, 1000)
        if language:
            sub.metadata["language"] = language
        subs.append((sub, [(0, 500, cues)] if isinstance(cues, str) else list(cues)))
    if chapters:
        out.set_chapters([{"id": 1, "start": 0, "end": 500, "time_base": fractions.Fraction(1, 1000),
                           "metadata": {"title": "Birthday"}}])
    if tags:
        out.metadata.update(tags)
    first = True
    for img in frames(w, h, frames_n):
        frame = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format=pix_fmt)
        for packet in v.encode(frame):
            if first and sei:
                data_bytes = bytes(packet)
                extra = b"".join(b"\x00\x00\x00\x01" + sei_nal(p) for p in sei)
                fresh = av.Packet(extra + data_bytes)
                fresh.pts, fresh.dts, fresh.time_base = packet.pts, packet.dts, packet.time_base
                fresh.is_keyframe = packet.is_keyframe
                fresh.duration = packet.duration
                packet = fresh
            first = False
            packet.stream = v
            out.mux(packet)
    for packet in v.encode():
        out.mux(packet)
    for a in sounds:
        _add_audio(out, a, frames_n / fps)
    if data is not None:
        for i in range(3):
            packet = av.Packet(b"DEVC" + b"GPS5" + struct.pack(">iiiii", 410082000, 289784000, 50, 0, 0))
            packet.stream = data
            packet.pts = packet.dts = i * 300
            packet.duration = 300
            packet.time_base = fractions.Fraction(1, 1000)
            out.mux(packet)
    for sub, cues in subs:
        for i, (start, end, text) in enumerate(cues):
            line = f"{i},0,Default,,0,0,0,,{text}".encode()
            packet = sub.codec_context.encode_subtitle(av.subtitles.subtitle.SubtitleSet.create(
                line, 0, end - start, pts=start * 1000))
            packet.stream = sub
            packet.pts = packet.dts = start
            packet.duration = end - start
            packet.time_base = fractions.Fraction(1, 1000)
            out.mux(packet)
    out.close()
    return path


def subtitle_lines(path: str) -> list[tuple[str, str, list[str]]]:
    """The file's subtitle tracks as (codec, language, plain text lines)."""
    from fotodit.core.video import subtitles

    result = []
    with av.open(path) as c:
        streams = [s for s in c.streams if s.type == "subtitle"]
        texts = {s.index: [] for s in streams}
        for packet in c.demux(*streams):
            if packet.size:
                texts[packet.stream.index].extend(t for t in subtitles.packet_texts(packet.stream, packet) if t)
        for s in streams:
            result.append((subtitles.codec_name(s), s.metadata.get("language", ""), texts[s.index]))
    return result


def _add_audio(out, stream, seconds: float) -> None:
    rate = stream.codec_context.sample_rate
    layout = stream.codec_context.layout.name
    channels = 1 if layout == "mono" else 2
    total = int(rate * seconds)
    t = np.arange(total) / rate
    tone = (np.sin(2 * np.pi * 440 * t) * 0.2).astype(np.float32)
    data = np.tile(tone, (channels, 1))
    size = stream.codec_context.frame_size or 1024
    pts = 0
    for start in range(0, total, size):
        chunk = np.ascontiguousarray(data[:, start:start + size])
        frame = av.AudioFrame.from_ndarray(chunk, format="fltp", layout=layout)
        frame.sample_rate = rate
        frame.pts = pts
        frame.time_base = fractions.Fraction(1, rate)
        pts += chunk.shape[1]
        for packet in stream.encode(frame):
            out.mux(packet)
    for packet in stream.encode():
        out.mux(packet)


def first_frame(path: str):
    """The first frame's image (PIL) and its rotation (degrees clockwise)."""
    with av.open(path) as c:
        for frame in c.decode(video=0):
            return frame.to_image(), (-(frame.rotation or 0)) % 360
    raise AssertionError("no frames")


def upright_frame(path: str):
    """The first frame as displayed (orientation applied)."""
    from PIL import Image

    im, rotation = first_frame(path)
    turns = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    return im.transpose(turns[rotation]) if rotation else im


# ------------------------------------------------------------ patched into a built file


def _traks(data: bytes):
    """(start, end) of each 'trak' box's contents in an MP4/MOV."""
    for typ, cs, ce, _ in _boxes(data, 0, len(data)):
        if typ == b"moov":
            return [(s, e) for t, s, e, _ in _boxes(data, cs, ce) if t == b"trak"]
    raise AssertionError("no moov")


def _child(data: bytes, start: int, end: int, *path: bytes) -> tuple[int, int]:
    for name in path:
        start, end = next((s, e) for t, s, e, _ in _boxes(data, start, end) if t == name)
    return start, end


def set_sample_entry(path: str, track: int, entry: bytes) -> None:
    """Renames the sample entry of the `track`-th track (0-based), e.g. a GoPro 'gpmd' track to a DJI 'djmd'
    one, or an AAC track to Apple's spatial audio 'apac' (its 'esds' is hidden too: FFmpeg would read AAC
    from it), which FFmpeg knows no decoder for."""
    with open(path, "r+b") as fh:
        data = bytearray(fh.read())
        s, e = _child(data, *_traks(data)[track], b"mdia", b"minf", b"stbl", b"stsd")
        data[s + 12:s + 16] = entry
        esds = data.find(b"esds", s, e)
        if esds > 0 and entry != b"mp4a":
            data[esds:esds + 4] = b"free"
        fh.seek(0)
        fh.write(data)


HFLIP = (-0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)  # QuickTime Player's "Flip Horizontal"


def set_matrix(path: str, matrix: tuple[int, ...]) -> None:
    """Writes another display matrix into the video track's header (tkhd); the translation follows the
    picture's width so that a mirrored picture stays in place."""
    with open(path, "r+b") as fh:
        data = bytearray(fh.read())
        for start, end in _traks(data):
            s, _ = _child(data, start, end, b"tkhd")
            at = s + (52 if data[s] == 1 else 40)
            width = struct.unpack_from(">I", data, at + 36)[0]
            if width:  # the video track
                values = list(matrix)
                if values[0] < 0:
                    values[6] = width
                struct.pack_into(">9i", data, at, *values)
        fh.seek(0)
        fh.write(data)


def avif_exif(tags: dict[int, str]) -> bytes:
    """An AVI 'strd' chunk's contents as Fujifilm/Pentax cameras write them: "AVIF", 4 bytes, then a
    little-endian EXIF IFD of text tags (e.g. 0x010F make, 0x0110 model, 0x0132 date)."""
    head = 2 + 12 * len(tags) + 4
    entries, values = b"", b""
    for tag, text in tags.items():
        raw = text.encode() + b"\x00"
        entries += struct.pack("<HHII", tag, 2, len(raw), head + len(values))
        values += raw + b"\x00" * (len(raw) & 1)
    return b"AVIF" + bytes(4) + struct.pack("<H", len(tags)) + entries + bytes(4) + values


def add_strd(path: str, payload: bytes) -> None:
    """Puts `payload` into the video stream header of an AVI made by FFmpeg as an 'strd' chunk, where camera
    AVIs keep their EXIF (in the place of FFmpeg's JUNK placeholder: nothing else moves)."""
    with open(path, "r+b") as fh:
        data = bytearray(fh.read())
        pos = data.index(b"strl") + 4
        while data[pos:pos + 4] != b"JUNK":
            pos += 8 + struct.unpack_from("<I", data, pos + 4)[0]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        assert len(payload) <= size
        data[pos:pos + 4] = b"strd"
        data[pos + 8:pos + 8 + size] = payload.ljust(size, b"\x00")
        fh.seek(0)
        fh.write(data)


def make_dv(path: str, when: str = "2003-05-17T14:22:33Z", frames_n: int = 10) -> str:
    """A DV camcorder clip in AVI (720×576, 25 fps, PCM sound) whose frames carry the recording date and
    time (`when`, in the VAUX, AAUX and subcode packs), as a tape captured from a camcorder."""
    raw = path + ".dv"
    out = av.open(raw, "w", format="dv")
    out.metadata["creation_time"] = when  # FFmpeg's DV muxer writes it into every frame
    v = out.add_stream("dvvideo", rate=25)
    v.width, v.height, v.pix_fmt = 720, 576, "yuv420p"
    a = out.add_stream("pcm_s16le", rate=48000, layout="stereo")
    for i, img in enumerate(frames(720, 576, frames_n)):
        out.mux(v.encode(av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")))
        sound = av.AudioFrame.from_ndarray(np.zeros((1, 1920 * 2), np.int16), format="s16", layout="stereo")
        sound.sample_rate, sound.pts, sound.time_base = 48000, i * 1920, fractions.Fraction(1, 48000)
        out.mux(a.encode(sound))
    out.mux(v.encode())
    out.mux(a.encode())
    out.close()
    with av.open(raw) as inp, av.open(path, "w", format="avi") as avi:
        streams = {s.index: avi.add_stream_from_template(s) for s in inp.streams}
        for packet in inp.demux():
            if packet.dts is not None:
                packet.stream = streams[packet.stream.index]
                avi.mux(packet)
    os.remove(raw)
    return path
