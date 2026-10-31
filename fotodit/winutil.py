"""Windows-specific helpers: Explorer sort order, Recycle Bin, opening folders, safe file names."""

from __future__ import annotations

import ctypes
import functools
import os
import re
import subprocess
import sys
from ctypes import wintypes

from .i18n import t

IS_WINDOWS = sys.platform == "win32"

# ------------------------------------------------------------ Explorer name order

if IS_WINDOWS:
    _StrCmpLogicalW = ctypes.windll.shlwapi.StrCmpLogicalW
    _StrCmpLogicalW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    _StrCmpLogicalW.restype = ctypes.c_int

    def _cmp(a: str, b: str) -> int:
        return _StrCmpLogicalW(a, b)

    explorer_key = functools.cmp_to_key(_cmp)
    """Sort key identical to Windows Explorer's name order (1, 2, 10 …)."""
else:  # pragma: no cover - fallback for non-Windows environments

    def explorer_key(name: str):
        return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", name)]


# ------------------------------------------------------------- Recycle Bin


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


_FO_DELETE = 0x0003
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400
_FOF_WANTNUKEWARNING = 0x4000


def recycle(paths: list[str]) -> set[str]:
    """Moves files to the Recycle Bin. Returns the set of files that could not be moved.

    Single files are moved with Qt's `QFile.moveToTrash` (IFileOperation): if a file cannot go to
    the Recycle Bin (e.g. on a network/USB drive), it fails instead of deleting the file
    permanently, and the original is left untouched.

    Multiple files on local disks with the Recycle Bin enabled are moved in a single shell operation
    (~12 ms per file; ~45 ms one by one). If this path would need a permanent delete too, Windows
    asks (FOF_WANTNUKEWARNING); files that could not be moved are then retried one by one.
    """
    paths = [os.path.abspath(p) for p in paths]
    if IS_WINDOWS and len(paths) > 1:
        roots: dict[str, str | None] = {}
        batch = []
        for p in paths:
            folder = os.path.dirname(p)
            if folder not in roots:
                roots[folder] = volume_root(folder)
            root = roots[folder]
            if root and _batch_recycle_ok(root):
                batch.append(p)
        if batch:
            _with_com(_recycle_legacy, batch)
            paths = [p for p in paths if os.path.lexists(p)]
    try:
        from PySide6.QtCore import QFile
    except ImportError:  # pragma: no cover - no Qt: use the legacy API
        return _recycle_legacy(paths)
    failed: set[str] = set()
    for p in paths:
        result = QFile.moveToTrash(p)
        ok = result[0] if isinstance(result, tuple) else bool(result)
        if not ok or os.path.lexists(p):
            failed.add(p)
    return failed


def _with_com(fn, *args):
    """Shell operations need COM; calls `fn` with COM initialized (for worker threads)."""
    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    try:
        return fn(*args)
    finally:
        if hr in (0, 1):  # S_OK, S_FALSE
            ole32.CoUninitialize()


_DRIVE_FIXED = 3
_BITBUCKET = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"


def volume_root(path: str) -> str | None:
    """Root of the volume that contains the path (e.g. "D:\\")."""
    if not IS_WINDOWS:  # pragma: no cover
        return None
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetVolumePathNameW(path, buf, len(buf)):
        return None
    return buf.value


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HANDLE),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", ctypes.c_wchar * 260),
        ("szTypeName", ctypes.c_wchar * 80),
    ]


_SHGFI_DISPLAYNAME = 0x200


def drive_label(root: str) -> str:
    """The name Explorer shows for a drive, "Yerel Disk (C:)"; the bare "C:" if there is none."""
    bare = root.rstrip("\\/") or root
    if not IS_WINDOWS:  # pragma: no cover
        return bare
    info = _SHFILEINFOW()

    def ask() -> int:
        return ctypes.windll.shell32.SHGetFileInfoW(ctypes.c_wchar_p(root), 0, ctypes.byref(info),
                                                    ctypes.sizeof(info), _SHGFI_DISPLAYNAME)

    try:
        ok = _with_com(ask)
    except OSError:
        ok = 0
    return info.szDisplayName if ok and info.szDisplayName else bare


@functools.lru_cache(maxsize=64)
def _volume_settings(root: str) -> tuple[bool, int | None]:
    """(Is the Recycle Bin enabled, its maximum size in bytes — None if unknown)."""
    import winreg

    buf = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buf, len(buf)):
        return True, None
    name = buf.value
    guid = name[name.find("{"):name.find("}") + 1]
    if not guid:
        return True, None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{_BITBUCKET}\\{guid}") as key:
            try:
                nuke = int(winreg.QueryValueEx(key, "NukeOnDelete")[0])
            except OSError:
                nuke = 0
            try:
                capacity = int(winreg.QueryValueEx(key, "MaxCapacity")[0]) * 1024 * 1024
            except OSError:
                capacity = None
    except OSError:
        return True, None
    return nuke == 0, capacity


@functools.lru_cache(maxsize=1)
def _recycling_disabled_by_policy() -> bool:
    """Is the group policy "Do not move deleted files to the Recycle Bin" (NoRecycleFiles) enabled?"""
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer") as key:
                if int(winreg.QueryValueEx(key, "NoRecycleFiles")[0]):
                    return True
        except OSError:
            continue
    return False


def _batch_recycle_ok(root: str) -> bool:
    """Batch moves (SHFileOperation) are used only on local disks whose Recycle Bin is enabled."""
    return (ctypes.windll.kernel32.GetDriveTypeW(root) == _DRIVE_FIXED and _volume_settings(root)[0]
            and not _recycling_disabled_by_policy())


def recycle_bin_capacity(path: str) -> int | None:
    """Maximum size (bytes) of the Recycle Bin on the disk that contains the path.

    If it is not in the registry, the Windows default (~5% of the disk) is assumed; returns 0 if
    the Recycle Bin is disabled.
    """
    root = volume_root(path)
    if not root:
        return None
    enabled, capacity = _volume_settings(root)
    if not enabled or _recycling_disabled_by_policy():
        return 0
    if capacity is None:
        import shutil

        try:
            capacity = shutil.disk_usage(root).total // 20
        except OSError:
            return None
    return capacity


def _recycle_legacy(paths: list[str]) -> set[str]:
    """Batch move with SHFileOperationW; Windows warns if a permanent delete would be needed."""
    if not paths:
        return set()
    if not IS_WINDOWS:  # pragma: no cover
        raise OSError(t("winutil.no_bin"))
    op = _SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = "\0".join(paths) + "\0\0"
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI | _FOF_WANTNUKEWARNING
    ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return {p for p in paths if os.path.lexists(p)}


# --------------------------------------------------------------------- opening folders


def reveal(path: str) -> None:
    """Opens a folder in Explorer; for a file, opens its folder with the file selected."""
    if not IS_WINDOWS:  # pragma: no cover
        return
    if os.path.isdir(path):
        os.startfile(path)  # noqa: S606 - the user's folder
    else:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])  # noqa: S603, S607


_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename_part(text: str) -> str:
    """Removes characters that cannot be used in file names."""
    text = _INVALID.sub("", text).strip()
    text = text.rstrip(". ")
    if text.upper() in _RESERVED:
        text = f"_{text}"
    return text
