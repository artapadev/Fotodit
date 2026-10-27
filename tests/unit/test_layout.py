"""The project's layout keeps itself: every folder and top-level file git tracks is on the approved list, and
the core never imports Qt (or the user interface), so it runs and is tested without a window."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Each folder by its purpose; a new one is added here on purpose.
FOLDERS = {
    "docs",
    "fotodit", "fotodit/core", "fotodit/core/meta", "fotodit/core/video", "fotodit/i18n",
    "fotodit/resources", "fotodit/resources/fonts", "fotodit/resources/icons", "fotodit/ui",
    "licenses", "packaging", "tests", "tests/unit", "tests/ui",
}
FILES = {  # at the top level
    ".editorconfig", ".gitattributes", ".gitignore", "LICENSE", "README.md", "THIRD-PARTY-NOTICES.md",
    "requirements.txt", "requirements-dev.txt",
}


def _tracked() -> list[str]:
    """The files git tracks, that is the published project; none outside a git checkout."""
    try:
        listing = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [p for p in listing.decode("utf-8").split("\0") if p]


TRACKED = _tracked()
needs_git = pytest.mark.skipif(not TRACKED, reason="not a git checkout")


@needs_git
def test_every_folder_is_approved():
    found = {str(parent) for p in TRACKED for parent in PurePosixPath(p).parents if str(parent) != "."}
    assert found <= FOLDERS, sorted(found - FOLDERS)


@needs_git
def test_top_level_files_are_approved():
    top = {p for p in TRACKED if "/" not in p}
    assert top <= FILES, sorted(top - FILES)


def _imports(path: Path) -> set[str]:
    """Every module a file imports, wherever in the file (relative ones as written, with dots)."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            names.add(module)
            if node.module is None:  # "from .. import ui"
                names.update(module + alias.name for alias in node.names)
    return names


def test_core_never_imports_qt_or_the_ui():
    for path in sorted((ROOT / "fotodit" / "core").rglob("*.py")):
        for name in _imports(path):
            bare = name.lstrip(".")
            assert not bare.startswith(("PySide6", "shiboken6")), f"{path.relative_to(ROOT)} imports {name}"
            assert not (bare == "ui" or bare.startswith("ui.") or bare.startswith("fotodit.ui")), \
                f"{path.relative_to(ROOT)} imports {name}"


def test_unit_tests_need_no_qt():
    for path in sorted((ROOT / "tests" / "unit").glob("*.py")):
        for name in _imports(path):
            assert not name.startswith(("PySide6", "fotodit.ui")), f"{path.relative_to(ROOT)} imports {name}"
