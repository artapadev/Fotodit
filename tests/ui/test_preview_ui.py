"""The large preview window: navigating, choosing, restoring."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import builders as b  # noqa: E402
from helpers import wait_until, write_in  # noqa: E402
from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fotodit.core.folderscan import walk  # noqa: E402

DISPLAY = b.photo(640, 480)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ------------------------------------------------------------------ preview dialog


def _items(paths):
    from fotodit.ui.preview_dialog import PreviewItem

    return [PreviewItem(p, os.stat(p).st_mtime, os.path.basename(p)) for p in paths]


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "Holiday"
    paths = [write_in(root, f"IMG_000{i}.jpg", b.jpeg(b.photo(800, 600).rotate(i * 30), everything=False))
             for i in range(1, 4)]
    paths.append(write_in(root, "Sea/broken.jpg", b.jpeg(DISPLAY, everything=False)[:400]))
    return str(root), paths


def test_dialog_navigation_toggle_and_restore(app, folder):
    from fotodit.ui.preview_dialog import PreviewDialog
    from fotodit.ui.selection import Selection
    from fotodit.ui.thumbs import ThumbnailLoader

    root, paths = folder
    sel = Selection()
    sel.add_entries(walk(root).photos, root=root)  # as if selected as a folder (root = Holiday)
    order = sel.paths()
    loader = ThumbnailLoader(64)
    dialog = PreviewDialog(_items(order), 1, sel, loader)
    try:
        dialog.show()
        assert dialog.position.text() == "2 / 4" and dialog.chip.text() == "Seçili"
        assert wait_until(app, lambda: dialog.view.current_pixmap() is not None and not dialog.view._placeholder)
        # The next photo is prepared in advance.
        nxt = dialog.items[2]
        assert wait_until(app, lambda: dialog.loader.get(nxt.path, nxt.mtime, dialog.box, urgent=False) is not None)
        QTest.keyClick(dialog, Qt.Key.Key_Right)
        assert dialog.position.text() == "3 / 4" and dialog.title.text() == nxt.name
        # Del removes it from the selection; Del again puts it back in its old place, with its folder root.
        QTest.keyClick(dialog, Qt.Key.Key_Delete)
        assert len(sel) == 3 and nxt.path not in sel and dialog.chip.text() == "Seçili değil"
        QTest.keyClick(dialog, Qt.Key.Key_Delete)
        assert sel.paths() == order
        assert {p.root for p in sel.items()} == {root}
        # Enter presses no button (the selection does not change, the dialog stays open).
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        assert len(sel) == 4 and dialog.isVisible()
        # 100% and back; no arrow at the end, a message on the broken file.
        QTest.keyClick(dialog, Qt.Key.Key_Z)
        assert dialog.view.is_full() and dialog.zoom_btn.text() == "Sığdır"
        assert wait_until(app, lambda: not dialog.view._placeholder and dialog.view.current_pixmap() is not None)
        assert dialog.view.current_pixmap().width() == 800  # full resolution
        QTest.keyClick(dialog, Qt.Key.Key_End)
        assert not dialog.view.is_full() and not dialog.view.can_next and dialog.view.can_prev
        assert wait_until(app, lambda: "bozuk" in dialog.view._message)
        dialog.strip.clicked.emit(dialog.strip_model.index(0))
        assert dialog.position.text() == "1 / 4"
        QTest.keyClick(dialog, Qt.Key.Key_I)
        assert dialog.info_card.isVisible()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert not dialog.isVisible()
    finally:
        loader.shutdown()
        sel.shutdown()


def test_preview_from_both_tabs(app, tmp_path, folder):
    from fotodit.core.planner import SORT_NAME
    from fotodit.ui.browser import TileDelegate  # noqa: F401 - loads the grid
    from fotodit.ui.main_window import MainWindow

    root, paths = folder
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("browse/folder", root)
    win = MainWindow(settings)
    opened = []
    real_open = win.open_preview
    win.open_preview = lambda items, index, plan=None, **kw: (opened.append((items, index, plan))
                                                              or real_open(items, index, plan, **kw))
    try:
        win.options.set_output_folder(str(tmp_path / "out"))
        win.show()
        app.processEvents()
        grid = win.browser.grid
        rows = {e.name: i for i, e in enumerate(win.browser.model.entries)}
        assert wait_until(app, lambda: grid.visualRect(win.browser.model.index(rows["IMG_0002.jpg"])).width() > 0)
        center = grid.visualRect(win.browser.model.index(rows["IMG_0002.jpg"])).center()
        # Browse tab: a double-click opens the preview and leaves the selection unchanged (the mouse first
        # clicks, then double-clicks; the selection change made by the first click is undone).
        QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        assert len(win.selection) == 1
        QTest.mouseDClick(grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        assert opened and [it.name for it in opened[-1][0]] == ["IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"]
        assert opened[-1][1] == 1 and opened[-1][2] is None and len(win.selection) == 0
        dialog = QApplication.activeModalWidget()
        QTest.keyClick(dialog, Qt.Key.Key_Delete)  # it was not selected: adds it to the selection
        assert len(win.selection) == 1
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        # Selected tab: with renaming on, the names after processing are shown, and removing
        # a photo in the preview updates the numbers.
        win.browser.select_all()
        assert wait_until(app, lambda: not win.folders.busy() and len(win.selection) == 4)
        win.options.rename.setChecked(True)
        win.options.sort.setCurrentIndex(win.options.sort.findData(SORT_NAME))
        win.tabs.setCurrentWidget(win.selected)
        assert wait_until(app, lambda: win.selection.pending() == 0)
        win.replan()
        table = win.selected.table
        table.setCurrentIndex(win.selected.model.index(1, 0))
        QTest.keyClick(table, Qt.Key.Key_Space)
        items, index, plan = opened[-1]
        assert index == 1 and plan is win.selected.model and len(items) == 4
        dialog = QApplication.activeModalWidget()
        assert dialog.target.text().startswith("İşlem sonrası:")
        first = dialog.target.text()
        QTest.keyClick(dialog, Qt.Key.Key_Left)
        QTest.keyClick(dialog, Qt.Key.Key_Delete)  # the first photo is removed → the others are renumbered
        QTest.keyClick(dialog, Qt.Key.Key_Right)
        assert wait_until(app, lambda: dialog.target.text() != first)
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        rect = table.visualRect(win.selected.model.index(0, 2))
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        assert opened[-1][1] == 0
        QTest.keyClick(QApplication.activeModalWidget(), Qt.Key.Key_Escape)
    finally:
        win.close()


def test_selection_entry_and_restore(app, folder):
    from fotodit.ui.selection import Selection

    root, paths = folder
    sel = Selection()
    try:
        sel.add_entries(walk(root).photos, root=root)
        before = sel.paths()
        photo, index = sel.entry(before[2])
        assert index == 2 and photo.root == root
        sel.remove([before[2]])
        sel.restore(photo, index)
        assert sel.paths() == before and sel.selected_under(photo.folder_key) >= 1
    finally:
        sel.shutdown()
