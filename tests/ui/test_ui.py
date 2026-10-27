"""UI logic tests (no window is shown; offscreen Qt)."""

from __future__ import annotations

import ctypes
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import builders as b  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fotodit.core.planner import (  # noqa: E402
    GROUP_NONE,
    GROUP_SOURCE,
    GROUP_YEAR,
    SORT_MANUAL,
)
from fotodit.core.runner import TEMP_PREFIX  # noqa: E402
from fotodit.i18n import t
from fotodit.ui.browser import scan_folder  # noqa: E402
from fotodit.ui.options_panel import OptionsPanel  # noqa: E402
from fotodit.ui.selected_page import SelectedPage  # noqa: E402
from fotodit.ui.selection import Selection  # noqa: E402
from fotodit.ui.thumbs import ThumbnailLoader, make_thumbnail  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def files(tmp_path, names):
    paths = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(b.jpeg(b.photo(64, 48), everything=False))
        paths.append(str(p))
    return paths


def test_selection_order_operations(app, tmp_path):
    a, bb, c, d = files(tmp_path, ["a.jpg", "b.jpg", "c.jpg", "d.jpg"])
    sel = Selection()
    assert sel.add([a, bb, c, d, a]) == 4  # duplicates are not added
    names = lambda: [os.path.basename(p) for p in sel.paths()]  # noqa: E731
    sel.move([c], -1)
    assert names() == ["a.jpg", "c.jpg", "b.jpg", "d.jpg"]
    sel.move([c, bb], 1)
    assert names() == ["a.jpg", "d.jpg", "c.jpg", "b.jpg"]
    sel.move([a], -1)
    assert names() == ["a.jpg", "d.jpg", "c.jpg", "b.jpg"]
    sel.reorder([d, a])
    assert names() == ["d.jpg", "a.jpg", "c.jpg", "b.jpg"]
    assert sel.toggle(a) is False and len(sel) == 3
    assert sel.toggle(a) is True and names()[-1] == "a.jpg"
    assert a.upper() in sel  # case-insensitive on Windows
    sel.clear()
    assert len(sel) == 0


def test_drag_drop_reorder_math(app):
    page = SelectedPage(ThumbnailLoader(64))
    emitted = []
    page.reorderRequested.connect(emitted.append)

    class E:
        def __init__(self, p):
            self.photo = type("P", (), {"path": p})()

    page.model.plan = [E(x) for x in "abcde"]
    page._rows_dropped([0, 1], 4)  # a, b → dropped on e's row: between d and e
    assert emitted[-1] == ["c", "d", "a", "b", "e"]
    page._rows_dropped([4], 0)
    assert emitted[-1] == ["e", "a", "b", "c", "d"]
    page._rows_dropped([2], 5)  # to the end
    assert emitted[-1] == ["a", "b", "d", "e", "c"]


def test_undated_files_are_explained(app, tmp_path):
    """When sorting by date, files without a capture date are explained in the Selected tab and option tooltip."""
    from fotodit.core.photoinfo import read_info
    from fotodit.core.planner import (
        SORT_DATE,
        JobOptions,
        OutputOptions,
        Photo,
        RenameOptions,
        build_plan,
        undated_count,
    )
    from fotodit.ui.selected_page import COL_DATE, UNDATED_TIP

    paths = []
    for name in ("b.jpg", "a.jpg"):
        b.photo(64, 48).save(tmp_path / name, "JPEG")  # no EXIF: no capture date
        paths.append(str(tmp_path / name))
    opts = JobOptions(rename=RenameOptions(enabled=True, sort_by=SORT_DATE),
                      output=OutputOptions(folder=str(tmp_path / "o")))
    plan = build_plan([Photo.from_path(p, i, read_info(p)) for i, p in enumerate(paths)], opts)
    assert [e.photo.name for e in plan] == ["b.jpg", "a.jpg"]  # selection order, not name order
    page = SelectedPage(ThumbnailLoader(64))
    page.set_plan(plan, False, str(tmp_path / "o"), False, "çekim tarihine göre", undated_count(plan))
    assert "Çekim tarihi olmayan 2 dosya en sonda, seçtiğiniz sırayla" in page.hint.text()
    assert page.model.data(page.model.index(0, COL_DATE), Qt.ItemDataRole.ToolTipRole) == t(UNDATED_TIP)
    page.set_plan(plan, False, str(tmp_path / "o"), False, "dosya boyutuna göre")
    assert "Elle sıralamak" in page.hint.text() and not page.hint.toolTip()
    panel = OptionsPanel()
    assert "en sona, seçtiğiniz sırayla" in panel.sort.toolTip()  # default: capture date


