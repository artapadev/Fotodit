"""App theme: Apple's Liquid Glass design language in a light and a dark variant.

The colors are named tokens (ACCENT, TEXT, MUTED …) with a value for each variant. The app is
black, white and grey: the "accent" is the ink (near black in light, near white in dark), and
only warnings and errors keep a color. Text passes WCAG AA (4.5:1) on the
window, on panels and on the grey selection. Code reads them from this module when it paints or
builds a text (`theme.TEXT`), so after a change they take effect on the next repaint; widgets
that keep a color (a style sheet of their own, a cached QColor, an icon) listen to
`changes().changed`. `apply()` sets Qt's palette and style sheet and tells Windows the color
scheme, so window title bars turn dark too (open windows get their title bar redrawn, see
`winutil.redraw_frame`). "Windows ayarına uy" follows the Windows setting (Settings →
Personalization → Colors), also while the app is running.

The structure: the window is the content layer (WINDOW); sidebars, toolbars and the
inspector float above it as glass (see `glass`). Controls are capsules and rounded
rectangles whose corners are concentric with the surface they sit on. The large preview
has its own dark look in both variants (photos are judged on a dark background).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle

from ..i18n import t

SYSTEM, LIGHT, DARK = "system", "light", "dark"
MODES = (SYSTEM, LIGHT, DARK)


def mode_label(mode: str) -> str:
    """A variant's name in the Görünüm menu."""
    return t(f"theme.mode.{mode}")

# Corner radii (logical px). Inner corners are concentric: inner radius = outer radius − padding.
RADIUS_PANEL = 18  # glass sidebar and inspector
RADIUS_CARD = 12  # groups inside a panel (panel radius 18 − padding 6)
RADIUS_CONTROL = 8  # text fields, pop-up buttons, list rows
BASE_SIZE = 9.5  # pt: the body text (Inter; its letters are larger than Segoe UI's at the same size)

_LIGHT = {
    "WINDOW": "#F2F2F2",  # the content layer: window and content background
    "BG": "#F2F2F2",  # dialogs
    "PANEL": "#FFFFFF",  # opaque surfaces: menus, list popups, cards
    "ACCENT": "#161616",  # the ink: primary button, switches, checks, rings (no color: black, white, grey)
    "ON_ACCENT": "#FFFFFF",  # text and marks drawn on the ink
    "ACCENT_HOVER": "#2B2B2B",
    "ACCENT_PRESSED": "#3D3D3D",
    "ACCENT_SOFT": "#DDDDDD",  # selected rows and tiles: a grey fill
    "ACCENT_TEXT": "#161616",  # links (underlined) and emphasized text
    "SELECTION": "#C9C9C9",  # selected text in fields
    "BORDER": "#CFCFCF",
    "SEPARATOR": "#E0E0E0",
    "TEXT": "#161616",
    "MUTED": "#5E5E5E",
    "WARN": "#9E4700",  # warnings and errors keep a color
    "DANGER": "#C00012",
    "SUCCESS": "#3A3A3A",
    "EDIT": "#3A3A3A",  # edited photos and videos
    "FILL": "#14000000",  # control fill on glass (8% black)
    "HOVER": "#0F000000",
    "PRESSED": "#1F000000",
    "DISABLED": "#A6A6A6",
    "DISABLED_BG": "#0A000000",
    "PRIMARY_DISABLED": "#E0E0E0",
    "PRIMARY_DISABLED_TEXT": "#696969",
    "INPUT": "#FFFFFF",
    "INPUT_DISABLED": "#EEEEEE",
    "INPUT_DISABLED_TEXT": "#9E9E9E",
    "ITEM_HOVER": "#0D000000",
    "GRID": "#E6E6E6",
    "HEADER": "#F2F2F2",
    "TRACK": "#D6D6D6",  # slider and progress tracks
    "TOOLTIP": "#2A2A2A",
    "TOOLTIP_TEXT": "#F2F2F2",
    "PLACEHOLDER": "#696969",
    "ALT_BASE": "#F8F8F8",
    "CHECK_BORDER": "#8A8A8A",
    "CHECK_BORDER_OFF": "#C7C7C7",
    "CHECK_OFF": "#F4F4F4",
    "CHECK_ON_OFF": "#BDBDBD",
    "SWITCH_OFF": "#D1D1D1",
    "TILE_FOCUS": "#C7C7C7",
    "TILE_EMPTY": "#E6E6E6",  # a thumbnail that is not ready yet
    "BADGE_RING": "#FFFFFF",
    "BADGE_RING_HOVER": "#FFFFFF",
    "BADGE_FILL": "#33000000",
    "BADGE_FILL_HOVER": "#59000000",
    "FOLDER": "#BDBDBD",  # folder icons: grey
    "FOLDER_TAB": "#A3A3A3",
    # Fusion's bevels and scroll bars
    "LIGHT": "#FFFFFF",
    "MIDLIGHT": "#F2F2F2",
    "MID": "#C7C7C7",
    "DARK": "#A3A3A3",
    "SHADOW": "#7D7D7D",
}

