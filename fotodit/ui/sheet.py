"""Sheets: questions, confirmations and progress shown on a card over the dimmed window.

A sheet is a frameless, translucent dialog covering its window: the window beneath is blurred and
dimmed, as Artapa's dialogs dim their page (rgb(5 5 5 / .6) over a 14 px blur), and the card in the
middle is Artapa's sign-in card, frosting a picture of the window taken when the sheet opens (see
`glass`). The title stands beside its icon on a tile; the buttons sit at the bottom right, the default
action as the silver button. Enter presses it and Esc cancels. A sheet is a QDialog, so `exec()`,
`accept()` and `reject()` work as usual.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..i18n import t
from . import glass, theme
from .controls import SECONDARY, SILVER, PillButton
from .widgets import IconTile

CARD_W = 560
RADIUS = theme.RADIUS_STAGE
DIM = QColor(5, 5, 5, 153)  # rgb(5 5 5 / .6)


class Sheet(QDialog):
    """A dialog drawn as a card over its dimmed parent window. Put the content into `self.body` (a
    vertical layout) and add buttons with `add_button`."""

    def __init__(self, parent: QWidget | None, title: str = "", icon: str | None = None, width: int = CARD_W):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.setObjectName("Sheet")
        self._card_w = width
        self._backdrop: QImage | None = None
        self._glass_image: tuple[QImage, float] | None = None
        self._clicked: QPushButton | None = None
        self._buttons: list[QPushButton] = []
        self.card = QWidget(self)
        self.card.setObjectName("SheetCard")
        outer = QVBoxLayout(self.card)
        outer.setContentsMargins(26, 24, 22, 20)
        outer.setSpacing(14)
        head = QHBoxLayout()
        head.setSpacing(14)
        self.icon_name = icon
        if icon:
            head.addWidget(IconTile(icon, "WARN" if icon == "warning" else "TEXT"), 0, Qt.AlignmentFlag.AlignTop)
        self.title = QLabel(title)
        self.title.setObjectName("SheetTitle")
        self.title.setWordWrap(True)
        head.addWidget(self.title, 1, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body, 1)
        self.button_row = QHBoxLayout()
        self.button_row.setSpacing(8)
        self.button_row.addStretch(1)
        outer.addSpacing(4)
        outer.addLayout(self.button_row)
        self.setStyleSheet(self._qss())
        # A bound method, not a lambda: it is disconnected when the sheet is deleted.
        theme.changes().changed.connect(self._restyle)

    def _restyle(self) -> None:
        self.setStyleSheet(self._qss())
        self._glass_image = None
        self.update()

    @staticmethod
    def _qss() -> str:
        return (f"QLabel#SheetTitle {{ font-size: 12.5pt; font-weight: 600; color: {theme.TEXT}; }}"
                f"QWidget#SheetCard QLabel {{ color: {theme.TEXT}; }}"
                f"QWidget#SheetCard QLabel#Muted {{ color: {theme.MUTED}; }}"
                f"QWidget#SheetCard QLabel#Warn {{ color: {theme.WARN}; }}")

    # ------------------------------------------------------------ buttons

    def add_button(self, text: str, role: str = "reject", prominent: bool = False) -> QPushButton:
        """`role`: "accept" (closes with Accepted), "reject" or "none" (the caller connects it)."""
        button = PillButton(text, None, kind=SILVER if prominent else SECONDARY)
        if prominent:
            button.setMinimumWidth(126)
        button.setAutoDefault(False)
        if role == "accept":
            button.clicked.connect(lambda: self._finish(button, True))
        elif role == "reject":
            button.clicked.connect(lambda: self._finish(button, False))
        self.button_row.addWidget(button)
        self._buttons.append(button)
        if prominent:
            button.setDefault(True)
        return button

    def buttons(self) -> list[QPushButton]:
        return list(self._buttons)

    def clickedButton(self) -> QPushButton | None:  # noqa: N802 - like QMessageBox
        return self._clicked

    def _finish(self, button: QPushButton, accepted: bool) -> None:
        self._clicked = button
        self.accept() if accepted else self.reject()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Only a button that can be seen: a hidden default (the job sheet's Close while it runs) waits.
            default = next((b for b in self._buttons if b.isDefault() and b.isEnabled() and b.isVisible()), None)
            if default is not None:
                default.click()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ geometry and painting

    def showEvent(self, event) -> None:
        host = self.parentWidget().window() if self.parentWidget() is not None else None
        if host is not None:
            top_left = host.mapToGlobal(QPoint(0, 0))
            self.setGeometry(QRect(top_left, host.size()))
            self._backdrop = None if glass.reduce_transparency() else self._dimmed(host.grab().toImage())
        self._place_card()
        super().showEvent(event)

    @staticmethod
    def _dimmed(window: QImage) -> QImage:
        """The window as the sheet shows it behind the card: blurred 14 px and darkened."""
        dpr = window.devicePixelRatio() or 1.0
        image = glass.frost(window.convertToFormat(glass.FMT), 14 * dpr, 1.0)
        image.setDevicePixelRatio(dpr)
        p = QPainter(image)
        p.fillRect(QRectF(0, 0, image.width() / dpr, image.height() / dpr), DIM)
        p.end()
        return image

    def hideEvent(self, event) -> None:
        # The picture of the window and the glass made from it are large: let them go with the sheet.
        self._backdrop = None
        self._glass_image = None
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_card()

    def _place_card(self) -> None:
        w = max(320, min(self._card_w, self.width() - 40))
        self.card.setFixedWidth(w)
        # Wrapped text is measured at the card's real width: the size hint guesses a narrower one
        # and leaves gaps between the lines of the sheet.
        h = self.card.heightForWidth(w)
        if h < 0:
            h = self.card.sizeHint().height()
        h = min(h, self.height() - 40)
        self.card.setGeometry((self.width() - self.card.width()) // 2,
                              max(20, int((self.height() - h) * 0.36)), self.card.width(), h)
        self._glass_image = None
        self.update()

    def _card_glass(self, rect: QRectF, mat: glass.Material, dpr: float) -> tuple[QImage | None, float]:
        """The card's frost from the picture of the window (made once per placement)."""
        if self._backdrop is None:
            return None, 0.0
        if self._glass_image is None:
            margin = glass.margin_of(mat)
            area = rect.toAlignedRect().adjusted(-margin, -margin, margin, margin)
            src_dpr = self._backdrop.devicePixelRatio() or 1.0
            img = QImage(round(area.width() * dpr), round(area.height() * dpr), glass.FMT)
            img.setDevicePixelRatio(dpr)
            img.fill(QColor(theme.WINDOW))
            p = QPainter(img)
            backdrop = QImage(self._backdrop)
            backdrop.setDevicePixelRatio(src_dpr)
            p.drawImage(QPointF(-area.x(), -area.y()), backdrop)
            p.end()
            w, h = round(rect.width() * dpr), round(rect.height() * dpr)
            self._glass_image = glass.render_glass(img, round(margin * dpr), w, h, mat, dpr)
        return self._glass_image

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._backdrop is not None:
            p.drawImage(QPointF(0, 0), self._backdrop)
        else:
            p.fillRect(self.rect(), DIM)
        rect = QRectF(self.card.geometry())
        mat = glass.material(glass.SHEET)
        dpr = self.devicePixelRatioF() or 1.0
        image, luminance = self._card_glass(rect, mat, dpr)
        # A large, soft shadow lifts the card off the dimmed window.
        shadow, margin = glass.shadow_image(rect.width(), rect.height(), RADIUS, mat, dpr)
        p.drawImage(QPointF(rect.x() - margin, rect.y() - margin), shadow)
        glass.paint_glass(p, rect, image, mat, RADIUS, dpr, luminance=luminance)


