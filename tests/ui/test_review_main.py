"""Fixes from the review of the main window: the Selected tab's table, the Browse tab, folder scans, the
selection's running counts and the window's sheets (no window is shown; offscreen Qt)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import builders as b  # noqa: E402
from helpers import make_files, wait_until  # noqa: E402
from PySide6.QtCore import (  # noqa: E402
    QEvent,
    QEventLoop,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QSettings,
    Qt,
    QTimer,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from fotodit import i18n  # noqa: E402
from fotodit.core.formats import Fmt  # noqa: E402
from fotodit.core.photoinfo import PhotoInfo  # noqa: E402
from fotodit.core.planner import SORT_MANUAL, Photo, PlanEntry  # noqa: E402
from fotodit.i18n import t  # noqa: E402
from fotodit.ui.folders import FolderSelection  # noqa: E402
from fotodit.ui.selected_page import COL_DATE, SelectedPage  # noqa: E402
from fotodit.ui.selection import Selection  # noqa: E402
from fotodit.ui.thumbs import ThumbnailLoader  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(app):
    loader = ThumbnailLoader(64)
    page = SelectedPage(loader)
    page.resize(1000, 600)
    page.show()
    yield page
    page.close()
    loader.shutdown()


@pytest.fixture
def window(app, tmp_path):
    from fotodit.ui.main_window import MainWindow

    folder = tmp_path / "photos"
    folder.mkdir()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("browse/folder", str(folder))
    win = MainWindow(settings)
    win.options.set_output_folder(str(tmp_path / "out"))
    yield win
    win._confirm_discard_edits = lambda count: True
    win.close()


@pytest.fixture
def browser(app):
    """A Browse page with its own selection (the files' info is not read)."""
    from fotodit.ui.browser import BrowserPage
    from fotodit.ui.main_window import TILE

    sel = Selection()
    sel.hold_info()
    folders = FolderSelection(sel)
    loader = ThumbnailLoader(64)
    page = BrowserPage(sel, folders, loader, TILE)
    yield page
    page.close()
    folders.shutdown()
    loader.shutdown()
    sel.shutdown()


def _plan(folder, names) -> list[PlanEntry]:
    """Plan rows for files that need not exist (taken on 5 December 2024, 14:30)."""
    photos = [Photo(os.path.join(str(folder), n), 1000, 0.0, i) for i, n in enumerate(names)]
    return [PlanEntry(p, i + 1, str(folder), p.name, None, datetime(2024, 12, 5, 14, 30), "exif")
            for i, p in enumerate(photos)]


def _names(plan) -> list[str]:
    return [os.path.basename(e.photo.path) + (" (skipped)" if e.skip_reason else "") for e in plan]


# ---------------------------------------------------------------- the Selected tab


def test_restoring_a_large_table_selection_is_one_step(app, page, tmp_path):
    """After a replan the highlighted rows are selected again in one step: row by row, every step
    recounted all the rows before it, and a few thousand selected rows held the window for minutes."""
    plan = _plan(tmp_path, [f"IMG_{i:04d}.jpg" for i in range(400)])
    page.set_plan(plan, False, str(tmp_path), False, "")
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    for lo, hi in ((0, 149), (200, 259), (399, 399)):
        page.table.selectionModel().select(QItemSelection(page.model.index(lo, 0), page.model.index(hi, 5)), flags)
    chosen = {e.photo.path for e in plan[:150] + plan[200:260] + plan[399:]}
    calls = []
    page._update_buttons = lambda: calls.append(1)
    turned = plan[::-1]
    page.set_plan(turned, False, str(tmp_path), False, "")  # a replan (after Ctrl+A and Ctrl+R, say)
    assert {turned[r].photo.path for r in page._selected_rows()} == chosen
    assert len(calls) <= 3  # once per change of the selection, not once per row


def test_duplicates_are_selected_in_one_step_and_fit_the_list(app, page, tmp_path):
    from fotodit.core.duplicates import EXACT, DuplicateGroup

    plan = _plan(tmp_path, [f"IMG_{i:04d}.jpg" for i in range(300)])
    page.set_plan(plan, False, str(tmp_path), False, "")
    paths = [e.photo.path for e in plan]
    gone = [os.path.join(str(tmp_path), f"gone{i}.jpg") for i in range(3)]  # taken off the list meanwhile
    result = [DuplicateGroup([paths[i], paths[i + 1]], EXACT) for i in range(0, 280, 2)]
    result += [DuplicateGroup(gone[:2], EXACT), DuplicateGroup([gone[2], paths[280]], EXACT)]
    messages, calls = [], []
    page.message.connect(messages.append)
    page._update_buttons = lambda: calls.append(1)
    page._search = search = object()
    page._duplicates_found(search, result)
    assert page._selected_rows() == list(range(1, 280, 2))
    assert messages == [t("selected.dupes_found", n=140)]  # only copies still on the list are counted
    assert len(calls) <= 3


def test_a_cancelled_duplicate_search_is_ignored(app, page, tmp_path, monkeypatch):
    from fotodit.core import duplicates

    started, stopped = threading.Event(), threading.Event()

    def slow_search(paths, cancelled=None, progress=None):
        started.set()
        end = time.monotonic() + 10
        while not cancelled() and time.monotonic() < end:
            time.sleep(0.01)
        stopped.set()
        return [duplicates.DuplicateGroup(paths[:2], duplicates.EXACT)]  # a result that comes too late

    monkeypatch.setattr(duplicates, "find_duplicates", slow_search)
    page.set_plan(_plan(tmp_path, ["a.jpg", "b.jpg", "c.jpg"]), False, str(tmp_path), False, "")
    messages = []
    page.message.connect(messages.append)
    page.find_duplicates()
    assert started.wait(5) and not page.duplicates_btn.isEnabled()
    page.cancel_duplicates()
    assert stopped.wait(5) and page.duplicates_btn.isEnabled()
    wait_until(app, lambda: False, 0.3)  # (time for the late result to arrive)
    assert messages == [t("selected.dupes_searching")]
    assert not page._selected_rows() and not page.model.copies


def _choose_while_replanned(app, page, order, label) -> None:
    """Right-clicks the first row and chooses `label` from the menu, while the list is planned anew in
    `order` (as the replan timer does while info arrives)."""

    def replan():
        page.set_plan(order, False, page.model.base, False, "")

    def choose():
        popup = QApplication.activePopupWidget()
        action = next(a for a in popup.actions() if a.text().startswith(label))
        action.trigger()
        popup.close()

    def give_up():  # (a test that fails must not hang in the menu)
        popup = QApplication.activePopupWidget()
        if popup is not None:
            popup.close()

    QTimer.singleShot(50, replan)
    QTimer.singleShot(150, choose)
    QTimer.singleShot(3000, give_up)
    page._context_menu(page.table.visualRect(page.model.index(0, 2)).center())


def test_context_menu_acts_on_the_files_it_was_opened_for(app, page, tmp_path):
    plan = _plan(tmp_path, ["A.jpg", "B.jpg", "C.jpg"])
    page.set_plan(plan, False, str(tmp_path), False, "")
    app.processEvents()
    removed, cropped = [], []
    page.removeRequested.connect(lambda paths: removed.append([os.path.basename(p) for p in paths]))
    page.cropRequested.connect(cropped.append)
    _choose_while_replanned(app, page, [plan[2], plan[0], plan[1]], t("menu.remove_from_list"))
    assert removed == [["A.jpg"]]  # A was right-clicked; C took its row while the menu was open
    page.set_plan(plan, False, str(tmp_path), False, "")
    _choose_while_replanned(app, page, [plan[2], plan[0], plan[1]], t("menu.crop"))
    assert cropped == [1]  # A's row now


def test_dragged_rows_travel_as_files(app, page, tmp_path):
    plan = _plan(tmp_path, ["A.jpg", "B.jpg", "C.jpg"])
    page.set_plan(plan, False, str(tmp_path), True, "")
    data = page.model.mimeData([page.model.index(0, 0)])  # A is dragged …
    page.set_plan([plan[2], plan[0], plan[1]], False, str(tmp_path), True, "")  # … while the list is planned anew
    dropped = []
    page.model.rowsDropped.connect(lambda rows, target: dropped.append(rows))
    page.model.dropMimeData(data, Qt.DropAction.MoveAction, 3, 0, QModelIndex())
    assert dropped == [[1]]


def test_dropping_many_rows_is_quick(app):
    page = SelectedPage(ThumbnailLoader(64))
    page.model.plan = [SimpleNamespace(photo=SimpleNamespace(path=f"p{i}")) for i in range(12_000)]
    emitted = []
    page.reorderRequested.connect(emitted.append)
    started = time.perf_counter()
    page._rows_dropped(list(range(0, 12_000, 3)), 6000)
    assert time.perf_counter() - started < 0.3  # (once over a second: a set was built for every row)
    assert len(emitted[-1]) == 12_000 and emitted[-1][4000] == "p0"  # (4,000 rows stay above the drop)


def test_date_column_follows_the_language(app, page, tmp_path):
    plan = _plan(tmp_path, ["a.jpg"])
    try:
        i18n.set_language("en")
        page.set_plan(plan, False, str(tmp_path), False, "")
        assert page.model.data(page.model.index(0, COL_DATE)) == "2024-12-05 14:30"
        i18n.set_language("tr")
        page.set_plan(list(plan), False, str(tmp_path), False, "")
        assert page.model.data(page.model.index(0, COL_DATE)) == "05.12.2024 14:30"
    finally:
        i18n.set_language("tr")


# ---------------------------------------------------------------- the window


def test_preview_starts_at_the_clicked_file_after_a_replan(app, window, tmp_path):
    win = window
    paths = [str(tmp_path / "photos" / n) for n in ("a.jpg", "b.jpg", "c.jpg")]
    win.selection.hold_info()
    win.selection.add_entries([(p, 1, 1.0) for p in paths])
    win.options.sort.setCurrentIndex(win.options.sort.findData(SORT_MANUAL))
    win.tabs.setCurrentWidget(win.selected)
    win.replan()
    assert _names(win.selected.model.plan) == ["a.jpg", "b.jpg", "c.jpg"]
    opened = []
    win.open_preview = lambda items, index, plan=None, crop=False: opened.append(items[index].path)
    win.selection.reorder([paths[2], paths[0], paths[1]])  # the list is about to be planned anew
    assert win._replan_timer.isActive()
    win._preview_selected(0)  # row 0 was a.jpg when it was double-clicked
    assert opened == [paths[0]]


def test_window_state_does_not_walk_the_selection(app, window, tmp_path, monkeypatch):
    """The counts are asked for after every batch of info; with 50,000 files a walk through all of them
    each time took a good part of every quarter second."""
    win = window
    win.selection.hold_info()
    win.selection.add_entries([(str(tmp_path / f"IMG_{i}.dng"), 1, 1.0) for i in range(300)])
    looked = []
    real = Photo.fmt
    monkeypatch.setattr(Photo, "fmt", lambda photo: looked.append(photo) or real(photo))
    win._update_state()
    assert not looked
    assert win.tabs.tabText(1) == t("main.selected_tab", n=300) and win.options.raw_row.isVisibleTo(win.options)


def test_moving_rows_follows_the_list_as_shown(app, window, tmp_path):
    """Up/Down move rows in the order the list shows: a file to be skipped is listed last, so a step in
    the selection's order could pass it and change nothing on screen."""
    win = window
    a, x, c = (str(tmp_path / "photos" / n) for n in ("A.jpg", "X.dng", "C.jpg"))
    win.selection.hold_info()
    win.selection.add_entries([(p, 1, 1.0) for p in (a, x, c)])
    for path, fmt in ((a, Fmt.JPEG), (x, Fmt.RAW), (c, Fmt.JPEG)):
        win.selection._on_info(path, 1.0, PhotoInfo(fmt=fmt))
    win.options.sort.setCurrentIndex(win.options.sort.findData(SORT_MANUAL))
    win.options.convert.setChecked(False)  # a RAW whose metadata is to be removed is skipped without JPG
    win.tabs.setCurrentWidget(win.selected)
    win.replan()
    assert _names(win.selected.model.plan) == ["A.jpg", "C.jpg", "X.dng (skipped)"]
    win.selected.table.selectRow(0)
    win.selected._move(1)
    win.replan()
    assert _names(win.selected.model.plan) == ["C.jpg", "A.jpg", "X.dng (skipped)"]
    win.selected._move(-1)  # (A stays highlighted)
    win.replan()
    assert _names(win.selected.model.plan) == ["A.jpg", "C.jpg", "X.dng (skipped)"]


def test_a_job_stops_the_duplicate_search_and_frees_its_sheets(app, window, tmp_path, monkeypatch):
    from fotodit.ui.run_dialog import RunDialog
    from fotodit.ui.sheet import Sheet

    win = window
    paths = []
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / "photos" / name).write_bytes(b.jpeg(b.photo(64, 48)))
        paths.append(str(tmp_path / "photos" / name))
    win.selection.add(paths)
    assert wait_until(app, lambda: win.selection.pending() == 0)
    win.replan()
    search = SimpleNamespace(cancel=threading.Event())
    win.selected._search = search  # a search for duplicates still running

    def start_on_confirmation(sheet):
        sheet._finish(sheet.buttons()[-1], True)  # "Start"
        return 1

    monkeypatch.setattr(Sheet, "exec", start_on_confirmation)
    monkeypatch.setattr(RunDialog, "exec", lambda dialog: wait_until(app, lambda: not dialog._running, 60) and 0)
    win.start()
    assert len(os.listdir(tmp_path / "out")) == 2  # the job ran
    assert search.cancel.is_set() and win.selected._search is None  # it no longer opens the files
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not win.findChildren(Sheet)  # neither the confirmation nor the run sheet (with the plan) stays


