"""Processing plan: order, new names, grouping into folders and name conflicts.

The plan is computed entirely in memory and shown as a preview in the UI; nothing is
written to the file system. Rules:

* Sorting starts from the user's selection order (the order in which the files were
  selected or arranged by hand; for a selected folder, Explorer's name order): files
  with equal sort keys stay in that order. When sorting by capture date, files whose
  capture date is unknown (they only have a modification time) are not slotted in by
  guesswork; they come after the dated files, in selection order. A reverse direction
  such as newest first only reverses the key order.
* When grouping into folders by date (year, month or day), files without a capture
  date go to the "Tarihsiz" ("Undated" in English) folder, undated_folder(), instead of a date folder.
* When renaming, numbering restarts in every target folder and follows the sort order:
  each year/month/day folder and the undated folder counts 1, 2, 3 … (with sorting
  by capture date, the undated files there are numbered in selection order).
* Names can also come from the capture date: "2024-05-12_14-30-22" (NAME_DATE_TIME; files
  of the same second in a folder get "_2", "_3" … in sort order) or "2024-05-12_001"
  (NAME_DATE_NUMBER; numbered per day). Files without a capture date are named
  "Tarihsiz_1" … / "Tarihsiz_001" … in sort order.
* Photos and videos are named separately: each has its own prefix and start number,
  and they are counted separately (Holiday_1.jpg, Holiday_2.jpg … and Video_1.mp4,
  Video_2.mp4 …).
* With metadata stripping on and renaming off, a kept name that gives away the capture
  date or the device/app (IMG_20240512_143022, DJI_0001, "WhatsApp Image …") is flagged
  (PlanEntry.name_leak).
* Files that cannot be processed (skip_reason) are listed last, in selection order,
  and get no number, so the numbered names have no gaps.
* An existing file is never overwritten: a conflicting name gets " (2)", " (3)" … added.
  When applying to originals (in place), the selected files' own names count as free
  (they are moved too), but files whose name does not change keep their names.
* Grouping by source folder (GROUP_SOURCE): the selected folders are recreated in the
  output folder with their names and subfolders; individually selected photos go to a
  folder named after their own folder. This grouping is ignored when applying in place.

The UI recomputes the plan on every option change; every step runs in (near-)linear
time so that it stays smooth even for archives of tens of thousands of photos.
"""

from __future__ import annotations

import functools
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from ..i18n import month_name, t
from ..winutil import explorer_key, safe_filename_part
from .edits import Edit
from .formats import EXTENSIONS, PREFERRED_EXT, Fmt, is_video
from .photoinfo import DATE_MTIME, PhotoInfo, best_date, name_reveals
from .processor import (
    RAW_EDIT_NEEDS_JPEG,
    RAW_NEEDS_JPEG,
    RAW_RESIZE_NEEDS_JPEG,
    ContentOptions,
)
from .video.process import VideoPlan, plan_video
from .video.process import edit_reason as video_edit_reason
from .video.process import output_format as video_output_format

SORT_DATE = "date"
SORT_NAME = "name"
SORT_MTIME = "mtime"
SORT_SIZE = "size"
SORT_MANUAL = "manual"

GROUP_NONE = "none"
GROUP_YEAR = "year"
GROUP_MONTH = "month"
GROUP_DAY = "day"
GROUP_SOURCE = "source"

DATE_GROUPS = (GROUP_YEAR, GROUP_MONTH, GROUP_DAY)

NAME_NUMBER = "number"  # prefix + number: Holiday_1, Holiday_2 …
NAME_DATE_TIME = "date_time"  # prefix + capture date and time: 2024-05-12_14-30-22
NAME_DATE_NUMBER = "date_number"  # prefix + capture day + number within the day: 2024-05-12_001

# The same folder name is compared thousands of times, so normcase (LCMapStringEx) is cached.
_normcase = functools.lru_cache(maxsize=65536)(os.path.normcase)

