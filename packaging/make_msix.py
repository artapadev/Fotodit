"""Packs Fotodit's folder build (dist\\Fotodit) as an MSIX package for the Microsoft Store.

The Store signs a package when it is submitted, so this script signs nothing. The package identity (name,
publisher and publisher display name, as Partner Center shows them under Product identity) comes from a
local JSON file that git ignores, so it never lands in the repository:

    packaging/msix/identity.json: {"name": "...", "publisher": "CN=...", "publisher_display_name": "..."}

Steps: hard links (or copies) of dist\\Fotodit in build\\msix\\layout, the logos drawn from
packaging/fotodit-logo.svg, the manifest from packaging/msix/AppxManifest.xml, resources.pri (makepri)
and the package (makeappx, which checks the manifest against Windows' schemas while it packs):
dist\\release\\Fotodit-<version>-x64.msix. Needs the Windows SDK.

    .venv\\Scripts\\python.exe packaging\\make_msix.py [--identity FILE] [--dist DIR] [--out DIR]
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import string
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEMPLATE = os.path.join(HERE, "msix", "AppxManifest.xml")
IDENTITY = os.path.join(HERE, "msix", "identity.json")
LOGO = os.path.join(HERE, "fotodit-logo.svg")
WORK = os.path.join(ROOT, "build", "msix")
DESCRIPTION = "Removes hidden details from photos and videos; converts, edits, trims, sorts and renames them."
MAX_VERSION_TESTED = "10.0.19045.0"  # the newest Windows build the package has been tried on

# Every image the manifest names: (file, width, height, share of the height the logo takes). The tiles
# keep a margin; the 44 px icon also comes in the target sizes the taskbar and Start ask for, plain and
# "unplated" (drawn without a tile behind it). resources.pri picks the right one for each place.
ASSETS = [
    ("StoreLogo.scale-100.png", 50, 50, 1.0), ("StoreLogo.scale-200.png", 100, 100, 1.0),
    ("Square150x150Logo.scale-100.png", 150, 150, 0.6), ("Square150x150Logo.scale-200.png", 300, 300, 0.6),
    ("Wide310x150Logo.scale-100.png", 310, 150, 0.6), ("Wide310x150Logo.scale-200.png", 620, 300, 0.6),
    ("Square44x44Logo.scale-100.png", 44, 44, 0.8), ("Square44x44Logo.scale-200.png", 88, 88, 0.8),
    *[(f"Square44x44Logo.targetsize-{s}{plate}.png", s, s, 1.0)
      for s in (16, 24, 32, 48, 256) for plate in ("", "_altform-unplated")],
]


class MsixError(Exception):
    """Why the package could not be made."""


def sdk_tool(name: str) -> str:
    """The newest x64 copy of a Windows SDK tool (makeappx.exe, makepri.exe)."""
    kits = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Windows Kits", "10", "bin")
    found = glob.glob(os.path.join(kits, "10.*", "x64", name))
    if not found:
        raise MsixError(f"{name} not found: install the Windows SDK")
    return max(found, key=lambda p: tuple(int(n) for n in os.path.basename(os.path.dirname(os.path.dirname(p))).split(".")))


def run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if result.returncode:
        tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
        raise MsixError(f"{os.path.basename(command[0])} failed:\n" + "\n".join(tail))


def package_version() -> str:
    """fotodit/__init__.py's version in the four-part form MSIX wants (the Store needs the last part 0)."""
    with open(os.path.join(ROOT, "fotodit", "__init__.py"), encoding="utf-8") as f:
        version = re.search(r'__version__ = "(\d+)\.(\d+)\.(\d+)"', f.read())
    if not version:
        raise MsixError("no x.y.z version in fotodit/__init__.py")
    return ".".join(version.groups()) + ".0"


def read_identity(path: str = IDENTITY) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as f:
            identity = json.load(f)
    except FileNotFoundError:
        raise MsixError(f"{path} is missing: put the name, publisher and publisher_display_name that Partner "
                        "Center shows under Product identity into it") from None
    missing = [key for key in ("name", "publisher", "publisher_display_name") if not identity.get(key)]
    if missing:
        raise MsixError(f"{path} lacks: {', '.join(missing)}")
    return identity


