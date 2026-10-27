"""Quick info for the selected photos and videos: format, size, capture date, camera, GPS.

When opening a file Pillow reads only the headers (it does not decode the pixels), so this is fast
even for thousands of photos. The full byte-level scan (`meta.scan_file`) is done only in the info
panel and during processing.
"""

from __future__ import annotations

import functools
import mmap
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..i18n import t
from .errors import explain
from .formats import Fmt, is_video, sniff_file
from .imaging import open_image
from .meta.exif import (
    TAG_MAKE,
    TAG_MODEL,
    TAG_ORIENTATION,
    camera_name,
    capture_datetime,
    gps_coordinates,
)
from .meta.xmp import xmp_datetime

DATE_EXIF = "exif"
DATE_XMP = "xmp"
DATE_FILENAME = "filename"
DATE_MTIME = "mtime"
DATE_VIDEO = "video"

DATE_SOURCE_LABELS = {  # keys of the texts
    DATE_EXIF: "date_source.exif",
    DATE_XMP: "date_source.xmp",
    DATE_VIDEO: "date_source.video",
    DATE_FILENAME: "date_source.filename",
    DATE_MTIME: "date_source.mtime",
}


@dataclass
class PhotoInfo:
    fmt: Fmt | None = None
    width: int = 0
    height: int = 0
    taken: datetime | None = None
    taken_source: str | None = None
    camera: str | None = None
    has_gps: bool = False
    error: str | None = None
    raw_error: str | None = None  # LibRaw cannot develop it: a problem only for converting it to JPG
    video: object | None = None  # for videos: fotodit.core.video.probe.VideoInfo (duration, codec, orientation …)

    @property
    def is_video(self) -> bool:
        return self.video is not None or is_video(self.fmt)


def read_info(path: str) -> PhotoInfo:
    info = PhotoInfo()
    try:
        info.fmt = sniff_file(path)
    except OSError as exc:
        info.error = t("error.read", error=exc.strerror or exc)
        return info
    if info.fmt is None:
        info.error = t("error.unsupported")
        return info
    if info.fmt == Fmt.RAW:
        return _read_raw(path, info)
    if is_video(info.fmt):
        return _read_video(path, info)
    try:
        with open_image(path) as im:
            info.width, info.height = im.size
            ex = im.getexif()
            # Pillow gives a TIFF's size already turned (it applies the orientation as it loads).
            if ex.get(TAG_ORIENTATION) in (5, 6, 7, 8) and im.format != "TIFF":
                info.width, info.height = info.height, info.width
            taken, ms = capture_datetime(ex)
            if taken:
                info.taken, info.taken_source = taken.replace(microsecond=ms * 1000), DATE_EXIF
            info.camera = camera_name(ex.get(TAG_MAKE), ex.get(TAG_MODEL))
            try:
                gps = ex.get_ifd(0x8825)
            except Exception:
                gps = {}
            info.has_gps = bool(gps) and (gps_coordinates(gps) is not None or len(gps) > 1)
            xmp = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
            if isinstance(xmp, str):
                xmp = xmp.encode("utf-8", "ignore")
            if xmp:
                if info.taken is None:
                    xt = xmp_datetime(xmp)
                    if xt:
                        info.taken, info.taken_source = xt, DATE_XMP
                if b"GPSLatitude" in xmp:
                    info.has_gps = True
    except Exception as exc:  # noqa: BLE001 - a broken file is shown as an error in the list
        info.error = explain(exc)
    return info


def _read_video(path: str, info: PhotoInfo) -> PhotoInfo:
    """Video: duration, size (orientation applied), capture date, device, location (see fotodit.core.video.probe)."""
    from .video.probe import ProbeError, probe

    try:
        video = probe(path, info.fmt)
    except ProbeError as exc:
        info.error = str(exc)
        return info
    except Exception as exc:  # noqa: BLE001 - a broken file is shown as an error in the list
        info.error = t("error.video_open", error=explain(exc))
        return info
    info.video = video
    info.width, info.height = video.display_size
    if video.taken is not None:
        info.taken, info.taken_source = video.taken, DATE_VIDEO
    info.camera = video.camera
    info.has_gps = video.has_gps
    return info


