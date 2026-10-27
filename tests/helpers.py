"""Helpers the tests share: files to write, a stand-in Recycle Bin, checks that the output is clean
and the right way up, and waiting for the user interface."""

from __future__ import annotations

import io
import os
import time

import builders as b
from oracle import assert_exiv2_clean
from PIL import Image

from fotodit.core.formats import Fmt
from fotodit.core.imaging import open_image
from fotodit.core.meta import Kind, residual_signatures, scan_bytes
from fotodit.core.photoinfo import read_info
from fotodit.core.planner import Photo

# Mimics what a camera records: displayed = T_o(stored) ⇒ stored = T_o⁻¹(displayed)
INVERSE = {
    1: None,
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_90,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_270,
}


def stored_for(displayed: Image.Image, orientation: int) -> Image.Image:
    t = INVERSE[orientation]
    return displayed.transpose(t) if t is not None else displayed


def decode(data: bytes) -> Image.Image:
    im = open_image(io.BytesIO(data))
    im.load()
    return im


def assert_clean(data: bytes, fmt: Fmt, oracle: bool = True, profile: bytes | None = None) -> None:
    """No metadata left; `profile`: apart from this clean color profile, which must be the one embedded."""
    res = scan_bytes(data, fmt, enrich=False)
    signatures = residual_signatures(data)
    if profile is not None:
        assert decode(data).info.get("icc_profile") == profile
        res.findings = [f for f in res.findings if f.kind != Kind.ICC]
        signatures = [s for s in signatures if s != "ICC"]
    assert res.clean, res.describe()
    assert signatures == []
    if oracle:
        assert_exiv2_clean(data, allow_icc=profile is not None)


def assert_corners(im: Image.Image) -> None:
    """Is the displayed orientation correct? (red top left, green top right, blue bottom left)"""
    assert b.dominant(b.marker_color(im, "tl")) == "red"
    assert b.dominant(b.marker_color(im, "tr")) == "green"
    assert b.dominant(b.marker_color(im, "bl")) == "blue"


def jpeg_taken(when: str, orientation: int = 1) -> bytes:
    """A metadata-rich JPEG with the given capture date."""
    raw = b.rich_exif(orientation).replace(b"2024:05:12 14:30:22", when.encode())
    buf = io.BytesIO()
    b.photo(96, 64).save(buf, "JPEG", exif=b"Exif\x00\x00" + raw, quality=85)
    return buf.getvalue()


def write(path, data: bytes, mtime: float | None = None) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return str(path)


def photos_of(paths) -> list[Photo]:
    return [Photo.from_path(p, i, read_info(p)) for i, p in enumerate(paths)]


class FakeBin:
    """A test folder that stands in for the Recycle Bin."""

    def __init__(self, root):
        self.root = root
        self.items: list[str] = []
        os.makedirs(root, exist_ok=True)

    def __call__(self, paths):
        failed = set()
        for p in paths:
            try:
                dest = os.path.join(self.root, f"{len(self.items)}_{os.path.basename(p)}")
                os.rename(p, dest)
                self.items.append(dest)
            except OSError:
                failed.add(os.path.abspath(p))
        return failed


def write_in(folder, name: str, data: bytes) -> str:
    """Writes `data` to `folder/name` (subfolders included) and returns the path."""
    return write(os.path.join(str(folder), name), data)


_JPEG: list[bytes] = []


def sample_jpeg() -> bytes:
    """A small sample with GPS, camera, date … (made once)."""
    if not _JPEG:
        _JPEG.append(b.jpeg(b.photo(64, 48)))
    return _JPEG[0]


def make_files(root, *names) -> list[str]:
    """Writes the sample JPEG under `root` for every relative name."""
    return [write(os.path.join(str(root), name), sample_jpeg()) for name in names]


def wait_until(app, condition, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    app.processEvents()
    return condition()
