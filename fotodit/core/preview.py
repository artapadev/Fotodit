"""Preparing images for the large preview (independent of the UI).

The same rules as for JPG conversion apply: the EXIF orientation is applied to the pixels and the
embedded color profile (iPhone Display P3, Adobe RGB, CMYK, HEIC 'nclx' …) is converted to sRGB, so
the preview looks the same as the processed result will. Images are decoded only as large as the
screen needs: JPEGs use fast 1/2, 1/4, 1/8 scaled decoding; for RAWs the camera's embedded preview
is shown; for videos, a frame from the start.
"""

from __future__ import annotations

import io

from PIL import Image

from ..i18n import t
from . import colors
from .errors import explain
from .formats import Fmt, is_video, sniff_file
from .imaging import apply_orientation, open_image, to_color_mode

FULL_LIMIT = 8192  # longest side in the 100% view (so huge panoramas do not fill up memory)


class PreviewError(Exception):
    """The preview could not be produced; the message is shown to the user."""


def _fit(im: Image.Image, limit: int) -> Image.Image:
    if max(im.size) > limit:
        im = im.copy() if im.readonly else im
        im.thumbnail((limit, limit), Image.Resampling.LANCZOS, reducing_gap=3.0)
    return im


def render(path: str, box: int | None = None) -> Image.Image:
    """Image prepared for display: correctly oriented, in sRGB colors, with the long side at most
    `box` pixels (None: full resolution, at most FULL_LIMIT). Transparency is kept (RGBA/LA); the
    returned mode is RGB, RGBA, L or LA."""
    limit = min(box, FULL_LIMIT) if box else FULL_LIMIT
    try:
        fmt = sniff_file(path)
    except OSError as exc:
        raise PreviewError(t("error.read", error=exc.strerror or exc)) from exc
    if fmt is None:
        raise PreviewError(t("error.unsupported"))
    if fmt == Fmt.RAW:
        return _render_raw(path, limit)
    if is_video(fmt):
        from .video.thumb import poster

        im = poster(path, limit)  # a frame from the start of the video (orientation applied, HDR tone-mapped)
        if im is None:
            raise PreviewError(t("preview.video_unsupported"))
        return im
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise PreviewError(t("error.read", error=exc.strerror or exc)) from exc
    try:
        from .processor import source_profile

        im = open_image(io.BytesIO(data))
        w, h = im.size
        if fmt == Fmt.JPEG and max(w, h) > limit:
            # Fast 1/2, 1/4, 1/8 scaled decoding. The target must keep the aspect ratio: with a square
            # box the short side limits the scale and a 12 MP photo gets decoded at full size.
            # Only the scale is requested; CMYK/grayscale JPEGs stay as they are.
            scale = limit / max(w, h)
            im.draft("RGB", (max(1, int(w * scale)), max(1, int(h * scale))))
        im.load()
        icc = source_profile(im, data, fmt)
        px = to_color_mode(apply_orientation(im))
        px = _fit(px, limit)  # color conversion on the downscaled image (fast)
        px, _ = colors.to_srgb(px, icc)
        return px if px.mode in ("RGB", "RGBA", "L", "LA") else px.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - broken file: the preview shows an error message
        raise PreviewError(explain(exc)) from exc


def _render_raw(path: str, limit: int) -> Image.Image:
    from .rawdev import thumbnail

    # If the embedded preview is smaller than half the needed size, the RAW data is developed (half size).
    im = thumbnail(path, limit, min_fraction=0.5)
    if im is None:
        raise PreviewError(t("preview.raw_none"))
    im = to_color_mode(im)
    return im if im.mode in ("RGB", "L") else im.convert("RGB")
