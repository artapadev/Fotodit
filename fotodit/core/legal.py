"""Where Fotodit's license, the third-party notices and the license texts are.

The packaged app carries them in its bundle's "legal" folder (see packaging/fotodit.spec); a release also puts
them next to Fotodit.exe (packaging/package_release.py). Run from source, they are in the project folder.
"""

from __future__ import annotations

import os
import sys

LICENSE = "LICENSE"
NOTICES = "THIRD-PARTY-NOTICES.md"


def legal_folder() -> str | None:
    """The folder with LICENSE, THIRD-PARTY-NOTICES.md and licenses/ (None if it is missing)."""
    if getattr(sys, "frozen", False):
        folder = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "legal")
    else:
        folder = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return folder if os.path.isfile(os.path.join(folder, LICENSE)) else None


def licenses_folder() -> str | None:
    """The license texts' folder to open in Explorer: in a release the one next to Fotodit.exe (the
    single-file version's own copy is in a temporary folder that goes away when it closes)."""
    if getattr(sys, "frozen", False):
        beside = os.path.join(os.path.dirname(sys.executable), "licenses")
        if os.path.isdir(beside):
            return beside
    folder = legal_folder()
    path = os.path.join(folder, "licenses") if folder is not None else None
    return path if path is not None and os.path.isdir(path) else None


def read_text(name: str) -> str | None:
    """A legal file's text (LICENSE or NOTICES), or None."""
    folder = legal_folder()
    if folder is None:
        return None
    try:
        with open(os.path.join(folder, name), encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None
