"""Flipping, rotation, straightening, cropping, RAW exposure and video trimming (edits).

An edit is defined on the photo or video as it is displayed (orientation applied) and is applied to
the pixels only when processing starts; until then the file is not touched. Order of application:

1. horizontal flip ("Yatay çevir"): left and right swap places (pixels are only rearranged, lossless),
2. 90° rotation (lossless as well),
3. straightening: a fine rotation about the center (−45°…+45°); the canvas size does not change,
4. crop: the box is stored in the 0–1 range relative to the rotated canvas, so it covers the same
   region in the thumbnail, in the preview and at full resolution.

Flipping the edited result once more on screen is an edit too (`Edit.mirrored`): a flip turns the
rotations and the straightening the other way and mirrors the crop box, so the flip can stay first.

When straightening, the crop box is always kept inside the rotated image so that no empty areas
(outside the image) remain in the corners. If no box is given, the largest box with the image's
aspect ratio is used (automatic crop).

The other edits are not geometric. A photo's exposure is in EV stops: a RAW is developed from its
RAW data with it (see `rawdev`), other photos are brightened or darkened in linear light
(`apply_exposure`). A video's trim is the part that is kept and its cuts are parts removed from
the middle, in seconds from the start; `mute` leaves its audio out (see `video.process`).

Coordinates are continuous pixel coordinates: a W×H image covers [0, W]×[0, H] and the center of
pixel i is at i + 0.5 (same as Pillow's affine transform).
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass, replace

from PIL import Image

from ..i18n import decimal, decimal_mark, t

MAX_ANGLE = 45.0
MAX_EXPOSURE = 3.0  # EV
MIN_CUT = 0.05  # s: a shorter removed part is ignored
_EPS = 1e-6

Box = tuple[float, float, float, float]
Trim = tuple[float, float | None]  # (start, end) in seconds; end None: to the end of the video
Cut = tuple[float, float]  # a part removed from a video, (start, end) in seconds

# Clockwise 90°·n (Pillow's ROTATE_90 is counterclockwise).
QUARTER = {1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_90}
# A horizontal flip followed by clockwise 90°·n, as one transpose.
MIRRORED_QUARTER = {
    0: Image.Transpose.FLIP_LEFT_RIGHT, 1: Image.Transpose.TRANSVERSE, 2: Image.Transpose.FLIP_TOP_BOTTOM,
    3: Image.Transpose.TRANSPOSE,
}
_HIGH_BIT = ("I;16", "I;16L", "I;16B", "I;16N")


def _number(value: float) -> str:
    return decimal(abs(value), trim=True)


def ev_text(ev: float) -> str:
    """Exposure for the UI: "+0,7 EV", "−1,3 EV", "0 EV" (with the language's decimal mark)."""
    if not ev:
        return "0 EV"
    return f"{'+' if ev > 0 else '−'}{decimal(abs(ev))} EV"


def clock(seconds: float) -> str:
    """A point in a video for the UI: "0:05", "1:02:03", with tenths when needed ("0:05,3")."""
    tenths = int(math.floor(max(0.0, seconds) * 10 + 0.5))  # halves round up (0,25 s → 0:00,3)
    whole, frac = divmod(tenths, 10)
    h, rest = divmod(whole, 3600)
    m, s = divmod(rest, 60)
    text = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return f"{text}{decimal_mark()}{frac}" if frac else text


@dataclass(frozen=True)
class Edit:
    quarter: int = 0  # number of clockwise 90° steps (0–3)
    angle: float = 0.0  # straightening angle in degrees (+: clockwise)
    crop: Box | None = None  # (left, top, right, bottom), 0–1 relative to the rotated canvas
    exposure: float = 0.0  # photo exposure in EV (stops): +1 is twice as bright
    trim: Trim | None = None  # video: the part that is kept
    cuts: tuple[Cut, ...] = ()  # video: parts removed from the middle (sorted, not overlapping)
    mute: bool = False  # video: the audio is removed
    mirror: bool = False  # flipped left-right, before everything else

    def __post_init__(self) -> None:
        object.__setattr__(self, "quarter", int(self.quarter) % 4)
        angle = max(-MAX_ANGLE, min(MAX_ANGLE, round(float(self.angle), 2)))
        object.__setattr__(self, "angle", 0.0 if abs(angle) < 0.05 else angle)
        crop = self.crop
        if crop is not None:
            x0, y0, x1, y1 = (round(min(1.0, max(0.0, float(v))), 6) for v in crop)
            x0, x1 = min(x0, x1), max(x0, x1)
            y0, y1 = min(y0, y1), max(y0, y1)
            full = x0 <= 1e-4 and y0 <= 1e-4 and x1 >= 1 - 1e-4 and y1 >= 1 - 1e-4
            crop = None if full or x1 - x0 < 1e-3 or y1 - y0 < 1e-3 else (x0, y0, x1, y1)
        object.__setattr__(self, "crop", crop)
        exposure = max(-MAX_EXPOSURE, min(MAX_EXPOSURE, round(float(self.exposure), 2)))
        object.__setattr__(self, "exposure", 0.0 if abs(exposure) < 0.005 else exposure)
        trim = self.trim
        if trim is not None:
            start = max(0.0, round(float(trim[0]), 3))
            end = None if trim[1] is None else round(float(trim[1]), 3)
            if end is not None and end <= start:
                end = None
            trim = None if not start and end is None else (start, end)
        object.__setattr__(self, "trim", trim)
        cuts: list[Cut] = []
        for a, b in sorted(tuple(sorted((max(0.0, round(float(a), 3)), max(0.0, round(float(b), 3)))))
                           for a, b in self.cuts):
            if b - a < MIN_CUT:
                continue
            if cuts and a <= cuts[-1][1]:  # overlapping or touching: one removed part
                cuts[-1] = (cuts[-1][0], max(cuts[-1][1], b))
            else:
                cuts.append((a, b))
        object.__setattr__(self, "cuts", tuple(cuts))
        object.__setattr__(self, "mute", bool(self.mute))
        object.__setattr__(self, "mirror", bool(self.mirror))

    # ------------------------------------------------------------------ queries

    def is_identity(self) -> bool:
        return (not self.has_geometry() and not self.exposure and self.trim is None and not self.cuts
                and not self.mute)

    def has_geometry(self) -> bool:
        """Does it flip, rotate, straighten or crop?"""
        return bool(self.mirror or self.quarter or self.angle or self.crop is not None)

    def geometry_only(self) -> Edit | None:
        """Only the flip, rotation, straightening and crop (None if there are none)."""
        return replace(self, exposure=0.0, trim=None, cuts=(), mute=False) if self.has_geometry() else None

    def photo_only(self) -> Edit | None:
        """The edit without its video-only parts (trim, cuts, mute); None if nothing is left."""
        edit = replace(self, trim=None, cuts=(), mute=False)
        return None if edit.is_identity() else edit

    def turn(self) -> Image.Transpose | None:
        """The flip and the 90° rotation as one Pillow transpose (None if there are none)."""
        return MIRRORED_QUARTER[self.quarter] if self.mirror else QUARTER.get(self.quarter)

    def segments(self, duration: float) -> list[tuple[float, float | None]]:
        """The kept parts of a video, in order (seconds): the trim's window without the cuts. If the
        duration is not known (0), the last part may end at None (the end of the video)."""
        start, end = self.trim_window(duration)
        parts: list[tuple[float, float | None]] = []
        cursor = start
        for a, b in self.cuts:
            if end is not None and a >= end:
                break
            if b <= cursor:
                continue
            if a > cursor:
                parts.append((cursor, a))
            cursor = b
        if end is None or cursor < end:
            parts.append((cursor, end))
        return parts

    def trim_window(self, duration: float) -> tuple[float, float | None]:
        """(start, end) of the kept part in seconds, for a video `duration` seconds long (end
        None: to the end, when the duration is not known)."""
        start, end = self.trim or (0.0, None)
        if duration > 0:
            end = duration if end is None else min(end, duration)
        return start, end

    def kept_duration(self, duration: float) -> float:
        """How long the video will be after trimming and cutting."""
        return sum(max(0.0, (duration if b is None else b) - a) for a, b in self.segments(duration))

    def tidied(self, duration: float, shortest: float = 0.0) -> Edit | None:
        """The same kept parts, written plainly: the removed parts cut to the trim (those outside it
        dropped), and kept parts shorter than `shortest` seconds removed as well. None if nothing
        would be left. A removed part stays one even at the start or the end: unlike a trim, it is
        removed for good (see `video.process`)."""
        start, end = self.trim_window(duration)
        kept = [(a, b) for a, b in self.segments(duration) if b is None or b - a >= shortest - _EPS]
        if not kept:
            return None
        cuts = [(kept[i][1], kept[i + 1][0]) for i in range(len(kept) - 1)]
        if kept[0][0] > start + _EPS:
            cuts.insert(0, (start, kept[0][0]))
        if kept[-1][1] is not None and end is not None and kept[-1][1] < end - _EPS:
            cuts.append((kept[-1][1], end))
        return replace(self, cuts=tuple(cuts))

    def swaps_axes(self) -> bool:
        """Do width and height swap (90° or 270°)?"""
        return self.quarter % 2 == 1

    def canvas_size(self, width: float, height: float) -> tuple[float, float]:
        """Size after the 90° rotation (the canvas)."""
        return (height, width) if self.swaps_axes() else (width, height)

    def box_on_canvas(self, cw: float, ch: float) -> Box:
        """Crop box in canvas pixels (fractional). When straightening, the box is always inside
        the rotated image."""
        if self.crop is None:
            if not self.angle:
                return 0.0, 0.0, float(cw), float(ch)
            return largest_box(cw, ch, self.angle, cw / ch)
        x0, y0, x1, y1 = self.crop
        box = (x0 * cw, y0 * ch, x1 * cw, y1 * ch)
        if self.angle and not box_fits(cw, ch, self.angle, box):
            box = shrink_to_fit(cw, ch, self.angle, box)
        return box

    def crop_box(self, width: float, height: float) -> Box:
        """Crop box (on the canvas) for a photo whose displayed size is `width`×`height`."""
        return self.box_on_canvas(*self.canvas_size(width, height))

    def pixels_on_canvas(self, cw: int, ch: int) -> tuple[float, float, int, int]:
        """(left, top, width, height): the output's position on the canvas and its pixel size.
        Without straightening all of them are integers (the crop follows pixel boundaries)."""
        x0, y0, x1, y1 = self.box_on_canvas(cw, ch)
        if not self.angle:
            left, top = int(round(x0)), int(round(y0))
            right, bottom = max(left + 1, int(round(x1))), max(top + 1, int(round(y1)))
            return left, top, right - left, bottom - top
        return x0, y0, max(1, int(round(x1 - x0))), max(1, int(round(y1 - y0)))

    def output_size(self, width: int, height: int) -> tuple[int, int]:
        """Pixel size of the processed result."""
        _, _, w, h = self.pixels_on_canvas(*(int(v) for v in self.canvas_size(width, height)))
        return w, h

    def detail_scale(self) -> float:
        """How many times larger the image must be decoded to show the cropped region at the
        same sharpness (for previews and thumbnails)."""
        if self.crop is None:
            return 1.0
        x0, y0, x1, y1 = self.crop
        return 1.0 / max(0.02, min(x1 - x0, y1 - y0))

    # --------------------------------------------------------------- changes

    def rotated(self, delta: int) -> Edit:
        """A copy rotated by 90°·delta (+: clockwise). The crop box rotates with the image; the
        straightening angle stays the same (rotations about the center commute)."""
        crop = self.crop
        if crop is not None:
            for _ in range(delta % 4):
                x0, y0, x1, y1 = crop
                crop = (1 - y1, x0, 1 - y0, x1)
        return replace(self, quarter=self.quarter + delta, crop=crop)

    def mirrored(self) -> Edit:
        """A copy whose result is this one's result flipped left-right, as seen on screen. The flip
        stays first, so the 90° rotation and the straightening turn the other way and the crop box is
        mirrored; the exposure and the video parts stay as they are."""
        crop = self.crop
        if crop is not None:
            x0, y0, x1, y1 = crop
            crop = (1 - x1, y0, 1 - x0, y1)
        return replace(self, mirror=not self.mirror, quarter=-self.quarter, angle=-self.angle, crop=crop)

    # ------------------------------------------------------------------ text

    def describe(self, future: bool = False) -> str:
        """UI text, e.g. "sağa 90° döndürüldü · eğikliği 2,5° düzeltildi · kırpıldı" ("rotated 90°
        right · straightened by 2.5° · cropped"); what will be done if `future`."""
        tense = "future" if future else "past"
        parts = []
        if self.mirror:
            parts.append(t(f"edit.flip.{tense}"))
        if self.quarter:
            parts.append(t(f"edit.rotate.{self.quarter}.{tense}"))
        if self.angle:
            parts.append(t(f"edit.straighten.{tense}", angle=_number(self.angle)))
        if self.crop is not None:
            parts.append(t(f"edit.crop.{tense}"))
        if self.exposure:
            parts.append(t(f"edit.exposure.{tense}", ev=ev_text(self.exposure)))
        if self.trim is not None or self.cuts:
            actions = []
            if self.trim is not None:
                start, end = self.trim
                which = "both" if start and end is not None else "start" if start else "end"
                actions.append(t(f"edit.trim.{which}.{tense}"))
            if self.cuts:
                actions.append(t(f"edit.cuts.{tense}", n=len(self.cuts)))
            parts.append(t("edit.kept", actions=", ".join(actions), kept=self._kept_text(limit=3)))
        if self.mute:
            parts.append(t(f"edit.mute.{tense}"))
        return " · ".join(parts)

    def short(self) -> str:
        """Short UI text for tables, e.g. "↻ 90° · ∡ 2,5° · kırpma" ("↻ 90° · ∡ 2.5° · crop")."""
        parts = []
        if self.mirror:
            parts.append(t("edit.short.flip"))
        if self.quarter:
            parts.append({1: "↻ 90°", 2: "↻ 180°", 3: "↺ 90°"}[self.quarter])
        if self.angle:
            parts.append(f"∡ {_number(self.angle)}°")
        if self.crop is not None:
            parts.append(t("edit.short.crop"))
        if self.exposure:
            parts.append(f"☀ {ev_text(self.exposure)}")
        if self.trim is not None or self.cuts:
            parts.append(f"✂ {self._kept_text(limit=2)}")
        if self.mute:
            parts.append(t("edit.short.mute"))
        return " · ".join(parts)

    def _kept_text(self, limit: int) -> str:
        """The kept parts ("0:02 – 0:05, 0:08 – son"); with more than `limit` parts, their number."""
        kept = self.segments(0.0)
        if len(kept) > limit:
            return t("edit.parts", n=len(kept))
        end = t("edit.end")
        return ", ".join(f"{clock(a)} – {clock(b) if b is not None else end}" for a, b in kept) or t("edit.none")


# ------------------------------------------------------------------ geometry


def _cos_sin(angle: float) -> tuple[float, float]:
    a = math.radians(angle)
    return math.cos(a), math.sin(a)


def inside(cw: float, ch: float, angle: float, x: float, y: float, eps: float = _EPS) -> bool:
    """Is the canvas point inside both the canvas and (when straightening) the rotated image?"""
    if x < -eps or y < -eps or x > cw + eps or y > ch + eps:
        return False
    if not angle:
        return True
    c, s = _cos_sin(angle)
    dx, dy = x - cw / 2, y - ch / 2
    # Canvas → unrotated image (inverse rotation).
    return abs(c * dx + s * dy) <= cw / 2 + eps and abs(-s * dx + c * dy) <= ch / 2 + eps


def box_fits(cw: float, ch: float, angle: float, box: Box, eps: float = _EPS) -> bool:
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return False
    return all(inside(cw, ch, angle, x, y, eps) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)))


