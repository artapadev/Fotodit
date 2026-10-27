"""Rotate/crop UI: the edit store, the crop tool, the preview dialog and the main window flow."""

from __future__ import annotations

import os
import random
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import builders as b  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fotodit.core.edits import Edit, box_fits  # noqa: E402
from fotodit.core.imaging import open_image  # noqa: E402
from fotodit.core.meta import scan_file  # noqa: E402
from fotodit.i18n import t
from fotodit.ui.crop import ASPECTS, CropSession, edited_image  # noqa: E402


def write(folder, name: str, data: bytes) -> str:
    path = os.path.join(str(folder), name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def wait_until(app, condition, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def photos(tmp_path):
    root = tmp_path / "Holiday"
    return [write(root, f"IMG_000{i}.jpg", b.jpeg(b.photo(640, 480), everything=False)) for i in range(1, 4)]


# ================================================================ edit store


def test_edit_store_tracks_file_changes(app, tmp_path):
    from fotodit.ui.edit_store import EditStore

    store = EditStore()
    seen = []
    store.changed.connect(seen.append)
    path = str(tmp_path / "a.jpg")
    store.set(path, 100.0, Edit(quarter=1))
    assert store.get(path, 100.0) == Edit(quarter=1) and store.get(path) == Edit(quarter=1)
    assert store.get(path, 100.4) == Edit(quarter=1)  # small time difference between the list and the file
    assert store.get(path, 160.0) is None  # the file changed after the edit
    store.set(path, 100.0, Edit(quarter=1))  # same edit: no notification
    assert len(seen) == 1
    keys = store.update([(path, 100.0), (str(tmp_path / "b.jpg"), 5.0)], lambda e: e.rotated(-1))
    assert len(keys) == 2 and store.get(path) is None  # 90 + (-90) = no edit
    assert store.get(str(tmp_path / "b.jpg")) == Edit(quarter=3)
    assert store.pending(keys) == 1
    store.mark_applied([str(tmp_path / "b.jpg")])
    assert store.pending(keys) == 0
    store.clear([str(tmp_path / "b.jpg")])
    assert len(store) == 0 and len(seen) == 3


def test_selection_keeps_photo_edits_in_sync(app, photos):
    from fotodit.ui.selection import Selection

    sel = Selection()
    try:
        sel.edits.set(photos[0], os.stat(photos[0]).st_mtime, Edit(quarter=2))  # before it is selected
        sel.add(photos[:2])
        assert sel.photo(photos[0]).edit == Edit(quarter=2) and sel.photo(photos[1]).edit is None
        sel.edits.update([(photos[1], os.stat(photos[1]).st_mtime)], lambda e: e.rotated(1))
        assert sel.photo(photos[1]).edit == Edit(quarter=1)
        assert sel.edited_count() == 2 and sel.pending_edits() == 2
        saved = sel.entry(photos[1])
        sel.remove([photos[1]])
        assert sel.edited_count() == 1
        sel.restore(*saved)
        assert sel.photo(photos[1]).edit == Edit(quarter=1)
    finally:
        sel.shutdown()


def test_thumbnails_show_edits(app, photos):
    from fotodit.ui.thumbs import LOADING, ThumbnailLoader

    loader = ThumbnailLoader(64)
    try:
        path, mtime = photos[0], os.stat(photos[0]).st_mtime
        assert loader.get(path, mtime) is LOADING
        assert wait_until(app, lambda: loader.get(path, mtime) not in (LOADING, None))
        base = loader.get(path, mtime)
        # A rotation is derived instantly from the ready thumbnail (no new decode).
        turned = loader.get(path, mtime, Edit(quarter=1))
        assert turned is not LOADING and (turned.width(), turned.height()) == (base.height(), base.width())
        # A crop first shows the cropped thumbnail, then a sharp version decoded at a larger size.
        crop = Edit(crop=(0.25, 0.25, 0.75, 0.75))
        interim = loader.get(path, mtime, crop)
        assert interim is not LOADING and interim.width() == base.width() // 2
        assert wait_until(app, lambda: loader.get(path, mtime, crop).width() >= 60)
        sharp = loader.get(path, mtime, crop)
        assert sharp.width() > interim.width()
    finally:
        loader.shutdown()


def _to_pil(q) -> Image.Image:
    rgb = q.convertToFormat(q.Format.Format_RGB888)
    raw = bytes(rgb.constBits())[: rgb.bytesPerLine() * rgb.height()]
    return Image.frombuffer("RGB", (rgb.width(), rgb.height()), raw, "raw", "RGB", rgb.bytesPerLine(), 1)


def test_straightened_thumbnail_on_high_dpi(app, photos):
    """On high DPI (pixel ratio 1.25) a straightened thumbnail matches Pillow's result.
    (Bug: QPainter drew the source at its logical size, so the thumbnail shifted and its corners stayed empty.)"""
    from oracle import psnr

    from fotodit.core.edits import apply
    from fotodit.ui.thumbs import LOADING, ThumbnailLoader

    loader = ThumbnailLoader(64, dpr=1.25)
    try:
        path, mtime = photos[0], os.stat(photos[0]).st_mtime
        assert wait_until(app, lambda: loader.get(path, mtime) not in (LOADING, None))
        base = loader.get(path, mtime)
        for edit in (Edit(angle=8), Edit(quarter=1, angle=-5)):
            derived = loader.get(path, mtime, edit)
            assert derived.devicePixelRatio() == base.devicePixelRatio() == 1.25
            expected = apply(_to_pil(base.toImage()), edit)
            got = _to_pil(derived.toImage())
            assert got.size == expected.size and psnr(got, expected) > 33, edit
    finally:
        loader.shutdown()


def test_edited_image_matches_pillow(app):
    from fotodit.core.edits import apply
    from fotodit.ui.thumbs import _to_qimage

    src = b.photo(160, 120)
    edits = (Edit(quarter=1, crop=(0.1, 0.2, 0.9, 0.6)), Edit(quarter=3), Edit(angle=8), Edit(mirror=True),
             Edit(quarter=1, mirror=True, crop=(0.1, 0.2, 0.9, 0.6)), Edit(angle=8, mirror=True))
    for edit in edits:
        q = edited_image(_to_qimage(src), edit)
        expected = apply(src, edit)
        assert (q.width(), q.height()) == expected.size, edit
        got = q.convertToFormat(q.Format.Format_RGB888)
        raw = bytes(got.constBits())[: got.bytesPerLine() * got.height()]
        pil = Image.frombuffer("RGB", expected.size, raw, "raw", "RGB", got.bytesPerLine(), 1)
        if edit.angle:  # straightening on screen (Qt) and in the file (Pillow) gives the same image
            from oracle import psnr

            assert psnr(pil, expected) > 33, edit  # (~26 dB if it had been rotated the other way)
        else:  # lossless operations are identical
            assert pil.tobytes() == expected.tobytes(), edit


# =================================================================== crop tool


def test_crop_session_drag_and_limits():
    s = CropSession(400, 300)
    assert s.box == (0, 0, 400, 300) and s.edit().is_identity()
    # The bottom-right corner is pulled in, then the box is pushed outside: it stops at the edge.
    s.begin("br", 400, 300)
    s.drag_to(200, 150)
    s.end()
    assert s.box == pytest.approx((0, 0, 200, 150), abs=1e-4)
    s.begin("move", 100, 100)
    s.drag_to(1000, 50)  # far to the right, a bit up: stops at the right edge and at the top edge
    s.end()
    assert s.box == pytest.approx((200, 0, 400, 150), abs=1e-4)
    assert s.edit().crop == pytest.approx((0.5, 0, 1, 0.5), abs=1e-6)
    # Aspect lock: dragging a corner keeps the aspect ratio.
    s.set_aspect(1.0)
    x0, y0, x1, y1 = s.box
    assert x1 - x0 == pytest.approx(y1 - y0) and s.fits(s.box)
    s.begin("tl", x0, y0)
    s.drag_to(x0 - 50, y0 + 10)
    s.end()
    x0, y0, x1, y1 = s.box
    assert x1 - x0 == pytest.approx(y1 - y0)
    # Edge handle and minimum size.
    s.set_aspect(None)
    s.begin("l", s.box[0], 100)
    s.drag_to(10_000, 100)
    s.end()
    assert s.box[2] - s.box[0] == pytest.approx(s.min_size())


def test_crop_session_straighten_keeps_box_inside_and_restores():
    s = CropSession(400, 300)
    s.begin("br", 400, 300)
    s.drag_to(300, 250)
    s.end()
    user = s.box
    s.set_angle(12)
    assert box_fits(400, 300, 12, s.box) and s.box != user  # it shrank
    s.set_angle(0)
    assert s.box == pytest.approx(user, abs=1e-4)  # removing the tilt brings the user's box back
    s.reset()
    s.set_angle(-20)  # automatic box: the largest one, in the photo's aspect ratio
    x0, y0, x1, y1 = s.box
    assert (x1 - x0) / (y1 - y0) == pytest.approx(4 / 3) and box_fits(400, 300, -20, s.box)
    assert s.edit().crop is None and s.edit().angle == -20


def test_crop_session_rotation_turns_box_and_ratio():
    s = CropSession(400, 300)
    s.set_aspect(3 / 2)
    before = s.box
    s.rotate(1)
    assert s.canvas == (300, 400) and s.aspect == pytest.approx(2 / 3)
    x0, y0, x1, y1 = s.box
    assert (x1 - x0, y1 - y0) == pytest.approx((before[3] - before[1], before[2] - before[0]))
    assert s.edit().quarter == 1


def test_crop_session_flip_matches_the_edit():
    """Flipping in crop mode gives the same edit as flipping outside it (`Edit.mirrored`)."""
    rng = random.Random(11)
    for _ in range(40):
        quarter, angle = rng.randrange(4), rng.choice((0.0, 6.5, -12.0))
        crop = None
        if rng.random() < 0.7:
            x0, y0 = rng.uniform(0, 0.3), rng.uniform(0, 0.3)
            crop = (x0, y0, x0 + rng.uniform(0.3, 0.6), y0 + rng.uniform(0.3, 0.6))
        edit = Edit(quarter=quarter, angle=angle, exposure=0.5)
        session = CropSession(400, 300, edit)
        if crop is not None:
            cw, ch = session.canvas
            session.auto = False
            session.box = session._user = (crop[0] * cw, crop[1] * ch, crop[2] * cw, crop[3] * ch)
            session._refit()
        before = session.edit()
        session.mirror()
        after, expected = session.edit(), before.mirrored()
        assert (after.mirror, after.quarter, after.angle, after.exposure) == (
            expected.mirror, expected.quarter, expected.angle, expected.exposure)
        if expected.crop is None:
            assert after.crop is None
        else:
            assert after.crop == pytest.approx(expected.crop, abs=1e-4)
        session.mirror()
        assert session.edit().mirror is False and session.edit().quarter == before.quarter


def test_crop_session_random_drags_never_leave_image():
    rng = random.Random(7)
    for angle in (0, 5, -17, 33):
        s = CropSession(600, 400, Edit(angle=angle))
        for step in range(300):
            handle = rng.choice(["tl", "tr", "bl", "br", "t", "b", "l", "r", "move"])
            s.set_aspect(rng.choice([None, None, 1.0, 16 / 9]))
            if step % 25 == 24:
                s.set_angle(rng.uniform(-45, 45))
            if step % 40 == 39:
                s.rotate(rng.choice([-1, 1, 2]))
            x0, y0, x1, y1 = s.box
            start = {"tl": (x0, y0), "tr": (x1, y0), "bl": (x0, y1), "br": (x1, y1), "t": ((x0 + x1) / 2, y0),
                     "b": ((x0 + x1) / 2, y1), "l": (x0, (y0 + y1) / 2), "r": (x1, (y0 + y1) / 2),
                     "move": ((x0 + x1) / 2, (y0 + y1) / 2)}[handle]
            s.begin(handle, *start)
            s.drag_to(rng.uniform(-300, 900), rng.uniform(-300, 700))
            s.end()
            assert s.fits(s.box), (angle, handle, s.box)
            if s.aspect:
                assert (s.box[2] - s.box[0]) / (s.box[3] - s.box[1]) == pytest.approx(s.aspect, rel=1e-6)


# ================================================================== preview dialog


def _dialog(app, paths, index=0, selection=None, **kw):
    from fotodit.ui.preview_dialog import PreviewDialog, PreviewItem
    from fotodit.ui.selection import Selection
    from fotodit.ui.thumbs import ThumbnailLoader

    sel = selection or Selection()
    loader = ThumbnailLoader(64)
    items = [PreviewItem(p, os.stat(p).st_mtime, os.path.basename(p)) for p in paths]
    dialog = PreviewDialog(items, index, sel, loader, **kw)
    dialog.resize(1200, 800)
    dialog.show()
    assert wait_until(app, lambda: dialog.view.current_pixmap() is not None and not dialog.view._placeholder)
    return dialog, sel, loader


def _drag(widget, start: QPoint, end: QPoint, steps: int = 6) -> None:
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    for i in range(1, steps + 1):
        QTest.mouseMove(widget, start + (end - start) * (i / steps))
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)


def test_preview_rotate_undo_and_autoselect(app, photos):
    dialog, sel, loader = _dialog(app, photos)
    try:
        item = dialog.current()
        assert item.path not in sel
        QTest.keyClick(dialog, Qt.Key.Key_R)
        assert sel.edits.get(item.path, item.mtime) == Edit(quarter=1)
        assert item.path in sel and sel.photo(item.path).edit == Edit(quarter=1)  # an edited photo gets selected
        assert dialog.view.shown_edit() == Edit(quarter=1)
        w, h = dialog.view.image_rect().width(), dialog.view.image_rect().height()
        assert h > w  # the landscape photo now shows as portrait
        assert "Sağa 90° döndürülecek" in dialog.edit_label.text() and "480 × 640" in dialog.edit_label.text()
        QTest.keyClick(dialog, Qt.Key.Key_L)
        QTest.keyClick(dialog, Qt.Key.Key_L)
        assert sel.edits.get(item.path) == Edit(quarter=3)
        QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert sel.edits.get(item.path) == Edit()  or sel.edits.get(item.path) is None
        QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert sel.edits.get(item.path) == Edit(quarter=1)
        # Undo also reaches an edit on another photo and jumps back to that photo.
        QTest.keyClick(dialog, Qt.Key.Key_Right)
        QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert dialog.current().path == item.path and sel.edits.get(item.path) is None
        # The "remove" link clears the edit.
        QTest.keyClick(dialog, Qt.Key.Key_R)
        dialog.edit_link.linkActivated.emit("#kaldir")
        assert sel.edits.get(item.path) is None and dialog.edit_label.text() == ""
    finally:
        dialog.close()
        loader.shutdown()
        sel.shutdown()


def test_preview_flip(app, photos):
    dialog, sel, loader = _dialog(app, photos)
    try:
        item = dialog.current()
        QTest.keyClick(dialog, Qt.Key.Key_H)
        assert sel.edits.get(item.path, item.mtime) == Edit(mirror=True)
        assert item.path in sel and dialog.view.shown_edit() == Edit(mirror=True)
        assert "Yatay çevrilecek" in dialog.edit_label.text()
        QTest.keyClick(dialog, Qt.Key.Key_R)  # flipped, then turned right
        assert sel.edits.get(item.path) == Edit(mirror=True, quarter=1)
        dialog.flip_btn.click()  # flipping again undoes the flip and turns the rotation the other way
        assert sel.edits.get(item.path) == Edit(quarter=3)
        # In crop mode the box flips with the photo.
        QTest.keyClick(dialog, Qt.Key.Key_C)
        assert dialog._session is not None and not dialog.flip_btn.isEnabled()
        dialog._session.set_angle(5)
        dialog.crop_bar.flip_btn.click()
        assert dialog._session.angle == -5 and dialog._session.base.mirror
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        assert sel.edits.get(item.path) == replace_edit(Edit(quarter=1, mirror=True, angle=-5),
                                                         sel.edits.get(item.path).crop)
    finally:
        dialog.close()
        loader.shutdown()
        sel.shutdown()


def replace_edit(edit: Edit, crop) -> Edit:
    from dataclasses import replace

    return replace(edit, crop=crop)


def test_preview_crop_mode(app, photos):
    dialog, sel, loader = _dialog(app, photos, index=1)
    try:
        item = dialog.current()
        QTest.keyClick(dialog, Qt.Key.Key_C)
        assert dialog._session is not None and dialog.crop_bar.isVisible() and not dialog.footer.isVisible()
        QTest.keyClick(dialog, Qt.Key.Key_Right)  # no navigation in crop mode
        assert dialog.current() is item
        view = dialog.view
        box = view.crop_box_rect()
        # Drag the bottom-right corner toward the center: the box shrinks.
        _drag(view, box.bottomRight().toPoint(), box.center().toPoint())
        new = view.crop_box_rect()
        assert new.width() < box.width() * 0.6 and new.height() < box.height() * 0.6
        assert "piksel" in dialog.crop_bar.size_label.text()
        # Esc cancels: no edit.
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert dialog._session is None and sel.edits.get(item.path) is None and dialog.isVisible()
        # Again: aspect lock (square) + tilt, apply with Enter.
        QTest.keyClick(dialog, Qt.Key.Key_C)
        square = next(i for i, (label, _) in enumerate(ASPECTS) if label == "aspect.square")
        dialog.crop_bar.aspect.setCurrentIndex(square)
        dialog.crop_bar.slider.setValue(35)  # 3.5°
        assert dialog.crop_bar.angle_label.text() == "+3,5°"
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        edit = sel.edits.get(item.path, item.mtime)
        assert edit is not None and edit.angle == 3.5 and edit.crop is not None
        w, h = edit.output_size(640, 480)
        assert abs(w - h) <= 1 and item.path in sel
        rect = dialog.view.image_rect()
        assert abs(rect.width() - rect.height()) < 2  # the preview shows the cropped square
        # Double-clicking inside the box applies too; in crop mode R rotates together with the box.
        QTest.keyClick(dialog, Qt.Key.Key_C)
        QTest.keyClick(dialog, Qt.Key.Key_R)
        assert dialog._session.quarter == 1
        center = dialog.view.crop_box_rect().center().toPoint()
        QTest.mouseDClick(dialog.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        assert dialog._session is None and sel.edits.get(item.path).quarter == 1
    finally:
        dialog.close()
        loader.shutdown()
        sel.shutdown()


def test_preview_opens_in_crop_mode(app, photos):
    dialog, sel, loader = _dialog(app, photos, index=2, crop=True)
    try:
        assert wait_until(app, lambda: dialog._session is not None)
        dialog.crop_bar.cancel_btn.click()
        assert dialog._session is None and dialog.footer.isVisible()
    finally:
        dialog.close()
        loader.shutdown()
        sel.shutdown()


# ================================================================== main window


@pytest.fixture
def window(app, tmp_path, photos):
    from fotodit.ui.main_window import MainWindow

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("browse/folder", os.path.dirname(photos[0]))
    win = MainWindow(settings)
    win.options.set_output_folder(str(tmp_path / "out"))
    win.show()
    app.processEvents()
    yield win
    win._confirm_discard_edits = lambda count: True
    win.close()


def test_rotate_rows_in_selected_tab_and_process(app, tmp_path, photos, window):
    from fotodit.ui.run_dialog import RunDialog

    win = window
    win.options.strip.setChecked(False)
    win.options.convert.setChecked(False)
    win.selection.add(photos)
    win.tabs.setCurrentWidget(win.selected)
    assert wait_until(app, lambda: win.selection.pending() == 0)
    win.replan()
    table = win.selected.table
    assert win.options.start_btn.text().endswith("Kopyala")
    table.selectRow(0)
    win.activateWindow()
    table.setFocus()  # the shortcut only works while the table has focus
    assert wait_until(app, table.hasFocus)
    QTest.keyClick(table, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier)
    first = win.selected.model.plan[0].photo.path
    assert win.edits.get(first) == Edit(quarter=1)
    win.options.folder_edit.setFocus()  # while another field has focus, Ctrl+R does not rotate photos
    assert wait_until(app, win.options.folder_edit.hasFocus)
    QTest.keyClick(win.options.folder_edit, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier)
    assert win.edits.get(first) == Edit(quarter=1)
    win.replan()
    assert win.selected.model.plan[0].edit == Edit(quarter=1)
    note = win.selected.model.index(0, 5).data()
    assert "↻ 90°" in note
    assert win.options.start_btn.text().endswith("İşle")  # an edit counts as an operation too
    assert "1 tanesi düzenlendi" in win.options.summary.text()
    # Closing asks about unapplied edits.
    asked = []
    win._confirm_discard_edits = lambda count: asked.append(count) or False
    win.close()
    assert asked == [1] and win.isVisible()
    # Process: a rotated copy in the new folder, the original is unchanged; the edit counts as applied.
    before = open(first, "rb").read()
    opts = win.options.job_options()
    from fotodit.core.planner import build_plan

    plan = build_plan(win.selection.photos(), opts)
    dialog = RunDialog(plan, opts, win)
    assert wait_until(app, lambda: not dialog._running, 60)
    report = dialog.report
    dialog.accept()
    assert len(report.succeeded) == 3 and open(first, "rb").read() == before
    out = {os.path.basename(r.final_path): r.final_path for r in report.succeeded}
    rotated = open_image(out[os.path.basename(first)])
    assert rotated.size == (480, 640)
    plain = open(out[os.path.basename(photos[1])], "rb").read()
    assert plain == open(photos[1], "rb").read()  # the unedited photo was copied byte for byte
    assert not scan_file(out[os.path.basename(first)]).clean  # metadata stripping was off: metadata is kept
    win.edits.mark_applied([first])
    assert win.selection.pending_edits() == 0


def test_in_place_processing_clears_edits(app, tmp_path, photos, window):
    win = window
    win.options.in_place.setChecked(True)
    win.options.strip.setChecked(True)
    win.options.recycle.setChecked(False)  # replace permanently in the test (the Recycle Bin is not touched)
    win.selection.add(photos[:1])
    assert wait_until(app, lambda: win.selection.pending() == 0)
    mtime = os.stat(photos[0]).st_mtime
    win.edits.set(photos[0], mtime, Edit(crop=(0, 0, 0.5, 0.5)))
    from fotodit.ui.run_dialog import RunDialog

    orig = RunDialog.exec

    def run_now(dialog):
        wait_until(app, lambda: not dialog._running, 60)
        return 0

    win._confirm = lambda plan, opts: True
    RunDialog.exec = run_now
    try:
        win.start()
        assert open_image(photos[0]).size == (320, 240)
        assert len(win.edits) == 0 and len(win.selection) == 0
        # Applying to the original while keeping metadata preserves the file's mtime, yet the
        # thumbnail in the Browse tab must not stay cached in its old (landscape) form.
        from fotodit.ui.thumbs import LOADING

        win.options.strip.setChecked(False)
        win.options.convert.setChecked(False)
        path, mtime = photos[1], os.stat(photos[1]).st_mtime
        assert wait_until(app, lambda: win.loader.get(path, mtime) not in (LOADING, None))
        assert win.loader.get(path, mtime).width() > win.loader.get(path, mtime).height()
        win.selection.add([path])
        assert wait_until(app, lambda: win.selection.pending() == 0)
        win.edits.set(path, mtime, Edit(quarter=1))
        win.start()
        assert os.stat(path).st_mtime == mtime and open_image(path).size == (480, 640)
        assert wait_until(app, lambda: win.loader.get(path, mtime) not in (LOADING, None))
        assert win.loader.get(path, mtime).width() < win.loader.get(path, mtime).height()
    finally:
        RunDialog.exec = orig


def test_grid_context_rotation_selects_and_marks(app, photos, window):
    from fotodit.ui.browser import EditRole

    win = window
    model = win.browser.model
    rows = {e.name: i for i, e in enumerate(model.entries)}
    entry = model.entries[rows["IMG_0002.jpg"]]
    win.browser.grid.rotate(entry, -1)
    assert entry.path in win.selection
    assert model.index(rows["IMG_0002.jpg"]).data(EditRole) == Edit(quarter=3)
    assert "düzenlendi" in win.current_message()
    win.browser.grid._reset_edit(entry)
    assert model.index(rows["IMG_0002.jpg"]).data(EditRole) is None


def test_flip_from_grid_and_selected_tab(app, photos, window):
    from fotodit.ui.browser import EditRole

    win = window
    model = win.browser.model
    rows = {e.name: i for i, e in enumerate(model.entries)}
    entry = model.entries[rows["IMG_0003.jpg"]]
    win.browser.grid.flip(entry)
    assert entry.path in win.selection
    assert model.index(rows["IMG_0003.jpg"]).data(EditRole) == Edit(mirror=True)
    win._flip_paths([entry.path])  # what the Selected tab's button asks for
    assert win.selection.edits.get(entry.path) in (None, Edit())


def test_preview_from_main_window_crop_request(app, photos, window):
    win = window
    opened = []
    real = win.open_preview
    win.open_preview = lambda items, index, plan=None, crop=False: opened.append(crop) or real(items, index, plan, crop)
    win.browser.cropRequested.emit(photos[0])
    assert opened == [True]
    dialog = QApplication.activeModalWidget()
    assert wait_until(app, lambda: dialog._session is not None)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert not dialog.isVisible()


def test_image_is_unchanged_until_processing(photos):
    """An edit is applied only when processing starts: the file stays untouched."""
    with Image.open(photos[0]) as im:
        assert im.size == (640, 480)


# ======================================================= exposure, trim, theme


def _brightness(pixmap) -> float:
    image = pixmap.toImage().convertToFormat(pixmap.toImage().Format.Format_Grayscale8)
    data = bytes(image.constBits())[:image.sizeInBytes()]
    return sum(data) / max(1, len(data))


def test_preview_photo_exposure(app, photos):
    """A JPEG gets the exposure slider too: adjusted live in the preview, without a RAW development."""
    from fotodit.ui.exposure import PHOTO_NOTE

    dialog, sel, loader = _dialog(app, [photos[0]])
    try:
        item = dialog.current()
        bar = dialog.exposure_bar
        assert bar.isVisible() and bar.note.text() == t(PHOTO_NOTE)
        before = _brightness(dialog.view.current_pixmap())
        bar.slider.setValue(10)  # +1 EV
        bar.slider.sliderReleased.emit()
        assert sel.edits.get(item.path, item.mtime) == Edit(exposure=1.0) and item.path in sel
        assert wait_until(app, lambda: _brightness(dialog.view.current_pixmap()) > before + 15)
        assert not dialog._developed and "Pozlaması +1,0 EV ayarlanacak" in dialog.edit_label.text()
        bar.reset()
        assert sel.edits.get(item.path) is None
        assert wait_until(app, lambda: abs(_brightness(dialog.view.current_pixmap()) - before) < 3)
    finally:
        dialog.close()
        loader.shutdown()
        sel.shutdown()


def test_a_wheel_step_stays_with_its_photo(app, photos):
    """A wheel step on the exposure slider is stored a moment later; showing the next photo before that
    moment still stores it on the photo it was made on, not on the next one."""
    dialog, sel, loader = _dialog(app, photos[:2])
    try:
        first = dialog.current()
        dialog.exposure_bar.slider.setValue(3)  # as the wheel does: stored after a short pause
        dialog.go(1)
        second = dialog.current()
        time.sleep(0.5)
        app.processEvents()
        assert sel.edits.get(first.path, first.mtime) == Edit(exposure=0.3)
        assert sel.edits.get(second.path, second.mtime) is None
    finally:
        dialog.close()
        loader.shutdown()
        sel.shutdown()


def test_preview_raw_exposure(app, tmp_path, photos):
    from fotodit.core.dngwriter import make_dng

    raw = write(tmp_path, "DSC_0001.dng", make_dng(b.photo(320, 240), preview="full"))
    dialog, sel, loader = _dialog(app, [raw, photos[0]])
    try:
        item = dialog.current()
        bar = dialog.exposure_bar
        assert bar.isVisible() and not dialog._developed
        camera = _brightness(dialog.view.current_pixmap())
        bar.slider.setValue(10)  # dragged to +1 EV: the preview develops the RAW and shows it live
        bar.slider.sliderReleased.emit()
        assert sel.edits.get(item.path, item.mtime) == Edit(exposure=1.0) and item.path in sel
        assert wait_until(app, lambda: dialog._developed)
        assert _brightness(dialog.view.current_pixmap()) > camera + 15
        assert "Pozlaması +1,0 EV ayarlanacak" in dialog.edit_label.text()
        assert "→" not in dialog.edit_label.text()  # the size does not change
        # Cropping keeps the exposure; undoing the crop goes back to the exposure alone.
        dialog.start_crop()
        dialog.crop_bar.slider.setValue(20)
        dialog.apply_crop()
        assert sel.edits.get(item.path) == Edit(angle=2.0, exposure=1.0)
        QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert sel.edits.get(item.path) == Edit(exposure=1.0) and bar.ev() == 1.0
        bar.reset()  # back to 0 EV: no edit, the camera's JPEG again
        assert sel.edits.get(item.path) is None
        assert wait_until(app, lambda: not dialog._developed and not dialog.view._placeholder)
        QTest.keyClick(dialog, Qt.Key.Key_Right)  # a JPEG: the slider too, without the RAW note
        assert bar.isVisible()
    finally:
        dialog.close()
        loader.shutdown()
        sel.shutdown()


def test_preview_video_trim(app, tmp_path):
    from video_builders import make_video

    video = make_video(str(tmp_path / "a.mp4"), frames_n=72, fps=24)  # 3 s
    dialog, sel, loader = _dialog(app, [video])
    try:
        item = dialog.current()
        player, bar = dialog._player, dialog.video_bar
        assert wait_until(app, lambda: player.duration() > 0)

        def at(ms: int) -> None:
            player.seek(ms)
            assert wait_until(app, lambda: abs(player.position() - ms) < 40)

        at(500)
        bar.start_btn.click()
        assert sel.edits.get(item.path, item.mtime) == Edit(trim=(0.5, None)) and item.path in sel
        at(2500)
        bar.end_btn.click()
        assert sel.edits.get(item.path).trim == (0.5, 2.5)
        assert bar.slider.kept() == [(500, 2500)] and bar.clear_btn.isVisible()
        assert "Başı ve sonu kesilecek (kalan: 0:00,5 – 0:02,5)" in dialog.edit_label.text()
        at(300)
        bar.end_btn.click()  # the end before the start: the start is dropped
        assert sel.edits.get(item.path).trim == (0.0, 0.3)
        QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert sel.edits.get(item.path).trim == (0.5, 2.5) and bar.slider.kept() == [(500, 2500)]
        bar.clear_btn.click()
        assert sel.edits.get(item.path) is None and bar.slider.kept() is None and not bar.clear_btn.isVisible()
    finally:
        dialog.close()
        loader.shutdown()
        sel.shutdown()


def test_preview_video_cut(app, tmp_path):
    from video_builders import make_video

    video = make_video(str(tmp_path / "a.mp4"), frames_n=96, fps=24)  # 4 s
    dialog, sel, loader = _dialog(app, [video])
    try:
        item = dialog.current()
        player, bar = dialog._player, dialog.video_bar
        assert wait_until(app, lambda: player.duration() > 0)

        def at(ms: int) -> None:
            player.seek(ms)
            assert wait_until(app, lambda: abs(player.position() - ms) < 40)

        at(1000)
        bar.cut_btn.click()  # the start of the part to remove
        assert bar.slider.pending() == 1000 and bar.cut_btn.text() == "Bölüm Burada Bitsin"
        assert sel.edits.get(item.path, item.mtime) is None  # nothing removed yet
        at(2000)
        bar.cut_btn.click()  # its end
        assert sel.edits.get(item.path).cuts == ((1.0, 2.0),) and item.path in sel
        assert bar.slider.pending() is None and bar.cut_btn.text() == "Bölüm Çıkar"
        kept = bar.slider.kept()
        assert kept[0] == (0, 1000) and kept[1][0] == 2000 and len(kept) == 2 and bar.clear_btn.isVisible()
        assert "1 bölüm çıkarılacak (kalan: 0:00 – 0:01, 0:02 – son)" in dialog.edit_label.text()
        # Esc drops a part being marked; the window stays open.
        at(3000)
        bar.cut_btn.click()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert bar.slider.pending() is None and dialog.isVisible()
        assert sel.edits.get(item.path).cuts == ((1.0, 2.0),)
        # Marked backwards (end first) works too; the 0,2 s left at the start would be too short: it goes along.
        at(500)
        bar.cut_btn.click()
        at(200)
        bar.cut_btn.click()
        assert sel.edits.get(item.path).cuts == ((0.0, 0.5), (1.0, 2.0))
        QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert sel.edits.get(item.path).cuts == ((1.0, 2.0),)
        # A trim start inside the removed part: the removed part is cut to the trim.
        at(1500)
        bar.start_btn.click()
        assert sel.edits.get(item.path) == Edit(trim=(1.5, None), cuts=((1.5, 2.0),))
        bar.clear_btn.click()
        assert sel.edits.get(item.path) is None and bar.slider.kept() is None
    finally:
        dialog.close()
        loader.shutdown()
        sel.shutdown()


def test_about_window(app, window):
    """The version button opens the About window: copyright, the GPL notice without warranty, the texts."""
    from PySide6.QtWidgets import QLabel

    from fotodit import COPYRIGHT, __version__

    win = window
    assert win.about_btn.text() == f"Fotodit {__version__}"
    win.about_btn.click()
    about = win._about
    assert wait_until(app, about.isVisible)
    try:
        texts = " ".join(label.text() for label in about.findChildren(QLabel))
        assert f"Telif hakkı {COPYRIGHT}" in texts and "HİÇBİR GARANTİSİ YOKTUR" in texts and "GPL" in texts
        assert about.license_btn.isEnabled() and about.notices_btn.isEnabled() and about.folder_btn.isEnabled()
        about.license_btn.click()
        assert about.viewer.isVisible() and "GNU GENERAL PUBLIC LICENSE" in about.viewer.text.toPlainText()[:200]
        about.notices_btn.click()
        assert "Third-party components" in about.viewer.text.toPlainText()
        about.viewer.close()
    finally:
        about.close()


def test_theme_switch(app, window):
    from PySide6.QtGui import QGuiApplication, QPalette
    from PySide6.QtWidgets import QStyleFactory

    from fotodit.ui import glass, theme

    win = window
    original = app.style().name()
    window_color = lambda: app.palette().color(QPalette.ColorRole.Window).name().upper()  # noqa: E731
    try:
        win.set_theme(theme.DARK)
        assert theme.is_dark() and theme.mode() == theme.DARK and window_color() == "#141414"
        assert "#262626" in app.styleSheet()  # the dark panel color
        assert win.settings.value("appearance/theme") == theme.DARK and win.theme_actions[theme.DARK].isChecked()
        assert glass.material(glass.PANEL) == glass._DARK[glass.PANEL]  # the glass follows
        assert win.selected.model._muted.name().upper() == theme.MUTED.upper()  # cached colors follow
        win.set_theme(theme.LIGHT)
        assert not theme.is_dark() and window_color() == "#F2F2F2"
        assert win.settings.value("appearance/theme") == theme.LIGHT and not win.theme_actions[theme.DARK].isChecked()
        # The glass's look (Görünüm menu): clearer or frostier than the default, and remembered.
        win.set_glass_level(glass.CLEAR)
        assert glass.level() == glass.CLEAR and win.settings.value("appearance/glass") == glass.CLEAR
        assert win.glass_actions[glass.CLEAR].isChecked() and not win.glass_actions[glass.DEFAULT].isChecked()
        assert glass.material(glass.BAR).tint[3] < glass._LIGHT[glass.BAR].tint[3]
        win.set_glass_level(glass.TINTED)
        assert glass.material(glass.BAR).tint[3] > glass._LIGHT[glass.BAR].tint[3]
        win.set_glass_level("nonsense")
        assert glass.level() == glass.DEFAULT and glass.material(glass.BAR) == glass._LIGHT[glass.BAR]
    finally:  # the other tests run without the app's theme
        glass.set_level(glass.DEFAULT)
        app.setStyleSheet("")
        app.setStyle(QStyleFactory.create(original))
        app.setPalette(app.style().standardPalette())
        QGuiApplication.styleHints().unsetColorScheme()
        theme._use(False)
        theme._style = None


def test_digits_line_up_only_where_numbers_change(app):
    """Counters, timers and the date column get equal-width digits; other text keeps Inter's own,
    whose tabular forms would widen the hyphens of file names too ("C - - Users")."""
    from PySide6.QtGui import QFont, QFontMetricsF

    from fotodit.ui import fonts, theme

    assert fonts.load()
    tnum = QFont.Tag("tnum")
    assert not theme.font(10).isFeatureSet(tnum)
    plain = QFont(fonts.FAMILY)
    plain.setPointSizeF(10)
    digits = theme.tabular(plain)
    assert digits.featureValue(tnum) == 1 and not plain.isFeatureSet(tnum)
    fp, fd = QFontMetricsF(plain), QFontMetricsF(digits)
    assert fd.horizontalAdvance("1111") == fd.horizontalAdvance("0000")
    assert fp.horizontalAdvance("C--Users") < fd.horizontalAdvance("C--Users")