_DARK = {
    "WINDOW": "#141414",
    "BG": "#1A1A1A",
    "PANEL": "#262626",
    "ACCENT": "#F2F2F2",
    "ON_ACCENT": "#141414",
    "ACCENT_HOVER": "#FFFFFF",
    "ACCENT_PRESSED": "#D4D4D4",
    "ACCENT_SOFT": "#383838",
    "ACCENT_TEXT": "#F2F2F2",
    "SELECTION": "#4D4D4D",
    "BORDER": "#4A4A4A",
    "SEPARATOR": "#353535",
    "TEXT": "#F2F2F2",
    "MUTED": "#A8A8A8",
    "WARN": "#FF9F0A",
    "DANGER": "#FF7B73",  # 4.6:1 even on a selected row
    "SUCCESS": "#D1D1D1",
    "EDIT": "#D1D1D1",
    "FILL": "#1FFFFFFF",  # 12% white
    "HOVER": "#14FFFFFF",
    "PRESSED": "#26FFFFFF",
    "DISABLED": "#666666",
    "DISABLED_BG": "#0DFFFFFF",
    "PRIMARY_DISABLED": "#3A3A3A",
    "PRIMARY_DISABLED_TEXT": "#A8A8A8",
    "INPUT": "#1C1C1C",
    "INPUT_DISABLED": "#222222",
    "INPUT_DISABLED_TEXT": "#666666",
    "ITEM_HOVER": "#12FFFFFF",
    "GRID": "#2C2C2C",
    "HEADER": "#141414",
    "TRACK": "#3D3D3D",
    "TOOLTIP": "#3A3A3A",
    "TOOLTIP_TEXT": "#F2F2F2",
    "PLACEHOLDER": "#9A9A9A",
    "ALT_BASE": "#1C1C1C",
    "CHECK_BORDER": "#8E8E8E",
    "CHECK_BORDER_OFF": "#4A4A4A",
    "CHECK_OFF": "#2C2C2C",
    "CHECK_ON_OFF": "#5C5C5C",
    "SWITCH_OFF": "#5C5C5C",
    "TILE_FOCUS": "#4A4A4A",
    "TILE_EMPTY": "#2C2C2C",
    "BADGE_RING": "#FFFFFF",
    "BADGE_RING_HOVER": "#FFFFFF",
    "BADGE_FILL": "#40000000",
    "BADGE_FILL_HOVER": "#66000000",
    "FOLDER": "#8C8C8C",
    "FOLDER_TAB": "#6E6E6E",
    "LIGHT": "#3A3A3A",
    "MIDLIGHT": "#333333",
    "MID": "#2A2A2A",
    "DARK": "#141414",
    "SHADOW": "#0A0A0A",
}

# The tokens as module attributes (filled in by `_use`, light until `apply` is called).
WINDOW = BG = PANEL = ACCENT = ON_ACCENT = ACCENT_HOVER = ACCENT_PRESSED = ACCENT_SOFT = ACCENT_TEXT = ""
SELECTION = BORDER = SEPARATOR = ""
TEXT = MUTED = WARN = DANGER = SUCCESS = EDIT = FILL = HOVER = PRESSED = DISABLED = DISABLED_BG = ""
PRIMARY_DISABLED = PRIMARY_DISABLED_TEXT = INPUT = INPUT_DISABLED = INPUT_DISABLED_TEXT = ITEM_HOVER = GRID = ""
HEADER = TRACK = TOOLTIP = TOOLTIP_TEXT = PLACEHOLDER = ALT_BASE = CHECK_BORDER = CHECK_BORDER_OFF = CHECK_OFF = ""
CHECK_ON_OFF = SWITCH_OFF = TILE_FOCUS = TILE_EMPTY = BADGE_RING = BADGE_RING_HOVER = BADGE_FILL = ""
BADGE_FILL_HOVER = FOLDER = FOLDER_TAB = LIGHT_ROLE = MIDLIGHT = MID = DARK_ROLE = SHADOW = ""