def max_half_height(cw: float, ch: float, angle: float, aspect: float, cx: float, cy: float) -> float:
    """Largest half height that fits for a box centered at (cx, cy) with aspect ratio `aspect`
    (width/height). Every corner condition is linear in the half height, so it is computed in
    closed form; 0 if the center is outside the image."""
    limits = [cx / aspect, (cw - cx) / aspect, cy, ch - cy]
    if angle:
        c, s = _cos_sin(angle)
        dx, dy = cx - cw / 2, cy - ch / 2
        u0, v0 = c * dx + s * dy, -s * dx + c * dy
        if abs(u0) > cw / 2 + _EPS or abs(v0) > ch / 2 + _EPS:
            return 0.0
        for sx in (-1, 1):
            for sy in (-1, 1):
                u1 = c * sx * aspect + s * sy
                v1 = -s * sx * aspect + c * sy
                for q0, q1, half in ((u0, u1, cw / 2), (v0, v1, ch / 2)):
                    if q1 > _EPS:
                        limits.append((half - q0) / q1)
                    elif q1 < -_EPS:
                        limits.append((half + q0) / -q1)
    return max(0.0, min(limits))


def largest_box(cw: float, ch: float, angle: float, aspect: float, center: tuple[float, float] | None = None) -> Box:
    """Largest box with the given aspect ratio around the given center (default: canvas center)."""
    cx, cy = center if center is not None else (cw / 2, ch / 2)
    t = max_half_height(cw, ch, angle, aspect, cx, cy)
    return cx - aspect * t, cy - t, cx + aspect * t, cy + t


