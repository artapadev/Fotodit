"""A narrow window, in English (the longer texts): the footer's buttons keep their whole words, the
rename rows fit the inspector at 1000 and 1440 px (in Turkish too), a pop-up button ends a long
choice in "…" and lists every choice in full, pop-up buttons and number boxes show their arrows, a
narrow column's title ends in "…", and scrolling the options over a box changes nothing (offscreen Qt)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRect, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QHeaderView,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QStyle,
    QStyleOptionFrame,
    QWidget,
)

from fotodit import i18n  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("tr")


@pytest.fixture(params=["en", "tr"])
def language(request):
    i18n.set_language(request.param)
    yield request.param
    i18n.set_language("tr")


def _window(tmp_path):
    from fotodit.ui.main_window import MainWindow

    folder = tmp_path / "photos"
    folder.mkdir()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("browse/folder", str(folder))
    win = MainWindow(settings)
    win.options.set_output_folder(str(tmp_path / "out"))
    return win


def _wheel(widget: QWidget) -> QWheelEvent:
    pos = QPointF(widget.width() / 2, widget.height() / 2)
    return QWheelEvent(pos, QPointF(widget.mapToGlobal(pos.toPoint())), QPoint(0, 0), QPoint(0, -120),
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)


def _marks(widget: QWidget, left: int, right: int, backdrop: str) -> int:
    """Pixels between columns `left` and `right` (from the right edge) that stand out from the box's
    most common shade, with the box drawn on `backdrop` (a translucent box shows what is behind it)."""
    grabbed = widget.grab().toImage()
    img = QImage(grabbed.size(), QImage.Format.Format_ARGB32)
    img.fill(QColor(backdrop))
    painter = QPainter(img)
    painter.drawImage(0, 0, grabbed)
    painter.end()
    shades = [img.pixelColor(x, y).lightness() for x in range(img.width() - left, img.width() - right)
              for y in range(3, img.height() - 3)]
    usual = max(set(shades), key=shades.count)
    return sum(1 for shade in shades if abs(shade - usual) > 50)


def test_the_footer_keeps_its_words_in_the_narrowest_window(app, tmp_path, english):
    win = _window(tmp_path)
    win.show()
    win.resize(400, 700)  # narrower than allowed: the window takes its minimum width
    app.processEvents()
    try:
        assert win.theme_btn.text() == "Appearance"
        for button in (win.theme_btn, win.about_btn):
            assert button.width() >= button.sizeHint().width(), button.text()
    finally:
        win._confirm_discard_edits = lambda count: True
        win.close()


def _text_area(edit: QLineEdit) -> QRect:
    """Where a line edit draws its text, as QLineEdit computes it: its contents less the text margins
    and the 2 px it keeps on either side. A text wider than this (with the cursor) scrolls."""
    opt = QStyleOptionFrame()
    edit.initStyleOption(opt)
    rect = edit.style().subElementRect(QStyle.SubElement.SE_LineEditContents, opt, edit)
    return rect.marginsRemoved(edit.textMargins()).adjusted(2, 0, -2, 0)


@pytest.mark.parametrize("size", [(1000, 700), (1440, 880)])
def test_the_rename_rows_fit_the_inspector(app, tmp_path, language, size):
    """At 1440 px the English photo prefix field showed "Photo_" as "hoto_": the start-number box
    beside it took 84 px where its largest number needs 69. At 1000 px "Name format:" and "first no.:"
    made the rows 17 px wider than the inspector: the cards lost their right edges, "Photo_" scrolled
    and the empty fields showed "e.g. …"; in Turkish "ilk no:" left room for "ör. Tat…" only. Measured
    in the app's font and style sheet, with the cursor at the end of the text as after typing it."""
    from fotodit.ui import fonts, theme

    old_font, old_sheet = app.font(), app.styleSheet()
    if fonts.load():
        font = QFont(fonts.FAMILY)
        font.setPointSizeF(theme.BASE_SIZE)
        app.setFont(font)
    app.setStyleSheet(theme._qss())
    win = _window(tmp_path)
    try:
        win.resize(*size)
        win.show()
        options = win.options
        options.rename.setChecked(True)
        app.processEvents()
        scroll = options.findChild(QScrollArea)
        assert scroll.widget().width() <= scroll.viewport().width()  # the cards keep their right edges
        for edit, text in ((options.prefix, "Photo_"), (options.video_prefix, "Video_")):
            area = _text_area(edit)
            hint = edit.placeholderText()  # Qt ends a hint that does not fit in "…"
            assert edit.fontMetrics().elidedText(hint, Qt.TextElideMode.ElideRight, area.width()) == hint
            edit.setText(text)
            assert area.width() >= edit.fontMetrics().horizontalAdvance(text) + 1, text  # the text and the cursor
            edit.grab()  # (painting scrolls a text that does not fit)
            assert edit.cursorPositionAt(QPoint(area.left(), area.center().y())) == 0, text  # its first letter shows
        for box in (options.start, options.video_start):
            box.setValue(box.maximum())
            assert _text_area(box.lineEdit()).width() >= box.fontMetrics().horizontalAdvance(box.text()) + 1
    finally:
        win._confirm_discard_edits = lambda count: True
        win.close()
        app.setStyleSheet(old_sheet)
        app.setFont(old_font)


