"""The interface in English: built in the chosen language, with no Turkish left on screen, and the
language menu (no window is shown; offscreen Qt)."""

from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import builders as b  # noqa: E402
from helpers import wait_until  # noqa: E402
from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QApplication,
    QComboBox,
    QLabel,
    QWidget,
)

from fotodit import i18n  # noqa: E402
from fotodit.i18n import t  # noqa: E402

# Turkish letters, or Turkish words written without them ("Kalite:" once slipped through).
TURKISH = re.compile(r"[çğıöşüÇĞİÖŞÜ]|\b(?:Kalite|Kapat|Tamam|Sonra|Durdur|Dosya|Klasor|Sil|Ekle|Bilgi|Yeni|Tarih|"
                     r"Oran|Kopya|Kaynak|Uzun|Kenar|Sira|Yon|Varsayilan|Gozat|Tumu)\b")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("tr")


def _window(tmp_path, names=("a.jpg", "b.jpg")):
    from fotodit.ui.main_window import MainWindow

    folder = tmp_path / "photos"
    folder.mkdir()
    paths = []
    for name in names:
        (folder / name).write_bytes(b.jpeg(b.photo(64, 48)))
        paths.append(str(folder / name))
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("browse/folder", str(folder))
    win = MainWindow(settings)
    win.options.set_output_folder(str(tmp_path / "out"))
    return win, settings, paths


def _texts(root: QWidget) -> list[str]:
    """What a person can read in a window: labels, buttons, tooltips, menus and the combo boxes' items."""
    out = []
    for w in [root, *root.findChildren(QWidget)]:
        out.append(w.toolTip())
        if isinstance(w, (QLabel, QAbstractButton)):
            out.append(w.text())
        if isinstance(w, QComboBox):
            for i in range(w.count()):
                out += [w.itemText(i), str(w.itemData(i, Qt.ItemDataRole.ToolTipRole) or "")]
        for action in w.actions():
            out += [action.text(), action.toolTip()]
    return [text for text in out if text]


def test_the_window_in_english(app, tmp_path, english):
    win, _, paths = _window(tmp_path)
    win.show()
    app.processEvents()
    try:
        assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["Browse", "Selected (0)"]
        assert win.options.start_btn.text() == "Select Photos or Videos First"
        win.selection.add(paths)
        win.tabs.setCurrentWidget(win.selected)
        assert wait_until(app, lambda: win.selection.pending() == 0)
        win.replan()
        assert win.tabs.tabText(1) == "Selected (2)"
        assert win.options.start_btn.text() == "Process 2 Photos"
        model = win.selected.model
        headers = [model.headerData(c, Qt.Orientation.Horizontal) for c in range(model.columnCount())]
        assert headers == ["", "Order", "Current File", "New Name / Location", "Date", "Info"]
        win.show_about()
        app.processEvents()
        own_name = t("main.language", name=i18n.NAMES["tr"])  # "Language: Türkçe": each language in its own words
        shown = _texts(win) + _texts(win._about) + headers
        assert "Quality:" in shown
        assert not [text for text in shown if TURKISH.search(text) and text != own_name]
    finally:
        win._confirm_discard_edits = lambda count: True
        win.close()


def test_choosing_a_language_offers_a_restart(app, tmp_path, monkeypatch):
    from fotodit.ui.sheet import Sheet

    win, settings, _ = _window(tmp_path)
    restarts = []
    monkeypatch.setattr(win, "restart", lambda: restarts.append(True))
    win.show()
    try:
        assert win.language_actions[i18n.SYSTEM].isChecked()  # follows Windows until one is chosen
        win.language_actions["en"].trigger()  # returns at once: the sheet does not wait in the menu's action
        assert settings.value("appearance/language") == "en"
        assert win.language_actions["en"].isChecked() and not win.language_actions[i18n.SYSTEM].isChecked()
        sheets = [s for s in win.findChildren(Sheet) if s.isVisible()]
        assert len(sheets) == 1 and sheets[0].title.text() == t("main.restart_title")
        later, restart = sheets[0].buttons()
        assert (later.text(), restart.text()) == (t("main.later"), t("main.restart"))
        later.click()
        app.processEvents()  # (the sheet deletes itself once closed)
        assert not restarts and not [s for s in win.findChildren(Sheet) if s.isVisible()]
        win.language_actions["tr"].trigger()  # the language in use: nothing to restart for
        assert settings.value("appearance/language") == "tr"
        assert not [s for s in win.findChildren(Sheet) if s.isVisible()]
        win.language_actions["en"].trigger()
        sheet = next(s for s in win.findChildren(Sheet) if s.isVisible())
        sheet.buttons()[1].click()  # Restart
        app.processEvents()
        assert restarts == [True]
    finally:
        win.close()