def _read_raw(path: str, info: PhotoInfo) -> PhotoInfo:
    """RAW: the metadata is read with our own parser (memory-mapped), the size with LibRaw.
    Pillow is not used; for TIFF-based RAWs it sees only the small preview."""
    from .meta import raw as raw_meta

    try:
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            parts = raw_meta.exif_parts(mm)
    except (OSError, ValueError):
        parts = None
    if parts:
        try:
            info.camera = raw_meta.camera_of(parts)
        except Exception:  # noqa: BLE001 - an unreadable make/model leaves the camera empty
            info.camera = None
        try:
            taken = raw_meta.taken_of(parts)
        except Exception:  # noqa: BLE001 - an unreadable date leaves it empty
            taken = None
        if taken:
            info.taken, info.taken_source = taken, DATE_EXIF
        info.has_gps = bool(parts.gps) and (raw_meta.gps_of(parts) is not None or len(parts.gps) > 1)
    try:
        import rawpy

        from .rawdev import output_size

        with rawpy.imread(path) as raw:
            info.width, info.height = output_size(raw)
            stamp = raw.other.timestamp
            if info.taken is None and stamp and stamp.year > 1971:
                info.taken, info.taken_source = stamp, DATE_EXIF
    except Exception as exc:  # noqa: BLE001 - skipped when converted (renaming, copying … still work)
        from .rawdev import explain_raw_error

        info.raw_error = str(explain_raw_error(exc)) if not isinstance(exc, ImportError) else t("photoinfo.no_raw")
    return info


# Date patterns in file names: IMG_20240512_143022, PXL_20240512_143022123,
# "WhatsApp Image 2024-05-12 at 14.30.22", Screenshot_2024-05-12-14-30-22,
# "Ekran görüntüsü 2024-05-12 143022" (Turkish Windows), signal-2024-05-12-143022 …
_DATE_TIME_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
    r"[ _\-T.]*(?:at[ _]*)?([01]\d|2[0-3])[-_.:h]?([0-5]\d)[-_.:m]?([0-5]\d)"
)
_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")


@functools.lru_cache(maxsize=1 << 18)  # the plan is recomputed on every change: room for a whole selection
def filename_date(name: str) -> datetime | None:
    stem = os.path.splitext(name)[0]
    limit = datetime.now() + timedelta(days=1)
    for pattern in (_DATE_TIME_RE, _DATE_RE):
        for m in pattern.finditer(stem):
            parts = [int(g) for g in m.groups()]
            try:
                value = datetime(*parts)
            except ValueError:
                continue
            if value <= limit:
                return value
    return None


# File names that give away the device or the app: drones and action cameras (DJI_0001, GOPR0001,
# GH010001, GX010001), cameras (DSC_0001 and _DSC0001 Nikon, DSCN Coolpix, DSCF Fujifilm, DSC01234 Sony,
# MVI_0001 Canon video), Pixel phones (PXL_, MVIMG_), messengers (WhatsApp, IMG-20240512-WA0003,
# Signal, Snapchat, Telegram, Facebook/Messenger) and screenshots ("Screenshot", "Ekran görüntüsü" on
# Turkish Windows, "Ekran Resmi" on Turkish macOS).
_DEVICE_NAME_RE = re.compile(
    r"^(?:dji_|gopr\d|gpbk\d|gpfr\d|g[phxsl]\d{6}|dsc[_nf]\d|_dsc\d|dsc\d{5}|mvi_\d|pxl_|mvimg_"
    r"|whatsapp (?:image|video)|signal-|snapchat-|telegram-|fb_img_|received_\d"
    r"|screenshot|screen shot|ekran görüntüsü|ekran resmi)"
    r"|^(?:img|vid|aud|ptt)-\d{8}-wa\d{4}",
    re.IGNORECASE,
)


@functools.lru_cache(maxsize=1 << 18)  # the plan is recomputed on every change: room for a whole selection
def name_reveals(name: str) -> str | None:
    """Why the file name gives something away, kept as it is after the metadata is stripped (the key
    of a short reason for the UI), or None: the capture date and time, or the device or app."""
    if filename_date(name) is not None:
        return "photoinfo.name_date"
    if _DEVICE_NAME_RE.search(os.path.splitext(name)[0]):
        return "photoinfo.name_device"
    return None


def best_date(info: PhotoInfo | None, name: str, mtime: float) -> tuple[datetime, str]:
    """The most reliable date for sorting/grouping, and its source."""
    if info and info.taken:
        return info.taken, info.taken_source or DATE_EXIF
    from_name = filename_date(name)
    if from_name:
        return from_name, DATE_FILENAME
    return local_time(mtime), DATE_MTIME


def local_time(timestamp: float) -> datetime:
    """A file time as local time. Windows' `fromtimestamp` fails before 1970 (an old scan, a zeroed
    NTFS time): such a time is counted from the start of 1970 instead."""
    try:
        return datetime.fromtimestamp(timestamp)
    except (OSError, OverflowError, ValueError):
        try:
            return datetime.fromtimestamp(0) + timedelta(seconds=timestamp)
        except (OSError, OverflowError, ValueError):
            return datetime(1970, 1, 1)