def test_a_long_choice_ends_in_an_ellipsis_and_is_listed_in_full(app, english):
    from fotodit.ui.options_panel import OptionsPanel
    from fotodit.ui.widgets import ElidedComboBox, NumberBox

    panel = OptionsPanel()
    assert all(isinstance(combo, ElidedComboBox) for combo in panel.findChildren(QComboBox))
    assert all(isinstance(box, NumberBox) for box in panel.findChildren(QSpinBox))
    combo = panel.video_codec
    combo.resize(160, 30)
    full = combo.currentText()
    shown = combo.shown_text()
    assert shown.endswith("…") and shown != full and full.startswith(shown[:-1])
    assert combo.fontMetrics().horizontalAdvance(shown) < 160
    combo.show()
    combo.showPopup()
    app.processEvents()
    try:
        assert combo.view().window().width() >= combo.view().sizeHintForColumn(0)  # every choice in full
    finally:
        combo.hidePopup()
        combo.hide()
    combo.resize(900, 30)
    assert combo.shown_text() == full


def test_pop_up_buttons_and_number_boxes_show_their_arrows(app):
    from fotodit.ui import theme
    from fotodit.ui.widgets import ElidedComboBox, NumberBox

    holder = QWidget()
    holder.setStyleSheet(theme._qss())  # the app's style sheet: its drop-down rule hides Qt's own arrows
    plain, combo = QComboBox(holder), ElidedComboBox(holder)
    for box in (plain, combo):
        box.addItems(["Very high (95%), recommended", "High (90%)"])
        box.resize(220, 30)
    assert _marks(plain, 22, 2, theme.PANEL) == 0  # what the boxes looked like: no arrow
    assert _marks(combo, 22, 2, theme.PANEL) > 6  # (on the inspector's card)
    plain_number, number = QSpinBox(holder), NumberBox(holder)
    for box in (plain_number, number):
        box.setRange(0, 99)
        box.setValue(5)
        box.resize(84, 30)
    assert _marks(plain_number, 16, 2, theme.PANEL) == 0
    assert _marks(number, 16, 2, theme.PANEL) > 6


def test_the_crop_bars_pop_up_button_shows_its_arrow_on_the_dark_stage(app):
    from PySide6.QtWidgets import QDialog

    from fotodit.ui import theme
    from fotodit.ui.crop import CropBar
    from fotodit.ui.preview_dialog import QSS
    from fotodit.ui.widgets import ElidedComboBox

    stage = QDialog()
    stage.setObjectName("Preview")
    stage.setStyleSheet(theme._qss() + QSS)  # the preview's light text on dark glass
    bar = CropBar(stage)
    assert isinstance(bar.aspect, ElidedComboBox)
    stage.show()  # (the bar's layout gives the box its width)
    app.processEvents()
    try:
        assert _marks(bar.aspect, 22, 2, "#1C1C1E") > 6
    finally:
        stage.close()


def test_scrolling_over_a_box_changes_nothing(app):
    from fotodit.ui import theme
    from fotodit.ui.widgets import NumberBox

    style = theme.FotoditStyle()
    plain_combo, combo = QComboBox(), QComboBox()
    for box in (plain_combo, combo):
        box.addItems(["Very high (95%), recommended", "High (90%)", "Medium (80%)"])
    combo.setStyle(style)
    QApplication.sendEvent(plain_combo, _wheel(plain_combo))
    assert plain_combo.currentIndex() == 1  # Qt's own style: the wheel changes the choice under it
    QApplication.sendEvent(combo, _wheel(combo))
    assert combo.currentIndex() == 0  # the choice stays; the panel behind it scrolls
    plain, number = QSpinBox(), NumberBox()
    for box in (plain, number):
        box.setRange(0, 99)
        box.setValue(5)
    plain.wheelEvent(_wheel(plain))
    assert plain.value() == 4  # a plain number box steps without the focus
    event = _wheel(number)
    number.wheelEvent(event)
    assert number.value() == 5 and not event.isAccepted()
    assert number.focusPolicy() == Qt.FocusPolicy.StrongFocus  # the wheel does not give it the focus


def test_a_narrow_column_title_ends_in_an_ellipsis(app, tmp_path, english):
    from fotodit.ui.selected_page import COL_TARGET
    from fotodit.ui.widgets import ElidedHeader

    win = _window(tmp_path)
    try:
        header = win.selected.table.horizontalHeader()
        assert isinstance(header, ElidedHeader)
        header.setSectionResizeMode(COL_TARGET, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(COL_TARGET, 90)
        shown = header.shown_title(COL_TARGET)
        assert shown.endswith("…") and "New Name / Location".startswith(shown[:-1])
        header.resizeSection(COL_TARGET, 400)
        assert header.shown_title(COL_TARGET) == "New Name / Location"
    finally:
        win._confirm_discard_edits = lambda count: True
        win.close()