_mode = SYSTEM  # what the user chose
_dark = False  # what is shown
_following = False  # connected to the Windows setting
_style: QProxyStyle | None = None  # the app's style, once set (app.style() does not return the Python object)
_family = "Segoe UI"
_hinting = QFont.HintingPreference.PreferDefaultHinting


def _use(dark: bool) -> None:
    global _dark
    _dark = dark
    colors = dict(_DARK if dark else _LIGHT)
    # LIGHT/DARK would clash with the mode names above.
    colors["LIGHT_ROLE"], colors["DARK_ROLE"] = colors.pop("LIGHT"), colors.pop("DARK")
    globals().update(colors)


_use(False)


def color(token: str) -> QColor:
    """A token as a QColor (tokens may carry alpha: #AARRGGBB)."""
    return QColor(token)


class _Changes(QObject):
    changed = Signal()


_changes: _Changes | None = None


def changes() -> _Changes:
    """Emits `changed` after the theme changed (for widgets that keep colors)."""
    global _changes
    if _changes is None:
        _changes = _Changes()
    return _changes


def mode() -> str:
    return _mode


def is_dark() -> bool:
    return _dark


def family() -> str:
    """The UI font: the bundled Inter (see `fonts`), else Segoe UI Variable or Segoe UI."""
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
    timer, a counter, the dates of a list), so they do not shift. Not for all text: Inter's
    tabular forms widen hyphens too, which spreads file names apart ("C - - Users")."""
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
    c = {name: _rgba(value) for name, value in globals().items()
         if name.isupper() and isinstance(value, str) and value.startswith("#")}
    return f"""