# ---------------------------------------------------------------- the selection's counts


def test_selection_keeps_its_counts(app, tmp_path):
    sel = Selection()
    sel.hold_info()
    paths = [str(tmp_path / n) for n in ("a.jpg", "b.dng", "c.mp4", "d.jpg", "e.cr3")]

    def counts():
        return sel.raw_count(), sel.video_count(), sel.gps_count()

    def recount():
        items = sel.items()
        return (sum(p.fmt() == Fmt.RAW for p in items), sum(p.is_video for p in items),
                sum(bool(p.info and p.info.has_gps) for p in items))

    try:
        sel.add_entries([(p, 1, 1.0) for p in paths])
        assert counts() == recount() == (2, 1, 0)
        sel._on_info(paths[3], 1.0, PhotoInfo(fmt=Fmt.JPEG, has_gps=True))  # d.jpg has a location
        sel._on_info(paths[1], 1.0, PhotoInfo(fmt=Fmt.JPEG))  # b.dng is really a JPEG
        assert counts() == recount() == (1, 1, 1)
        sel.remove([paths[3]])
        assert counts() == recount() == (1, 1, 0)
        photo, index = sel.entry(paths[2])
        sel.remove([paths[2]])
        assert counts() == recount() == (1, 0, 0)
        sel.restore(photo, index)
        assert counts() == recount() == (1, 1, 0)
        sel.remove_under([str(tmp_path)])
        assert counts() == recount() == (0, 0, 0)
        sel.add_entries([(p, 1, 1.0) for p in paths])
        sel.clear()
        assert counts() == (0, 0, 0)
    finally:
        sel.shutdown()


