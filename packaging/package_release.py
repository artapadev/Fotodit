"""Packs a release from the two builds (see packaging\\build.ps1 -Release).

    .venv\\Scripts\\python.exe packaging\\package_release.py

Writes to dist\\release:
  Fotodit-<version>-windows-x64.zip           the folder build (Fotodit\\Fotodit.exe and _internal)
  Fotodit-<version>-windows-x64-portable.zip  the single-file exe
  Fotodit-<version>-source.zip                the source code (the GPL's "Corresponding Source" for Fotodit):
                                              the files git tracks, so it needs a git checkout
  SHA256SUMS.txt                              checksums of the three
Both program zips carry LICENSE.txt, THIRD-PARTY-NOTICES.txt and the licenses folder next to
Fotodit.exe (copied from the build's own "legal" folder, see packaging/fotodit.spec). Nothing is packed if a
license text that THIRD-PARTY-NOTICES.md names is missing from the build.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = ("LICENSE", "THIRD-PARTY-NOTICES.md", "README.md", "requirements.txt", "requirements-dev.txt",
                ".gitignore")
SOURCE_DIRS = ("fotodit", "tests", "docs", "packaging", "licenses")


class ReleaseError(Exception):
    pass


def version() -> str:
    text = (ROOT / "fotodit" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    if match is None:
        raise ReleaseError("fotodit/__init__.py has no __version__.")
    return match.group(1)


def named_licenses(notices: str) -> list[str]:
    """The license files and folders THIRD-PARTY-NOTICES.md points to (relative to licenses/; a
    folder ends with a backslash)."""
    return sorted(set(re.findall(r"`licenses\\([^`]+)`", notices)))


def missing_licenses(legal: Path) -> list[str]:
    """What the notices name but the build's legal folder lacks."""
    notices = legal / "THIRD-PARTY-NOTICES.md"
    if not (legal / "LICENSE").is_file() or not notices.is_file():
        return ["LICENSE / THIRD-PARTY-NOTICES.md"]
    missing = []
    for name in named_licenses(notices.read_text(encoding="utf-8")):
        path = legal / "licenses" / name.rstrip("\\").replace("\\", "/")
        ok = path.is_dir() and any(path.iterdir()) if name.endswith("\\") else path.is_file()
        if not ok:
            missing.append(name)
    return missing


def _legal_entries(legal: Path, prefix: str) -> list[tuple[Path, str]]:
    """The legal files as they appear next to Fotodit.exe in a zip."""
    entries = [(legal / "LICENSE", prefix + "LICENSE.txt"),
               (legal / "THIRD-PARTY-NOTICES.md", prefix + "THIRD-PARTY-NOTICES.txt")]
    for path in sorted((legal / "licenses").rglob("*")):
        if path.is_file():
            entries.append((path, prefix + "licenses/" + path.relative_to(legal / "licenses").as_posix()))
    return entries


def _write_zip(out: Path, entries: list[tuple[Path, str]]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + ".part")
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, name in entries:
            zf.write(path, name)
    os.replace(temp, out)


def folder_zip(folder: Path, out: Path) -> None:
    """The folder build, under "Fotodit/" in the zip, with the legal files next to the exe."""
    legal = folder / "_internal" / "legal"
    entries = [(p, "Fotodit/" + p.relative_to(folder).as_posix()) for p in sorted(folder.rglob("*")) if p.is_file()]
    _write_zip(out, entries + _legal_entries(legal, "Fotodit/"))


def portable_zip(exe: Path, legal: Path, out: Path) -> None:
    _write_zip(out, [(exe, "Fotodit.exe"), *_legal_entries(legal, "")])


def tracked_files(root: Path = ROOT) -> list[str]:
    """The files git tracks, relative to root with forward slashes: what is public, and nothing else."""
    try:
        listing = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseError(f"The source zip is made from a git checkout of Fotodit (git ls-files: {exc}).") from exc
    return sorted(p for p in listing.decode("utf-8").split("\0") if p)


def source_entries(root: Path = ROOT) -> list[tuple[Path, str]]:
    """The published source: the tracked files among SOURCE_FILES and in SOURCE_DIRS."""
    prefix = f"Fotodit-{version()}/"
    return [(root / relative, prefix + relative) for relative in tracked_files(root)
            if (relative in SOURCE_FILES or relative.split("/")[0] in SOURCE_DIRS) and (root / relative).is_file()]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def package(dist: Path, out_dir: Path) -> list[Path]:
    folder, exe = dist / "Fotodit", dist / "onefile" / "Fotodit.exe"
    legal = folder / "_internal" / "legal"
    if not (folder / "Fotodit.exe").is_file() or not exe.is_file():
        raise ReleaseError("Both builds are needed: dist\\Fotodit\\Fotodit.exe and dist\\onefile\\Fotodit.exe "
                           "(packaging\\build.ps1 -Release makes them).")
    missing = missing_licenses(legal)
    if missing:
        raise ReleaseError("License texts missing from the build: " + ", ".join(missing))
    source = source_entries()  # before anything is written: it needs git
    v = version()
    outputs = [out_dir / f"Fotodit-{v}-windows-x64.zip", out_dir / f"Fotodit-{v}-windows-x64-portable.zip",
               out_dir / f"Fotodit-{v}-source.zip"]
    folder_zip(folder, outputs[0])
    portable_zip(exe, legal, outputs[1])
    _write_zip(outputs[2], source)
    sums = "".join(f"{sha256(p)}  {p.name}\n" for p in outputs)
    (out_dir / "SHA256SUMS.txt").write_text(sums, encoding="utf-8", newline="\n")
    return outputs


def main() -> int:
    try:
        outputs = package(ROOT / "dist", ROOT / "dist" / "release")
    except ReleaseError as exc:
        print(f"Release not packed: {exc}", file=sys.stderr)
        return 1
    for path in outputs:
        print(f"{path.relative_to(ROOT)}  ({path.stat().st_size / 1e6:.1f} MB)")
    print(f"{(ROOT / 'dist' / 'release' / 'SHA256SUMS.txt').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