@dataclass
class RenameOptions:
    enabled: bool = False
    sort_by: str = SORT_DATE
    descending: bool = False
    prefix: str = ""  # photos
    start: int = 1
    zero_pad: bool = False
    video_prefix: str = ""  # videos (numbered separately)
    video_start: int = 1
    pattern: str = NAME_NUMBER  # NAME_NUMBER, NAME_DATE_TIME or NAME_DATE_NUMBER


@dataclass
class OutputOptions:
    in_place: bool = False
    folder: str = ""
    group_by: str = GROUP_NONE
    recycle: bool = True


@dataclass
class JobOptions:
    content: ContentOptions = field(default_factory=ContentOptions)
    rename: RenameOptions = field(default_factory=RenameOptions)
    output: OutputOptions = field(default_factory=OutputOptions)

    def changes_content(self) -> bool:
        """Do the options change file contents? (An edited photo's content changes in any
        case; see PlanEntry.edit. Shrinking applies to photos larger than the limit.)"""
        c = self.content
        return c.strip_metadata or c.convert_to_jpeg or c.convert_video or c.max_side > 0

    def needs_dates(self) -> bool:
        """Does the job depend on the capture dates (sorting, naming or grouping by date, or keeping
        the date while the hidden details are removed)? Then it waits until they have been read."""
        r = self.rename
        named_by_date = r.enabled and (r.sort_by == SORT_DATE or r.pattern != NAME_NUMBER)
        keeps_date = self.content.strip_metadata and self.content.keep_date
        return named_by_date or keeps_date or self.output.group_by in DATE_GROUPS


@dataclass(eq=False)
class Photo:
    path: str
    size: int
    mtime: float
    order: int = 0
    info: PhotoInfo | None = None
    root: str | None = None  # the selected folder, if the photo was selected as part of one
    edit: Edit | None = None  # the user's rotate/crop edit

    def __post_init__(self) -> None:
        self.folder, self.name = os.path.split(self.path)
        self.folder_key = _normcase(self.folder)
        self.stem, self.ext = os.path.splitext(self.name)

    @classmethod
    def from_path(cls, path: str, order: int, info: PhotoInfo | None = None, root: str | None = None) -> Photo:
        st = os.stat(path)
        return cls(os.path.abspath(path), st.st_size, st.st_mtime, order, info, root)

    def fmt(self) -> Fmt | None:
        """Format: from the content once the file has been read, otherwise from the extension."""
        if self.info is not None and self.info.fmt is not None:
            return self.info.fmt
        return EXTENSIONS.get(self.ext.lower())

    @property
    def is_video(self) -> bool:
        return is_video(self.fmt())


@dataclass(eq=False)
class PlanEntry:
    photo: Photo
    position: int
    target_dir: str
    target_name: str
    out_fmt: Fmt | None
    date: datetime
    date_source: str
    renamed_for_conflict: bool = False
    skip_reason: str | None = None  # why the file cannot be processed: it is skipped, gets no number
    dir_key: str = ""  # comparison key of the target folder (normcase)
    edit: Edit | None = None  # the edit as of planning (must not change during the run)
    video_plan: VideoPlan | None = None  # what to do with the video (if its info has been read)
    name_leak: str | None = None  # why the kept name gives something away (stripping without renaming)

    def changes_content(self, opts: JobOptions) -> bool:
        c = opts.content
        if self.photo.is_video:
            return c.strip_metadata or c.convert_video or self.edit is not None
        return c.strip_metadata or c.convert_to_jpeg or self.edit is not None or _larger(self.photo, c.max_side)

    @property
    def target_path(self) -> str:
        return os.path.join(self.target_dir, self.target_name)

    @property
    def moves(self) -> bool:
        if (self.dir_key or _normcase(self.target_dir)) != self.photo.folder_key:
            return True
        return os.path.normcase(self.target_name) != os.path.normcase(self.photo.name)


def _larger(photo: Photo, max_side: int) -> bool:
    """Will the photo be shrunk to `max_side`? (If its size is not known yet, it may be.)"""
    if not max_side:
        return False
    info = photo.info
    if info is None or not info.width or not info.height:
        return True
    return max(info.width, info.height) > max_side