def test_a_file_that_breaks_the_reader_does_not_stop_the_others(app, tmp_path, monkeypatch):
    """With two readers, two files whose reading raised once stopped info reading for the session."""
    import struct

    from fotodit.ui import selection as selection_module

    paths = make_files(tmp_path / "photos", "a.jpg", "bad1.jpg", "c.jpg", "d.jpg", "bad2.jpg", "f.jpg")
    real = selection_module.read_info

    def read(path):
        if os.path.basename(path).startswith("bad"):
            raise struct.error("unpack requires a buffer of 4 bytes")  # (a damaged file's parser, say)
        return real(path)

    monkeypatch.setattr(selection_module, "read_info", read)
    sel = Selection()
    try:
        sel.add(paths)
        assert wait_until(app, lambda: sel.pending() == 0, 10)
        info = {os.path.basename(p.path): p.info for p in sel.items()}
        assert all(info[n].error is None and info[n].fmt == Fmt.JPEG for n in ("a.jpg", "c.jpg", "d.jpg", "f.jpg"))
        assert info["bad1.jpg"].error == t("error.other", error="unpack requires a buffer of 4 bytes")
        assert wait_until(app, lambda: sel._workers == 0, 10)  # the readers ended as usual
    finally:
        sel.shutdown()


# ---------------------------------------------------------------- the Browse tab