def _question(parent: QWidget | None, title: str, text: str, accept: str | None, reject: str | None,
              icon: str | None) -> Sheet:
    sheet = Sheet(parent, title, icon)
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    sheet.body.addWidget(label)
    sheet.add_button(reject or t("common.no"), "reject")
    sheet.add_button(accept or t("common.yes"), "accept", prominent=True)
    return sheet


def ask(parent: QWidget | None, title: str, text: str, accept: str | None = None, reject: str | None = None,
        icon: str | None = "warning") -> bool:
    """A yes/no question on a sheet ("Evet" / "Hayır" unless named); True if accepted."""
    sheet = _question(parent, title, text, accept, reject, icon)
    accepted = sheet.exec() == QDialog.DialogCode.Accepted
    sheet.deleteLater()  # (a sheet belongs to its window, which would keep every one of them)
    return accepted


def offer(parent: QWidget, title: str, text: str, accept: str, reject: str, on_accept: Callable[[], None],
          icon: str | None = "info") -> Sheet:
    """`ask` without waiting: the sheet opens over its window and `on_accept` runs if it is accepted. For a
    question asked from a menu, whose action then returns at once instead of holding a nested event loop."""
    sheet = _question(parent, title, text, accept, reject, icon)
    sheet.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    sheet.accepted.connect(on_accept)
    sheet.open()
    return sheet


def inform(parent: QWidget | None, title: str, text: str) -> None:
    """A message on a sheet with a single "Tamam" (OK) button."""
    sheet = Sheet(parent, title, "info")
    label = QLabel(text)
    label.setWordWrap(True)
    sheet.body.addWidget(label)
    sheet.add_button(t("common.ok"), "accept", prominent=True)
    sheet.exec()
    sheet.deleteLater()
