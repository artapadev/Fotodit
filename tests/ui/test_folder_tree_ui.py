"""The sidebar's folder tree: in its narrowest width a folder ten levels deep keeps its name readable
and the narrower steps keep their arrows easy to hit (in the app's own look); the open folder stays
in view while the folders above it are listed, until the user scrolls (offscreen Qt)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from helpers import wait_until  # noqa: E402
from PySide6.QtCore import QDir, QPersistentModelIndex, QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QFontMetrics, QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractSlider,
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleFactory,
    QStyleOptionViewItem,
)

from fotodit.ui import theme  # noqa: E402

NARROWEST = 230  # the folders' minimum width in the window (MainWindow: self.left)
FONT_CLASSES = ("QAbstractItemView", "QMenu", "QMenuBar", "QMessageBox", "QTipLabel")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def app_look(app):
    """The app's own style, style sheet and Inter: the room a name gets depends on all three."""
    style, fonts = app.style().name(), {name: QApplication.font(name) for name in FONT_CLASSES}
    font = app.font()
    theme.apply(app, theme.LIGHT)
    yield
    app.setStyleSheet("")  # the other tests run without the app's theme
    app.setStyle(QStyleFactory.create(style))
    app.setPalette(app.style().standardPalette())
    QGuiApplication.styleHints().unsetColorScheme()
    theme._use(False)
    theme._style = None
    app.setFont(font)
    for name, class_font in fonts.items():
        app.setFont(class_font, name)


def _shown_name(tree, index) -> tuple[str, QRect]:
    """The folder's name as the tree draws it, cut to its row's room and ending in "…", and that room."""
    opt = QStyleOptionViewItem()
    tree.initViewItemOption(opt)
    opt.rect = tree.visualRect(index)
    QStyledItemDelegate().initStyleOption(opt, index)
    style = tree.style()
    margin = style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, None, tree) + 1  # as Qt draws the text
    room = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, tree).adjusted(margin, 0, -margin, 0)
    return QFontMetrics(opt.font).elidedText(opt.text, Qt.TextElideMode.ElideRight, room.width()), room


def test_a_folder_ten_levels_deep_keeps_its_name_in_the_narrowest_sidebar(app, app_look, tmp_path):
    from fotodit.ui.folders import FolderSelection
    from fotodit.ui.nav import FolderTree, NavPanel
    from fotodit.ui.selection import Selection

    parent = tmp_path / "Arşiv"  # parts[0] is the drive: a folder's level in the tree is len(parts) - 1
    while len(parent.parts) < 10:
        parent = parent / "Klasör"
    if len(parent.parts) > 10:
        pytest.skip("the temporary folder lies too deep for a folder at level 10")
    name = "Fotoğraflar 2024"
    folder = parent / name  # C: › Users › … › Arşiv › Fotoğraflar 2024, at level 10
    (folder / "Bodrum").mkdir(parents=True)

    sel = Selection()
    panel = NavPanel(FolderSelection(sel))  # with the folders' check boxes, as in the window
    panel.resize(NARROWEST, 700)
    panel.show()
    tree = panel.tree

    def shown_folder():
        """The folder's row, brought into view: folders loading above it push it down."""
        index = panel.fs.index(QDir.fromNativeSeparators(str(folder)))
        tree.scrollTo(index)
        return index

    try:
        panel.sync(str(folder))
        wait_until(app, tree.verticalScrollBar().isVisible, timeout=5.0)  # the folders above have loaded
        index = shown_folder()
        assert panel.width() == NARROWEST

        shown, room = _shown_name(tree, index)
        assert shown == name or (shown.endswith("…") and shown.startswith(name[:8])), shown
        # The tree draws it so: the name's ink reaches at least as far as its first eight characters.
        image = tree.viewport().grab().toImage()
        ink = [x for x in range(room.left(), min(room.right() + 1, image.width()))
               for y in range(room.top() + 4, room.bottom() - 3)
               if image.pixelColor(x, y).alpha() > 160 and image.pixelColor(x, y).lightness() < 110]
        assert ink and max(ink) - room.left() >= QFontMetrics(tree.font()).horizontalAdvance(name[:8]) - 2
        assert FolderTree.MIN_STEP <= tree.indentation() < FolderTree.STEP  # by taking smaller steps

        # A narrow step's arrow is as easy to hit as a full one: a click just left of it shows the
        # folder's subfolders, and does not open the folder in the browser.
        opened = []
        panel.folderSelected.connect(opened.append)
        index = shown_folder()
        row, folder_index = tree.visualRect(index), QPersistentModelIndex(index)  # the model may sort meanwhile
        assert not tree.isExpanded(folder_index)
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(row.left() - tree.indentation() - 4, row.center().y()))
        app.processEvents()
        assert tree.isExpanded(folder_index) and not opened

        panel.sync("")  # every folder closed: the full steps come back
        assert tree.indentation() == FolderTree.STEP
    finally:
        panel.close()
        sel.shutdown()


def test_the_open_folder_stays_in_view_while_the_folders_above_it_load(app, tmp_path):
    from fotodit.ui.nav import NavPanel

    parent = tmp_path / "Arşiv"
    for i in range(60):  # listed a moment after the open folder's own row, and sorted above it
        (parent / f"Albüm {i:02d}").mkdir(parents=True)
    (parent / "Albüm 00" / "Bodrum").mkdir()
    folder = parent / "Zeytinlik"
    folder.mkdir()
    panel = NavPanel()
    panel.resize(260, 600)
    panel.show()
    tree, fs = panel.tree, panel.fs

    def in_view() -> bool:
        index = fs.index(QDir.fromNativeSeparators(str(folder)))
        return tree.viewport().rect().contains(tree.visualRect(index).center())

    try:
        panel.sync(str(folder))
        listed = fs.index(QDir.fromNativeSeparators(str(parent)))
        assert wait_until(app, lambda: fs.rowCount(listed) == 61)
        QTest.qWait(300)  # Qt sorts the listing a moment later
        assert wait_until(app, in_view, timeout=2.0)

        # Once the user scrolls, the tree is theirs: folders listed later do not pull it back.
        bar = tree.verticalScrollBar()
        bar.triggerAction(QAbstractSlider.SliderAction.SliderToMinimum)
        album = fs.index(QDir.fromNativeSeparators(str(parent / "Albüm 00")))
        tree.expand(album)
        assert wait_until(app, lambda: fs.rowCount(album) == 1)
        QTest.qWait(300)
        assert bar.value() == 0 and not in_view()
    finally:
        panel.close()