def output_format(photo: Photo, content: ContentOptions) -> Fmt | None:
    fmt = photo.fmt()
    if fmt is not None and is_video(fmt):
        return video_output_format(fmt, content)
    if content.convert_to_jpeg and fmt is not None:
        return Fmt.JPEG
    return fmt


def undated_folder() -> str:
    """The folder of files without a capture date when grouping by date ("Tarihsiz" / "Undated"); with
    the date name patterns they are named after it too ("Tarihsiz_1" …)."""
    return t("planner.undated")


def group_folder(group_by: str, when: datetime, photo: Photo, dated: bool = True) -> str:
    if group_by in DATE_GROUPS and not dated:
        return undated_folder()
    if group_by == GROUP_YEAR:
        return f"{when.year}"
    if group_by == GROUP_MONTH:
        return f"{when.year}-{when.month:02d} {month_name(when.month)}"
    if group_by == GROUP_DAY:
        return f"{when:%Y-%m-%d}"
    if group_by == GROUP_SOURCE:
        return _folder_label(photo.folder)
    return ""


# --------------------------------------------------------- source folder structure


def _folder_label(path: str) -> str:
    """The folder's name in the output; the drive letter for a drive root."""
    stripped = path.rstrip("\\/")
    name = os.path.basename(stripped) or os.path.splitdrive(path)[0].rstrip(":")
    return safe_filename_part(name) or t("planner.root")


def _ancestors(key: str):
    parent = os.path.dirname(key)
    while parent and parent != key:
        yield parent
        key, parent = parent, os.path.dirname(parent)


def _within(folder_key: str, root_key: str) -> bool:
    return folder_key == root_key or folder_key.startswith(root_key.rstrip("\\/") + os.sep)


def source_layout(photos: list[Photo]) -> dict[str, str]:
    """Relative path, inside the output folder, of each photo folder (normcase key).

    Keeps the selected folder (root) by name together with its subfolder structure: if
    "Archive" was selected, "Archive\\2019\\Holiday". An individually selected photo's
    root is its own folder. With nested roots the topmost one wins; roots with the same
    name in different places become "Holiday", "Holiday (2)" ….
    """
    tops: dict[str, str] = {}
    for p in photos:
        root = p.root if p.root and _within(p.folder_key, _normcase(p.root)) else p.folder
        tops.setdefault(_normcase(root), root)
    topmost = {k: v for k, v in tops.items() if not any(a in tops for a in _ancestors(k))}
    labels: dict[str, str] = {}
    used: set[str] = set()
    for key, path in sorted(topmost.items(), key=lambda kv: explorer_key(kv[1])):
        base = _folder_label(path)
        label, n = base, 2
        while label.casefold() in used:
            label, n = f"{base} ({n})", n + 1
        used.add(label.casefold())
        labels[key] = label
    layout: dict[str, str] = {}
    for p in photos:
        if p.folder_key in layout:
            continue
        for candidate in (p.folder_key, *_ancestors(p.folder_key)):
            if candidate in topmost:
                rest = os.path.relpath(p.folder, topmost[candidate])
                layout[p.folder_key] = labels[candidate] if rest == "." else os.path.join(labels[candidate], rest)
                break
    return layout


# ----------------------------------------------------------------------- plan


def skip_reason(photo: Photo, content: ContentOptions) -> str | None:
    """Why the file cannot be processed (None if it can): it cannot be opened; metadata stripping,
    an edit or shrinking is requested for a RAW file (they apply to RAW only while converting to
    JPG); or a video in a format that cannot store rotation/crop (AVI, MKV …) is to be edited."""
    info = photo.info
    if info and info.error:
        return info.error
    fmt = photo.fmt()
    if info and info.raw_error and fmt == Fmt.RAW and content.convert_to_jpeg:
        return info.raw_error
    if fmt is not None and is_video(fmt):
        return video_edit_reason(fmt, content, photo.edit)
    if not content.convert_to_jpeg and fmt == Fmt.RAW:
        if content.strip_metadata:
            return t(RAW_NEEDS_JPEG)
        if photo.edit is not None:
            return t(RAW_EDIT_NEEDS_JPEG)
        if _larger(photo, content.max_side):
            return t(RAW_RESIZE_NEEDS_JPEG)
    return None


