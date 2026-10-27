"""The large preview after the review: info of photos prepared in advance, the video bar in narrow
windows, the live exposure preview, the keys after a click on the info panel, a paused video's frame,
narrow windows, controls that stepped aside, quick clicks on the arrows, RAW developments, the info
panel's cache and the crop angle's decimal mark."""

from __future__ import annotations

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import builders as b  # noqa: E402
from helpers import wait_until, write_in  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fotodit import i18n  # noqa: E402
from fotodit.core.edits import Edit  # noqa: E402
from fotodit.i18n import t  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def turkish():
    """These tests read Turkish texts and widths; the language is put back afterwards."""
    before = i18n.language()
    i18n.set_language("tr")
    yield
    i18n.set_language(before)


def _photos(folder, n: int, size=(640, 480), everything: bool = True) -> list[str]:
    return [write_in(folder, f"IMG_{i}.jpg", b.jpeg(b.photo(*size), everything=everything)) for i in range(n)]


def _video(folder, frames: int = 96) -> str:
    from video_builders import make_video

    return make_video(os.path.join(str(folder), "clip.mp4"), frames_n=frames, fps=24)


def _open(app, paths, selection=None, size=(1200, 800)):
    from fotodit.ui.preview_dialog import PreviewDialog, PreviewItem
    from fotodit.ui.selection import Selection
    from fotodit.ui.thumbs import ThumbnailLoader

    sel = selection if selection is not None else Selection()
    loader = ThumbnailLoader(64)
    items = [PreviewItem(p, os.stat(p).st_mtime, os.path.basename(p)) for p in paths]
    dialog = PreviewDialog(items, 0, sel, loader)
    dialog.show()
    dialog.setWindowState(Qt.WindowState.WindowNoState)  # (it opens maximized)
    dialog.resize(*size)
    assert wait_until(app, lambda: dialog.view.has_image() and not dialog.view._placeholder)
    return dialog, sel, loader


def _close(dialog, sel, loader) -> None:
    dialog.done(0)
    loader.shutdown()
    sel.shutdown()


