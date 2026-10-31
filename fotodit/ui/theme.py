"""App theme: Artapa's look (artapa.com), dark only.

The colors are named tokens (TEXT, MUTED, ACCENT …) with Artapa's values (its `tokens.css`): a
near-black page, two surfaces, three tones of text, white at low opacities for wells, lines and
light, and a few tones of its own (peach, periwinkle, lilac, the aurora and the premium gradient),
used where Artapa uses them. Warnings, errors and success keep its amber, red and green, always
with a symbol too. Text passes WCAG AA (4.5:1) on the page, on the cards and on selected rows. Code
reads the tokens from this module when it paints or builds a text (`theme.TEXT`); widgets that keep
a color (a style sheet of their own, a cached QColor, an icon) listen to `changes().changed`.
`apply()` sets Qt's palette and style sheet and tells Windows the color scheme, so the window title
bars are dark too.

The structure follows Artapa's admin panel: the window is the calm stage (see `glass.paint_stage`),
the sidebar and the inspector are cards on it, and the toolbars float above the content as the
site's header pills (see `glass`). Controls are pills and rounded rectangles whose corners are
concentric with the card they sit on.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle

# Corner radii (logical px), Artapa's. Inner corners are concentric: inner radius = outer radius − padding.
RADIUS_STAGE = 24  # the sidebar and inspector cards, sheets
RADIUS_CARD = 16  # groups inside a card (24 − the card's padding of 8)
RADIUS_FLOAT = 20  # floating panels (the preview's info panel)
RADIUS_MENU = 12  # menus
RADIUS_WELL = 10  # text fields, pop-up buttons, list rows, tooltips
RADIUS_MEDIA = 9  # thumbnails (Artapa's card media)
BASE_SIZE = 9.75  # pt: the body text (Onest; 13 px at 100 %, as Artapa's app text)

_TOKENS = {
    # The page and its surfaces
    "WINDOW": "#070707",  # the calm stage behind everything (Artapa's admin panel)
    "PAGE": "#060606",  # Artapa's page: the large preview's stage, the knob of an open switch
    "PANEL": "#0D0D0D",  # surface-1: the cards, menus, list pop-ups
    "SURFACE_2": "#141414",  # surface-2: a thumbnail that is not ready yet
    # Text
    "TEXT": "#F5F5F5",  # text-primary
    "MUTED": "#A6A6A6",  # text-secondary: what is read second
    "FAINT": "#868686",  # text-muted: hints, column titles, placeholders
    "DISABLED": "#5E5E5E",
    # What is on or chosen: Artapa's accent-1 (checks, rings, slider fills), marks on it in on-premium
    "ACCENT": "#E8EAEF",
    "ON_ACCENT": "#0D0D0D",
    "ACCENT_SOFT": "#1AFFFFFF",  # a chosen row or tab (white at 10%, Artapa's active link)
    "ACCENT_TEXT": "#F5F5F5",  # links (underlined) and emphasized text
    "SELECTION": "#4A4C52",  # selected text in fields
    # White at low opacities: wells (fields), lines and light
    "WELL": "#0DFFFFFF",  # 5 %
    "WELL_HOVER": "#13FFFFFF",  # 7.5 %
    "HOVER": "#0FFFFFFF",  # 6 %: a row or button under the pointer (Artapa's nav links)
    "PRESSED": "#1AFFFFFF",
    "ITEM_HOVER": "#0AFFFFFF",  # 4 %: a tile under the pointer (Artapa's cards)
    "CARD_BG": "#07FFFFFF",  # 2.7 %: groups inside a card
    "BORDER": "#14FFFFFF",  # border-soft, 8 %
    "BORDER_HOVER": "#29FFFFFF",  # 16 %
    "SEPARATOR": "#14FFFFFF",
    "GRID": "#0FFFFFFF",  # the lines between table rows
    "RIM": "#0FFFFFFF",  # the line of light inside a top edge
    "TRACK": "#1FFFFFFF",  # slider and progress tracks (12 %)
    # States: Artapa's positive, opt-mid and field-error; its danger for what ends something
    "SUCCESS": "#86E3B5",
    "WARN": "#F4C983",
    "DANGER": "#FF7676",
    "STOP": "#FF8585",
    "STOP_TINT": "#0FFF5A5A",
    "STOP_RING": "#47FF5A5A",
    # Artapa's tones
    "PEACH": "#FFBEA2",
    "PERIWINKLE": "#A2B7F5",
    "LILAC": "#D4D9E4",
    "EDIT": "#A2B7F5",  # edited photos and videos (periwinkle, Artapa's "new")
    "AURORA_WARM": "#FEEAE1",
    "AURORA_COOL": "#C0CEF2",
    "PREMIUM_START": "#BCC0CA",  # the premium gradient: PREMIUM_START, AURORA_WARM, AURORA_COOL
    "SILVER_TOP": "#FFFFFF",  # the silver circle: SILVER_TOP, SILVER_MIDDLE at 55 %, SILVER_FOOT
    "SILVER_MIDDLE": "#E6E7EE",
    "SILVER_FOOT": "#C9CCD6",
    # Controls
    "INPUT_DISABLED": "#06FFFFFF",
    "TOOLTIP": "#1E1E1E",  # Artapa's toast, rgb(30 30 30 / .95), made opaque
    "PLACEHOLDER": "#868686",
    "CHECK_BORDER": "#3DFFFFFF",  # an empty check box's ring (24 %), brighter under the pointer
    "CHECK_BORDER_HOVER": "#66FFFFFF",
    "CHECK_ON_OFF": "#4D4D4D",  # a check box that is on but cannot be changed
    "SWITCH_OFF": "#1FFFFFFF",
    "FOLDER": "#4A4A4E",  # folder icons: grey
    "FOLDER_TAB": "#38383C",
    # Fusion's bevels and scroll bars
    "LIGHT_ROLE": "#2A2A2A",
    "MIDLIGHT": "#1E1E1E",
    "MID": "#29FFFFFF",
    "DARK_ROLE": "#060606",
    "SHADOW": "#000000",
}

# The tokens as module attributes (filled in from _TOKENS just below).
WINDOW = PAGE = PANEL = SURFACE_2 = TEXT = MUTED = FAINT = DISABLED = ACCENT = ON_ACCENT = ACCENT_SOFT = ""
ACCENT_TEXT = SELECTION = WELL = WELL_HOVER = HOVER = PRESSED = ITEM_HOVER = CARD_BG = BORDER = ""
BORDER_HOVER = SEPARATOR = GRID = RIM = TRACK = SUCCESS = WARN = DANGER = STOP = STOP_TINT = STOP_RING = ""
PEACH = PERIWINKLE = LILAC = EDIT = AURORA_WARM = AURORA_COOL = PREMIUM_START = SILVER_TOP = SILVER_MIDDLE = ""
SILVER_FOOT = INPUT_DISABLED = TOOLTIP = PLACEHOLDER = CHECK_BORDER = CHECK_BORDER_HOVER = CHECK_ON_OFF = ""
SWITCH_OFF = FOLDER = FOLDER_TAB = LIGHT_ROLE = MIDLIGHT = MID = DARK_ROLE = SHADOW = ""
globals().update(_TOKENS)

_style: QProxyStyle | None = None  # the app's style, once set (app.style() does not return the Python object)
_family = "Segoe UI"
_hinting = QFont.HintingPreference.PreferDefaultHinting


def color(token: str) -> QColor:
    """A token as a QColor (tokens may carry alpha: #AARRGGBB)."""
    return QColor(token)


def premium_gradient(start: QPointF, end: QPointF, alpha: float = 1.0) -> QLinearGradient:
    """Artapa's premium gradient (silver, warm white, cool blue) from `start` to `end`."""
    gradient = QLinearGradient(start, end)
    for at, token in ((0.0, PREMIUM_START), (0.42, AURORA_WARM), (1.0, AURORA_COOL)):
        c = QColor(token)
        c.setAlphaF(alpha)
        gradient.setColorAt(at, c)
    return gradient


def silver_gradient(top: float, bottom: float) -> QLinearGradient:
    """The silver of Artapa's icon circle, from `top` to `bottom` (y)."""
    gradient = QLinearGradient(QPointF(0, top), QPointF(0, bottom))
    gradient.setColorAt(0.0, QColor(SILVER_TOP))
    gradient.setColorAt(0.55, QColor(SILVER_MIDDLE))
    gradient.setColorAt(1.0, QColor(SILVER_FOOT))
    return gradient


class _Changes(QObject):
    changed = Signal()


_changes: _Changes | None = None


def changes() -> _Changes:
    """Emits `changed` after the look changed (for widgets that keep colors or drawn images)."""
    global _changes
    if _changes is None:
        _changes = _Changes()
    return _changes


def family() -> str:
    """The UI font: the bundled Onest (see `fonts`), else Segoe UI Variable or Segoe UI."""
    return _family


def font(size: float = 10.0, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont(_family)
    f.setPointSizeF(size)
    f.setWeight(weight)
    f.setHintingPreference(_hinting)
    f.setFeature(QFont.Tag("calt"), 0)  # file names shown letter for letter: "->" is not turned into an arrow
    return f


def tabular(f: QFont) -> QFont:
    """`f` with equal-width digits, for numbers that change in place or stand in a column (a
    timer, a counter, the dates of a list), so they do not shift."""
    f = QFont(f)
    f.setFeature(QFont.Tag("tnum"), 1)
    return f


def _rgba(token: str) -> str:
    """QSS color: #AARRGGBB tokens become rgba() (QSS reads 8-digit hex as #AARRGGBB too, but
    rgba() is unambiguous)."""
    c = QColor(token)
    if c.alpha() == 255:
        return c.name()
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


def _qss() -> str:
    c = {name: _rgba(value) for name, value in _TOKENS.items()}
    premium = (f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {PREMIUM_START}, stop:0.42 {AURORA_WARM}, "
               f"stop:1 {AURORA_COOL})")
    return f"""
QMainWindow, QDialog {{ background: {c['WINDOW']}; }}
QWidget#Glass, QWidget#Glass QWidget#GlassBody {{ background: transparent; }}
QLabel {{ color: {c['TEXT']}; }}
QLabel#CardTitle {{ font-size: 10pt; font-weight: 600; color: {c['TEXT']}; }}
QLabel#SectionTitle {{ font-size: 8.5pt; font-weight: 500; color: {c['FAINT']}; }}
QLabel#Muted {{ color: {c['MUTED']}; }}
QLabel#Hint {{ color: {c['FAINT']}; font-size: 8.75pt; }}
QLabel#Warn {{ color: {c['WARN']}; }}
QLabel#Example {{ color: {c['FAINT']}; font-size: 8.75pt; }}
QLabel#BarText {{ color: {c['MUTED']}; }}
QLabel#EmptyTitle {{ color: {c['TEXT']}; font-size: 13pt; font-weight: 600; }}
QLabel#Empty {{ color: {c['MUTED']}; }}
QLineEdit#GlassField {{ background: transparent; border: none; padding: 0 2px; }}
QFrame#GroupLine {{ background: {c['SEPARATOR']}; border: none; }}
QSplitter::handle {{ background: transparent; }}
QPushButton {{
    background: rgba(255,255,255,11); color: {c['TEXT']}; border: 1px solid {c['BORDER']};
    border-radius: 15px; padding: 5px 15px; min-height: 18px; font-weight: 500;
}}
QPushButton:hover {{ background: rgba(255,255,255,18); border-color: {c['BORDER_HOVER']}; }}
QPushButton:pressed {{ background: {c['PRESSED']}; }}
QPushButton:disabled {{ color: {c['DISABLED']}; background: {c['INPUT_DISABLED']}; border-color: {c['INPUT_DISABLED']}; }}
QPushButton:checked {{ background: {c['ACCENT_SOFT']}; color: {c['TEXT']}; }}
QPushButton#Primary {{ font-size: 10pt; font-weight: 500; }}
QPushButton#Link {{
    background: transparent; border: none; color: {c['ACCENT_TEXT']}; padding: 2px 4px; text-decoration: underline;
}}
QToolButton {{ color: {c['TEXT']}; background: transparent; border: none; border-radius: 15px; padding: 5px; }}
QToolButton:hover {{ background: {c['HOVER']}; }}
QToolButton:pressed {{ background: {c['PRESSED']}; }}
QToolButton:disabled {{ background: transparent; }}
QCheckBox, QRadioButton {{ spacing: 8px; color: {c['TEXT']}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {c['DISABLED']}; }}
QCheckBox#Section {{ font-weight: 600; font-size: 10pt; }}
QLineEdit, QComboBox, QSpinBox {{
    padding: 4px 10px; border: 1px solid transparent; border-radius: {RADIUS_WELL}px;
    background: {c['WELL']}; color: {c['TEXT']}; min-height: 20px;
    selection-background-color: {c['SELECTION']}; selection-color: {c['TEXT']};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ background: {c['WELL_HOVER']}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ background: {c['WELL_HOVER']}; border-color: {c['BORDER_HOVER']}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    background: {c['INPUT_DISABLED']}; color: {c['DISABLED']}; border-color: transparent;
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 16px; }}
QComboBox QAbstractItemView {{
    background: {c['PANEL']}; color: {c['TEXT']}; border: 1px solid {c['BORDER_HOVER']}; padding: 4px;
    selection-background-color: {c['ACCENT_SOFT']}; selection-color: {c['TEXT']}; outline: 0;
}}
QListView, QTreeView, QTableView {{ background: transparent; color: {c['TEXT']}; border: none; outline: 0; }}
QTreeView#Sidebar, QListWidget#Sidebar, QScrollArea#Sidebar, QWidget#Sidebar {{ background: transparent; }}
QListWidget#Sidebar::item {{ padding: 4px 4px 4px 13px; }}
QListWidget#Sidebar::item:selected, QListWidget#Sidebar::item:hover {{ background: transparent; }}
QTreeView#Sidebar {{ show-decoration-selected: 0; }}
QTreeView#Sidebar::item {{ padding: 4px 4px; }}
QTreeView#Sidebar::item:selected, QTreeView#Sidebar::item:hover {{ background: transparent; }}
QTableView {{ gridline-color: {c['GRID']}; selection-background-color: transparent; selection-color: {c['TEXT']}; }}
QHeaderView {{ background: transparent; font-weight: 500; font-size: 8.5pt; }}
QHeaderView::section {{
    background: transparent; border: none; border-bottom: 1px solid {c['SEPARATOR']}; padding: 6px 8px;
    color: {c['FAINT']}; font-weight: 500; font-size: 8.5pt;
}}
QProgressBar {{
    border: none; border-radius: 2px; background: {c['TRACK']}; color: transparent; max-height: 4px;
}}
QProgressBar::chunk {{ background: {premium}; border-radius: 2px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px 1px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 1px 2px; }}
QScrollBar::handle:vertical {{ background: {c['MID']}; border-radius: 2px; min-height: 36px; margin: 0 3px; }}
QScrollBar::handle:horizontal {{ background: {c['MID']}; border-radius: 2px; min-width: 36px; margin: 3px 0; }}
QScrollBar::handle:hover {{ background: rgba(255,255,255,77); }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{
    background: {c['TOOLTIP']}; color: {c['TEXT']}; border: 1px solid {c['BORDER']}; padding: 6px 10px;
    border-radius: {RADIUS_WELL}px;
}}
QMenu {{ background: #0B0B0B; color: {c['TEXT']}; border: 1px solid rgba(255,255,255,23); padding: 6px; }}
QMenu#AppMenu {{ border-radius: {RADIUS_MENU}px; }}
QMenu::item {{ padding: 7px 24px 7px 12px; border-radius: 8px; }}
QMenu::item:selected {{ background: {c['WELL_HOVER']}; color: {c['TEXT']}; }}
QMenu::item:disabled {{ color: {c['DISABLED']}; }}
QMenu::separator {{ height: 1px; background: {c['SEPARATOR']}; margin: 5px 10px; }}
QMenu::icon {{ padding-left: 8px; }}
QMessageBox QLabel {{ color: {c['TEXT']}; }}
QPlainTextEdit {{ background: {c['PANEL']}; color: {c['TEXT']}; border: 1px solid {c['BORDER']};
    border-radius: {RADIUS_WELL}px; padding: 6px; }}
"""


def _palette() -> QPalette:
    roles = QPalette.ColorRole
    palette = QPalette()
    for role, value in (
        (roles.Window, WINDOW), (roles.WindowText, TEXT), (roles.Base, PANEL), (roles.AlternateBase, "#0F0F0F"),
        (roles.Text, TEXT), (roles.Button, PANEL), (roles.ButtonText, TEXT), (roles.BrightText, "#FFFFFF"),
        (roles.Highlight, SELECTION), (roles.HighlightedText, TEXT), (roles.PlaceholderText, PLACEHOLDER),
        (roles.ToolTipBase, TOOLTIP), (roles.ToolTipText, TEXT), (roles.Link, ACCENT_TEXT),
        (roles.LinkVisited, ACCENT_TEXT), (roles.Light, LIGHT_ROLE), (roles.Midlight, MIDLIGHT),
        (roles.Mid, "#2A2A2A"), (roles.Dark, DARK_ROLE), (roles.Shadow, SHADOW),
    ):
        palette.setColor(role, QColor(value))
    disabled = QPalette.ColorGroup.Disabled
    for role, value in ((roles.WindowText, DISABLED), (roles.Text, DISABLED), (roles.ButtonText, DISABLED),
                        (roles.Base, "#0A0A0A"), (roles.Button, "#0A0A0A"), (roles.Highlight, CHECK_ON_OFF)):
        palette.setColor(disabled, role, QColor(value))
    return palette


class FotoditStyle(QProxyStyle):
    """Fusion plus the indicators we draw ourselves: check boxes, radio buttons, the arrows of
    pop-up buttons and the chevrons of trees, crisp at every DPI (Fusion's thin frames can vanish at
    125% or 150%)."""

    def __init__(self):
        super().__init__("Fusion")

    def pixelMetric(self, metric, option=None, widget=None):
        if metric in (QStyle.PixelMetric.PM_IndicatorWidth, QStyle.PixelMetric.PM_IndicatorHeight,
                      QStyle.PixelMetric.PM_ExclusiveIndicatorWidth, QStyle.PixelMetric.PM_ExclusiveIndicatorHeight):
            return 17
        return super().pixelMetric(metric, option, widget)

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ComboBox_Popup:
            return 0  # a plain list under the box: Fusion's "pop-up over the box" lists cover the field
        if hint == QStyle.StyleHint.SH_ComboBox_AllowWheelScrolling:
            return 0  # as on macOS: scrolling a panel over a pop-up button scrolls the panel, never the choice
        return super().styleHint(hint, option, widget, returnData)

    def subElementRect(self, element, option, widget=None):
        rect = super().subElementRect(element, option, widget)
        if element == QStyle.SubElement.SE_TreeViewDisclosureItem and rect.width() < 14:
            # A deep tree's narrow steps (see `nav.FolderTree`) keep an arrow as easy to hit as the
            # usual 14 px step: the empty indent to its left counts too.
            rect.setLeft(rect.right() + 1 - 14)
        return rect

    def drawPrimitive(self, element, option, painter, widget=None):
        pe = QStyle.PrimitiveElement
        if element == pe.PE_IndicatorCheckBox:
            self._check_box(option, painter)
        elif element == pe.PE_IndicatorRadioButton:
            self._radio(option, painter)
        elif element in (pe.PE_IndicatorArrowDown, pe.PE_IndicatorArrowUp):
            self._chevron(option, painter, up=element == pe.PE_IndicatorArrowUp)
        elif element == pe.PE_IndicatorBranch:
            self._branch(option, painter)
        else:
            super().drawPrimitive(element, option, painter, widget)

    @staticmethod
    def _state(option) -> tuple[bool, bool, bool, bool]:
        state = option.state
        flags = QStyle.StateFlag
        return (bool(state & flags.State_Enabled), bool(state & flags.State_On),
                bool(state & flags.State_NoChange), bool(state & flags.State_MouseOver))

    def _check_box(self, option, painter) -> None:
        """Artapa's check box: a well with a ring; checked, it fills with the accent and holds a dark tick."""
        enabled, on, partial, hover = self._state(option)
        side = min(option.rect.width(), option.rect.height()) - 1
        box = QRectF(option.rect.x() + 0.5, option.rect.center().y() - side / 2 + 0.5, side, side)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if on or partial:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(ACCENT if enabled else CHECK_ON_OFF))
            painter.drawRoundedRect(box, 4.5, 4.5)
            pen = QPen(QColor(ON_ACCENT if enabled else "#A6A6A6"), max(1.7, side / 8))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            if partial and not on:
                y = box.center().y()
                painter.drawLine(QPointF(box.left() + side * 0.27, y), QPointF(box.right() - side * 0.27, y))
            else:
                tick = QPainterPath()
                tick.moveTo(box.left() + side * 0.25, box.top() + side * 0.53)
                tick.lineTo(box.left() + side * 0.43, box.top() + side * 0.70)
                tick.lineTo(box.left() + side * 0.76, box.top() + side * 0.31)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(tick)
        else:
            ring = CHECK_BORDER_HOVER if (hover and enabled) else (CHECK_BORDER if enabled else BORDER)
            painter.setPen(QPen(QColor(ring), 1.2))
            painter.setBrush(QColor(WELL if enabled else INPUT_DISABLED))
            painter.drawRoundedRect(box.adjusted(0.4, 0.4, -0.4, -0.4), 4.5, 4.5)
        painter.restore()

    def _radio(self, option, painter) -> None:
        enabled, on, _partial, hover = self._state(option)
        side = min(option.rect.width(), option.rect.height()) - 1
        c = QPointF(option.rect.x() + side / 2 + 0.5, option.rect.center().y() + 0.5)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if on:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(ACCENT if enabled else CHECK_ON_OFF))
            painter.drawEllipse(c, side / 2, side / 2)
            painter.setBrush(QColor(ON_ACCENT if enabled else "#A6A6A6"))
            painter.drawEllipse(c, side * 0.19, side * 0.19)
        else:
            ring = CHECK_BORDER_HOVER if (hover and enabled) else (CHECK_BORDER if enabled else BORDER)
            painter.setPen(QPen(QColor(ring), 1.2))
            painter.setBrush(QColor(WELL if enabled else INPUT_DISABLED))
            painter.drawEllipse(c, side / 2 - 0.4, side / 2 - 0.4)
        painter.restore()

    def _chevron(self, option, painter, up: bool) -> None:
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        paint_chevron(painter, option.rect, QColor(MUTED if enabled else DISABLED), up)

    @staticmethod
    def _branch(option, painter) -> None:
        """A tree row's disclosure chevron: right when closed, down when open; no lines. A deep
        tree's narrow steps do not shrink it."""
        flags = QStyle.StateFlag
        if not option.state & flags.State_Children:
            return
        r = QRectF(option.rect)
        s = min(r.height(), 12.0) * 0.5
        c = r.center()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(FAINT), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        path = QPainterPath()
        d, w = s * 0.28, s * 0.55
        if option.state & flags.State_Open:
            path.moveTo(c.x() - w, c.y() - d)
            path.lineTo(c.x(), c.y() + d)
            path.lineTo(c.x() + w, c.y() - d)
        else:
            path.moveTo(c.x() - d, c.y() - w)
            path.lineTo(c.x() + d, c.y())
            path.lineTo(c.x() - d, c.y() + w)
        painter.drawPath(path)
        painter.restore()


