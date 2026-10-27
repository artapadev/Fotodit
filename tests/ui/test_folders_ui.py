"""Folder selection in the window: folder tiles, the path bar, the tree and the whole workflow."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from helpers import wait_until  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fotodit.core.folderscan import folder_key, walk  # noqa: E402
from fotodit.core.meta import scan_file  # noqa: E402
from fotodit.core.planner import (  # noqa: E402
    GROUP_SOURCE,
)
from fotodit.core.runner import Runner  # noqa: E402
from fotodit.ui.folders import (  # noqa: E402
    FULL,
    NONE,
    PARTIAL,
    SCANNING,
    FolderSelection,
)
from fotodit.ui.selection import Selection  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- selection


def test_selection_counts_roots_and_folder_removal(app, archive):
    sel = Selection()
    sub = os.path.join(archive, "2019")
    assert sel.add_entries(walk(sub).photos, root=sub) == 3
    assert sel.selected_under(folder_key(sub)) == 3
    assert sel.selected_under(folder_key(archive)) == 3
    assert sel.selected_under(folder_key(os.path.join(sub, "Holiday"))) == 2
    assert {p.root for p in sel.items()} == {sub}
    # Selecting the parent folder makes it the root of the already selected photos too (folder structure).
    assert sel.add_entries(walk(archive).photos, root=archive) == 2
    assert {p.root for p in sel.items()} == {archive}
    sel.add_entries(walk(sub).photos, root=sub)  # a deeper root does not override the outer one
    assert {p.root for p in sel.items()} == {archive}
    assert len(sel.folders()) == 4
    assert sel.remove_under([os.path.join(sub, "Holiday")]) == 2
    assert sel.selected_under(folder_key(archive)) == 3 and sel.selected_under(folder_key(sub)) == 1
    # Info is read in the background; photos removed from the selection are never read.
    assert wait_until(app, lambda: sel.pending() == 0)
    assert all(p.info is not None and p.info.has_gps for p in sel.items())
    os.remove(os.path.join(archive, "cover.jpg"))
    sel.drop_missing()
    assert len(sel) == 2
    sel.shutdown()


def test_folder_selection_states(app, archive):
    sel = Selection()
    folders = FolderSelection(sel)
    y2019, y2020 = os.path.join(archive, "2019"), os.path.join(archive, "2020")
    try:
        folders.set_selected([archive], True)
        assert folders.state(archive) == SCANNING and folders.state(y2019) == SCANNING
        assert wait_until(app, lambda: not folders.busy())
        assert len(sel) == 5 and folders.state(archive) == FULL and folders.state(y2019) == FULL
        assert folders.state(os.path.dirname(archive)) == PARTIAL  # the parent folder was not counted
        folders.toggle(y2019)  # all selected → deselect
        assert len(sel) == 2 and folders.state(y2019) == NONE and folders.state(archive) == PARTIAL
        folders.toggle(archive)  # partial → select all
        assert wait_until(app, lambda: not folders.busy()) and len(sel) == 5
        assert folders.state(archive) == FULL
        folders.toggle(archive)
        assert len(sel) == 0 and folders.state(archive) == NONE
        # Deselecting a subfolder while the scan is running drops it from the result.
        folders.set_selected([archive], True)
        folders.set_selected([y2020], False)
        assert folders.state(y2020) == NONE and folders.state(archive) == SCANNING
        assert wait_until(app, lambda: not folders.busy())
        assert len(sel) == 4 and folders.state(y2020) == NONE and folders.state(archive) == PARTIAL
        folders.set_selected([archive], False)
        # A cancelled scan adds nothing.
        done = []
        folders.scanned.connect(lambda root, found, added, unreadable, cancelled: done.append((added, cancelled)))
        folders.set_selected([archive], True)
        folders.cancel_all()
        assert wait_until(app, lambda: not folders.busy() and done)
        assert len(sel) == 0 and done[-1] == (0, True)
    finally:
        folders.shutdown()
        sel.shutdown()


def test_large_folder_selection_asks_first(app, archive):
    sel = Selection()
    asked = []
    folders = FolderSelection(sel, confirm=lambda n: asked.append(n) or False, bulk_limit=3)
    try:
        folders.set_selected([archive], True)
        assert wait_until(app, lambda: not folders.busy())
        assert asked == [5] and len(sel) == 0
    finally:
        folders.shutdown()
        sel.shutdown()


# ------------------------------------------------------------------ UI


def test_browser_folder_tiles_select_without_entering(app, tmp_path, archive):
    from fotodit.ui.browser import BrowserPage, TileDelegate
    from fotodit.ui.main_window import TILE
    from fotodit.ui.thumbs import ThumbnailLoader

    sel = Selection()
    folders = FolderSelection(sel)
    loader = ThumbnailLoader(64)
    page = BrowserPage(sel, folders, loader, TILE)
    try:
        page.resize(1000, 700)
        page.show()
        page.navigate(archive)
        app.processEvents()
        kinds = [(e.kind, e.name) for e in page.model.entries]
        assert kinds == [("dir", "2019"), ("dir", "2020"), ("photo", "cover.jpg")]
        # Tile counts ("3 photos") arrive in the background.
        assert wait_until(app, lambda: folders.count(os.path.join(archive, "2019")) is not None)
        assert folders.count(os.path.join(archive, "2019")).photos == 3
        grid = page.grid
        assert wait_until(app, lambda: grid.visualRect(page.model.index(0)).width() > 0)
        rect = grid.visualRect(page.model.index(0))
        badge = TileDelegate.badge_rect(rect).center().toPoint()
        QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, badge)
        assert page.folder == archive  # did not enter the folder
        assert wait_until(app, lambda: not folders.busy() and len(sel) == 3)
        assert wait_until(app, lambda: "1 seçili" in page.count_label.text())  # the view updates ~10 times/s
        # A single click on the tile only highlights the folder; a double-click opens it.
        rect = grid.visualRect(page.model.index(1))
        QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        assert page.folder == archive and grid.currentIndex().row() == 1
        assert len(sel) == 3  # nothing selected or deselected
        QTest.mouseDClick(grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        assert page.folder == os.path.join(archive, "2020")
        page.go_back()
        page.select_all()  # photos + subfolders
        assert wait_until(app, lambda: not folders.busy() and len(sel) == 5)
        assert wait_until(app, lambda: not page.all_btn.isEnabled() and page.none_btn.isEnabled())
        # The open folder has been counted too, so it shows as fully selected (in the tree).
        assert wait_until(app, lambda: folders.state(archive) == FULL)
        page.select_none()
        assert len(sel) == 0
        # Ctrl+click selects the folder too.
        rect = grid.visualRect(page.model.index(1))
        QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, rect.center())
        assert wait_until(app, lambda: not folders.busy() and len(sel) == 1)
    finally:
        page.close()
        folders.shutdown()
        loader.shutdown()
        sel.shutdown()


def test_browser_path_bar_steps_and_typing(app, archive):
    from fotodit.ui.browser import BrowserPage, crumbs
    from fotodit.ui.main_window import TILE
    from fotodit.ui.thumbs import ThumbnailLoader

    holiday, y2019, y2020 = (os.path.join(archive, *p) for p in (("2019", "Holiday"), ("2019",), ("2020",)))
    steps = crumbs(holiday)
    assert steps[-3:] == [("Ayşe", archive), ("2019", y2019), ("Holiday", holiday)]
    assert steps[0][1] == os.path.splitdrive(archive)[0] + os.sep  # the steps start at the drive
    sel = Selection()
    folders = FolderSelection(sel)
    loader = ThumbnailLoader(64)
    page = BrowserPage(sel, folders, loader, TILE)
    try:
        page.resize(1000, 700)
        page.show()
        page.path_bar.setGeometry(0, 0, 900, 40)
        page.path_bar.show()
        page.navigate(archive)
        # A photo's tooltip keeps its name as it is ("cover.jpg", not "cover,jpg").
        tip = page.model.data(page.model.index(2), Qt.ItemDataRole.ToolTipRole)
        assert tip.startswith("cover.jpg\n") and " KB\n" in tip
        page.navigate(holiday)
        app.processEvents()
        bar = page.crumbs
        assert [label for _r, label, _p in bar._shown][-3:] == ["Ayşe", "2019", "Holiday"]
        # A click on a step opens its folder.
        step = next(r for r, label, _p in bar._shown if label == "2019")
        QTest.mouseClick(bar, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, step.center().toPoint())
        assert page.folder == y2019
        # A click on the empty part turns the bar into a field; Esc gives the steps back.
        QTest.mouseClick(bar, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(bar.width() - 4, bar.height() // 2))
        assert page.path_edit.isVisible() and not bar.isVisible()
        assert page.path_edit.text() == y2019
        QTest.keyClick(page.path_edit, Qt.Key.Key_Escape)
        assert bar.isVisible() and not page.path_edit.isVisible()
        # A path that does not exist stays in the field to be corrected; Enter on a folder opens it.
        page.edit_path()
        page.path_edit.setText(os.path.join(archive, "yok"))
        QTest.keyClick(page.path_edit, Qt.Key.Key_Return)
        assert page.path_edit.isVisible() and page.folder == y2019
        assert page.stack.currentWidget() is page.empty_holder
        page.path_edit.setText(f'"{y2020}"')
        QTest.keyClick(page.path_edit, Qt.Key.Key_Return)
        assert page.folder == y2020 and bar.isVisible() and not page.path_edit.isVisible()
        assert page.stack.currentWidget() is page.grid
    finally:
        page.close()
        folders.shutdown()
        loader.shutdown()
        sel.shutdown()


def test_tree_checkbox_selects_folder(app, archive):
    from fotodit.ui.nav import FolderTreeModel

    sel = Selection()
    folders = FolderSelection(sel)
    model = FolderTreeModel(folders)
    try:
        model.setRootPath("")
        model.checkToggled.connect(folders.toggle)
        index = model.index(archive.replace("\\", "/"))
        assert index.isValid() and model.flags(index) & Qt.ItemFlag.ItemIsUserCheckable
        assert model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
        drive = model.index(archive[:3].replace("\\", "/"))
        assert model.data(drive, Qt.ItemDataRole.CheckStateRole) is None  # a drive root cannot be selected
        model.setData(index, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
        assert model.toggled
        assert wait_until(app, lambda: not folders.busy() and len(sel) == 5)
        assert model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    finally:
        folders.shutdown()
        sel.shutdown()


def test_main_window_folder_workflow(app, tmp_path, archive):
    from fotodit.ui.main_window import MainWindow

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("browse/folder", os.path.dirname(archive))
    win = MainWindow(settings)
    out = str(tmp_path / "Output")
    try:
        win.options.set_output_folder(out)
        win.add_recursive([archive])
        assert wait_until(app, lambda: not win.folders.busy() and len(win.selection) == 5)
        assert wait_until(app, lambda: win.selection.pending() == 0)
        win.tabs.setCurrentWidget(win.selected)
        app.processEvents()
        model = win.selected.model
        assert len(model.plan) == 5 and model.multi_folder
        shown = sorted(model.source_text(e) for e in model.plan)
        assert "2019\\Holiday\\IMG_0001.jpg" in shown and "cover.jpg" in shown
        assert wait_until(app, lambda: win.options.structure_hint.isVisibleTo(win.options))  # panel updates in ~0.1 s
        win.options.keep_structure()
        win.replan()
        opts = win.options.job_options()
        assert opts.output.group_by == GROUP_SOURCE
        assert win._space_warnings(model.plan, opts) == []
        report = Runner(model.plan, opts, workers=2).run()
        assert not report.failed
        written = sorted(os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs)
        assert written == ["Ayşe\\2019\\Holiday\\IMG_0001.jpg", "Ayşe\\2019\\Holiday\\IMG_0002.jpg",
                           "Ayşe\\2019\\IMG_0001.jpg", "Ayşe\\2020\\IMG_0001.jpg", "Ayşe\\cover.jpg"]
        assert all(scan_file(os.path.join(out, w)).clean for w in written)
    finally:
        win.close()


def test_counts_refresh_when_stale(monkeypatch):
    from fotodit.ui import folders as folders_module

    count = folders_module.FolderCount(5)
    assert count.fresh()
    monkeypatch.setattr(folders_module, "COUNT_TTL", 0.0)
    assert not count.fresh()  # stale count: the folder is counted again when it shows up again


def test_badge_hit_area():
    from PySide6.QtCore import QRect

    from fotodit.ui.browser import TileDelegate

    rect = QRect(0, 0, 188, 206)
    badge = TileDelegate.badge_rect(rect)
    assert TileDelegate.folder_rect(rect).contains(badge)  # on the folder icon
    assert TileDelegate.badge_hit(rect, badge.center().toPoint())
    assert TileDelegate.badge_hit(rect, (badge.topLeft() - QPointF(6, 6)).toPoint())  # a forgiving target
    assert not TileDelegate.badge_hit(rect, QPoint(94, 100))