def shrink_to_fit(cw: float, ch: float, angle: float, box: Box) -> Box:
    """Shrinks a box that does not fit, keeping its center and aspect ratio; if its center is
    outside the image, the largest centered box is used."""
    x0, y0, x1, y1 = box
    w, h = max(x1 - x0, _EPS), max(y1 - y0, _EPS)
    aspect = w / h
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    t = min(h / 2, max_half_height(cw, ch, angle, aspect, cx, cy))
    if t < h / 2 * 0.05:
        return largest_box(cw, ch, angle, aspect)
    return cx - aspect * t, cy - t, cx + aspect * t, cy + t


# ------------------------------------------------------------------ applying


def apply(im: Image.Image, edit: Edit | None) -> Image.Image:
    """Applies the edit's geometry to the (already oriented) image. The flip, 90° rotation and crops
    without straightening are lossless and work in every pixel mode; straightening uses bicubic
    resampling (with premultiplied alpha)."""
    if edit is None or not edit.has_geometry():
        return im
    turn = edit.turn()
    if turn is not None:
        im = im.transpose(turn)
    w, h = im.size
    left, top, ow, oh = edit.pixels_on_canvas(w, h)
    if not edit.angle:
        if (left, top, ow, oh) == (0, 0, w, h):
            return im
        return im.crop((left, top, left + ow, top + oh))

    x0, y0, x1, y1 = edit.box_on_canvas(w, h)
    sx, sy = (x1 - x0) / ow, (y1 - y0) / oh
    c, s = _cos_sin(edit.angle)
    cx, cy = w / 2, h / 2
    # Output pixel → canvas (x0 + u·sx, y0 + v·sy) → inverse rotation → source image.
    data = (
        c * sx, s * sy, cx + c * (x0 - cx) + s * (y0 - cy),
        -s * sx, c * sy, cy - s * (x0 - cx) + c * (y0 - cy),
    )
    mode = im.mode
    work = im
    if mode in ("P", "PA"):
        work = im.convert("RGBA" if im.has_transparency_data else "RGB")
    elif mode == "1":
        work = im.convert("L")
    elif mode in _HIGH_BIT:
        work = im.convert("I")
    out = work.transform((ow, oh), Image.Transform.AFFINE, data, resample=Image.Resampling.BICUBIC)
    if mode in _HIGH_BIT:
        import numpy as np

        # Bicubic resampling can overshoot the range at sharp edges: clip to 16 bits.
        out = Image.fromarray(np.clip(np.asarray(out), 0, 65535).astype(np.uint16))
    return out