def render_assets(folder: str, logo: str = LOGO) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no window needed, also on a build machine
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841  (Qt paints only with one)
    renderer = QSvgRenderer(logo)
    if not renderer.isValid():
        raise MsixError(f"cannot read {logo}")
    os.makedirs(folder, exist_ok=True)
    for name, width, height, share in ASSETS:
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        side = height * share
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF((width - side) / 2, (height - side) / 2, side, side))
        painter.end()
        if not image.save(os.path.join(folder, name), "PNG"):
            raise MsixError(f"cannot write {name}")


def write_manifest(path: str, values: dict[str, str], template: str = TEMPLATE) -> None:
    with open(template, encoding="utf-8") as f:
        text = string.Template(f.read()).substitute(values)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def link_tree(source: str, target: str) -> None:
    """Hard links of every file (copies where links fail), so a layout costs almost no disk space."""
    for folder, _dirs, files in os.walk(source):
        into = os.path.join(target, os.path.relpath(folder, source))
        os.makedirs(into, exist_ok=True)
        for name in files:
            try:
                os.link(os.path.join(folder, name), os.path.join(into, name))
            except OSError:
                shutil.copy2(os.path.join(folder, name), os.path.join(into, name))


def build_layout(dist: str, work: str, values: dict[str, str]) -> str:
    """The package's folder (work\\layout): the build, the logos, the manifest and resources.pri. The index
    is made from a folder that holds only the logos, so it names nothing that is not in the package."""
    if not os.path.isfile(os.path.join(dist, "Fotodit.exe")):
        raise MsixError(f"no Fotodit.exe in {dist}: build the folder version first")
    layout = os.path.join(work, "layout")
    link_tree(dist, layout)
    render_assets(os.path.join(layout, "Assets"))
    write_manifest(os.path.join(layout, "AppxManifest.xml"), values)
    shutil.copytree(os.path.join(layout, "Assets"), os.path.join(work, "pri", "Assets"))
    makepri, config = sdk_tool("makepri.exe"), os.path.join(work, "priconfig.xml")
    run([makepri, "createconfig", "/cf", config, "/dq", "en-US", "/pv", "10.0.0", "/o"])
    run([makepri, "new", "/pr", os.path.join(work, "pri"), "/cf", config, "/mn", os.path.join(layout, "AppxManifest.xml"),
         "/of", os.path.join(layout, "resources.pri"), "/o"])
    return layout


def pack(layout: str, package: str) -> str:
    """The .msix, and its SHA-256."""
    os.makedirs(os.path.dirname(package), exist_ok=True)
    run([sdk_tool("makeappx.exe"), "pack", "/d", layout, "/p", package, "/o"])
    digest = hashlib.sha256()
    with open(package, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--identity", default=IDENTITY, help="JSON with name, publisher, publisher_display_name")
    parser.add_argument("--dist", default=os.path.join(ROOT, "dist", "Fotodit"))
    parser.add_argument("--out", default=os.path.join(ROOT, "dist", "release"))
    parser.add_argument("--alias", default="fotodit.exe", help="the command that starts Fotodit (Run, Send To)")
    args = parser.parse_args()
    try:
        identity = read_identity(args.identity)
        version = package_version()
        values = {
            "name": identity["name"], "publisher": identity["publisher"],
            "publisher_display_name": identity["publisher_display_name"], "display_name": "Fotodit",
            "description": DESCRIPTION, "version": version, "max_version_tested": MAX_VERSION_TESTED,
            "alias": args.alias,
        }
        if os.path.exists(WORK):
            shutil.rmtree(WORK)  # the previous run's layout (hard links: the build itself stays)
        layout = build_layout(args.dist, WORK, values)
        package = os.path.join(args.out, f"Fotodit-{version[:-2]}-x64.msix")
        digest = pack(layout, package)
    except MsixError as exc:
        print(f"MSIX package not made: {exc}", file=sys.stderr)
        return 1
    print(f"{package}: {os.path.getsize(package) / 1e6:.1f} MB, SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