def test_select_all_in_browse_opens_no_file(app, browser, tmp_path, monkeypatch):
    folder = str(tmp_path / "many")
    make_files(folder, *(f"IMG_{i:03d}.jpg" for i in range(40)))
    browser.navigate(folder)
    opened = []
    real = os.stat

    def stat(path, *args, **kwargs):
        if str(path).startswith(folder):
            opened.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    browser.select_all()
    assert len(browser.selection) == 40
    browser.selection.clear()
    browser.grid._anchor = 0
    browser.grid._select_range(39, True)  # Shift+click from the first photo to the last
    assert len(browser.selection) == 40
    monkeypatch.undo()
    assert not opened  # the folder's listing already has every file's size and date


def test_browse_counts_do_no_path_work_per_file(app, browser, tmp_path, monkeypatch):
    folder = str(tmp_path / "many")
    make_files(folder, *(f"IMG_{i:03d}.jpg" for i in range(200)))
    browser.navigate(folder)
    browser.select_all()
    browser.selection.remove([e.path for e in browser.model.photos()[100:]])
    calls = []
    real = os.path.abspath
    monkeypatch.setattr(os.path, "abspath", lambda path: calls.append(path) or real(path))
    browser._update_counts()  # (after every change of the selection)
    monkeypatch.undo()
    assert len(calls) < 10
    assert t("browser.chosen_paren", n=100) in browser.count_label.text()