def _sort_with_ties(items: list, primary, secondary) -> None:
    """Stable-sorts by `primary` and orders only runs of equal keys by `secondary`. Explorer
    order (StrCmpLogicalW) is expensive, so this way it is computed only for real ties."""
    items.sort(key=primary)
    keys = [primary(t) for t in items]
    start = 0
    for i in range(1, len(items) + 1):
        if i == len(items) or keys[i] != keys[start]:
            if i - start > 1:
                items[start:i] = sorted(items[start:i], key=secondary)
            start = i


def undated(source: str) -> bool:
    """Is the capture date unknown (the date comes only from the file's modification time)?"""
    return source == DATE_MTIME


def undated_count(plan: list[PlanEntry]) -> int:
    """Number of files without a capture date (info read, to be processed): they go last when
    sorting by capture date and into the undated folder when grouping into folders by date."""
    return sum(1 for e in plan if not e.skip_reason and e.photo.info is not None and undated(e.date_source))


def _sort(dated: list[tuple[Photo, datetime, str]], sort_by: str, descending: bool) -> None:
    """Puts the items in selection order, then stable-sorts them by the chosen key: items with
    equal keys and (when sorting by capture date) items without a capture date stay in selection
    order."""
    dated.sort(key=lambda t: t[0].order)
    if sort_by == SORT_MANUAL:
        return
    if sort_by == SORT_NAME:
        folders = sorted(sorted({t[0].folder for t in dated}), key=explorer_key)
        rank = {f: i for i, f in enumerate(folders)}
        _sort_with_ties(dated, lambda t: rank[t[0].folder], lambda t: explorer_key(t[0].name))
        if descending:
            dated.reverse()  # no two files in a folder share a name: there are no ties
    elif sort_by == SORT_MTIME:
        dated.sort(key=lambda t: t[0].mtime, reverse=descending)
    elif sort_by == SORT_SIZE:
        dated.sort(key=lambda t: t[0].size, reverse=descending)
    else:
        # The modification time is often when the file was copied or processed; used in place
        # of the capture date, it would scatter undated files to arbitrary positions.
        known = [t for t in dated if not undated(t[2])]
        known.sort(key=lambda t: t[1], reverse=descending)
        dated[:] = known + [t for t in dated if undated(t[2])]


