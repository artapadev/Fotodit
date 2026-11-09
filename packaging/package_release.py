"""Packs a release from the two builds (see packaging\\build.ps1 -Release).

    .venv\\Scripts\\python.exe packaging\\package_release.py

Writes to dist\\release:
  Fotodit-<version>-windows-x64.zip           the folder build (Fotodit\\Fotodit.exe and _internal)
  Fotodit-<version>-windows-x64-portable.zip  the single-file exe
  Fotodit-<version>-source.zip                the source code (the GPL's "Corresponding Source" for Fotodit):
                                              the files git tracks, so it needs a git checkout
  Fotodit-<version>-third-party-sources.zip   the sources of the GPL and LGPL parts inside the programs, with
                                              the build scripts and patches that made them: the files
                                              packaging/third_party_sources.json lists, from third-party-sources/
  SHA256SUMS.txt                              checksums of the four
Both program zips carry LICENSE.txt, THIRD-PARTY-NOTICES.txt and the licenses folder next to
Fotodit.exe (copied from the build's own "legal" folder, see packaging/fotodit.spec). Nothing is packed if a
license text that THIRD-PARTY-NOTICES.md names is missing from the build, or if a third-party source is missing
or differs from the list (packaging/fetch_sources.py downloads and checks them; packing itself stays offline).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = ("LICENSE", "THIRD-PARTY-NOTICES.md", "README.md", "requirements.txt", "requirements-dev.txt",
                ".gitignore")
SOURCE_DIRS = ("fotodit", "tests", "docs", "packaging", "licenses")
SOURCES = ROOT / "third-party-sources"
MANIFEST = ROOT / "packaging" / "third_party_sources.json"
COMPRESSED = (".gz", ".xz", ".bz2", ".zst", ".zip")  # stored as they are in the zip


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


def third_party_manifest(path: Path = MANIFEST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def third_party_problems(manifest: dict, sources: Path) -> list[str]:
    """What keeps the third-party sources from being packed: a file the list does not pin yet, a file missing
    from the folder, or one that differs from the list."""
    found = []
    for entry in manifest["files"]:
        path = sources / entry["file"]
        if entry.get("sha256") is None or entry.get("size") is None:
            found.append(f"{entry['file']} (not pinned in the list)")
        elif not path.is_file():
            found.append(f"{entry['file']} (missing)")
        elif path.stat().st_size != entry["size"] or sha256(path) != entry["sha256"]:
            found.append(f"{entry['file']} (differs from the list)")
    return found


def _third_party_entries(manifest: dict, part: str) -> str:
    def block(text: str) -> str:
        return textwrap.fill(text, width=112, initial_indent=" " * 6, subsequent_indent=" " * 6,
                             break_long_words=False, break_on_hyphens=False)

    lines = []
    for entry in manifest["files"]:
        if entry["part"] == part:
            licence = f" {entry['license']}." if entry["license"] else ""
            lines += [f"  {entry['file']}", block(f"{entry['what']}.{licence}"), f"      {entry['url']}",
                      block(f"Checked: {entry['checked']}."), ""]
    return "\n".join(lines)


def third_party_readme(manifest: dict, version: str) -> str:
    """README.txt of the third-party sources zip: what each file is, where it was published, what it was checked
    against."""
    build, pyav, pillow_heif = manifest["pyav_ffmpeg"], manifest["pyav"], manifest["pillow_heif"]
    title = f"Fotodit {version}: the source code of its GPL and LGPL third-party parts"
    return f"""{title}
{"=" * len(title)}

This archive belongs to the Fotodit {version} release for Windows (Fotodit-{version}-windows-x64.zip and
Fotodit-{version}-windows-x64-portable.zip). It keeps the source code of the components inside those programs
that are under the GNU GPL or LGPL, with the build scripts and patches that made them, so that they stay
available even when an upstream download disappears. Fotodit's own source code is in
Fotodit-{version}-source.zip. Every file here is unchanged, as published at the address given for it.
THIRD-PARTY-NOTICES.txt, next to Fotodit.exe, lists every component and its licence.


