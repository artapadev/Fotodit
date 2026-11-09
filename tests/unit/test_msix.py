"""The Microsoft Store package: the manifest template, the identity kept out of git, the version and the
images the manifest names. (Packing itself needs the Windows SDK and runs at release time.)"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))

import make_msix  # noqa: E402

from fotodit import __version__  # noqa: E402

NS = {
    "m": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
    "uap5": "http://schemas.microsoft.com/appx/manifest/uap/windows10/5",
}
VALUES = {
    "name": "Example.Fotodit", "publisher": "CN=00000000-0000-0000-0000-000000000000",
    "publisher_display_name": "Example", "display_name": "Fotodit", "description": make_msix.DESCRIPTION,
    "version": "1.2.3.0", "max_version_tested": make_msix.MAX_VERSION_TESTED, "alias": "fotodit.exe",
}


def _manifest(tmp_path) -> tuple[ET.Element, str]:
    path = tmp_path / "AppxManifest.xml"
    make_msix.write_manifest(str(path), VALUES)
    return ET.parse(path).getroot(), path.read_text(encoding="utf-8")


def test_the_identity_comes_only_from_the_values(tmp_path):
    root, _text = _manifest(tmp_path)
    identity = root.find("m:Identity", NS)
    assert identity.get("Name") == VALUES["name"]
    assert identity.get("Publisher") == VALUES["publisher"]
    assert identity.get("Version") == "1.2.3.0"
    assert identity.get("ProcessorArchitecture") == "x64"
    assert root.find("m:Properties/m:PublisherDisplayName", NS).text == "Example"


def test_the_template_holds_no_identity_and_git_ignores_the_identity_file():
    text = Path(make_msix.TEMPLATE).read_text(encoding="utf-8")
    for key in ("name", "publisher", "publisher_display_name", "version"):
        assert "${" + key + "}" in text
    assert "CN=" not in text
    assert "packaging/msix/identity.json" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_a_missing_or_incomplete_identity_file_is_explained(tmp_path):
    with pytest.raises(make_msix.MsixError, match="Partner Center"):
        make_msix.read_identity(str(tmp_path / "identity.json"))
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"name": "Example.Fotodit"}), encoding="utf-8")
    with pytest.raises(make_msix.MsixError, match="publisher, publisher_display_name"):
        make_msix.read_identity(str(incomplete))


def test_the_package_version_is_the_app_version_with_a_last_zero():
    assert make_msix.package_version() == __version__ + ".0"


def test_fotodit_is_a_full_trust_desktop_app_with_its_command(tmp_path):
    root, _text = _manifest(tmp_path)
    app = root.find("m:Applications/m:Application", NS)
    assert (app.get("Executable"), app.get("EntryPoint")) == ("Fotodit.exe", "Windows.FullTrustApplication")
    assert app.find(".//uap5:ExecutionAlias", NS).get("Alias") == "fotodit.exe"
    # Only runFullTrust: turning AppData virtualisation off (unvirtualizedResources) needs the Store's approval.
    assert [c.get("Name") for c in root.find("m:Capabilities", NS)] == ["runFullTrust"]


def test_every_image_the_manifest_names_is_drawn_at_both_scales(tmp_path):
    _root, text = _manifest(tmp_path)
    named = set(re.findall(r"Assets\\(\w+)\.png", text))
    drawn = {name for name, *_ in make_msix.ASSETS}
    assert named == {"StoreLogo", "Square150x150Logo", "Square44x44Logo", "Wide310x150Logo"}
    for name in named:
        assert {f"{name}.scale-100.png", f"{name}.scale-200.png"} <= drawn