def build_plan(photos: list[Photo], opts: JobOptions) -> list[PlanEntry]:
    rename, output = opts.rename, opts.output
    reasons = [skip_reason(p, opts.content) for p in photos]
    valid = [p for p, reason in zip(photos, reasons, strict=True) if not reason]
    broken = [(p, reason) for p, reason in zip(photos, reasons, strict=True) if reason]
    dated = [(p, *best_date(p.info, p.name, p.mtime)) for p in valid]
    _sort(dated, rename.sort_by, rename.descending)

    group_by = output.group_by
    if output.in_place and group_by == GROUP_SOURCE:
        group_by = GROUP_NONE
    layout = source_layout(valid) if group_by == GROUP_SOURCE else None

    target_dirs: dict[tuple[str, str], tuple[str, str]] = {}
    entries: list[PlanEntry] = []
    exts: list[str] = []
    leaks = opts.content.strip_metadata and not rename.enabled  # the kept names may give something away
    for position, (photo, when, source) in enumerate(dated, start=1):
        base = photo.folder if output.in_place else output.folder
        if layout is not None:
            sub = layout[photo.folder_key]
        else:
            sub = group_folder(group_by, when, photo, dated=not undated(source))
        target = target_dirs.get((base, sub))
        if target is None:
            target_dir = os.path.normpath(os.path.join(base, sub) if sub else base)
            target = target_dirs[(base, sub)] = (target_dir, _normcase(target_dir))
        fmt = output_format(photo, opts.content)
        ext = photo.ext
        if rename.enabled:
            # When renaming, the extension is corrected to match the actual format
            # (e.g. a ".jpg" file that is really a PNG becomes ".png").
            ext = PREFERRED_EXT.get(fmt, ext.lower()) if fmt is not None else ext.lower()
        elif opts.content.convert_to_jpeg and fmt == Fmt.JPEG and ext.lower() != ".jpg":
            ext = ".jpg"  # .jpeg / .jfif / .heic … → .jpg
        elif fmt == Fmt.MP4 and photo.fmt() != Fmt.MP4:
            ext = ".mp4"  # .mov / .avi / .mts … → .mp4
        video = getattr(photo.info, "video", None)
        entries.append(PlanEntry(photo, position, target[0], photo.stem + ext, fmt, when, source, dir_key=target[1],
                                 edit=photo.edit,
                                 video_plan=plan_video(video, opts.content, photo.edit) if video else None,
                                 name_leak=name_reveals(photo.name) if leaks else None))
        exts.append(ext)

    if rename.enabled:
        # Photos and videos are named separately in each folder.
        prefixes = {False: safe_filename_part(rename.prefix), True: safe_filename_part(rename.video_prefix)}
        starts = {False: rename.start, True: rename.video_start}
        per_dir: dict[tuple[str, bool], list[int]] = {}
        for i, e in enumerate(entries):
            per_dir.setdefault((e.dir_key, e.photo.is_video), []).append(i)
        for (_, video), group in per_dir.items():
            if rename.pattern == NAME_DATE_TIME:
                names = _date_time_names([entries[i] for i in group], [exts[i] for i in group], prefixes[video],
                                         rename.zero_pad)
            elif rename.pattern == NAME_DATE_NUMBER:
                names = _date_number_names([entries[i] for i in group], prefixes[video])
            else:
                names = _numbers(prefixes[video], starts[video], len(group), rename.zero_pad)
            for i, name in zip(group, names, strict=True):
                entries[i].target_name = name + exts[i]

    # Skipped files go last and get no number, so the numbered names have no gaps.
    for photo, reason in sorted(broken, key=lambda t: t[0].order):
        when, source = best_date(photo.info, photo.name, photo.mtime)
        entries.append(PlanEntry(
            photo, len(entries) + 1, photo.folder, photo.name, None, when, source, skip_reason=reason,
            dir_key=photo.folder_key, edit=photo.edit,
        ))

    _resolve_conflicts(entries, output.in_place)
    return entries


# ---------------------------------------------------------------------- names


def _numbers(prefix: str, start: int, count: int, zero_pad: bool) -> list[str]:
    """prefix + start, start + 1 …; padded with zeros to the width of the last number (at least 2)."""
    width = max(2, len(str(start + count - 1))) if zero_pad else 0
    return [f"{prefix}{start + n:0{width}d}" for n in range(count)]


def _day(when: datetime) -> str:
    return f"{when.year:04d}-{when.month:02d}-{when.day:02d}"


def _date_time_names(group: list[PlanEntry], exts: list[str], prefix: str, zero_pad: bool) -> list[str]:
    """Names from the capture date and time ("2024-05-12_14-30-22"), for the files of one folder in
    sort order. Files that would get the same name (same second, same extension) get "_2", "_3" … (the
    first one stays plain), so a RAW and its JPEG keep matching names. Undated files are numbered."""
    undated_total = sum(1 for e in group if undated(e.date_source))
    width = max(2, len(str(undated_total))) if zero_pad else 0
    undated_name = undated_folder()
    seen: dict[str, int] = {}
    names = []
    count = 0
    for e, ext in zip(group, exts, strict=True):
        if undated(e.date_source):
            count += 1
            names.append(f"{prefix}{undated_name}_{count:0{width}d}")
            continue
        w = e.date
        base = f"{prefix}{_day(w)}_{w.hour:02d}-{w.minute:02d}-{w.second:02d}"
        key = (base + ext).casefold()
        n = seen[key] = seen.get(key, 0) + 1
        names.append(base if n == 1 else f"{base}_{n}")
    return names


