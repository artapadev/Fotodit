"""The About window ("Hakkında"): the version, the copyright, the license (GPL-3.0-or-later, without
warranty) and the components Fotodit is built on, with their license texts. (The GPL asks a program
with a user interface to show these "Appropriate Legal Notices".) It opens as a sheet over the
main window; the license texts open in a window of their own.

The texts are found by `fotodit.core.legal`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase, QIcon
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from .. import APP_NAME, COPYRIGHT, __version__
from ..core.legal import LICENSE, NOTICES, licenses_folder, read_text
from ..i18n import t
from .controls import BarButton
from .sheet import Sheet


class TextDialog(QDialog):
    """A read-only text (a license) in a scrollable window."""

    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.text = QPlainTextEdit(text)
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)  # the texts have their own lines
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        # Wide enough for the longest line (no sideways scrolling), within the screen.
        metrics = self.text.fontMetrics()
        longest = max((metrics.horizontalAdvance(line) for line in text.splitlines()), default=0)
        screen = self.screen().availableGeometry().width() if self.screen() is not None else 1280
        self.resize(min(max(640, longest + 90), int(screen * 0.9)), 640)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(t("common.close"))
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.addWidget(self.text)
        layout.addWidget(buttons)


class AboutDialog(Sheet):
    def __init__(self, parent=None):
        super().__init__(parent, f"{APP_NAME} {__version__}", None, width=600)
        self.setWindowTitle(t("about.window_title", app=APP_NAME))
        icon = QIcon(parent.windowIcon()) if parent is not None else QIcon()
        if not icon.isNull():
            mark = QLabel()
            mark.setPixmap(icon.pixmap(56, 56))
            self.body.addWidget(mark, 0, Qt.AlignmentFlag.AlignLeft)
        summary = QLabel(t("about.summary"))
        copyright_label = QLabel(t("about.copyright", copyright=COPYRIGHT))
        copyright_label.setObjectName("Muted")
        notice = QLabel(t("about.notice", app=APP_NAME))
        built = QLabel(t("about.built_with"))
        built.setObjectName("Muted")
        for label in (summary, notice, built):
            label.setWordWrap(True)
        for label in (self.title, summary, copyright_label, notice, built):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for widget in (summary, copyright_label, notice, built):
            self.body.addWidget(widget)
        self.license_btn = BarButton(t("about.license"), "doc")
        self.license_btn.setToolTip(t("about.license_tip"))
        self.license_btn.clicked.connect(lambda: self._show(t("about.license_title"), LICENSE))
        self.notices_btn = BarButton(t("about.components"), "list")
        self.notices_btn.setToolTip(t("about.components_tip"))
        self.notices_btn.clicked.connect(lambda: self._show(t("about.components"), NOTICES))
        self.folder_btn = BarButton(t("about.licenses_folder"), "folder")
        self.folder_btn.setToolTip(t("about.licenses_folder_tip"))
        self.folder_btn.clicked.connect(self._open_folder)
        self.license_btn.setEnabled(read_text(LICENSE) is not None)
        self.notices_btn.setEnabled(read_text(NOTICES) is not None)
        self.folder_btn.setEnabled(licenses_folder() is not None)
        row = QHBoxLayout()
        row.setSpacing(2)
        for button in (self.license_btn, self.notices_btn, self.folder_btn):
            row.addWidget(button)
        row.addStretch(1)
        self.body.addLayout(row)
        self.add_button(t("common.close"), "accept", prominent=True)
        self.viewer: TextDialog | None = None

    def _show(self, title: str, name: str) -> None:
        text = read_text(name)
        if text is None:
            return
        self.viewer = TextDialog(title, text, self)
        self.viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.viewer.show()

    def _open_folder(self) -> None:
        folder = licenses_folder()
        if folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