def _run(app, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def _play_and_pause(app, dialog) -> None:
    dialog._player.play()
    assert wait_until(app, dialog.view.has_frame, 10)
    dialog._player.pause()
    _run(app, 0.2)


# ------------------------------------------------------------------ the photo's info


def test_info_of_a_photo_prepared_in_advance(app, tmp_path, turkish):
    """Opened from Browse (nothing selected): the next photo is prepared in advance, and its details
    and location warning still show when it is reached."""
    dialog, sel, loader = _open(app, _photos(tmp_path, 3))
    try:
        nxt = dialog.items[1]
        key = dialog.loader.key(nxt.path, nxt.mtime, dialog._box_for(nxt))
        assert wait_until(app, lambda: key in dialog.loader._cache)
        QTest.keyClick(dialog, Qt.Key.Key_Right)
        assert wait_until(app, lambda: dialog.gps.text() == t("preview.has_gps"), 5)
        assert "640 × 480" in dialog.details.text()
    finally:
        _close(dialog, sel, loader)


def test_info_stays_when_the_photo_leaves_the_selection(app, tmp_path, turkish):
    from fotodit.ui.selection import Selection

    sel = Selection()
    sel.add(_photos(tmp_path, 2))
    assert wait_until(app, lambda: all(sel.info(p) is not None for p in sel.paths()))
    dialog, sel, loader = _open(app, sel.paths(), selection=sel)
    try:
        assert dialog.gps.text() == t("preview.has_gps")
        QTest.keyClick(dialog, Qt.Key.Key_Delete)  # out of the selection (Del)
        assert dialog.current().path not in sel
        assert dialog.gps.text() == t("preview.has_gps") and "640 × 480" in dialog.details.text()
    finally:
        _close(dialog, sel, loader)


# ------------------------------------------------------------------ the video bar


def test_video_bar_buttons_never_overlap(app, tmp_path, turkish):
    """No button of the trimming row lies on another one, in a wide and a narrow window, with and
    without a trim ("Kesmeyi Kaldır" showing). A name hidden in a narrow bar stays the button's name."""
    dialog, sel, loader = _open(app, [_video(tmp_path, 48)], size=(1920, 900))
    try:
        bar, item = dialog.video_bar, dialog.current()

        def check() -> None:
            app.processEvents()
            row = [w for w in (bar.start_btn, bar.end_btn, bar.cut_btn, bar.clear_btn, bar.audio_btn)
                   if w.isVisible()]
            for i, w in enumerate(row):
                assert bar.rect().contains(w.geometry()), (bar.width(), w.text())
                assert w.width() >= w.minimumSizeHint().width(), (bar.width(), w.text())  # (not squeezed)
                for other in row[i + 1:]:
                    assert not w.geometry().intersects(other.geometry()), (bar.width(), w.text(), other.text())

        for width in (1920, 760):
            dialog.resize(width, 900)
            check()
        assert bar.cut_btn.text() == t("player.cut") and bar.cut_btn.toolTip()
        sel.edits.set(item.path, item.mtime, Edit(trim=(0.5, None)))
        assert bar.clear_btn.isVisible()
        for width in (1920, 1200, 760):
            dialog.resize(width, 900)
            check()
    finally:
        _close(dialog, sel, loader)


def test_video_controls_follow_a_resize_while_stepped_aside(app, tmp_path):
    """A playing video's controls step aside while the mouse rests. The picture does not grow under
    them meanwhile, and a resize then (full screen by a double-click) moves them along."""
    from fotodit.ui.preview_dialog import MARGIN

    dialog, sel, loader = _open(app, [_video(tmp_path, 240)], size=(1400, 900))
    try:
        view = dialog.view
        dialog._player.play()
        assert wait_until(app, view.has_frame, 10)
        safe = view._safe
        dialog._set_chrome_visible(False)
        assert not dialog.video_bar.isVisible()
        dialog._update_bar()  # (e.g. the Selected tab's plan is rebuilt meanwhile)
        assert view._safe == safe
        dialog.resize(1600, 1000)
        app.processEvents()
        dialog._set_chrome_visible(True)
        bar = dialog.video_bar.geometry()
        assert dialog.close_bar.geometry().right() == dialog.width() - MARGIN - 1
        dialog._relayout()
        assert dialog.video_bar.geometry() == bar and not bar.intersects(view._safe)
    finally:
        _close(dialog, sel, loader)


def test_paused_video_frame_stays_on_zoom_and_crop(app, tmp_path):
    """The frame a video was paused at stays on screen at 100 % and after a crop (it is not replaced
    by the video's first frame)."""
    video = _video(tmp_path)
    dialog, sel, loader = _open(app, [video])
    try:
        view = dialog.view
        _play_and_pause(app, dialog)
        QTest.keyClick(dialog, Qt.Key.Key_Z)
        assert view.is_full() and view.has_frame()
        _run(app, 0.5)  # (the full-size first frame is prepared meanwhile)
        assert view.has_frame()
        QTest.keyClick(dialog, Qt.Key.Key_Z)
        dialog.start_crop()
        s = dialog._session
        s.box = s._user = (s.box[0] + 20, s.box[1] + 20, s.box[2] - 20, s.box[3] - 20)
        s.auto = False
        dialog.apply_crop()  # (prepared larger for the cropped region)
        assert sel.edits.get(video).crop is not None and view.has_frame()
        _run(app, 0.5)
        assert view.has_frame()
    finally:
        _close(dialog, sel, loader)


# ------------------------------------------------------------------ exposure


def test_exposure_drag_works_on_a_reduced_copy(app, tmp_path, monkeypatch):
    """While the exposure slider is dragged, each position is shown from a reduced copy of the photo
    (quick on large screens); the sharp photo is back when it is released. A rotation afterwards
    reuses the shown result instead of redoing the exposure."""
    from fotodit.ui import exposure, preview_dialog

    monkeypatch.setattr(exposure, "DRAFT_SIDE", 320)
    calls = []
    real = preview_dialog.exposed_image
    monkeypatch.setattr(preview_dialog, "exposed_image", lambda image, ev: calls.append(ev) or real(image, ev))
    path = _photos(tmp_path, 1, everything=False)[0]
    dialog, sel, loader = _open(app, [path])
    try:
        slider = dialog.exposure_bar.slider
        y = slider.height() // 2
        QTest.mousePress(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(slider.width() * 3 // 4, y))
        QTest.mouseMove(slider, QPoint(slider.width() * 4 // 5, y))
        assert wait_until(app, lambda: any(calls), 5)  # (an exposure was shown)
        assert dialog.view.current_pixmap().width() <= 320  # the reduced copy
        QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           QPoint(slider.width() * 4 // 5, y))
        assert wait_until(app, lambda: dialog.view.current_pixmap().width() == 640, 5)
        assert sel.edits.get(path).exposure > 0
        _run(app, 0.1)
        calls.clear()
        QTest.keyClick(dialog, Qt.Key.Key_R)
        app.processEvents()
        assert sel.edits.get(path).quarter == 1 and not calls
    finally:
        _close(dialog, sel, loader)


def test_raw_developments_newest_first_and_stale_ones_dropped(app, tmp_path, monkeypatch):
    """RAW developments for the live exposure run newest first, and those not started when the user
    moves on are dropped (they held up the photo on screen)."""
    from fotodit.ui import exposure

    gate = threading.Event()
    order = []

    def develop(path, box):
        order.append(path)
        if path == "a.dng":
            gate.wait(10)
        return None  # (a development that failed: enough here)

    monkeypatch.setattr(exposure, "linear_preview", develop)
    dev = exposure.RawDeveloper(100)
    try:
        dev.get("a.dng", 1.0)
        assert wait_until(app, lambda: order == ["a.dng"], 5)  # it runs; the only thread is busy
        dev.get("b.dng", 1.0)
        dev.get("c.dng", 1.0)
        dev.clear_queue()  # the user moved on
        dev.get("a.dng", 1.0)  # (still running: not started twice)
        dev.get("d.dng", 1.0)
        dev.get("e.dng", 1.0)
        gate.set()
        assert wait_until(app, lambda: dev.failed("d.dng", 1.0), 5)
        _run(app, 0.2)
        assert order == ["a.dng", "e.dng", "d.dng"]
    finally:
        gate.set()
        dev.shutdown()
    # Moving to another photo drops the developments that have not started.
    dialog, sel, loader = _open(app, _photos(tmp_path, 2, size=(160, 120), everything=False))
    try:
        dropped = []
        monkeypatch.setattr(dialog.developer, "clear_queue", lambda: dropped.append(True))
        QTest.keyClick(dialog, Qt.Key.Key_Right)
        assert dropped
    finally:
        _close(dialog, sel, loader)


# ------------------------------------------------------------------ keyboard and mouse


def test_arrow_keys_after_clicking_the_info_text(app, tmp_path):
    """A click on the info panel's text (to select it) does not take the arrow keys away from
    moving between photos."""
    dialog, sel, loader = _open(app, _photos(tmp_path, 3, size=(320, 240)))
    try:
        dialog.activateWindow()
        assert wait_until(app, lambda: QApplication.activeWindow() is dialog)
        QTest.keyClick(dialog, Qt.Key.Key_I)
        assert wait_until(app, lambda: dialog.info_card.isVisible())
        body = dialog.info_panel.body
        QTest.mouseClick(body, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(8, 8))
        assert QApplication.focusWidget() is body  # the text keeps its focus (for selecting and copying)
        QTest.keyClick(body, Qt.Key.Key_Right)  # where the key press goes
        assert dialog._index == 1
        QTest.keyClick(body, Qt.Key.Key_PageDown)
        assert dialog._index == 2
        QTest.keyClick(body, Qt.Key.Key_Left)
        assert dialog._index == 1
    finally:
        _close(dialog, sel, loader)


def test_quick_second_click_on_an_arrow_counts(app, tmp_path):
    """Two quick clicks on the edge arrow move two photos (the second click arrives as a double-click)."""
    dialog, sel, loader = _open(app, _photos(tmp_path, 4, size=(320, 240), everything=False))
    try:
        view = dialog.view
        pos = view._arrow_center(1)
        left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
        for kind, buttons in ((QEvent.Type.MouseButtonPress, left), (QEvent.Type.MouseButtonRelease, none),
                              (QEvent.Type.MouseButtonDblClick, left), (QEvent.Type.MouseButtonRelease, none)):
            QApplication.sendEvent(view, QMouseEvent(kind, pos, view.mapToGlobal(pos), left, buttons,
                                                     Qt.KeyboardModifier.NoModifier))
        assert dialog._index == 2
    finally:
        _close(dialog, sel, loader)


# ------------------------------------------------------------------ narrow windows


def test_narrow_window_keeps_the_tools_inside(app, tmp_path, turkish):
    """In a narrow window the tool buttons show only their icons (their names become tooltips), so no
    group runs off the left edge or under the title."""
    from fotodit.ui.preview_dialog import MARGIN

    dialog, sel, loader = _open(app, _photos(tmp_path, 2, size=(320, 240), everything=False), size=(1366, 760))
    try:
        crop = dialog.crop_btn
        assert crop.shown_text() == crop.text()
        dialog.resize(683, 760)
        app.processEvents()
        tools, head = dialog.edit_tools.geometry(), dialog.head.geometry()
        assert tools.x() >= MARGIN and head.right() < tools.x()
        assert crop.shown_text() == "" and crop.toolTip()
        dialog.resize(1366, 760)
        app.processEvents()
        assert crop.shown_text() == crop.text()
    finally:
        _close(dialog, sel, loader)


def test_a_narrow_window_leaves_the_file_name_room(app, tmp_path):
    """The tools give way to the file name: the title capsule also holds the position and the chip, and
    with only 180 px kept for the whole capsule the name shrank to "I…g" (English, in the app's own
    font, in a window as narrow as the main window gets)."""
    from PySide6.QtGui import QFont

    from fotodit.ui import fonts, theme
    from fotodit.ui.preview_dialog import NAME_MIN

    old_font, before = app.font(), i18n.language()
    try:
        assert fonts.load()
        f = QFont(fonts.FAMILY)
        f.setPointSizeF(theme.BASE_SIZE)
        app.setFont(f)
        i18n.set_language("en")
        paths = [write_in(tmp_path, f"IMG_2024051{i}_143022.jpg", b.jpeg(b.photo(320, 240), everything=False))
                 for i in range(2)]
        dialog, sel, loader = _open(app, paths, size=(1000, 700))
        try:
            assert dialog.crop_btn.text() == "Crop & Straighten"
            assert dialog.title.width() >= NAME_MIN  # (the name needs more)
            assert dialog.head.geometry().right() < dialog.edit_tools.geometry().x()
        finally:
            _close(dialog, sel, loader)
    finally:
        app.setFont(old_font)
        i18n.set_language(before)


# ------------------------------------------------------------------ smaller parts


def test_info_panel_keeps_a_bounded_cache(app, tmp_path):
    """The info panel remembers the files it showed, but not without end (browsing thousands of photos)."""
    from fotodit.core.photoinfo import PhotoInfo
    from fotodit.ui.info_panel import InfoPanel

    panel = InfoPanel()
    for i in range(400):
        path = os.path.join(str(tmp_path), f"{i}.jpg")
        with open(path, "wb"):
            pass
        panel._on_done(path, PhotoInfo(), None, "")
    assert len(panel._cache) <= 250


def test_crop_angle_uses_the_language_decimal_mark():
    from fotodit.ui.crop import angle_text

    before = i18n.language()
    try:
        i18n.set_language("en")
        assert angle_text(2.5) == "+2.5°" and angle_text(-1.5) == "−1.5°"
        i18n.set_language("tr")
        assert angle_text(2.5) == "+2,5°" and angle_text(0) == "0°"
    finally:
        i18n.set_language(before)