def _date_number_names(group: list[PlanEntry], prefix: str) -> list[str]:
    """Names from the capture day and a number that restarts every day ("2024-05-12_001"; wider than
    three digits only when needed), for the files of one folder in sort order. Undated files:
    "Tarihsiz_001" …."""
    counts: dict[str, int] = {}
    names = []
    undated_name = undated_folder()
    for e in group:
        day = undated_name if undated(e.date_source) else _day(e.date)
        n = counts[day] = counts.get(day, 0) + 1
        names.append(f"{prefix}{day}_{n:03d}")
    return names


# ------------------------------------------------------------- name conflicts

# Folder listings are cached across plans (the plan is recomputed on every option change).
# A listing is read again when the folder's mtime changes or the entry expires; during the
# run, conflicts are checked on disk as well.
_LISTINGS: dict[str, tuple[int, float, float, frozenset[str]]] = {}  # stamp, read, checked, names
_LISTING_TTL = 30.0
_LISTING_FRESH = 2.0  # a listing checked this recently is used without asking the disk (a network drive is slow)
_LISTING_LIMIT = 20000


def forget_listings() -> None:
    """Clears the folder listing cache (called before and after a run)."""
    _LISTINGS.clear()


def _listdir(directory: str) -> frozenset[str]:
    key = os.path.normcase(directory)
    now = time.monotonic()
    hit = _LISTINGS.get(key)
    if hit is not None and now - hit[2] < _LISTING_FRESH:
        return hit[3]
    try:
        stamp = os.stat(directory).st_mtime_ns
    except OSError:
        _LISTINGS.pop(key, None)
        return frozenset()
    if hit is not None and hit[0] == stamp and now - hit[1] < _LISTING_TTL:
        _LISTINGS[key] = (stamp, hit[1], now, hit[3])
        return hit[3]
    try:
        names = frozenset(n.casefold() for n in os.listdir(directory))
    except OSError:
        names = frozenset()
    if len(_LISTINGS) >= _LISTING_LIMIT:
        _LISTINGS.clear()
    _LISTINGS[key] = (stamp, now, now, names)
    return names


def _resolve_conflicts(entries: list[PlanEntry], in_place: bool) -> None:
    taken: dict[str, set[str]] = {}
    # When applying in place, the selected files are moved anyway, so their names become free.
    vacating: dict[str, set[str]] = {}
    if in_place:
        for e in entries:
            vacating.setdefault(e.photo.folder_key, set()).add(e.photo.name.casefold())

    def occupied(e: PlanEntry) -> set[str]:
        key = e.dir_key or _normcase(e.target_dir)
        names = taken.get(key)
        if names is None:
            names = taken[key] = set(_listdir(e.target_dir)) - vacating.get(key, set())
        return names

    # First, files that keep both their folder and their name reserve those names.
    fixed: set[int] = set()
    if in_place:
        for e in entries:
            if not e.moves:
                fixed.add(id(e))
                occupied(e).add(e.target_name.casefold())
    next_suffix: dict[tuple[str, str], int] = {}
    for e in entries:
        if e.skip_reason or id(e) in fixed:
            continue
        names = occupied(e)
        folded = e.target_name.casefold()
        if folded in names:
            # Continue from the last number used for this name; with thousands of files of the
            # same name (IMG_0001.jpg …), counting up from 2 every time would be far too slow.
            slot = (e.dir_key or _normcase(e.target_dir), folded)
            stem, ext = os.path.splitext(e.target_name)
            n = next_suffix.get(slot, 2)
            candidate = f"{stem} ({n}){ext}"
            while candidate.casefold() in names:
                n += 1
                candidate = f"{stem} ({n}){ext}"
            next_suffix[slot] = n + 1
            e.target_name = candidate
            e.renamed_for_conflict = True
            folded = candidate.casefold()
        names.add(folded)
