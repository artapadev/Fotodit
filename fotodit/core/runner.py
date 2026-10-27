"""Runner that applies the processing plan to disk.

Safety principles:

1. **Prepare first, then apply.** Each file's new content is first written to a
   temporary file in the target folder and verified. The originals are touched only
   after all files have been prepared. On cancel, the temporary files are deleted and
   no file changes.
2. **Never overwrite.** Final names are given with `os.rename`, which fails on Windows
   if the target exists. On an unexpected conflict, " (2)" is added to the name.
3. **Originals can be restored.** When applying to originals (in place), the originals
   of changed files are moved to the Recycle Bin (when the Recycle Bin option is on, as
   it is by default).
4. **Two-phase renaming.** Files that are only renamed are first moved to temporary
   names, then to their final names, so swaps such as "1.jpg ↔ 2.jpg" work too.

Videos are not loaded into memory: they are processed straight from the source file
into a temporary file in the target folder (see `fotodit.core.video.process`). Re-encoding
uses all CPU cores, so only one video is processed at a time; photos are processed
first. For long videos, progress within the file is reported too (STAGE_FILE).

A written file's modified time: with metadata kept, the source's; with metadata stripped,
the processing time (a new file), unless the capture date is kept ("Çekim tarihini koru"):
then the capture date. A date that comes only from the source's own modified time is not
a capture date and is never written.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from ..i18n import t
from ..winutil import recycle
from .photoinfo import DATE_MTIME
from .planner import JobOptions, PlanEntry
from .processor import UNCHANGED, ProcessError, process_bytes
from .video.probe import ProbeError, probe
from .video.process import VideoCancelled, VideoError, plan_video, process_video

TEMP_PREFIX = ".fotodit-"

STAGE_PROCESS = "process"
STAGE_RECYCLE = "recycle"
STAGE_COMMIT = "commit"
STAGE_FILE = "file"  # progress within a single file (long video), in per mille

# Antivirus software, the search indexer or OneDrive may lock a file briefly
# (WinError 5 / 32 / 33); on these errors the call is retried at short intervals.
_RETRY_WINERRORS = {5, 32, 33}
_RETRIES = 6
_RETRY_DELAY = 0.25

RECYCLE_BATCH = 100  # number of files moved to the Recycle Bin in one call


def _retry(fn, *args):
    for attempt in range(_RETRIES):
        try:
            return fn(*args)
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in _RETRY_WINERRORS or attempt == _RETRIES - 1:
                raise
            time.sleep(_RETRY_DELAY * (attempt + 1))
    return None


@dataclass
class ItemResult:
    entry: PlanEntry
    ok: bool = False
    final_path: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    method: str = UNCHANGED
    temp_path: str | None = None
    staged_path: str | None = None
    content_changed: bool = False


@dataclass
class RunReport:
    results: list[ItemResult]
    in_place: bool
    output_folder: str | None
    cancelled: bool = False
    recycled: int = 0

    @property
    def succeeded(self) -> list[ItemResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[ItemResult]:
        return [r for r in self.results if not r.ok]


ProgressFn = Callable[[str, int, int, str], None]


def default_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 2) - 1))


class Runner:
    def __init__(
        self,
        plan: list[PlanEntry],
        opts: JobOptions,
        *,
        on_progress: ProgressFn | None = None,
        cancel_event: threading.Event | None = None,
        workers: int | None = None,
        recycle_func: Callable[[list[str]], set[str]] = recycle,
    ):
        self.plan = plan
        self.opts = opts
        self.on_progress = on_progress or (lambda *a: None)
        self.cancel = cancel_event or threading.Event()
        self.workers = workers or default_workers()
        self.recycle_func = recycle_func
        self._lock = threading.Lock()
        self._done = 0
        self._video_slot = threading.Semaphore(1)
        self._made: list[str] = []  # folders this job created, deepest first

    # ---------------------------------------------------------------- execution

    def run(self) -> RunReport:
        in_place = self.opts.output.in_place
        results = [ItemResult(e) for e in self.plan]
        report = RunReport(results, in_place, None if in_place else self.opts.output.folder)
        total = len(results)
        self.on_progress(STAGE_PROCESS, 0, total, "")
        # Photos first: they need not wait while the videos are processed one at a time.
        ordered = sorted(results, key=lambda r: r.entry.photo.is_video)
        if self.workers > 1 and total > 1:
            with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="fotodit") as pool:
                list(pool.map(self._prepare_safely, ordered))
        else:
            for r in ordered:
                self._prepare_safely(r)
        if self.cancel.is_set():
            self._discard(results)
            self._remove_empty_folders()
            report.cancelled = True
            return report
        self.on_progress(STAGE_COMMIT, 0, total, "")
        if in_place:
            report.recycled = self._commit_in_place(results)
        else:
            self._commit_new_folder(results)
        self._remove_empty_folders()
        self.on_progress(STAGE_COMMIT, total, total, "")
        return report

    def _ensure_dir(self, directory: str) -> None:
        """Creates the folder and its missing parents, remembering them (see `_remove_empty_folders`)."""
        missing = []
        folder = directory
        while folder and not os.path.isdir(folder):
            missing.append(folder)
            parent = os.path.dirname(folder)
            if parent == folder:
                break
            folder = parent
        if not missing:
            return
        os.makedirs(directory, exist_ok=True)
        with self._lock:
            self._made.extend(missing)

    def _remove_empty_folders(self) -> None:
        """A cancelled job leaves no folder behind, nor does a folder whose files all failed."""
        for folder in sorted(set(self._made), key=len, reverse=True):
            try:
                os.rmdir(folder)  # only an empty folder
            except OSError:
                pass

    def _prepare_safely(self, r: ItemResult) -> None:
        if self.cancel.is_set():
            return
        try:
            self._prepare(r)
        except VideoCancelled:
            r.error = t("runner.cancelled")
        except MemoryError:
            r.error = t("runner.out_of_memory")
        except Exception as exc:  # noqa: BLE001 - one file's error must not stop the whole job
            r.error = t("runner.unexpected", error=exc)
        finally:
            if r.error and r.temp_path:
                _remove_quietly(r.temp_path)
                r.temp_path = None
            with self._lock:
                self._done += 1
                done = self._done
            self.on_progress(STAGE_PROCESS, done, len(self.plan), r.entry.photo.name)

    def _prepare(self, r: ItemResult) -> None:
        e, opts = r.entry, self.opts
        if e.skip_reason:
            r.error = e.skip_reason
            return
        src = e.photo.path
        in_place = opts.output.in_place
        try:
            st = os.stat(src)
        except FileNotFoundError:
            r.error = t("error.not_found")
            return
        changes = e.changes_content(opts)
        will_touch = changes or e.moves
        if in_place and will_touch and not os.access(src, os.W_OK):
            r.error = t("runner.read_only")
            return
        taken = e.date if e.date_source != DATE_MTIME else None  # a capture date (EXIF, XMP, video, file name)
        if e.photo.is_video:
            if changes and self._prepare_video(r, st, taken):
                return
        elif changes:
            try:
                with open(src, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                r.error = t("error.read", error=exc.strerror or exc)
                return
            try:
                result = process_bytes(data, opts.content, name=e.photo.name, edit=e.edit, taken=taken)
            except ProcessError as exc:
                r.error = str(exc)
                return
            r.notes, r.method = result.notes, result.method
            if result.data is not None:
                r.content_changed = True
                r.temp_path = self._write_temp(r, e.target_dir, result.data)
                if r.temp_path:
                    self._stamp(r.temp_path, st, taken)
                return
        if not in_place:
            r.temp_path = self._copy_temp(r, e.target_dir, src, st)

    def _stamp(self, path: str, st: os.stat_result, taken: datetime | None) -> None:
        """Sets the written file's modified time: the source's when metadata is kept, the capture date
        when only it is kept; otherwise the file keeps the processing time."""
        c = self.opts.content
        if not c.strip_metadata:
            os.utime(path, (st.st_atime, st.st_mtime))
        elif c.keep_date and taken is not None:
            try:
                stamp = taken.timestamp()  # the capture date is local time
            except (OverflowError, OSError, ValueError):
                return
            os.utime(path, (time.time(), stamp))

    def _prepare_video(self, r: ItemResult, st: os.stat_result, taken: datetime | None = None) -> bool:
        """Processes the video. Returns False if the content would not change (e.g. already MP4
        with metadata kept): the file is then copied or left as is. `taken`: the capture date."""
        e, opts = r.entry, self.opts
        info = getattr(e.photo.info, "video", None)
        try:
            info = info or probe(e.photo.path)
        except ProbeError as exc:
            r.error = str(exc)
            return True
        plan = plan_video(info, opts.content, e.edit)
        if plan.noop(info):
            return False
        path = self._new_temp(r, e.target_dir)
        if path is None:
            return True
        name = e.photo.name

        def progress(fraction: float) -> None:
            self.on_progress(STAGE_FILE, int(fraction * 1000), 1000, name)

        with self._video_slot:
            if self.cancel.is_set():
                _remove_quietly(path)
                r.error = t("runner.cancelled")
                return True
            try:
                result = process_video(e.photo.path, path, opts.content, e.edit, info=info,
                                       progress=progress, cancelled=self.cancel.is_set, taken=taken)
            except VideoError as exc:
                r.error = str(exc)
                return True
        r.notes, r.method = result.notes, result.method
        r.content_changed = True
        r.temp_path = path
        self._stamp(path, st, taken)
        return True

    def _new_temp(self, r: ItemResult, directory: str) -> str | None:
        path = os.path.join(directory, f"{TEMP_PREFIX}{uuid.uuid4().hex}.tmp")
        try:
            self._ensure_dir(directory)
            with open(path, "xb"):
                pass
        except OSError as exc:
            _remove_quietly(path)
            r.error = t("runner.write_failed", error=exc.strerror or exc)
            return None
        return path

    def _write_temp(self, r: ItemResult, directory: str, data: bytes) -> str | None:
        path = os.path.join(directory, f"{TEMP_PREFIX}{uuid.uuid4().hex}.tmp")
        try:
            self._ensure_dir(directory)
            with open(path, "xb") as fh:
                fh.write(data)
        except OSError as exc:
            _remove_quietly(path)
            r.error = t("runner.write_failed", error=exc.strerror or exc)
            return None
        return path

    def _copy_temp(self, r: ItemResult, directory: str, src: str, st: os.stat_result) -> str | None:
        path = os.path.join(directory, f"{TEMP_PREFIX}{uuid.uuid4().hex}.tmp")
        try:
            self._ensure_dir(directory)
            shutil.copyfile(src, path)
            if not self.opts.content.strip_metadata:
                os.utime(path, (st.st_atime, st.st_mtime))
        except OSError as exc:
            _remove_quietly(path)
            r.error = t("runner.copy_failed", error=exc.strerror or exc)
            return None
        return path

    # ------------------------------------------------------------------- commit

    def _commit_new_folder(self, results: list[ItemResult]) -> None:
        for i, r in enumerate(results, start=1):
            if r.error or not r.temp_path:
                continue
            self._finalize(r, r.temp_path)
            self.on_progress(STAGE_COMMIT, i, len(results), r.entry.photo.name)

    def _commit_in_place(self, results: list[ItemResult]) -> int:
        replaced = [r for r in results if not r.error and r.content_changed]
        recycled = 0
        use_bin = self.opts.output.recycle
        done = 0
        # Originals are moved to the Recycle Bin in batches (moving them one by one takes far
        # too long for large archives); each file's result is evaluated separately.
        batch_size = RECYCLE_BATCH if use_bin else 1
        for start in range(0, len(replaced), batch_size):
            batch = replaced[start:start + batch_size]
            failed = self.recycle_func([r.entry.photo.path for r in batch]) if use_bin else set()
            for r in batch:
                src = r.entry.photo.path
                if use_bin:
                    removed = os.path.abspath(src) not in failed
                    reason = "runner.not_recycled"
                elif r.entry.moves:
                    try:
                        _retry(os.remove, src)
                        removed = True
                    except OSError:
                        removed = False
                    reason = "runner.not_deleted"
                else:
                    removed = True  # will be written atomically under the same name (os.replace)
                if removed:
                    recycled += use_bin
                else:
                    r.error = t(reason)
                    _remove_quietly(r.temp_path)
                    r.temp_path = None
            done += len(batch)
            self.on_progress(STAGE_RECYCLE, done, len(replaced), batch[-1].entry.photo.name)

        for r in results:
            if r.error or r.content_changed or not r.entry.moves:
                continue
            src = r.entry.photo.path
            staged = os.path.join(os.path.dirname(src), f"{TEMP_PREFIX}{uuid.uuid4().hex}.tmp")
            try:
                _retry(os.rename, src, staged)
                r.staged_path = staged
            except OSError as exc:
                r.error = t("runner.rename_failed", error=exc.strerror or exc)

        for i, r in enumerate(results, start=1):
            if r.error:
                continue
            if r.content_changed:
                if not use_bin and not r.entry.moves:
                    try:
                        _retry(os.replace, r.temp_path, r.entry.photo.path)
                        r.ok, r.final_path = True, r.entry.photo.path
                    except OSError as exc:
                        r.error = t("runner.write_failed", error=exc.strerror or exc)
                        _remove_quietly(r.temp_path)
                else:
                    self._finalize(r, r.temp_path)
            elif r.staged_path:
                self._finalize(r, r.staged_path, restore_to=r.entry.photo.path)
            else:
                r.ok, r.final_path, r.method = True, r.entry.photo.path, UNCHANGED
            self.on_progress(STAGE_COMMIT, i, len(results), r.entry.photo.name)
        return recycled

    def _finalize(self, r: ItemResult, source: str, restore_to: str | None = None) -> None:
        target = r.entry.target_path
        try:
            self._ensure_dir(r.entry.target_dir)
            final = _free_path(target)
            _retry(os.rename, source, final)
        except OSError as exc:
            r.error = t("runner.place_failed", error=exc.strerror or exc)
            if restore_to:
                try:
                    _retry(os.rename, source, restore_to)
                except OSError:
                    r.error += " " + t("runner.temp_left", path=source)
            elif r.content_changed and self.opts.output.in_place:
                r.error += " " + t("runner.new_temp_left", path=source)
            else:
                _remove_quietly(source)
            return
        if final != target:
            r.notes.append(t("runner.saved_as", name=os.path.basename(final)))
        r.ok, r.final_path = True, final

    def _discard(self, results: list[ItemResult]) -> None:
        for r in results:
            if r.temp_path:
                _remove_quietly(r.temp_path)
                r.temp_path = None
            if not r.error:
                r.error = t("runner.cancelled")


def _free_path(path: str) -> str:
    if not os.path.lexists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.lexists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def _remove_quietly(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass
