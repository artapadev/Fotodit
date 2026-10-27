"""Open-source release: the license files, the notices, the About window and the release packaging."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))

import package_release  # noqa: E402

from fotodit import COPYRIGHT, LICENSE_NAME, __version__  # noqa: E402


def _tracked() -> set[str]:
    """The files git tracks, that is the published project; none outside a git checkout."""
    try:
        listing = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {p for p in listing.decode("utf-8").split("\0") if p}


TRACKED = _tracked()


def test_license_is_the_gpl_3_and_the_notices_name_every_text():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in license_text[:100] and "Version 3, 29 June 2007" in license_text[:200]
    assert "END OF TERMS AND CONDITIONS" in license_text and LICENSE_NAME == "GPL-3.0-or-later"
    notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    assert "Copyright (C) 2026 Artapa" in notices and "Artapa" in COPYRIGHT
    named = package_release.named_licenses(notices)
    ours = sorted(os.listdir(ROOT / "licenses"))
    assert {n for n in named if not n.startswith("python-packages")} == set(ours)  # each text named, none missing
    for name in ours:
        text = (ROOT / "licenses" / name).read_text(encoding="utf-8")
        assert len(text) > 500 and "\r" not in text, name
    assert re.search(r"\(C\)|©|Copyright", (ROOT / "licenses" / "dav1d.txt").read_text(encoding="utf-8"))


def _fake_builds(tmp_path: Path, drop: str | None = None) -> Path:
    """A dist folder shaped like the two builds, with the legal folder fotodit.spec makes."""
    dist = tmp_path / "dist"
    folder = dist / "Fotodit"
    (folder / "_internal").mkdir(parents=True)
    (folder / "Fotodit.exe").write_bytes(b"MZ folder")
    (folder / "_internal" / "python314.dll").write_bytes(b"MZ dll")
    (dist / "onefile").mkdir()
    (dist / "onefile" / "Fotodit.exe").write_bytes(b"MZ onefile")
    legal = folder / "_internal" / "legal"
    (legal / "licenses").mkdir(parents=True)
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
        (legal / name).write_bytes((ROOT / name).read_bytes())
    notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    for name in package_release.named_licenses(notices):
        path = legal / "licenses" / name.rstrip("\\").replace("\\", "/")
        if name.endswith("\\"):
            path.mkdir(parents=True, exist_ok=True)
            (path / "LICENSE").write_text("text", encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("text", encoding="utf-8")
    if drop is not None:
        (legal / "licenses" / drop).unlink()
    return dist


@pytest.mark.skipif(not TRACKED, reason="the source zip is made from a git checkout")
def test_release_zips_carry_the_license_files(tmp_path):
    dist = _fake_builds(tmp_path)
    outputs = package_release.package(dist, tmp_path / "release")
    names = [p.name for p in outputs]
    assert names == [f"Fotodit-{__version__}-windows-x64.zip", f"Fotodit-{__version__}-windows-x64-portable.zip",
                     f"Fotodit-{__version__}-source.zip"]
    with zipfile.ZipFile(outputs[0]) as zf:
        inside = set(zf.namelist())
    assert {"Fotodit/Fotodit.exe", "Fotodit/_internal/python314.dll", "Fotodit/LICENSE.txt",
            "Fotodit/THIRD-PARTY-NOTICES.txt", "Fotodit/licenses/LGPL-3.0.txt",
            "Fotodit/licenses/python-packages/av/LICENSE.txt"} <= inside
    with zipfile.ZipFile(outputs[1]) as zf:
        inside = set(zf.namelist())
    assert {"Fotodit.exe", "LICENSE.txt", "THIRD-PARTY-NOTICES.txt", "licenses/GPL-2.0.txt"} <= inside
    with zipfile.ZipFile(outputs[2]) as zf:
        source = set(zf.namelist())
    prefix = f"Fotodit-{__version__}/"
    assert {prefix + "LICENSE", prefix + "fotodit/__init__.py", prefix + "packaging/fotodit.spec",
            prefix + "packaging/build.ps1", prefix + "licenses/opus.txt", prefix + "packaging/package_release.py",
            prefix + "tests/ui/smoke_run.py"} <= source
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in source)
    assert prefix + "docs/RESEARCH.md" in source
    published = {p for p in TRACKED
                 if p in package_release.SOURCE_FILES or p.split("/")[0] in package_release.SOURCE_DIRS}
    assert {n[len(prefix):] for n in source} == published  # what git tracks, nothing it does not
    sums = (tmp_path / "release" / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    assert [line.split("  ")[1] for line in sums] == names
    assert sums[0].split("  ")[0] == package_release.sha256(outputs[0])


def test_release_is_refused_without_every_license_text(tmp_path):
    dist = _fake_builds(tmp_path, drop="opus.txt")
    with pytest.raises(package_release.ReleaseError, match="opus.txt"):
        package_release.package(dist, tmp_path / "release")
    assert not (tmp_path / "release").exists() or not any((tmp_path / "release").iterdir())
    os.remove(dist / "onefile" / "Fotodit.exe")
    with pytest.raises(package_release.ReleaseError, match="Both builds"):
        package_release.package(dist, tmp_path / "release")


def test_spec_bundles_every_license_text():
    """fotodit.spec's legal folder: our texts plus the ones the bundled Python packages ship."""
    source = (ROOT / "packaging" / "fotodit.spec").read_text(encoding="utf-8")
    namespace = {"os": os, "SPECPATH": str(ROOT / "packaging")}
    exec(source[source.index("ROOT = "):source.index("a = Analysis(")], namespace)  # noqa: S102
    files = {(Path(src).name, target) for src, target in namespace["legal_files"]()}
    assert ("LICENSE", "legal") in files and ("THIRD-PARTY-NOTICES.md", "legal") in files
    assert {name for name, target in files if target == "legal/licenses"} == set(os.listdir(ROOT / "licenses"))
    for package in ("av", "pillow", "pillow_heif", "numpy", "rawpy", "pyinstaller"):
        assert any(target == f"legal/licenses/python-packages/{package}" for _, target in files), package
    assert ("LICENSE.LibRaw", "legal/licenses/python-packages/rawpy") in files
    assert not any(name.endswith((".py", ".pyc", ".json")) for name, _ in files)
