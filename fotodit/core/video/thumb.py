"""A representative frame from a video, for thumbnails and the preview.

The frame is taken a little after the start of the video (at most 1 s in); the first
frames are often black or blurry. The orientation (portrait shots) is applied, and HDR
videos are tone-mapped to SDR so they look right on screen (see `tonemap`).
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import numpy as np
from PIL import Image

from .probe import ATTACHED_PIC, frame_orientation
from .tonemap import HLG, PQ, tonemap

POSTER_AT = 1.0  # seconds (10% of the duration for short videos)
_TRC_HDR = {18: HLG, 16: PQ}
_MATRIX = {1: "ITU709", 5: "ITU601", 6: "SMPTE170M", 7: "SMPTE240M", 9: "BT2020", 10: "BT2020"}
_TRANSPOSE = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}

_lock = threading.Lock()
_durations: OrderedDict[tuple[str, float], float] = OrderedDict()


def remember_duration(path: str, mtime: float, seconds: float) -> None:
    with _lock:
        _durations[(path, mtime)] = seconds
        while len(_durations) > 20000:
            _durations.popitem(last=False)


def known_duration(path: str, mtime: float) -> float | None:
    with _lock:
        return _durations.get((path, mtime))


def colorspace_of(frame) -> str:
    cs = _MATRIX.get(frame.colorspace)
    if cs:
        return cs
    return "ITU709" if frame.height > 576 else "ITU601"


def frame_to_image(frame, box: int | None = None) -> Image.Image:
    """Decoded frame → RGB image (at most `box` pixels, orientation applied, tone-mapped if HDR)."""
    w, h = frame.width, frame.height
    if box and max(w, h) > box:
        scale = box / max(w, h)
        w, h = max(2, int(round(w * scale))), max(2, int(round(h * scale)))
    hdr = _TRC_HDR.get(frame.color_trc)
    if hdr:
        rgb = frame.reformat(width=w, height=h, format="rgb48le", src_colorspace="BT2020",
                             interpolation="AREA").to_ndarray()
        signal = rgb.astype(np.float32) / 65535.0
        out = np.clip(tonemap(signal, hdr) * 255.0 + 0.5, 0, 255).astype(np.uint8)
        im = Image.fromarray(out, "RGB")
    else:
        im = frame.reformat(width=w, height=h, format="rgb24", src_colorspace=colorspace_of(frame),
                            interpolation="AREA").to_image()
    rotation, mirrored = frame_orientation(frame)
    if mirrored:
        im = im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    return im.transpose(_TRANSPOSE[rotation]) if rotation in _TRANSPOSE else im


def poster(path: str, box: int | None = None, mtime: float | None = None, start: float = 0.0) -> Image.Image | None:
    """The frame for the video's thumbnail; None if the video cannot be opened. `start`: the start
    of the part that is kept (seconds), for a trimmed video."""
    import av

    from . import quiet_ffmpeg

    quiet_ffmpeg()

    try:
        frame = _frame_near(path, mtime, seek=True, start=start)
        if frame is None:
            # In MPEG-TS/AVI the seek sometimes lands past the keyframe and no frame decodes:
            # then read from the start.
            frame = _frame_near(path, None, seek=False, start=start)
        return frame_to_image(frame, box) if frame is not None else None
    except (av.error.FFmpegError, OSError, ValueError):
        return None


def _frame_near(path: str, mtime: float | None, seek: bool, start: float = 0.0):
    """The frame ~1 s after `start` (10% of the rest for a short video)."""
    import av

    with av.open(path) as container:
        video = next((s for s in container.streams.video if not s.disposition & ATTACHED_PIC), None)
        if video is None:
            return None
        duration = (container.duration or 0) / 1_000_000
        if mtime is not None:
            remember_duration(path, mtime, duration)
        first = float(video.start_time * video.time_base) if video.start_time is not None and video.time_base else 0.0
        start = min(max(0.0, start), max(0.0, duration - 0.05))
        at = first + start + min(POSTER_AT, (duration - start) * 0.1)
        if seek and at - first > 0.05 and video.time_base:
            container.seek(int(at / video.time_base), stream=video)
        found = None
        for n, frame in enumerate(container.decode(video)):
            found = frame
            if frame.time is None or frame.time >= at - 0.02 or (n >= 90 and not start):
                break
        return found
