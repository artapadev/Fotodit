# -*- mode: python ; coding: utf-8 -*-
"""Fotodit PyInstaller configuration.

Default: a folder distribution (dist/Fotodit/Fotodit.exe) — starts quickly and trips fewer
antivirus false positives. For a single file: $env:FOTODIT_ONEFILE = "1"
"""

import os

ROOT = os.path.dirname(os.path.abspath(SPECPATH))  # the project
ONEFILE = os.environ.get("FOTODIT_ONEFILE") == "1"

# Python packages whose own license files go into the release (see THIRD-PARTY-NOTICES.md).
LICENSED_PACKAGES = {"av": "av", "pillow": "pillow", "pillow-heif": "pillow_heif", "numpy": "numpy",
                     "rawpy": "rawpy", "pyinstaller": "pyinstaller"}


def legal_files():
    """Fotodit's license, the notices and every license text, for the bundle's "legal" folder: the
    About window shows them, and packaging/package_release.py also puts them next to Fotodit.exe."""
    from importlib.metadata import distribution

    files = [(os.path.join(ROOT, "LICENSE"), "legal"), (os.path.join(ROOT, "THIRD-PARTY-NOTICES.md"), "legal")]
    for name in sorted(os.listdir(os.path.join(ROOT, "licenses"))):
        files.append((os.path.join(ROOT, "licenses", name), "legal/licenses"))
    for package, folder in LICENSED_PACKAGES.items():
        dist = distribution(package)
        for f in dist.files or ():
            parts = f.parts
            if not parts[0].endswith(".dist-info") or f.suffix in (".py", ".pyc") \
                    or len(parts) > 2 and parts[1] != "licenses":
                continue
            inside = parts[2:] if len(parts) > 2 and parts[1] == "licenses" else parts[1:]
            if len(parts) == 2 and not inside[0].upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                continue  # METADATA, RECORD …
            target = "/".join(["legal/licenses/python-packages", folder, *inside[:-1]])
            files.append((str(dist.locate_file(f)), target))
    return files


a = Analysis(
    [os.path.join(ROOT, "packaging", "run_fotodit.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "fotodit", "resources"), os.path.join("fotodit", "resources")),
           (os.path.join(ROOT, "fotodit", "i18n", "*.json"), os.path.join("fotodit", "i18n")),  # the texts
           *legal_files()],
    # Video: PyAV (FFmpeg 8, x264/x265 → av.libs) and Qt Multimedia for playback (FFmpeg backend,
    # the avcodec-61 … DLLs in the PySide6 folder). Both are imported inside functions.
    hiddenimports=["pillow_heif", "av", "PySide6.QtMultimedia"],
    excludes=[
        "tkinter", "_tkinter", "PIL.ImageTk", "PIL.ImageQt", "PIL.ImageShow",
        "pytest", "pyexiv2", "unittest", "pydoc", "doctest", "pdb",
        # The app never uses the network: no OpenSSL needed (hashlib falls back to its built-in algorithms).
        "ssl", "_ssl", "_hashlib",
    ],
    noarchive=False,
)

# Unused Qt parts: software OpenGL (~20 MB), Qt's own image decoders (Pillow decodes the
# images; only qico is needed, for the .ico icon), the extra platform plugins (offscreen,
# minimal, Direct2D) and the Windows style plugin (Fusion is used).
DROP = {
    "opengl32sw.dll", "qpdf.dll", "qsvg.dll", "qtiff.dll", "qwebp.dll", "qjpeg.dll", "qgif.dll",
    "qicns.dll", "qtga.dll", "qwbmp.dll", "qoffscreen.dll", "qminimal.dll", "qdirect2d.dll",
    "qmodernwindowsstyle.dll", "qtuiotouchplugin.dll", "qsvgicon.dll",
    "libssl-3.dll", "libcrypto-3.dll", "libssl-3-x64.dll", "libcrypto-3-x64.dll",
}
DROP_DIRS = ("plugins/tls/", "plugins/networkinformation/")
a.binaries = [
    b for b in a.binaries
    if os.path.basename(b[0]).lower() not in DROP
    and not any(d in b[0].replace("\\", "/") for d in DROP_DIRS)
]
# Of Qt's translations only qtbase_tr.qm is kept: the Turkish texts of the standard dialogs (OK/Cancel …).
a.datas = [
    d for d in a.datas
    if "translations" not in d[0].replace("\\", "/").split("/") or os.path.basename(d[0]) == "qtbase_tr.qm"
]

pyz = PYZ(a.pure)
common = dict(
    name="Fotodit",
    icon=os.path.join(ROOT, "fotodit", "resources", "fotodit.ico"),
    version=os.path.join(ROOT, "packaging", "version_info.txt"),
    console=False,
    upx=False,
    debug=False,
    strip=False,
)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], runtime_tmpdir=None, **common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Fotodit")
