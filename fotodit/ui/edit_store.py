"""Holds rotate/crop edits until a run is started.

Edits are not written to the files right away; they are applied when the run starts
(together with the other options). Photos processed with "apply to originals (in place)"
lose their stored edit (it is now part of the file itself). Each edit is also stored with
the file's modification time (mtime) at that moment: if another program changes the file
in the meantime, the edit is treated as invalid (it was made against the old image).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable

from PySide6.QtCore import QObject, Signal

from ..core.edits import Edit

MTIME_TOLERANCE = 2.0  # seconds


def key_of(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


class EditStore(QObject):
    changed = Signal(list)  # keys of the changed photos (normcase paths)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._edits: dict[str, tuple[Edit, float]] = {}
        self._applied: set[str] = set()  # edits already applied by saving to a new folder

    def __len__(self) -> int:
        return len(self._edits)

    def keys(self) -> list[str]:
        return list(self._edits)

    def get(self, path: str, mtime: float | None = None) -> Edit | None:
        return self.get_key(key_of(path), mtime)

    def get_key(self, key: str, mtime: float | None = None) -> Edit | None:
        hit = self._edits.get(key)
        # Times read from the folder listing and from the file itself can differ slightly;
        # only a clear difference counts as "the file changed after it was edited".
        if hit is None or (mtime is not None and abs(hit[1] - mtime) > MTIME_TOLERANCE):
            return None
        return hit[0]

    def set(self, path: str, mtime: float, edit: Edit | None) -> None:
        key = key_of(path)
        if edit is None or edit.is_identity():
            if self._edits.pop(key, None) is None:
                return
        else:
            if self._edits.get(key) == (edit, mtime):
                return
            self._edits[key] = (edit, mtime)
        self._applied.discard(key)
        self.changed.emit([key])

    def update(self, items: Iterable[tuple[str, float]], change: Callable[[Edit], Edit]) -> list[str]:
        """Changes the edits of several photos at once (e.g. rotate them all right)."""
        keys = []
        for path, mtime in items:
            key = key_of(path)
            new = change(self.get_key(key, mtime) or Edit())
            if new.is_identity():
                if self._edits.pop(key, None) is None:
                    continue
            else:
                self._edits[key] = (new, mtime)
            self._applied.discard(key)
            keys.append(key)
        if keys:
            self.changed.emit(keys)
        return keys

    def clear(self, paths: Iterable[str]) -> None:
        keys = [k for k in map(key_of, paths) if self._edits.pop(k, None) is not None]
        self._applied.difference_update(keys)
        if keys:
            self.changed.emit(keys)

    def mark_applied(self, paths: Iterable[str]) -> None:
        """Marks edits as applied by saving to a new folder (no warning about them on close)."""
        self._applied.update(k for k in map(key_of, paths) if k in self._edits)

    def pending(self, keys: Iterable[str]) -> int:
        """How many of the given photos have an edit that has not been applied anywhere yet."""
        return sum(1 for k in keys if k in self._edits and k not in self._applied)