# ------------------------------------------------------------------ exposure


@functools.lru_cache(maxsize=64)
def exposure_curve(ev: float, top: int = 255) -> tuple[int, ...]:
    """Lookup table for `ev` stops of exposure on sRGB-encoded values 0…`top`: the value is made
    linear (sRGB curve), multiplied by 2^ev and encoded again. When brightening, the highlights roll
    off along the same soft shoulder as a RAW development (see `rawdev.exposure_lut`)."""
    gain = 2.0 ** ev
    knee = 1.0 - 0.25 * min(1.0, ev) if ev > 0 else None
    table = []
    for i in range(top + 1):
        v = i / top
        x = (v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4) * gain
        if knee is not None and x > knee:
            x = knee + (1.0 - knee) * -math.expm1(-(x - knee) / (1.0 - knee))
        x = min(max(x, 0.0), 1.0)
        s = x * 12.92 if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
        table.append(int(round(s * top)))
    return tuple(table)


def apply_exposure(im: Image.Image, ev: float) -> Image.Image:
    """Brightens (ev > 0) or darkens a photo that is not a RAW, in linear light. Transparency stays as
    it is; palette images get a new palette; CMYK is adjusted through its inverted (light) values.
    Detail that is pure white or black in the file cannot come back (unlike with a RAW)."""
    if not ev:
        return im
    mode = im.mode
    lut = list(exposure_curve(ev))
    same = list(range(256))
    if mode in ("RGB", "L"):
        return im.point(lut * len(im.getbands()))
    if mode in ("RGBA", "LA"):
        return im.point(lut * (len(im.getbands()) - 1) + same)
    if mode in ("P", "PA"):
        out = im.copy()
        rawmode = out.palette.mode if out.palette is not None else "RGB"
        palette = out.getpalette(rawmode)
        if palette:  # (the alpha of an RGBA palette stays as it is)
            step = len(rawmode)
            out.putpalette([v if i % step == 3 else lut[v] for i, v in enumerate(palette)], rawmode)
        return out
    if mode == "CMYK":
        inverse = [255 - lut[255 - v] for v in range(256)]
        return im.point(inverse * 4)
    if mode in _HIGH_BIT:
        import numpy as np

        table = np.asarray(exposure_curve(ev, 65535), dtype=np.uint16)
        return Image.fromarray(table[np.asarray(im, dtype=np.uint16)])
    if mode == "1":
        return im  # black and white only: nothing to brighten
    from .imaging import to_color_mode

    return apply_exposure(to_color_mode(im) if mode not in ("LAB",) else im.convert("RGB"), ev)
