"""The GPU (hardware) video encoder: NVIDIA NVENC, Intel Quick Sync, AMD AMF or
Windows Media Foundation (whichever is available). Speeds up re-encoding.

These encoders depend on the graphics driver: settings that work on one computer
may fail to open on another, and old drivers can produce corrupted video. Therefore:

* The working encoder is found with a small test encode (once per session):
  NVENC, Quick Sync, AMF and Media Foundation are tried in that order; the test
  encode is also decoded back to check that the picture comes out right.
* If during processing the encoder fails to open, reports an error, or the first
  frame of the output differs noticeably from the source, the video is re-encoded
  with x264 on the CPU (see `process.process_video`); an encoder that produced
  corrupted output is not used for the rest of the session.

If a (10-bit) HEVC encoder is found, cropped/straightened HDR videos can stay HDR
with the "keep codec" option (`process.VIDEO_KEEP`); x265 on the CPU is too slow for this.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from fractions import Fraction


@dataclass(frozen=True)
class Encoder:
    name: str  # FFmpeg encoder (h264_nvenc …)
    label: str  # name shown to the user
    codec: str  # "h264" | "hevc"
    options: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    pix_fmt: str = "nv12"
    bit_rate: int | None = None  # 0: constant quality (no NVENC default bitrate cap)

    @property
    def settings(self) -> dict[str, str]:
        return dict(self.options)


def _opts(**kw: str) -> tuple[tuple[str, str], ...]:
    return tuple(kw.items())


# Quality chosen to be close to x264 at CRF 18 (measurements: docs/RESEARCH.md §6e).
CANDIDATES: dict[str, tuple[Encoder, ...]] = {
    "h264": (
        Encoder("h264_nvenc", "NVIDIA NVENC", "h264",
                _opts(preset="p5", tune="hq", rc="vbr", cq="23", bf="2"), bit_rate=0),
        Encoder("h264_qsv", "Intel Quick Sync", "h264", _opts(preset="medium", global_quality="23")),
        Encoder("h264_amf", "AMD AMF", "h264",
                _opts(usage="transcoding", quality="quality", rc="cqp", qp_i="20", qp_p="22", qp_b="24")),
        Encoder("h264_mf", "Windows Media Foundation", "h264",
                _opts(hw_encoding="1", rate_control="quality", quality="80")),
    ),
    "hevc": (
        Encoder("hevc_nvenc", "NVIDIA NVENC", "hevc",
                _opts(preset="p5", tune="hq", rc="vbr", cq="24", profile="main10"), "p010le", bit_rate=0),
        Encoder("hevc_qsv", "Intel Quick Sync", "hevc", _opts(preset="medium", global_quality="24", profile="main10"),
                "p010le"),
        Encoder("hevc_amf", "AMD AMF", "hevc",
                _opts(usage="transcoding", quality="quality", rc="cqp", qp_i="22", qp_p="24", profile="main10"),
                "p010le"),
    ),
}

_lock = threading.Lock()
_found: dict[str, Encoder | None] = {}
_broken: set[str] = set()


def find(codec: str = "h264") -> Encoder | None:
    """The first GPU encoder that works on this computer. Tested on the first call (may take
    a second or two; do not call from the UI thread), then remembered."""
    with _lock:
        if codec not in _found:
            _found[codec] = next((e for e in CANDIDATES.get(codec, ()) if works(e)), None)
        encoder = _found[codec]
    return None if encoder is None or encoder.name in _broken else encoder


def peek(codec: str = "h264") -> Encoder | None:
    """Without testing: the encoder found earlier (None if not tested yet)."""
    encoder = _found.get(codec)
    return None if encoder is None or encoder.name in _broken else encoder


def detected(codec: str = "h264") -> bool:
    return codec in _found


def mark_broken(encoder: Encoder) -> None:
    """An encoder that produced corrupted output is not used for the rest of the session."""
    _broken.add(encoder.name)


def works(encoder: Encoder) -> bool:
    """Test encode: does it open, does it produce frames, is the decoded-back picture right?"""
    import av
    import numpy as np

    from . import quiet_ffmpeg

    quiet_ffmpeg()
    w, h = 320, 240
    image = np.zeros((h, w, 3), np.uint8)
    image[:, : w // 2] = 40
    image[:, w // 2:] = 210
    try:
        ctx = av.CodecContext.create(encoder.name, "w")
        ctx.width, ctx.height, ctx.pix_fmt = w, h, encoder.pix_fmt
        ctx.time_base = Fraction(1, 30)
        ctx.framerate = Fraction(30)
        if encoder.bit_rate is not None:
            ctx.bit_rate = encoder.bit_rate
        ctx.options = encoder.settings
        packets = []
        for i in range(8):
            frame = av.VideoFrame.from_ndarray(image, format="rgb24").reformat(format=encoder.pix_fmt)
            frame.pts = i
            packets.extend(ctx.encode(frame))
        packets.extend(ctx.encode(None))
        decoder = av.CodecContext.create(encoder.codec, "r")
        frames = []
        for packet in packets:
            frames.extend(decoder.decode(packet))
        try:
            frames.extend(decoder.decode(None))
        except av.error.EOFError:
            pass
        if len(frames) < 4:
            return False
        gray = frames[0].reformat(format="gray").to_ndarray().astype(np.float32)
        return float(gray[:, : w // 2 - 16].mean()) < 90 and float(gray[:, w // 2 + 16:].mean()) > 160
    except Exception:  # noqa: BLE001 - no such encoder, the driver lacks support, or it rejects the settings
        return False


def reset() -> None:
    """(For tests) forgets the encoders found and those marked broken."""
    with _lock:
        _found.clear()
        _broken.clear()