1. FFmpeg and its codec libraries (_internal\\av.libs)
-----------------------------------------------------
These DLLs come from the PyAV {pyav} wheel for Windows. PyAV makes them with its build project,
https://github.com/PyAV-Org/pyav-ffmpeg, release {build["release"]}, which is commit
{build["commit"]}. The build applies its own patches to some of the libraries; they are in
the patches folder of its archive below. FFmpeg is configured under the LGPL, version 3 or later, and linked with
x264 and x265; with them these libraries are distributed under the GPL, version 3 or later.

{_third_party_entries(manifest, "av.libs")}
The other libraries in av.libs are under licences that do not require their source to be passed on;
THIRD-PARTY-NOTICES.txt lists them, and scripts/pkg.py in the build archive gives the address and SHA-256 of
each one the build makes. The GCC runtime (libgcc_s_seh-1, libstdc++-6) is GPL-3.0-or-later with the GCC Runtime
Library Exception 3.1; it comes from MSYS2, which keeps its source at https://repo.msys2.org/mingw/sources/.


2. libheif and the second x265 (pillow-heif)
--------------------------------------------
pillow-heif {pillow_heif}, which reads and writes HEIC and HEIF, bundles libheif, libde265 and x265
(_internal\\libheif-*.dll, libde265-0-*.dll and libx265-*.dll). Its Windows wheel builds libheif with MSYS2 from
pillow-heif's own recipe and takes x265 and libde265 from MSYS2's packages.

{_third_party_entries(manifest, "pillow-heif")}

3. Checking the files
---------------------
SHA256SUMS.txt holds the SHA-256 of every file here. "Checked" under each file says what its SHA-256 or its
contents were compared with before it was packed.
"""


def third_party_zip(manifest: dict, sources: Path, out: Path, version: str) -> None:
    """The third-party sources zip: README.txt, SHA256SUMS.txt and the listed files, under one folder."""
    prefix = f"Fotodit-{version}-third-party-sources/"
    sums = "".join(f"{entry['sha256']}  {entry['file']}\n" for entry in manifest["files"])
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + ".part")
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, text in (("README.txt", third_party_readme(manifest, version)), ("SHA256SUMS.txt", sums)):
            info = zipfile.ZipInfo(prefix + name, date_time=time.localtime()[:6])
            info.external_attr = 0o644 << 16
            zf.writestr(info, text, compress_type=zipfile.ZIP_DEFLATED)
        for entry in manifest["files"]:
            stored = entry["file"].endswith(COMPRESSED)
            zf.write(sources / entry["file"], prefix + entry["file"],
                     compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
    os.replace(temp, out)


def package(dist: Path, out_dir: Path, sources: Path = SOURCES, manifest: dict | None = None) -> list[Path]:
    folder, exe = dist / "Fotodit", dist / "onefile" / "Fotodit.exe"
    legal = folder / "_internal" / "legal"
    if not (folder / "Fotodit.exe").is_file() or not exe.is_file():
        raise ReleaseError("Both builds are needed: dist\\Fotodit\\Fotodit.exe and dist\\onefile\\Fotodit.exe "
                           "(packaging\\build.ps1 -Release makes them).")
    missing = missing_licenses(legal)
    if missing:
        raise ReleaseError("License texts missing from the build: " + ", ".join(missing))
    manifest = third_party_manifest() if manifest is None else manifest
    problems = third_party_problems(manifest, sources)
    if problems:
        raise ReleaseError("Third-party sources missing or not matching packaging/third_party_sources.json "
                           "(packaging\\fetch_sources.py downloads and checks them): " + ", ".join(problems))
    source = source_entries()  # before anything is written: it needs git
    v = version()
    outputs = [out_dir / f"Fotodit-{v}-windows-x64.zip", out_dir / f"Fotodit-{v}-windows-x64-portable.zip",
               out_dir / f"Fotodit-{v}-source.zip", out_dir / f"Fotodit-{v}-third-party-sources.zip"]
    folder_zip(folder, outputs[0])
    portable_zip(exe, legal, outputs[1])
    _write_zip(outputs[2], source)
    third_party_zip(manifest, sources, outputs[3], v)
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