def _gray_mean(image) -> float:
    gray = image.convertToFormat(image.Format.Format_Grayscale8)
    data = bytes(gray.constBits())[:gray.sizeInBytes()]
    return sum(data) / max(1, len(data))


def test_thumbnails_show_exposure_and_trim(app, tmp_path):
    """A RAW with an exposure gets a thumbnail developed with it (other photos are adjusted); a trimmed
    video shows its new start."""
    import time

    from video_builders import make_video

    from fotodit.core.dngwriter import make_dng
    from fotodit.core.edits import Edit
    from fotodit.ui.thumbs import LOADING

    raw = tmp_path / "a.dng"
    raw.write_bytes(make_dng(b.photo(320, 240), preview="full"))
    plain, bright = make_thumbnail(str(raw), 64), make_thumbnail(str(raw), 64, Edit(exposure=1.5))
    assert _gray_mean(bright) > _gray_mean(plain) + 15
    jpeg = tmp_path / "b.jpg"
    jpeg.write_bytes(b.jpeg(b.photo(320, 240), everything=False))
    assert _gray_mean(make_thumbnail(str(jpeg), 64, Edit(exposure=1.0))) > _gray_mean(make_thumbnail(str(jpeg), 64)) + 15
    video = make_video(str(tmp_path / "v.mp4"), frames_n=72)
    assert make_thumbnail(video, 64) != make_thumbnail(video, 64, Edit(trim=(2.0, None)))
    loader = ThumbnailLoader(64)
    try:
        mtime = os.path.getmtime(raw)
        assert loader.get(str(raw), mtime, Edit(exposure=1.5)) is LOADING  # its own thumbnail is made
        end = time.time() + 20
        while loader.get(str(raw), mtime, Edit(exposure=1.5)) is LOADING and time.time() < end:
            app.processEvents()
            time.sleep(0.01)
        shown = loader.get(str(raw), mtime, Edit(exposure=1.5))
        assert shown is not LOADING and _gray_mean(shown.toImage()) > _gray_mean(plain) + 15
    finally:
        loader.shutdown()


def test_options_mapping_and_button_states(app):
    panel = OptionsPanel()
    o = panel.job_options()
    assert o.content.strip_metadata and o.content.convert_to_jpeg and o.content.jpeg_quality == 95
    assert not o.rename.enabled and o.output.group_by == GROUP_NONE and not o.output.in_place

    panel.set_state(0, 0, 0, 0)
    assert not panel.start_btn.isEnabled()
    # While file info is being read: no waiting if the plan does not depend on dates (only stripping/JPG) …
    panel.set_state(5, 2, 3, 1)
    assert panel.start_btn.isEnabled() and not panel.waiting_for_info()
    # … but it waits when sorting or grouping by date.
    panel.rename.setChecked(True)
    assert not panel.start_btn.isEnabled() and "Okunuyor" in panel.start_btn.text()
    panel.rename.setChecked(False)
    panel.set_state(5, 2, 0, 1, scanning=True)
    assert not panel.start_btn.isEnabled() and "Taranıyor" in panel.start_btn.text()
    panel.set_state(5, 2, 0, 1)
    assert panel.start_btn.isEnabled() and "5 Fotoğrafı İşle" in panel.start_btn.text()
    assert "2 klasörden" in panel.summary.text() and "konum" in panel.summary.text()
    # With files from different folders, keeping the folder structure is suggested; a click selects it.
    assert panel.structure_hint.isVisibleTo(panel)
    panel.keep_structure()
    assert panel.job_options().output.group_by == GROUP_SOURCE and not panel.structure_hint.isVisibleTo(panel)
    panel.group.setChecked(False)

    for w in (panel.strip, panel.convert):
        w.setChecked(False)
    panel.in_place.setChecked(True)
    assert not panel.start_btn.isEnabled()  # in place + no operation
    panel.to_folder.setChecked(True)
    assert "Kopyala" in panel.start_btn.text()

    panel.rename.setChecked(True)
    panel.sort.setCurrentIndex(panel.sort.findData(SORT_MANUAL))
    panel.prefix.setText("Summer:")
    panel.pad.setChecked(True)
    panel.group.setChecked(True)
    panel.group_mode.setCurrentIndex(panel.group_mode.findData(GROUP_YEAR))
    o = panel.job_options()
    assert o.rename.enabled and o.rename.sort_by == SORT_MANUAL and o.rename.zero_pad
    assert o.output.group_by == GROUP_YEAR
    assert not panel.order.isEnabled()  # no direction to choose for manual order
    assert "Summer01" in panel.example.text()  # ':' is invalid in file names and was removed; padded to 01


