"""The settings file is settings.ini; one left by version 1.3.0 or earlier (ayarlar.ini) is renamed, never lost."""

from __future__ import annotations

import pytest

from fotodit import app


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("FOTODIT_DATA_DIR", str(tmp_path))
    return tmp_path


def test_a_new_install_writes_settings_ini(folder):
    assert app.settings_path() == str(folder / "settings.ini")
    assert not (folder / "ayarlar.ini").exists()


def test_an_old_settings_file_is_renamed_with_its_settings(folder):
    (folder / "ayarlar.ini").write_text("[appearance]\nlanguage=en\n", encoding="utf-8")
    assert app.settings_path() == str(folder / "settings.ini")
    assert (folder / "settings.ini").read_text(encoding="utf-8") == "[appearance]\nlanguage=en\n"
    assert not (folder / "ayarlar.ini").exists()


def test_settings_ini_wins_over_an_old_file(folder):
    (folder / "settings.ini").write_text("[appearance]\nlanguage=tr\n", encoding="utf-8")
    (folder / "ayarlar.ini").write_text("[appearance]\nlanguage=en\n", encoding="utf-8")
    assert app.settings_path() == str(folder / "settings.ini")
    assert (folder / "settings.ini").read_text(encoding="utf-8") == "[appearance]\nlanguage=tr\n"
