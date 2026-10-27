"""The list of photos selected from different folders (the selection basket).

The selection is kept while browsing between folders. When a folder is selected,
all photos in it (subfolders included) are added and the folder they were selected
with (the root) is stored; it is used to keep the folder structure in the output.

Info such as date, camera and GPS is read in the background for every photo; it
is needed for sorting and previews. To keep the UI smooth even with selections of
tens of thousands of photos, requests wait in a single queue (photos removed from
the selection are never read), results are reported in batches, and the selected
photo counts per folder are updated once per folder rather than once per photo.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections import deque

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

from ..core.errors import explain
from ..core.folderscan import folder_key
from ..core.formats import Fmt
from ..core.photoinfo import PhotoInfo, read_info
from ..core.planner import Photo
from .edit_store import EditStore

INFO_THREADS = 2
_CACHE_LIMIT = 200_000


def norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _within(key: str, root_key: str) -> bool:
    return key == root_key or key.startswith(root_key.rstrip("\\/") + os.sep)


def moved(items: list, moving: set, delta: int) -> list:
    """`items` with the ones in `moving` taken `delta` places up (-1) or down (+1); moving items do not
    pass one another, so a block of them moves as one."""
    items = list(items)
    indices = [i for i, item in enumerate(items) if item in moving]
    if delta < 0:
        for i in indices:
            j = max(0, i + delta)
            while j < i and items[j] in moving:
                j += 1
            items.insert(j, items.pop(i))
    else:
        for i in reversed(indices):
            j = min(len(items) - 1, i + delta)
            while j > i and items[j] in moving:
                j -= 1
            items.insert(j, items.pop(i))
    return items


class _InfoEmitter(QObject):
    done = Signal(str, float, object)


class _InfoWorker(QRunnable):
    """Reads photo info for each job it takes from the queue; finishes when the queue is empty."""

    def __init__(self, owner: Selection):
        super().__init__()
        self.owner = owner

    def run(self) -> None:
        while True:
            job = self.owner._next_job()
            if job is None:
                return
            path, mtime = job
            try:
                info = read_info(path)
            except Exception as exc:  # noqa: BLE001 - one file the reader trips over must not stop the others
                info = PhotoInfo(error=explain(exc))  # it is listed as unreadable
            self.owner._emitter.done.emit(path, mtime, info)  # (always: the file is no longer waiting)


class Selection(QObject):
    changed = Signal()  # membership or order changed
    infoChanged = Signal()  # new photo info arrived (batched)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._items: dict[str, Photo] = {}
        self._under: dict[str, int] = {}  # folder key → number selected beneath it (all subfolders included)
        self._direct: dict[str, int] = {}  # folder key → number selected directly inside it
        self._chains: dict[str, tuple[str, ...]] = {}  # folder key → the folder itself and all its parents
        self._missing = 0  # number of selected photos whose info has not been read yet
        # RAW photos, videos and files with a location among the selected ones, kept up to date as files
        # come and go and as their info arrives: the window asks for them after every batch of info.
        self._raw = self._videos = self._gps = 0
        self._cache: dict[tuple[str, float, int], PhotoInfo] = {}
        self._lock = threading.Lock()
        self._queue: deque[tuple[str, str, float]] = deque()
        self._queued: set[str] = set()
        self._workers = 0
        self._paused = False
        self._held = 0
        self._batch_depth = 0
        self._changed_pending = False
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(INFO_THREADS)
        self._emitter = _InfoEmitter()
        self._emitter.done.connect(self._on_info)
        self._batch = QTimer(self)
        self._batch.setSingleShot(True)
        self._batch.setInterval(250)
        self._batch.timeout.connect(self.infoChanged)
        # Rotate/crop edits: those of unselected photos are kept too (they come back when
        # the photo is selected again); for selected photos they are also mirrored onto `Photo.edit`.
        self.edits = EditStore(self)
        self.edits.changed.connect(self._edits_changed)

    @contextlib.contextmanager
    def batch(self):
        """Batch change: a single `changed` signal for all the changes made inside it."""
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
            if not self._batch_depth and self._changed_pending:
                self._changed_pending = False
                self.changed.emit()

    def _emit_changed(self) -> None:
        if self._batch_depth:
            self._changed_pending = True
        else:
            self.changed.emit()

    # ------------------------------------------------------------------- queries

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, path: str) -> bool:
        return norm(path) in self._items

    def has_key(self, key: str) -> bool:
        """`path in selection` for a path already made a key with `norm` (no path work per call)."""
        return key in self._items

    def items(self) -> list[Photo]:
        return list(self._items.values())

    def paths(self) -> list[str]:
        return [p.path for p in self._items.values()]

    def folders(self) -> set[str]:
        """Folders that contain selected photos (as keys)."""
        return set(self._direct)

    def selected_under(self, key: str) -> int:
        """Number of selected photos inside folder `key` (subfolders included)."""
        return self._under.get(key, 0)

    def pending(self) -> int:
        """Number of selected photos whose info has not been read yet."""
        return self._missing

    def gps_count(self) -> int:
        """Number of selected files known to carry a location."""
        return self._gps

    def raw_count(self) -> int:
        return self._raw

    def video_count(self) -> int:
        return self._videos

    def _count(self, photo: Photo, sign: int) -> None:
        """Adds a photo to the running counts (sign 1) or takes it out of them (sign -1)."""
        if photo.fmt() == Fmt.RAW:
            self._raw += sign
        if photo.is_video:
            self._videos += sign
        if photo.info is not None and photo.info.has_gps:
            self._gps += sign

    def info(self, path: str) -> PhotoInfo | None:
        photo = self._items.get(norm(path))
        return photo.info if photo else None

    def photo(self, path: str) -> Photo | None:
        return self._items.get(norm(path))

    def _edited_keys(self) -> list[str]:
        return [k for k in self.edits.keys() if (p := self._items.get(k)) is not None and p.edit is not None]

    def edited_count(self) -> int:
        """Number of selected photos that have been edited (rotated/cropped)."""
        return len(self._edited_keys())

    def pending_edits(self) -> int:
        """Number of selected photos whose edit has never been applied yet."""
        return self.edits.pending(self._edited_keys())

    def forget_info(self, paths) -> None:
        """Drops the cached info of the files (after their content was changed in place)."""
        keys = {norm(p) for p in paths}
        for cached in [k for k in self._cache if k[0] in keys]:
            del self._cache[cached]

    def _edits_changed(self, keys: list[str]) -> None:
        for key in keys:
            photo = self._items.get(key)
            if photo is not None:
                photo.edit = self.edits.get_key(key, photo.mtime)

    def photos(self) -> list[Photo]:
        photos = list(self._items.values())
        for order, photo in enumerate(photos):
            photo.order = order
        return photos

    # ------------------------------------------------------------------ changes

    def add(self, paths) -> int:
        """Adds individually selected photos."""
        entries = []
        for path in paths:
            if norm(path) in self._items:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            entries.append((os.path.abspath(path), st.st_size, st.st_mtime))
        return self.add_entries(entries)

    def add_entries(self, entries, root: str | None = None, record: list[str] | None = None) -> int:
        """Adds photos whose size and date are already known (from a folder scan).

        `root`: the folder the photos were selected with. When a folder higher up is
        selected, it becomes the root of already selected photos (folder structure).
        `record`: if given, the keys of newly added photos are appended to it (for undo).
        """
        root = os.path.abspath(root) if root else None
        root_key = folder_key(root) if root else None
        added = rerooted = 0
        deltas: dict[str, int] = {}
        requests: list[tuple[str, str, float]] = []
        for path, size, mtime in entries:
            key = os.path.normcase(path)
            photo = self._items.get(key)
            if photo is not None:
                if root_key and (photo.root is None or (
                    folder_key(photo.root) != root_key and _within(folder_key(photo.root), root_key)
                )):
                    photo.root = root
                    rerooted += 1
                continue
            photo = Photo(path, size, mtime, info=self._cache.get((key, mtime, size)), root=root,
                          edit=self.edits.get_key(key, mtime))
            self._items[key] = photo
            self._count(photo, 1)
            if record is not None:
                record.append(key)
            deltas[photo.folder_key] = deltas.get(photo.folder_key, 0) + 1
            if photo.info is None:
                self._missing += 1
                requests.append((key, path, mtime))
            added += 1
        self._apply(deltas)
        self._request_many(requests)
        if added or rerooted:
            self._emit_changed()
        return added

    def remove(self, paths) -> int:
        return self._remove_keys([norm(p) for p in paths])

    def entry(self, path: str) -> tuple[Photo, int] | None:
        """The selected photo's record and its position in the list (kept for undo)."""
        key = norm(path)
        photo = self._items.get(key)
        if photo is None:
            return None
        return photo, list(self._items).index(key)

    def restore(self, photo: Photo, index: int) -> None:
        """Puts a photo removed from the selection back in its old place, with its root and already read info."""
        key = os.path.normcase(photo.path)
        if key in self._items:
            return
        items = list(self._items.items())
        items.insert(max(0, min(index, len(items))), (key, photo))
        self._items = dict(items)
        photo.edit = self.edits.get_key(key, photo.mtime)
        self._count(photo, 1)
        self._apply({photo.folder_key: 1})
        if photo.info is None:
            self._missing += 1
            self._request_many([(key, photo.path, photo.mtime)])
        self._emit_changed()

    def remove_under(self, folders) -> int:
        """Removes all photos in the folders (subfolders included) from the selection.
        Each photo's folder chain is checked against the set: fast even when thousands of folders are deselected."""
        targets = {folder_key(f) for f in folders}
        if not targets:
            return 0
        inside: dict[str, bool] = {}
        keys = []
        for key, photo in self._items.items():
            hit = inside.get(photo.folder_key)
            if hit is None:
                hit = inside[photo.folder_key] = any(f in targets for f in self._chain(photo.folder_key))
            if hit:
                keys.append(key)
        return self._remove_keys(keys)

    def _remove_keys(self, keys) -> int:
        removed = 0
        deltas: dict[str, int] = {}
        for key in keys:
            photo = self._items.pop(key, None)
            if photo is None:
                continue
            self._count(photo, -1)
            deltas[photo.folder_key] = deltas.get(photo.folder_key, 0) - 1
            if photo.info is None:
                self._missing -= 1
            removed += 1
        self._apply(deltas)
        if removed:
            self._emit_changed()
        return removed

    def toggle(self, path: str) -> bool:
        if path in self:
            self.remove([path])
            return False
        self.add([path])
        return True

    def set_selected(self, paths, selected: bool) -> None:
        if selected:
            self.add(paths)
        else:
            self.remove(paths)

    def clear(self) -> None:
        if self._items:
            self._items.clear()
            self._under.clear()
            self._direct.clear()
            self._missing = 0
            self._raw = self._videos = self._gps = 0
            self._emit_changed()

    def reorder(self, paths_in_order: list[str]) -> None:
        """Manual order: rearranges the items in the given order."""
        keys = [k for k in map(norm, paths_in_order) if k in self._items]
        chosen = set(keys)
        rest = [k for k in self._items if k not in chosen]
        self._items = {k: self._items[k] for k in keys + rest}
        self._emit_changed()

    def move(self, paths: list[str], delta: int) -> None:
        moving = {norm(p) for p in paths} & self._items.keys()
        if not moving:
            return
        self._items = {k: self._items[k] for k in moved(list(self._items), moving, delta)}
        self._emit_changed()

    def drop_missing(self) -> None:
        """Removes photos that no longer exist on disk (one directory listing per folder)."""
        listings: dict[str, set[str] | None] = {}
        gone = []
        for key, photo in self._items.items():
            names = listings.get(photo.folder_key, False)
            if names is False:
                try:
                    names = {os.path.normcase(n) for n in os.listdir(photo.folder)}
                except OSError:
                    names = None
                listings[photo.folder_key] = names
            if names is None or os.path.normcase(photo.name) not in names:
                gone.append(key)
        self._remove_keys(gone)

    def _chain(self, key: str) -> tuple[str, ...]:
        chain = self._chains.get(key)
        if chain is None:
            parts = [key]
            parent = os.path.dirname(key)
            while parent != parts[-1]:
                parts.append(parent)
                parent = os.path.dirname(parent)
            chain = self._chains[key] = tuple(parts)
        return chain

    def _apply(self, deltas: dict[str, int]) -> None:
        """Updates the folder counters once per folder (not once per photo)."""
        for key, delta in deltas.items():
            n = self._direct.get(key, 0) + delta
            if n:
                self._direct[key] = n
            else:
                self._direct.pop(key, None)
            for folder in self._chain(key):
                n = self._under.get(folder, 0) + delta
                if n:
                    self._under[folder] = n
                else:
                    self._under.pop(folder, None)

    # ------------------------------------------------------------ background

    def _can_run(self) -> bool:
        return not self._paused and not self._held

    def _kick(self) -> None:
        """Starts readers (up to INFO_THREADS) if the queue has work and reading is not paused or held."""
        with self._lock:
            start = 0
            if self._can_run():
                start = max(0, min(INFO_THREADS - self._workers, len(self._queue)))
                self._workers += start
        for _ in range(start):
            self._pool.start(_InfoWorker(self))

    def _request_many(self, requests: list[tuple[str, str, float]]) -> None:
        if not requests:
            return
        with self._lock:
            for key, path, mtime in requests:
                if key not in self._queued:
                    self._queued.add(key)
                    self._queue.append((key, path, mtime))
        self._kick()

    def _next_job(self) -> tuple[str, float] | None:
        """(On a worker thread) the next photo that is still selected and still lacks its info."""
        with self._lock:
            while self._queue and self._can_run():
                key, path, mtime = self._queue.popleft()
                photo = self._items.get(key)
                if photo is not None and photo.info is None:
                    return path, mtime
                self._queued.discard(key)
            self._workers -= 1
            return None

    @Slot(str, float, object)
    def _on_info(self, path: str, mtime: float, info: PhotoInfo) -> None:
        key = os.path.normcase(path)
        with self._lock:
            self._queued.discard(key)
        photo = self._items.get(key)
        if photo is not None and photo.info is None:
            if photo.mtime != mtime:
                self._request_many([(key, photo.path, photo.mtime)])  # the file changed in the meantime
            else:
                self._count(photo, -1)  # (the content may tell another format than the name, and a location)
                photo.info = info
                self._count(photo, 1)
                self._missing -= 1
                if len(self._cache) >= _CACHE_LIMIT:
                    self._cache.clear()
                self._cache[(key, mtime, photo.size)] = info
        if not self._batch.isActive():
            self._batch.start()

    def hold_info(self) -> None:
        """Holds info reading back briefly (without waiting for running reads) so the readers
        don't compete for the GIL while the UI thread does heavy work (planning)."""
        with self._lock:
            self._held += 1

    def release_info(self) -> None:
        with self._lock:
            self._held = max(0, self._held - 1)
        self._kick()

    def pause_info(self) -> None:
        """Stops info reading while a job runs and waits for running reads to finish
        (so files are not held open in another thread while they are being moved)."""
        with self._lock:
            self._paused = True
        self._pool.waitForDone(5000)

    def resume_info(self) -> None:
        with self._lock:
            self._paused = False
        self._kick()

    def shutdown(self) -> None:
        with self._lock:
            self._paused = True
            self._queue.clear()
        self._pool.waitForDone(2000)