QMainWindow, QDialog {{ background: {c['WINDOW']}; }}
QWidget#Glass, QWidget#Glass QWidget#GlassBody {{ background: transparent; }}
QLabel {{ color: {c['TEXT']}; }}
QLabel#CardTitle {{ font-size: 10pt; font-weight: 700; color: {c['TEXT']}; }}
QLabel#SectionTitle {{ font-size: 8.5pt; font-weight: 700; color: {c['MUTED']}; }}
QLabel#Muted {{ color: {c['MUTED']}; }}
QLabel#Hint {{ color: {c['MUTED']}; font-size: 8.5pt; }}
QLabel#Warn {{ color: {c['WARN']}; }}
QLabel#Example {{ color: {c['MUTED']}; font-size: 8.5pt; }}
QLabel#BarText {{ color: {c['TEXT']}; }}
QLabel#EmptyTitle {{ color: {c['TEXT']}; font-size: 12.5pt; font-weight: 700; }}
QLabel#Empty {{ color: {c['MUTED']}; }}
QLineEdit#GlassField {{ background: transparent; border: none; padding: 0 2px; }}
QFrame#Group {{ background: {c['FILL']}; border: none; border-radius: {RADIUS_CARD}px; }}
QFrame#GroupLine {{ background: {c['SEPARATOR']}; border: none; }}
QSplitter#Sidebar::handle {{ background: transparent; }}
QPushButton {{
    background: {c['FILL']}; color: {c['TEXT']}; border: none; border-radius: {RADIUS_CONTROL}px;
    padding: 5px 14px; min-height: 20px; font-weight: 700;
}}
QPushButton:hover {{ background: {c['PRESSED']}; }}
QPushButton:pressed {{ background: {c['PRESSED']}; }}
QPushButton:disabled {{ color: {c['DISABLED']}; background: {c['DISABLED_BG']}; }}
QPushButton:checked {{ background: {c['ACCENT_SOFT']}; color: {c['TEXT']}; }}
QPushButton#Primary {{
    background: {c['ACCENT']}; color: {c['ON_ACCENT']}; font-weight: 700; font-size: 10pt;
    padding: 10px 18px; border-radius: 19px;
}}
QPushButton#Primary:hover {{ background: {c['ACCENT_HOVER']}; }}
QPushButton#Primary:pressed {{ background: {c['ACCENT_PRESSED']}; }}
QPushButton#Primary:disabled {{ background: {c['PRIMARY_DISABLED']}; color: {c['PRIMARY_DISABLED_TEXT']}; }}
QPushButton#Accent {{ color: {c['ACCENT_TEXT']}; font-weight: 700; }}
QPushButton#Accent:disabled {{ color: {c['DISABLED']}; }}
QPushButton#Link {{ background: transparent; color: {c['ACCENT_TEXT']}; padding: 2px 4px; text-decoration: underline; }}
QToolButton {{ color: {c['TEXT']}; background: transparent; border: none; border-radius: 15px; padding: 5px; }}
QToolButton:hover {{ background: {c['HOVER']}; }}
QToolButton:pressed {{ background: {c['PRESSED']}; }}
QToolButton:disabled {{ background: transparent; }}
QCheckBox, QRadioButton {{ spacing: 8px; color: {c['TEXT']}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {c['DISABLED']}; }}
QCheckBox#Section {{ font-weight: 700; font-size: 10pt; }}
QLineEdit, QComboBox, QSpinBox {{
    padding: 4px 9px; border: 1px solid {c['BORDER']}; border-radius: {RADIUS_CONTROL}px;
    background: {c['INPUT']}; color: {c['TEXT']}; min-height: 20px;
    selection-background-color: {c['SELECTION']}; selection-color: {c['TEXT']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {c['ACCENT']}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    background: {c['INPUT_DISABLED']}; color: {c['INPUT_DISABLED_TEXT']}; border-color: {c['SEPARATOR']};
}}
QLineEdit#PathField {{ border-radius: 15px; padding: 4px 12px; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 16px; }}
QComboBox QAbstractItemView {{
    background: {c['PANEL']}; color: {c['TEXT']}; border: 1px solid {c['SEPARATOR']}; padding: 4px;
    selection-background-color: {c['ACCENT_SOFT']}; selection-color: {c['TEXT']}; outline: 0;
}}
QListView, QTreeView, QTableView {{ background: {c['WINDOW']}; color: {c['TEXT']}; border: none; outline: 0; }}
QTreeView#Sidebar, QListWidget#Sidebar, QScrollArea#Sidebar, QWidget#Sidebar {{ background: transparent; }}
QListWidget#Sidebar::item {{ padding: 4px 4px; border-radius: 6px; }}
QListWidget#Sidebar::item:selected {{ background: {c['ACCENT_SOFT']}; color: {c['TEXT']}; }}
QListWidget#Sidebar::item:hover:!selected {{ background: {c['HOVER']}; }}
QTreeView#Sidebar {{ show-decoration-selected: 0; }}
QTreeView#Sidebar::item {{ padding: 4px 4px; }}
QTreeView#Sidebar::item:selected, QTreeView#Sidebar::item:hover {{ background: transparent; color: {c['TEXT']}; }}
QTableView {{ gridline-color: {c['GRID']}; selection-background-color: {c['ACCENT_SOFT']}; selection-color: {c['TEXT']}; }}
QHeaderView {{ background: transparent; font-weight: 700; font-size: 8.5pt; }}
QHeaderView::section {{
    background: transparent; border: none; border-bottom: 1px solid {c['SEPARATOR']}; padding: 6px 8px;
    color: {c['MUTED']}; font-weight: 700; font-size: 8.5pt;
}}
QProgressBar {{
    border: none; border-radius: 4px; background: {c['TRACK']}; color: transparent; max-height: 8px;
}}
QProgressBar::chunk {{ background: {c['ACCENT']}; border-radius: 4px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QSplitter::handle {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px 1px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 1px 2px; }}
QScrollBar::handle:vertical {{ background: {c['MID']}; border-radius: 4px; min-height: 36px; margin: 0 2px; }}
QScrollBar::handle:horizontal {{ background: {c['MID']}; border-radius: 4px; min-width: 36px; margin: 2px 0; }}
QScrollBar::handle:hover {{ background: {c['MUTED']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{
    background: {c['TOOLTIP']}; color: {c['TOOLTIP_TEXT']}; border: none; padding: 6px 9px; border-radius: 7px;
}}
QMenu {{ background: {c['PANEL']}; color: {c['TEXT']}; border: 1px solid {c['SEPARATOR']}; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c['ACCENT_SOFT']}; color: {c['TEXT']}; }}
QMenu::item:disabled {{ color: {c['DISABLED']}; }}
QMenu::separator {{ height: 1px; background: {c['SEPARATOR']}; margin: 4px 10px; }}
QMenu::icon {{ padding-left: 8px; }}
QMessageBox QLabel {{ color: {c['TEXT']}; }}
QPlainTextEdit {{ background: {c['PANEL']}; color: {c['TEXT']}; border: 1px solid {c['SEPARATOR']};
    border-radius: {RADIUS_CONTROL}px; padding: 6px; }}
"""


def _palette() -> QPalette:
    roles = QPalette.ColorRole
    palette = QPalette()
    for role, value in (
        (roles.Window, WINDOW), (roles.WindowText, TEXT), (roles.Base, PANEL), (roles.AlternateBase, ALT_BASE),
        (roles.Text, TEXT), (roles.Button, PANEL), (roles.ButtonText, TEXT), (roles.BrightText, "#FFFFFF"),
        (roles.Highlight, SELECTION), (roles.HighlightedText, TEXT), (roles.PlaceholderText, PLACEHOLDER),
        (roles.ToolTipBase, TOOLTIP), (roles.ToolTipText, TOOLTIP_TEXT), (roles.Link, ACCENT_TEXT),
        (roles.LinkVisited, ACCENT_TEXT), (roles.Light, LIGHT_ROLE), (roles.Midlight, MIDLIGHT),
        (roles.Mid, MID), (roles.Dark, DARK_ROLE), (roles.Shadow, SHADOW),
    ):
        palette.setColor(role, QColor(value))
    disabled = QPalette.ColorGroup.Disabled
    for role, value in ((roles.WindowText, DISABLED), (roles.Text, DISABLED), (roles.ButtonText, DISABLED),
                        (roles.Base, INPUT_DISABLED), (roles.Button, INPUT_DISABLED),
                        (roles.Highlight, CHECK_ON_OFF)):
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
        enabled, on, partial, hover = self._state(option)
        side = min(option.rect.width(), option.rect.height()) - 1
        box = QRectF(option.rect.x() + 0.5, option.rect.center().y() - side / 2 + 0.5, side, side)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if on or partial:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(ACCENT if enabled else CHECK_ON_OFF))
            painter.drawRoundedRect(box, 4.5, 4.5)
            pen = QPen(QColor(ON_ACCENT if enabled else "#FFFFFF"), max(1.7, side / 8))
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
            border = ACCENT if (hover and enabled) else (CHECK_BORDER if enabled else CHECK_BORDER_OFF)
            painter.setPen(QPen(QColor(border), 1.2))
            painter.setBrush(QColor(INPUT if enabled else CHECK_OFF))
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
            painter.setBrush(QColor(ON_ACCENT if enabled else "#FFFFFF"))
            painter.drawEllipse(c, side * 0.19, side * 0.19)
        else:
            border = ACCENT if (hover and enabled) else (CHECK_BORDER if enabled else CHECK_BORDER_OFF)
            painter.setPen(QPen(QColor(border), 1.2))
            painter.setBrush(QColor(INPUT if enabled else CHECK_OFF))
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
        pen = QPen(QColor(MUTED), 1.6)
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


def system_is_dark() -> bool:
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def _pick_family() -> str:
    from . import fonts

    if fonts.load():
        return fonts.FAMILY
    families = set(QFontDatabase.families())
    for name in ("Segoe UI Variable Text", "Segoe UI"):
        if name in families:
            return name
    return QFont().defaultFamily()


def apply(app: QApplication, choice: str = LIGHT) -> None:
    """Shows the app in the chosen variant: LIGHT, DARK or SYSTEM (as Windows is set)."""
    global _mode, _following, _style, _family, _hinting
    _mode = choice if choice in MODES else SYSTEM
    hints = QGuiApplication.styleHints()
    if _mode == SYSTEM:
        hints.unsetColorScheme()  # the window frames follow Windows again
        dark = system_is_dark()
    else:
        dark = _mode == DARK
        hints.setColorScheme(Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light)
    if not _following:
        _following = True
        hints.colorSchemeChanged.connect(_system_changed)
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
    changed = first or dark != _dark
    _use(dark)
    app.setPalette(_palette())
    app.setStyleSheet(_qss())
    class_fonts(app)
    if changed:
        changes().changed.emit()
    if changed and not first:
        # Qt sets the new frame color on its own schedule: the title bars are redrawn shortly after, twice.
        for delay in (80, 400):
            QTimer.singleShot(delay, _redraw_frames)


def class_fonts(app: QApplication) -> None:
    """Windows gives lists, trees, tables, menus and tooltips fonts of their own (Segoe UI 9), which
    the app's font does not replace and which come back with every style sheet and theme change:
    they get the app's typeface too."""
    for name in ("QAbstractItemView", "QMenu", "QMenuBar", "QMessageBox"):
        app.setFont(font(BASE_SIZE), name)
    app.setFont(font(BASE_SIZE - 0.5), "QTipLabel")


def _redraw_frames() -> None:
    from ..winutil import redraw_frame

    if QGuiApplication.platformName() != "windows":  # e.g. the tests' offscreen windows have no real frame
        return
    for widget in QApplication.topLevelWidgets():
        if widget.isVisible() and widget.testAttribute(Qt.WidgetAttribute.WA_WState_Created):
            redraw_frame(int(widget.winId()))


def _system_changed(_scheme) -> None:
    app = QApplication.instance()
    if _mode == SYSTEM and app is not None and system_is_dark() != _dark:
        apply(app, SYSTEM)
