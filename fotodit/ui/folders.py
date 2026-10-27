"""Folder selection: selecting folders together with their subfolders without opening them.

The folder tiles in the Browse grid and the folder tree on the left can be selected with
a single click. Two background jobs make this possible:

* **Counting** — how many photos the folders visible on screen contain (subfolders
  included). The tile shows this count, and it also decides whether the folder is fully
  or partly selected. Runs on a single low-priority thread and the results are cached;
  in very large folders (e.g. C:\\Windows) it stops after a limit.
* **Scanning** — the list of all photos in a selected folder. Scans run one after
  another, report progress and can be cancelled; if the folder is deselected while it
  is being scanned, the scan stops, and if one of its subfolders is deselected, that
  subfolder is dropped from the result.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ..core.folderscan import FolderScan, folder_key, walk
from .selection import Selection

NONE, PARTIAL, FULL, SCANNING = range(4)

COUNT_BUDGET = 150_000  # max. number of files/folders examined while counting one folder
COUNT_TTL = 300.0  # counts are refreshed after this many seconds (when the folder is shown again)
BATCH_WINDOW = 0.1  # time window for batching scan results (s)
SHUTDOWN_WAIT = 3.0  # s a closing window waits for a count or scan to notice it is to stop
_COUNT_CACHE_LIMIT = 200_000


def within(key: str, root_key: str) -> bool:
    """Is folder `key` the folder `root_key` itself or inside it?"""
    return key == root_key or key.startswith(root_key.rstrip("\\/") + os.sep)


def _background_mode() -> None:
    """Lowers this thread's disk and CPU priority (Windows background mode) so that
    folder counting does not get ahead of thumbnails and selection scans."""
    if os.name != "nt":
        return
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentThread.restype = ctypes.c_void_p
        k32.SetThreadPriority.argtypes = (ctypes.c_void_p, ctypes.c_int)
        k32.SetThreadPriority(k32.GetCurrentThread(), 0x00010000)  # THREAD_MODE_BACKGROUND_BEGIN
    except (AttributeError, OSError):
        pass


@dataclass
class FolderCount:
    photos: int  # photos and videos together
    complete: bool = True  # False: counting hit the limit, the real number is larger
    readable: bool = True
    videos: int = 0  # how many of them are videos
    at: float = field(default_factory=time.monotonic)

    def fresh(self) -> bool:
        """Files may have been added to the folder during the session; old counts are recounted."""
        return time.monotonic() - self.at < COUNT_TTL


# -------------------------------------------------------------------- counting
#
# Counting and scanning run on plain Python threads that are left behind when the app quits: a listing
# can hang for long (a network share, a disk spinning up), and Qt ends the whole process when a QThread
# that is still running is destroyed. They report through a QObject of their own, and never once their
# owner has been shut down (checked under the owner's lock, so no signal is sent after `shutdown`).


class _CountSignals(QObject):
    counted = Signal(str, object)  # folder key, FolderScan


class FolderCounter(QObject):
    changed = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._cache: dict[str, FolderCount] = {}
        self._cond = threading.Condition()
        self._queue: list[tuple[str, str]] = []
        self._queued: set[str] = set()
        self._generation = 0
        self._stop = False
        self._signals = _CountSignals()
        self._signals.counted.connect(self._on_counted)
        self._thread = threading.Thread(target=self._run, name="folder-count", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """(On the counting thread.) Counts the requested folders, the most recent request first."""
        _background_mode()
        while True:
            with self._cond:
                while not self._queue and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                key, path = self._queue.pop()  # most recently requested (visible on screen) first
                generation = self._generation
            done = self._cache.get(key)
            if done is not None and done.fresh():  # learned from a parent folder's count in the meantime
                with self._cond:
                    self._queued.discard(key)
                continue
            result = walk(path, collect=False, budget=COUNT_BUDGET, known=self.known,
                          cancelled=lambda g=generation: self._stop or self._generation != g)
            with self._cond:
                if self._stop:
                    return
                if self._generation == generation:
                    self._signals.counted.emit(key, result)
                else:
                    self._queued.discard(key)

    def peek(self, key: str) -> FolderCount | None:
        return self._cache.get(key)

    def known(self, key: str) -> tuple[int, int] | None:
        """(Called from the counting thread.) The (files, videos) count, if it is known for certain."""
        count = self._cache.get(key)
        if count is None or not (count.complete and count.readable and count.fresh()):
            return None
        return count.photos, count.videos

    def request(self, key: str, path: str) -> None:
        hit = self._cache.get(key)
        if hit is not None and hit.fresh():
            return
        with self._cond:
            if key in self._queued:
                return
            self._queued.add(key)
            self._queue.append((key, path))
            self._cond.notify()

    def get(self, path: str) -> FolderCount | None:
        key = folder_key(path)
        hit = self._cache.get(key)
        if hit is None or not hit.fresh():
            self.request(key, path)  # if stale, the old count is shown until the new one arrives
        return hit

    def clear_queue(self) -> None:
        """Drops the counts that have not started (when the folder changes); a running count finishes."""
        with self._cond:
            self._queue.clear()
            self._queued.clear()

    def invalidate(self) -> None:
        """Forgets all counts (on Refresh / after a run): folders are counted again."""
        with self._cond:
            self._queue.clear()
            self._queued.clear()
            self._generation += 1
        self._cache.clear()
        self.changed.emit()

    def merge(self, scan: FolderScan) -> None:
        """Caches the folder counts from a scan (selection scans contribute too)."""
        if len(self._cache) + len(scan.totals) > _COUNT_CACHE_LIMIT:
            self._cache.clear()
        for key, n in scan.totals.items():
            complete = key not in scan.partial
            old = self._cache.get(key)
            if old is not None and old.complete and not complete:
                continue
            self._cache[key] = FolderCount(n, complete, videos=scan.videos.get(key, 0))

    @Slot(str, object)
    def _on_counted(self, key: str, scan: FolderScan) -> None:
        with self._cond:
            self._queued.discard(key)
        if key not in scan.totals:
            self._cache[key] = FolderCount(0, True, readable=False)
        self.merge(scan)
        self.changed.emit()

    def shutdown(self) -> None:
        with self._cond:
            self._stop = True
            self._queue.clear()
            self._cond.notify_all()
        self._thread.join(SHUTDOWN_WAIT)


# -------------------------------------------------------------------- scanning


@dataclass(eq=False)
class ScanJob:
    root: str
    key: str
    excluded: set[str] = field(default_factory=set)  # subfolders deselected while the scan was running
    cancelled: bool = False

    def covers(self, key: str) -> bool:
        return (not self.cancelled and within(key, self.key)
                and not any(within(key, ex) for ex in self.excluded))


def chain(key: str):
    """The folder itself and all of its parent folders (as keys)."""
    yield key
    parent = os.path.dirname(key)
    while parent != key:
        yield parent
        key, parent = parent, os.path.dirname(parent)


class _ScanSignals(QObject):
    progress = Signal(object, int, str)  # job, photos found, folder being scanned
    done = Signal(object)  # [(job, FolderScan), …]


class FolderScanner(QObject):
    """Scans the selected folders one after another. Jobs are also indexed by folder key:
    "is this folder being scanned?" is answered via the chain of parent folders, regardless
    of the number of jobs (fast even in archives with thousands of subfolders)."""

    progress = Signal(object, int, str)
    finished = Signal(object)  # [(ScanJob, FolderScan), …]
    changed = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._cond = threading.Condition()
        self._pending: deque[ScanJob] = deque()
        self._jobs: dict[int, ScanJob] = {}  # all jobs whose result has not been applied yet
        self._by_key: dict[str, list[ScanJob]] = {}
        self.batch_total = 0  # number of jobs submitted since the scanner was last idle (for progress)
        self.batch_done = 0
        self._stop = False
        self._signals = _ScanSignals()
        self._signals.progress.connect(self.progress)
        self._signals.done.connect(self._on_done)
        self._thread = threading.Thread(target=self._run, name="folder-scan", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """(On the scanning thread.) Scans the submitted folders one after another."""
        while True:
            with self._cond:
                while not self._pending and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
            # Results of small folders are sent in batches of ~0.1 s: when thousands of folders
            # are selected, a separate signal and update for each one would bog down the UI.
            batch = []
            started = time.monotonic()
            while time.monotonic() - started < BATCH_WINDOW:
                with self._cond:
                    if self._stop:
                        return
                    if not self._pending:
                        break
                    job = self._pending.popleft()
                if job.cancelled:
                    batch.append((job, FolderScan(complete=False)))
                    continue
                result = walk(
                    job.root,
                    cancelled=lambda j=job: j.cancelled or self._stop,
                    progress=lambda n, folder, j=job: self._report(j, n, folder),
                )
                batch.append((job, result))
            with self._cond:
                if self._stop:
                    return
                if batch:
                    self._signals.done.emit(batch)

    def _report(self, job: ScanJob, found: int, folder: str) -> None:
        """(On the scanning thread.) Progress of a scan, unless the scanner has been shut down."""
        with self._cond:
            if not self._stop:
                self._signals.progress.emit(job, found, folder)

    def submit(self, paths) -> list[ScanJob]:
        jobs = [ScanJob(os.path.abspath(p), folder_key(p)) for p in paths]
        if not jobs:
            return jobs
        for job in jobs:
            self._jobs[id(job)] = job
            self._by_key.setdefault(job.key, []).append(job)
        self.batch_total += len(jobs)
        with self._cond:
            self._pending.extend(jobs)
            self._cond.notify()
        self.changed.emit()
        return jobs

    def busy(self) -> bool:
        return any(not job.cancelled for job in self._jobs.values())

    def covering(self, key: str) -> ScanJob | None:
        """The (running) scan that covers folder `key`."""
        if not self._jobs:
            return None
        for folder in chain(key):
            for job in self._by_key.get(folder, ()):
                if job.covers(key):
                    return job
        return None

    def include(self, key: str) -> bool:
        """Re-includes a folder that was excluded from a running scan."""
        for folder in chain(key):
            for job in self._by_key.get(folder, ()):
                if not job.cancelled and job.excluded:
                    job.excluded = {ex for ex in job.excluded if not within(ex, key)}
        if self.covering(key) is not None:
            self.changed.emit()
            return True
        return False

    def exclude(self, keys) -> None:
        """Removes folders from the scans: scans inside them are cancelled, and they are
        dropped from the results of the scans that cover them."""
        targets = set(keys)
        if not targets or not self._jobs:
            return
        touched = False
        for job in self._jobs.values():
            if not job.cancelled and any(folder in targets for folder in chain(job.key)):
                job.cancelled = True
                touched = True
        for key in targets:
            for folder in chain(os.path.dirname(key)):
                for job in self._by_key.get(folder, ()):
                    if not job.cancelled and folder != key:
                        job.excluded.add(key)
                        touched = True
        if touched:
            self.changed.emit()

    def cancel_all(self) -> None:
        for job in self._jobs.values():
            job.cancelled = True
        self.changed.emit()

    def jobs_covering_children(self, parent_key: str) -> list[ScanJob]:
        """Jobs scanning folder `parent_key` or one of its parent folders."""
        if not self._jobs:
            return []
        return [job for folder in chain(parent_key) for job in self._by_key.get(folder, ()) if not job.cancelled]

    def jobs_at(self, key: str) -> list[ScanJob]:
        return self._by_key.get(key, []) if self._jobs else []

    @Slot(object)
    def _on_done(self, batch) -> None:
        self.batch_done += len(batch)
        try:
            self.finished.emit(batch)
        finally:
            for job, _ in batch:
                self._jobs.pop(id(job), None)
                same = self._by_key.get(job.key)
                if same is not None:
                    same.remove(job)
                    if not same:
                        del self._by_key[job.key]
            if not self._jobs:
                self.batch_total = self.batch_done = 0
            self.changed.emit()

    def shutdown(self) -> None:
        with self._cond:
            self._stop = True
            for job in self._pending:
                job.cancelled = True
            self._pending.clear()
            self._cond.notify_all()
        for job in self._jobs.values():
            job.cancelled = True
        self._thread.join(SHUTDOWN_WAIT)


# ------------------------------------------------------------------ coordination


class FolderSelection(QObject):
    """Selection state of folders and the operations that select/deselect folders.

    States: NONE (nothing selected), PARTIAL (some), FULL (all),
    SCANNING (selected, contents are being scanned).
    """

    changed = Signal()  # the displayed state of folders may have changed (at most ~10 times/s)
    progress = Signal(str, int, str)  # scanned root, photos found, current folder
    scanned = Signal(str, int, int, int, bool)  # root, found, added, unreadable folders, cancelled?

    def __init__(self, selection: Selection, parent: QObject | None = None,
                 confirm: Callable[[int], bool] | None = None, bulk_limit: int = 10_000):
        super().__init__(parent)
        self.selection = selection
        self.confirm = confirm
        self.bulk_limit = bulk_limit
        self.counter = FolderCounter(self)
        self.scanner = FolderScanner(self)
        # Items added by one selection action (a single click, "Select all" …): a large selection
        # asks for confirmation once; if the answer is "no", everything it added is removed again.
        self._batch_keys: list[str] = []
        self._batch_asked = False
        self._throttle = QTimer(self)
        self._throttle.setSingleShot(True)
        self._throttle.setInterval(100)
        self._throttle.timeout.connect(self._flush)
        self._dirty = False
        selection.changed.connect(self._notify)
        self.counter.changed.connect(self._notify)
        self.scanner.changed.connect(self._notify)
        self.scanner.progress.connect(lambda job, n, folder: self.progress.emit(job.root, n, folder))
        self.scanner.finished.connect(self._scan_batch)

    def _notify(self) -> None:
        """The first change is reported at once (instant response to a click); the ones after it
        are coalesced: while thousands of folders are scanned, views update ~10 times a second."""
        if self._throttle.isActive():
            self._dirty = True
            return
        self.changed.emit()
        self._throttle.start()

    def _flush(self) -> None:
        if self._dirty:
            self._dirty = False
            self.changed.emit()
            self._throttle.start()

    # ------------------------------------------------------------------ state

    def state(self, path: str, key: str | None = None) -> int:
        """The folder's selection state. Whether all of it is selected is decided from the
        folder's photo count; if the count is unknown (the folder has not been counted yet),
        PARTIAL is returned. No count is started here: the folder tree must not trigger counts
        of every parent of the selected folders (e.g. the whole user folder); the visible tiles
        in the grid are counted anyway, and the counts of selected folders come from their scans."""
        key = key or folder_key(path)
        if self.scanner.covering(key) is not None:
            return SCANNING
        n = self.selection.selected_under(key)
        if n == 0:
            return NONE
        count = self.counter.peek(key)
        if count is None:
            return PARTIAL
        return FULL if count.complete and n >= count.photos else PARTIAL

    def states(self, parent_path: str, entries) -> list[int]:
        """States of the subfolders of one folder, in bulk and fast (for thousands of folders).
        `entries`: items with a `key` attribute (the folder tiles of the Browse grid)."""
        parent_jobs = self.scanner.jobs_covering_children(folder_key(parent_path)) if parent_path else []
        states = []
        for e in entries:
            key = e.key
            if any(job.covers(key) for job in parent_jobs) or any(job.covers(key) for job in self.scanner.jobs_at(key)):
                states.append(SCANNING)
                continue
            n = self.selection.selected_under(key)
            if n == 0:
                states.append(NONE)
                continue
            count = self.counter.peek(key)
            states.append(FULL if count is not None and count.complete and n >= count.photos else PARTIAL)
        return states

    def count(self, path: str) -> FolderCount | None:
        return self.counter.get(path)

    def selected_in(self, path: str) -> int:
        return self.selection.selected_under(folder_key(path))

    def busy(self) -> bool:
        return self.scanner.busy()

    # ---------------------------------------------------------------- operations

    def toggle(self, path: str) -> None:
        self.set_selected([path], self.state(path) in (NONE, PARTIAL))

    def set_selected(self, paths, selected: bool) -> None:
        paths = list(paths)
        if not selected:
            self.scanner.exclude(folder_key(p) for p in paths)
            self.selection.remove_under(paths)
            return
        todo = []
        for path in paths:
            key = folder_key(path)
            if self.scanner.covering(key) is not None or self.scanner.include(key):
                continue
            if self.state(path, key) == FULL:
                continue
            todo.append(path)
        if todo and not self.scanner.busy():
            self._batch_keys = []
            self._batch_asked = False
        self.scanner.submit(todo)

    def cancel_all(self) -> None:
        self.scanner.cancel_all()

    def invalidate_counts(self) -> None:
        self.counter.invalidate()

    def _scan_batch(self, batch) -> None:
        with self.selection.batch():  # a single "selection changed" notification for the batch
            for job, result in batch:
                self._scan_finished(job, result)

    def _scan_finished(self, job: ScanJob, result: FolderScan) -> None:
        self.counter.merge(result)
        if job.cancelled:
            self.scanned.emit(job.root, result.count, 0, result.unreadable, True)
            return
        photos = result.photos
        if job.excluded:
            excluded = tuple(job.excluded)
            photos = [p for p in photos
                      if not any(within(os.path.normcase(os.path.dirname(p[0])), ex) for ex in excluded)]
        new = sum(1 for p in photos if p[0] not in self.selection)
        total = len(self._batch_keys) + new
        if self.confirm is not None and not self._batch_asked and total > self.bulk_limit:
            self._batch_asked = True
            if not self.confirm(total):
                # Everything added by this selection action is removed and the remaining scans are stopped.
                # This scan is reported before they stop: the window sums up the results once nothing is
                # being scanned any more.
                self.selection.remove(self._batch_keys)
                self._batch_keys = []
                self.scanned.emit(job.root, len(photos), 0, result.unreadable, True)
                self.scanner.cancel_all()
                return
        added = self.selection.add_entries(photos, root=job.root, record=self._batch_keys)
        self.scanned.emit(job.root, len(photos), added, result.unreadable, False)

    def shutdown(self) -> None:
        self._throttle.stop()
        self.scanner.shutdown()
        self.counter.shutdown()