def test_options_panel_fits_its_narrowest_width(app):
    """With every operation on, each row of the panel still fits the narrowest the panel gets
    (340 px) in the app's own font: a long check box once pushed the switches out of sight."""
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QScrollArea

    from fotodit.ui import fonts, theme

    old = app.font()
    try:
        if fonts.load():
            f = QFont(fonts.FAMILY)
            f.setPointSizeF(theme.BASE_SIZE)
            app.setFont(f)
        panel = OptionsPanel()
        for w in (panel.strip, panel.convert, panel.shrink, panel.video, panel.rename, panel.group):
            w.setChecked(True)
        panel.resize(340, 900)
        panel.show()
        app.processEvents()
        scroll = panel.findChild(QScrollArea)
        assert scroll.widget().minimumSizeHint().width() <= scroll.viewport().width()
        panel.close()
    finally:
        app.setFont(old)


def test_keep_color_profile_option(app, tmp_path):
    from PySide6.QtCore import QSettings

    panel = OptionsPanel()
    assert panel.keep_profile.isEnabled() and not panel.job_options().content.keep_color_profile
    panel.keep_profile.setChecked(True)
    assert panel.job_options().content.keep_color_profile
    panel.strip.setChecked(False)
    assert not panel.keep_profile.isEnabled()  # without stripping, every profile stays anyway
    panel.strip.setChecked(True)
    assert panel.keep_profile.isEnabled()
    settings = QSettings(str(tmp_path / "options.ini"), QSettings.Format.IniFormat)
    panel.save(settings)
    restored = OptionsPanel()
    restored.load(settings)
    assert restored.keep_profile.isChecked() and restored.job_options().content.keep_color_profile


def test_scan_folder_skips_hidden_and_temp(app, tmp_path):
    files(tmp_path, ["b.jpg", "a10.jpg", "a2.jpg", f"{TEMP_PREFIX}x.jpg", "._b.jpg"])
    (tmp_path / "not.txt").write_text("x")
    (tmp_path / "Sub").mkdir()
    (tmp_path / ".thumbnails").mkdir()
    (tmp_path / "$RECYCLE.BIN").mkdir()
    hidden = tmp_path / "hidden.jpg"
    hidden.write_bytes(b.jpeg(b.photo(32, 32), everything=False))
    ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)
    entries = scan_folder(str(tmp_path))
    assert [(e.kind, e.name) for e in entries] == [("dir", "Sub"), ("photo", "a2.jpg"), ("photo", "a10.jpg"), ("photo", "b.jpg")]


def test_thumbnail_respects_orientation_and_bad_files(app, tmp_path):
    displayed = b.photo(200, 100)
    stored = displayed.transpose(b.Image.Transpose.ROTATE_90)
    p = tmp_path / "rot.jpg"
    p.write_bytes(b.jpeg(stored, orientation=6, everything=False))
    img = make_thumbnail(str(p), 80)
    assert img is not None and img.width() > img.height()
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    assert make_thumbnail(str(bad), 80) is None
