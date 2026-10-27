"""Scanning folders together with their subfolders (independent of the UI).

The same rules as in the Browse grid apply, so selecting a folder gives the same result as opening
it and selecting the photos one by one:

* Files and folders with the hidden/system attribute, names starting with "." (Android's
  .thumbnails / .trashed folders, macOS "._" files, Fotodit's temporary files) and names starting
  with "$" ($RECYCLE.BIN) are skipped.
* Folder links (junctions / symbolic links) are not followed: no loops, and the same photo is never
  counted twice. Cloud folders such as OneDrive also carry reparse points but are not links; they
  are scanned.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..winutil import explorer_key
from .formats import is_supported_name, is_video_name

SKIP_PREFIXES = (".", "$")
_HIDDEN = 0x2 | 0x4  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
_LINK_TAGS = {0xA0000003, 0xA000000C}  # IO_REPARSE_TAG_MOUNT_POINT (junction), IO_REPARSE_TAG_SYMLINK
PROGRESS_INTERVAL = 0.15


def folder_key(path: str) -> str:
    """Comparison key for folders (case-insensitive on Windows)."""
    return os.path.normcase(os.path.abspath(path))


def is_hidden(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _HIDDEN)


def is_link(st: os.stat_result) -> bool:
    return getattr(st, "st_reparse_tag", 0) in _LINK_TAGS


@dataclass
class FolderScan:
    photos: list[tuple[str, int, float]] = field(default_factory=list)  # (path, size, mtime)
    totals: dict[str, int] = field(default_factory=dict)  # folder key → file count including subfolders
    videos: dict[str, int] = field(default_factory=dict)  # folder key → how many of those are videos
    complete: bool = True  # False if cancelled or the budget was exceeded
    partial: set[str] = field(default_factory=set)  # folders scanned only partly (counts are lower bounds)
    unreadable: int = 0  # number of folders that could not be opened (no access permission)

    @property
    def count(self) -> int:
        return len(self.photos)


def walk(
    root: str,
    *,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, str], None] | None = None,
    budget: int | None = None,
    collect: bool = True,
    known: Callable[[str], tuple[int, int] | None] | None = None,
) -> FolderScan:
    """Finds the supported photos and videos in `root` and all of its subfolders.

    Files are returned in Explorer order: first the folder's own files, then its subfolders (by
    name). `totals` gives the file count including subfolders for every visited folder, `videos`
    how many of them are videos. `budget` limits the number of entries examined (for counting
    only); `collect=False` does not collect the paths. When only counting, `known` avoids
    re-entering subfolders whose counts are already known (key → (files, videos)).
    """
    result = FolderScan()
    root = os.path.abspath(root)
    parents: dict[str, str | None] = {}
    direct: dict[str, int] = {}
    direct_videos: dict[str, int] = {}
    reuse = known if not collect else None
    visited: list[str] = []
    stack: list[tuple[str, str | None]] = [(root, None)]
    seen = found = 0
    last = time.monotonic()
    while stack:
        if cancelled is not None and cancelled():
            result.complete = False
            break
        path, parent = stack.pop()
        key = os.path.normcase(path)
        files: list[tuple[str, str, int, float]] = []
        dirs: list[tuple[str, str]] = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    seen += 1
                    name = entry.name
                    if name.startswith(SKIP_PREFIXES):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            if not is_hidden(st) and not is_link(st):
                                dirs.append((name, entry.path))
                        elif is_supported_name(name) and entry.is_file():
                            st = entry.stat()
                            if not is_hidden(st):
                                files.append((name, entry.path, st.st_size, st.st_mtime))
                    except OSError:
                        continue
        except OSError:
            result.unreadable += 1
            if parent is None:
                result.complete = False
            continue
        parents[key] = parent
        direct[key] = len(files)
        direct_videos[key] = sum(1 for f in files if is_video_name(f[0]))
        visited.append(key)
        found += len(files)
        if collect and files:
            files.sort(key=lambda f: explorer_key(f[0]))
            result.photos.extend((p, size, mtime) for _, p, size, mtime in files)
        if reuse is not None:
            fresh = []
            for name, p in dirs:
                count = reuse(os.path.normcase(p))
                if count is None:
                    fresh.append((name, p))
                else:
                    direct[key] += count[0]  # a subfolder with a known count is not re-entered
                    direct_videos[key] += count[1]
                    found += count[0]
            dirs = fresh
        dirs.sort(key=lambda d: explorer_key(d[0]), reverse=True)  # so they pop off the stack in Explorer order
        stack.extend((p, key) for _, p in dirs)
        if budget is not None and seen > budget:
            result.complete = False
            break
        if progress is not None and time.monotonic() - last >= PROGRESS_INTERVAL:
            progress(found, path)
            last = time.monotonic()
    # Reverse of the pre-order visit: every folder is added up before its parent.
    totals, videos = dict(direct), dict(direct_videos)
    for key in reversed(visited):
        parent = parents[key]
        if parent is not None:
            totals[parent] += totals[key]
            videos[parent] += videos[key]
    result.totals, result.videos = totals, videos
    # If the scan stopped early, the counts of all ancestors of the still-queued folders are incomplete.
    for _, parent in stack:
        while parent is not None and parent not in result.partial:
            result.partial.add(parent)
            parent = parents.get(parent)
    if progress is not None:
        progress(found, root)
    return result