def paint_chevron(painter: QPainter, rect, color: QColor, up: bool = False) -> None:
    """The arrow of a pop-up button: a small chevron, down (or up). Widgets that the style sheet draws
    call it too (its drop-down rule hides the style's own arrow)."""
    r = QRectF(rect)
    s = min(r.width(), r.height(), 12.0) * 0.5
    c = r.center()
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    dy = -s * 0.28 if up else s * 0.28
    path = QPainterPath()
    path.moveTo(c.x() - s * 0.55, c.y() - dy)
    path.lineTo(c.x(), c.y() + dy)
    path.lineTo(c.x() + s * 0.55, c.y() - dy)
    painter.drawPath(path)
    painter.restore()


def _pick_family() -> str:
    from . import fonts

    if fonts.load():
        return fonts.FAMILY
    families = set(QFontDatabase.families())
    for name in ("Segoe UI Variable Text", "Segoe UI"):
        if name in families:
            return name
    return QFont().defaultFamily()


def apply(app: QApplication) -> None:
    """Shows the app in Artapa's look: the palette, the style sheet, the fonts and dark title bars."""
    global _style, _family, _hinting
    QGuiApplication.styleHints().setColorScheme(Qt.ColorScheme.Dark)  # the window frames are dark too
    first = _style is None
    if first:
        _style = FotoditStyle()
        app.setStyle(_style)
        # At 125 % or 150 % Qt hints only small text and leaves titles unhinted, which blurs them;
        # vertical hinting keeps every size on the pixel grid without crowding the letters
        # (full hinting would, since the advances are hinted at the unscaled size).
        if 1.0 < app.devicePixelRatio() < 2.0:
            _hinting = QFont.HintingPreference.PreferVerticalHinting
        _family = _pick_family()
        app.setFont(font(BASE_SIZE))
    app.setPalette(_palette())
    app.setStyleSheet(_qss())
    class_fonts(app)
    if first:
        changes().changed.emit()


def class_fonts(app: QApplication) -> None:
    """Windows gives lists, trees, tables, menus and tooltips fonts of their own (Segoe UI 9), which
    the app's font does not replace and which come back with every style sheet change: they get the
    app's typeface too."""
    for name in ("QAbstractItemView", "QMenu", "QMenuBar", "QMessageBox"):
        app.setFont(font(BASE_SIZE), name)
    app.setFont(font(BASE_SIZE - 0.5), "QTipLabel")
