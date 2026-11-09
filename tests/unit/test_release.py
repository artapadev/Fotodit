"""Open-source release: the license files, the notices, the About window, the release packaging and the
third-party sources that go with every release."""

from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))

import fetch_sources  # noqa: E402
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


def _fake_sources(tmp_path: Path) -> tuple[Path, dict]:
    """A third-party sources folder and a list that pins its two files."""
    sources = tmp_path / "third-party-sources"
    sources.mkdir()
    files = []
    for name, part, data in (("x264-test.tar.bz2", "av.libs", b"x264 source"),
                             ("libheif-test.tar.gz", "pillow-heif", b"libheif source")):
        (sources / name).write_bytes(data)
        files.append({"component": name.split("-")[0], "part": part, "file": name,
                      "url": f"https://example.org/{name}", "sha256": hashlib.sha256(data).hexdigest(),
                      "size": len(data), "what": f"{name} for the test", "license": "GPL-2.0-or-later",
                      "checked": "the test"})
    manifest = {"pyav": "1.0", "pyav_ffmpeg": {"release": "1.0-1", "commit": "0" * 40}, "pillow_heif": "1.0",
                "files": files}
    return sources, manifest


@pytest.mark.skipif(not TRACKED, reason="the source zip is made from a git checkout")
def test_release_zips_carry_the_license_files(tmp_path):
    dist = _fake_builds(tmp_path)
    sources, manifest = _fake_sources(tmp_path)
    outputs = package_release.package(dist, tmp_path / "release", sources=sources, manifest=manifest)
    names = [p.name for p in outputs]
    assert names == [f"Fotodit-{__version__}-windows-x64.zip", f"Fotodit-{__version__}-windows-x64-portable.zip",
                     f"Fotodit-{__version__}-source.zip", f"Fotodit-{__version__}-third-party-sources.zip"]
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
    with zipfile.ZipFile(outputs[3]) as zf:
        third = f"Fotodit-{__version__}-third-party-sources/"
        assert set(zf.namelist()) == {third + n for n in ("README.txt", "SHA256SUMS.txt", "x264-test.tar.bz2",
                                                          "libheif-test.tar.gz")}
        assert zf.read(third + "x264-test.tar.bz2") == b"x264 source"
        readme = zf.read(third + "README.txt").decode("utf-8")
        inner_sums = zf.read(third + "SHA256SUMS.txt").decode("utf-8")
    assert "release 1.0-1, which is commit" in readme and "https://example.org/libheif-test.tar.gz" in readme
    assert readme.index("x264-test.tar.bz2") < readme.index("2. libheif") < readme.index("libheif-test.tar.gz")
    assert f"{hashlib.sha256(b'x264 source').hexdigest()}  x264-test.tar.bz2\n" in inner_sums
    sums = (tmp_path / "release" / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    assert [line.split("  ")[1] for line in sums] == names
    assert sums[0].split("  ")[0] == package_release.sha256(outputs[0])
    assert sums[3].split("  ")[0] == package_release.sha256(outputs[3])


def test_release_is_refused_without_its_third_party_sources(tmp_path):
    dist = _fake_builds(tmp_path)
    sources, manifest = _fake_sources(tmp_path)
    release = tmp_path / "release"
    (sources / "x264-test.tar.bz2").unlink()
    with pytest.raises(package_release.ReleaseError, match=r"x264-test\.tar\.bz2 \(missing\)"):
        package_release.package(dist, release, sources=sources, manifest=manifest)
    (sources / "x264-test.tar.bz2").write_bytes(b"x264 source, changed")
    with pytest.raises(package_release.ReleaseError, match=r"x264-test\.tar\.bz2 \(differs"):
        package_release.package(dist, release, sources=sources, manifest=manifest)
    manifest["files"][1]["sha256"] = None
    with pytest.raises(package_release.ReleaseError, match=r"libheif-test\.tar\.gz \(not pinned"):
        package_release.package(dist, release, sources=sources, manifest=manifest)
    assert not release.exists()


def test_third_party_sources_list_matches_the_release():
    """The list pins every GPL and LGPL part, and follows requirements.txt: upgrading PyAV or pillow-heif
    without changing the list fails here."""
    manifest = package_release.third_party_manifest()
    files = manifest["files"]
    keys = {"component", "part", "file", "url", "sha256", "size", "what", "license", "checked"}
    for entry in files:
        assert set(entry) == keys, entry["file"]
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) and entry["size"] > 0, entry["file"]
        assert entry["url"].startswith("https://") and entry["part"] in ("av.libs", "pillow-heif"), entry["file"]
        assert entry["what"] and entry["checked"] and "/" not in entry["file"], entry["file"]
    assert len({e["file"] for e in files}) == len(files)
    by_part = {part: {e["component"] for e in files if e["part"] == part} for part in ("av.libs", "pillow-heif")}
    assert {"pyav-ffmpeg", "ffmpeg", "x264", "x265", "lame", "libiconv", "pyav"} <= by_part["av.libs"]
    assert {"x265", "libde265", "libheif", "libheif-recipe"} <= by_part["pillow-heif"]
    pins = dict(re.findall(r"^([A-Za-z0-9_-]+)==(\S+)", (ROOT / "requirements.txt").read_text(encoding="utf-8"),
                           re.M))
    names = {e["file"] for e in files}
    assert manifest["pyav"] == pins["av"] and f"av-{pins['av']}.tar.gz" in names
    assert manifest["pillow_heif"] == pins["pillow-heif"]
    assert f"pillow_heif-{pins['pillow-heif']}-mingw-w64-libheif-PKGBUILD" in names
    commit = manifest["pyav_ffmpeg"]["commit"]
    assert re.fullmatch(r"[0-9a-f]{40}", commit) and f"pyav-ffmpeg-{commit}.tar.gz" in names


