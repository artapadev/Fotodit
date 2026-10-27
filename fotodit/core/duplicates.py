"""Duplicates within a selection: the same file twice, or the same picture with different metadata.

* "exact" (EXACT): the same bytes. Files are grouped by size first; only files of the same size are
  compared, by their first 64 KB and then, if those match too, by a hash (blake2b) of the whole file
  read in chunks.
* "same_image" (SAME_IMAGE): the same picture with different metadata, e.g. a photo and a copy whose
  metadata Fotodit removed losslessly. JPEG, PNG, WebP and GIF files are compared by a hash of their
  metadata-stripped bytes (the byte-level `strip` of each `meta` module, the one that cleans them).
  So that not every photo has to be read in full, a JPEG, PNG or WebP is first identified by the
  start of its image data (`image_prefix` of its `meta` module): only files whose image data starts
  the same way are read whole. Other formats (HEIC, TIFF, RAW, videos …) are duplicates only when
  their bytes are the same.

Every file belongs to at most one group: a group is "exact" if all of its files have the same bytes,
otherwise "same_image". Groups come in the order their first file was given; inside a group the given
order is kept, so the first file is the one to keep. The same file given twice (under another name
too) counts once. Unreadable files are skipped. Nothing is deleted here.
"""

from __future__ import annotations

import hashlib
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass

from .formats import Fmt, sniff
from .meta import gif as gif_meta
from .meta import jpeg as jpeg_meta
from .meta import png as png_meta
from .meta import webp as webp_meta
from .meta.model import FormatError

EXACT = "exact"
SAME_IMAGE = "same_image"

HEAD = 64 * 1024  # bytes read first from every file
PREFIX = 32 * 1024  # bytes of image data that tell pictures apart before whole files are read
CHUNK = 1024 * 1024

_STRIP = {Fmt.JPEG: jpeg_meta.strip, Fmt.PNG: png_meta.strip, Fmt.WEBP: webp_meta.strip, Fmt.GIF: gif_meta.strip}
_PREFIX = {Fmt.JPEG: jpeg_meta.image_prefix, Fmt.PNG: png_meta.image_prefix, Fmt.WEBP: webp_meta.image_prefix}


@dataclass
class DuplicateGroup:
    paths: list[str]
    kind: str  # EXACT: identical bytes; SAME_IMAGE: the same picture with different metadata


@dataclass(eq=False)
class _File:
    index: int
    path: str
    size: int
    fmt: Fmt | None
    head: bytes  # hash of the first HEAD bytes
    prefix: bytes | None = None  # hash of the start of the image data (JPEG, PNG, WebP)
    full: bytes | None = None  # hash of the whole file
    image: bytes | None = None  # hash of the metadata-stripped bytes
    lost: bool = False  # could not be read again


def _hash(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


def _read(path: str, limit: int | None = None) -> bytes:
    with open(path, "rb") as fh:
        return fh.read() if limit is None else fh.read(limit)


def _hash_file(path: str) -> bytes:
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.digest()


def _examine(f: _File, data: bytes) -> None:
    """The whole file is in memory: its hash and, for the formats stripped at the byte level, the
    hash of its stripped bytes (none if the file is broken)."""
    f.full = _hash(data)
    strip = _STRIP.get(f.fmt)
    if strip is not None:
        try:
            f.image = _hash(strip(data))
        except (FormatError, ValueError, IndexError, struct.error):
            f.image = None


def _look(index: int, path: str, seen: set[tuple[int, int]]) -> _File | None:
    """A first look at the file: size, format, the hash of its first bytes and (JPEG, PNG, WebP) of the
    start of its image data. None if it cannot be read or was given already."""
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            head = fh.read(HEAD)
    except OSError:
        return None
    if st.st_ino:
        if (st.st_dev, st.st_ino) in seen:
            return None
        seen.add((st.st_dev, st.st_ino))
    f = _File(index, path, st.st_size, sniff(head), _hash(head))
    complete = len(head) >= f.size
    if complete:
        _examine(f, head)
    prefix = _PREFIX.get(f.fmt)
    if prefix is not None:
        data = head
        # Large metadata before the image data (an embedded preview, a long XMP): read further.
        while (found := prefix(data, PREFIX, complete)) is None and not complete:
            want = len(data) * 4
            try:
                data = _read(path, want)
            except OSError:
                return None
            complete = len(data) < want or len(data) >= f.size
        f.prefix = _hash(found) if found is not None else None
    return f


def find_duplicates(paths: list[str], cancelled: Callable[[], bool] | None = None,
                    progress: Callable[[int, int], None] | None = None) -> list[DuplicateGroup]:
    """The duplicate groups among `paths` (see the module docstring). `progress(done, total)`: the
    first half of `total` is the first look at every file, the second half the files read in full.
    Returns an empty list if `cancelled()` becomes true."""
    is_cancelled = cancelled or (lambda: False)
    report = progress or (lambda done, total: None)
    total = 2 * len(paths)
    files: list[_File] = []
    seen: set[tuple[int, int]] = set()
    for i, path in enumerate(paths):
        if is_cancelled():
            return []
        f = _look(i, path, seen)
        if f is not None:
            files.append(f)
        report(i + 1, total)

    # Whole files to read: pictures whose image data starts like another one's (in the same format)
    # and, for the bytes, files of the same size whose first bytes match too.
    starts: dict[tuple, list[_File]] = {}
    for f in files:
        if f.fmt in _STRIP:
            starts.setdefault((f.fmt, f.prefix), []).append(f)
    to_strip = {id(f) for group in starts.values() if len(group) > 1 for f in group if f.full is None}
    sizes: dict[tuple[int, bytes], list[_File]] = {}
    for f in files:
        sizes.setdefault((f.size, f.head), []).append(f)
    to_hash = {id(f) for group in sizes.values() if len(group) > 1 for f in group if f.full is None}
    work = [f for f in files if id(f) in to_strip or id(f) in to_hash]
    for k, f in enumerate(work, start=1):
        if is_cancelled():
            return []
        try:
            if id(f) in to_strip:
                _examine(f, _read(f.path))
            else:
                f.full = _hash_file(f.path)
        except OSError:
            f.lost = True
        report(len(paths) + (len(paths) * k) // len(work), total)
    report(total, total)

    files = [f for f in files if not f.lost]
    groups: list[list[_File]] = []
    pictures: dict[tuple, list[_File]] = {}
    for f in files:
        if f.image is not None:
            pictures.setdefault((f.fmt, f.image), []).append(f)
    grouped: set[int] = set()
    for group in pictures.values():
        if len(group) > 1:
            groups.append(group)
            grouped.update(id(f) for f in group)
    same: dict[tuple[int, bytes], list[_File]] = {}
    for f in files:
        if id(f) not in grouped and f.full is not None:
            same.setdefault((f.size, f.full), []).append(f)
    groups += [group for group in same.values() if len(group) > 1]
    groups.sort(key=lambda group: group[0].index)
    return [DuplicateGroup([f.path for f in group], EXACT if len({f.full for f in group}) == 1 else SAME_IMAGE)
            for group in groups]