def test_up_from_a_drive_root_goes_to_this_pc():
    from fotodit.ui.browser import BrowserPage, folder_path

    assert folder_path("") == ""
    assert folder_path("D:") == "D:\\"  # the drive, not the current folder on it
    assert folder_path("D:\\Photos\\") == "D:\\Photos"
    went = []
    page = SimpleNamespace(folder="C:\\", navigate=lambda path, record=True: went.append(path))
    BrowserPage.go_up(page)
    page.folder = "C:\\Photos"
    BrowserPage.go_up(page)
    assert went == ["", "C:\\"]


def test_a_folder_that_is_gone_leaves_nothing_selectable(app, browser, tmp_path):
    import shutil

    a, c = str(tmp_path / "A"), str(tmp_path / "C")
    make_files(a, "1.jpg", "2.jpg")
    make_files(c, "3.jpg")
    browser.navigate(a)
    browser.navigate(c)
    shutil.rmtree(a)
    browser.go_back()
    assert browser.stack.currentWidget() is browser.empty_holder
    assert browser._pos == 1 and browser.back_btn.isEnabled()  # the history stays where it was
    assert not browser.all_btn.isEnabled() and not browser.count_label.isVisibleTo(browser)
    browser.select_all()
    assert len(browser.selection) == 0  # nothing of C, which is out of sight
    browser.refresh()  # F5 shows the open folder again
    assert browser.stack.currentWidget() is browser.grid and len(browser.model.photos()) == 1


# ---------------------------------------------------------------- folder scans


def test_bulk_no_is_reported_before_the_scans_stop(app, archive):
    """The window sums up the scans once nothing is scanned any more: the folder answered with "No"
    must be reported by then, or the message is lost and turns up with the next scan's."""

    def confirm(count):
        loop = QEventLoop()  # the sheet's own event loop while the question is read
        QTimer.singleShot(300, loop.quit)
        loop.exec()
        return False

    sel = Selection()
    folders = FolderSelection(sel, confirm=confirm, bulk_limit=3)
    events = []
    folders.scanned.connect(lambda root, found, added, unreadable, cancelled: events.append(("scanned", cancelled)))
    folders.changed.connect(lambda: None if folders.busy() else events.append(("idle", None)))
    try:
        folders.set_selected([archive], True)
        assert wait_until(app, lambda: ("scanned", True) in events and ("idle", None) in events)
        assert events.index(("scanned", True)) < events.index(("idle", None))
        assert len(sel) == 0
    finally:
        folders.shutdown()
        sel.shutdown()


def test_closing_during_a_stuck_folder_scan_does_not_crash(tmp_path):
    """A scan stuck in a listing (a network share, a disk spinning up) outlasts the window's wait when it
    closes; the process must still end normally (a QThread destroyed while running ended it with an abort)."""
    folder = tmp_path / "share"
    folder.mkdir()
    script = tmp_path / "stuck_scan.py"
    script.write_text(textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {ROOT!r})
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        import fotodit.ui.folders as folders
        from fotodit.ui.selection import Selection

        real_walk = folders.walk

        def stuck(root, **kwargs):
            time.sleep(4.5)
            return real_walk(root, **kwargs)

        folders.walk = stuck
        folders.SHUTDOWN_WAIT = 0.2
        sel = Selection()
        scans = folders.FolderSelection(sel)
        scans.set_selected([{str(folder)!r}], True)
        time.sleep(0.3)
        scans.shutdown()  # what the closing window does
        sel.shutdown()
        del scans, sel
        print("closed")
    """), encoding="utf-8")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "FOTODIT_DATA_DIR": str(tmp_path)}
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120, env=env)
    assert done.returncode == 0 and "closed" in done.stdout, done.stderr