def test_notices_point_to_the_third_party_sources():
    notices = " ".join((ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8").split())
    build = package_release.third_party_manifest()["pyav_ffmpeg"]
    assert "`Fotodit-<version>-third-party-sources.zip`" in notices
    assert f"release {build['release']}, which is commit {build['commit']}" in notices


def _tar(path: Path, mode: str, files: dict[str, bytes], **options) -> Path:
    with tarfile.open(path, mode, **options) as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def test_fetch_checks_commit_archives_and_msys2_packages(tmp_path):
    commit = "a" * 40
    archive = _tar(tmp_path / f"build-{commit}.tar.gz", "w:gz", {f"build-{commit}/setup.py": b"x"},
                   format=tarfile.PAX_FORMAT, pax_headers={"comment": commit})
    assert fetch_sources.commit_archive_problems(archive, commit) == []
    assert fetch_sources.commit_archive_problems(archive, "b" * 40) == [f"the archive carries commit {commit}, "
                                                                        f"not {'b' * 40}"]
    source = b"the upstream source"
    srcinfo = ("pkgbase = mingw-w64-demo\n\tsource = demo-1.0.tar.gz::https://example.org/demo_1.0.tar.gz\n"
               f"\tsha256sums = {hashlib.sha256(source).hexdigest()}\n")
    good = _tar(tmp_path / "good.src.tar.zst", "w:zst", {"mingw-w64-demo/.SRCINFO": srcinfo.encode(),
                                                          "mingw-w64-demo/demo-1.0.tar.gz": source})
    assert fetch_sources.msys2_problems(good) == []
    bad = _tar(tmp_path / "bad.src.tar.zst", "w:zst", {"mingw-w64-demo/.SRCINFO": srcinfo.encode(),
                                                        "mingw-w64-demo/demo-1.0.tar.gz": b"changed"})
    assert fetch_sources.msys2_problems(bad) == ["demo-1.0.tar.gz differs from the checksum in its .SRCINFO"]
    entry = {"sha256": "0" * 64, "size": 1}
    size, digest = fetch_sources.measure(archive)
    found = fetch_sources.problems(entry, archive, size, digest)
    assert found[0] == f"size {size}, the list says 1" and found[1].startswith(f"SHA-256 {digest}")


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


def test_spec_declares_per_monitor_v2_dpi_awareness():
    """Fotodit.exe's manifest declares Per-Monitor v2 for the Store's certification kit (Qt sets the same at
    run time) and keeps PyInstaller's own entries; the self-test checks the built exe itself."""
    from xml.etree import ElementTree

    from PyInstaller.utils.win32.winmanifest import create_application_manifest

    source = (ROOT / "packaging" / "fotodit.spec").read_text(encoding="utf-8")
    namespace = {"os": os, "SPECPATH": str(ROOT / "packaging")}
    exec(source[source.index("ROOT = "):source.index("a = Analysis(")], namespace)  # noqa: S102
    assert "manifest=exe_manifest()," in source[source.index("common = dict("):]  # both builds' EXE() get it
    root = ElementTree.fromstring(create_application_manifest(namespace["exe_manifest"]().encode("utf-8")))
    settings = {el.tag.rpartition("}")[2]: el.text for el in root.iter() if el.tag.endswith(("Aware", "Awareness"))}
    assert settings == {"longPathAware": "true", "dpiAware": "true", "dpiAwareness": "PerMonitorV2"}
    assert next(el for el in root.iter() if el.tag.endswith("requestedExecutionLevel")).get("level") == "asInvoker"
