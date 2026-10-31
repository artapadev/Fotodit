"""Crop & straighten tool (in the large preview).

`CropSession` handles mouse editing of the crop box independently of Qt: the box always
stays inside the image (inside the rotated image when straightening) and its aspect ratio
can be locked. At the edge the box stops moving outward and keeps sliding along the
edge instead. Because the valid box positions form a convex set (each corner condition
is linear), the edge is found by binary search.

Coordinates are canvas coordinates (the image after the 90° rotation; see
`fotodit.core.edits`). The image itself stays unchanged; the edit is applied when painting.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QTransform,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout

from ..core.edits import Box, Edit, box_fits, largest_box, shrink_to_fit
from ..i18n import decimal, t
from . import glass, theme
from .controls import SILVER, BarButton, PillButton, Slider
from .widgets import ElidedComboBox, ElidedLabel

ORIGINAL = "original"
ASPECTS: list[tuple[str, float | str | None]] = [
    ("aspect.free", None),
    ("aspect.original", ORIGINAL),
    ("aspect.square", 1.0),
    ("4:3", 4 / 3),
    ("3:4", 3 / 4),
    ("3:2", 3 / 2),
    ("2:3", 2 / 3),
    ("16:9", 16 / 9),
    ("9:16", 9 / 16),
    ("5:4", 5 / 4),
    ("4:5", 4 / 5),
]
CORNERS = ("tl", "tr", "bl", "br")
CURSORS = {
    "tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
    "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
    "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
    "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
    "move": Qt.CursorShape.SizeAllCursor,
}


# ------------------------------------------------------------ Qt geometry


def quarter_transform(quarter: int, width: float, height: float) -> QTransform:
    """From displayed (width×height) coordinates to the canvas rotated clockwise by 90°·quarter."""
    if quarter == 1:
        return QTransform(0, 1, -1, 0, height, 0)
    if quarter == 2:
        return QTransform(-1, 0, 0, -1, width, height)
    if quarter == 3:
        return QTransform(0, -1, 1, 0, 0, width)
    return QTransform()


def upright_to_canvas(edit: Edit, width: float, height: float) -> QTransform:
    """From displayed coordinates to the canvas: the horizontal flip, the 90° rotation, then the tilt
    around the center."""
    t = quarter_transform(edit.quarter, width, height)
    if edit.mirror:
        t = QTransform(-1, 0, 0, 1, width, 0) * t
    if edit.angle:
        cw, ch = edit.canvas_size(width, height)
        rotation = QTransform()
        rotation.rotate(edit.angle)  # with the y axis pointing down, a positive angle is clockwise
        t = t * QTransform.fromTranslate(-cw / 2, -ch / 2) * rotation * QTransform.fromTranslate(cw / 2, ch / 2)
    return t


def edited_image(img: QImage, edit: Edit) -> QImage:
    """Applies the edit to a (small) image, for instant display in the UI.

    The math is done in physical pixels. On high-DPI screens the thumbnail's device pixel
    ratio (e.g. 1.25) is reset; otherwise QPainter draws the image at its logical size
    (scaled down) and the straightened thumbnail ends up shifted. The returned image has
    a ratio of 1."""
    img = QImage(img)
    img.setDevicePixelRatio(1.0)
    w, h = img.width(), img.height()
    left, top, ow, oh = edit.pixels_on_canvas(*edit.canvas_size(w, h))
    if not edit.angle:
        if edit.mirror:
            img = img.flipped(Qt.Orientation.Horizontal)
        if edit.quarter:
            turn = QTransform()
            turn.rotate(90 * edit.quarter)
            img = img.transformed(turn)
        return img.copy(int(left), int(top), ow, oh)
    out = QImage(ow, oh, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.setTransform(upright_to_canvas(edit, w, h) * QTransform.fromTranslate(-left, -top))
    p.drawImage(QPointF(0, 0), img)
    p.end()
    return out


# ------------------------------------------------------------ session


def _rotate_box(box: Box, height: float) -> Box:
    """90° clockwise: (x, y) → (height − y, x)."""
    x0, y0, x1, y1 = box
    return height - y1, x0, height - y0, x1


class CropSession:
    """Interactive editing of the crop box. `width`×`height`: the photo's displayed size
    (the unit does not matter; the box is converted to the 0–1 range in the output)."""

    def __init__(self, width: float, height: float, edit: Edit | None = None):
        edit = edit or Edit()
        self.base = edit  # its exposure / trim are kept as they are
        self.width, self.height = float(width), float(height)
        self.quarter = edit.quarter
        self.angle = edit.angle
        self.aspect: float | None = None
        self.auto = edit.crop is None  # automatic box: the whole image / the largest box when tilted
        self.box: Box = edit.crop_box(self.width, self.height)
        self._user: Box = self.box  # the box the user left (grows back when the tilt is reduced)
        self._drag: tuple[str, Box, float, float] | None = None

    # ------------------------------------------------------------ queries

    @property
    def canvas(self) -> tuple[float, float]:
        return (self.height, self.width) if self.quarter % 2 else (self.width, self.height)

    @property
    def dragging(self) -> bool:
        return self._drag is not None

    def min_size(self) -> float:
        return max(1e-6, min(self.canvas) * 0.02)

    def edit(self) -> Edit:
        cw, ch = self.canvas
        x0, y0, x1, y1 = self.box
        crop = None if self.auto else (x0 / cw, y0 / ch, x1 / cw, y1 / ch)
        return replace(self.base, quarter=self.quarter, angle=self.angle, crop=crop)

    def fits(self, box: Box, eps: float = 1e-6) -> bool:
        cw, ch = self.canvas
        m = self.min_size() * 0.999
        return box[2] - box[0] >= m and box[3] - box[1] >= m and box_fits(cw, ch, self.angle, box, eps)

    def hit(self, x: float, y: float, tol: float) -> str | None:
        """The handle at (x, y): a corner, an edge, "move" (inside the box) or None."""
        x0, y0, x1, y1 = self.box
        near_l, near_r = abs(x - x0) <= tol, abs(x - x1) <= tol
        near_t, near_b = abs(y - y0) <= tol, abs(y - y1) <= tol
        for name, ok in (("tl", near_t and near_l), ("tr", near_t and near_r),
                         ("bl", near_b and near_l), ("br", near_b and near_r)):
            if ok:
                return name
        inside_x, inside_y = x0 - tol < x < x1 + tol, y0 - tol < y < y1 + tol
        for name, ok in (("t", near_t and inside_x), ("b", near_b and inside_x),
                         ("l", near_l and inside_y), ("r", near_r and inside_y)):
            if ok:
                return name
        if x0 < x < x1 and y0 < y < y1:
            return "move"
        return None

    # ---------------------------------------------------------- changes

    def _refit(self) -> None:
        cw, ch = self.canvas
        if self.auto:
            self.box = self._user = Edit(self.quarter, self.angle).box_on_canvas(cw, ch)
        elif box_fits(cw, ch, self.angle, self._user):
            self.box = self._user
        else:
            self.box = shrink_to_fit(cw, ch, self.angle, self._user)

    def set_angle(self, angle: float) -> None:
        self.angle = Edit(angle=angle).angle
        self._refit()

    def rotate(self, delta: int) -> None:
        """90° rotation; the box turns with the image, and so does a locked ratio (3:2 → 2:3)."""
        for _ in range(delta % 4):
            ch = self.canvas[1]
            self.box = _rotate_box(self.box, ch)
            self._user = _rotate_box(self._user, ch)
            self.quarter = (self.quarter + 1) % 4
            if self.aspect:
                self.aspect = 1 / self.aspect
        self._refit()

    def mirror(self) -> None:
        """Horizontal flip: the box and the tilt flip with the image (see `Edit.mirrored`)."""
        cw = self.canvas[0]

        def flip(box: Box) -> Box:
            return cw - box[2], box[1], cw - box[0], box[3]

        self.box = flip(self.box)
        self._user = flip(self._user)
        self.angle = -self.angle
        self.quarter = (-self.quarter) % 4
        self.base = replace(self.base, mirror=not self.base.mirror)
        self._refit()

    def aspect_value(self, choice) -> float | None:
        if choice == ORIGINAL:
            cw, ch = self.canvas
            return cw / ch
        return choice

    def set_aspect(self, aspect: float | None) -> None:
        """Locks the ratio: the largest box with that ratio, as close to the old box as possible."""
        self.aspect = aspect
        if aspect is None:
            return
        cw, ch = self.canvas
        x0, y0, x1, y1 = self.box
        box = largest_box(cw, ch, self.angle, aspect)
        dx = (x0 + x1 - box[0] - box[2]) / 2
        dy = (y0 + y1 - box[1] - box[3]) / 2
        self.box = self._user = self._slide(box, dx, dy)
        self.auto = False

    def reset(self) -> None:
        """Removes the crop and the straightening (the 90° rotation stays)."""
        self.angle = 0.0
        self.aspect = None
        self.auto = True
        self._refit()

    def begin(self, handle: str, x: float, y: float) -> None:
        self._drag = (handle, self.box, x, y)

    def drag_to(self, x: float, y: float) -> None:
        if self._drag is None:
            return
        handle, start, sx, sy = self._drag
        if handle == "move":
            box = self._slide(start, x - sx, y - sy)
        elif handle in CORNERS:
            box = self._resize_corner(start, handle, x, y)
        else:
            box = self._resize_edge(start, handle, x, y)
        self.box = self._user = box
        self.auto = False

    def end(self) -> None:
        self._drag = None

    # ------------------------------------------------------------ search

    def _best(self, make: Callable[[float], Box], start: Box) -> tuple[Box, float]:
        """The largest t ∈ [0, 1] for which make(t) is valid, and that box; if none is valid,
        `start` (t = 0). The search uses a much tighter tolerance than acceptance does, so
        boxes resting against the edge do not spill over through rounding errors in later steps."""
        box = make(1.0)
        if self.fits(box, 1e-9):
            return box, 1.0
        lo, hi, best = 0.0, 1.0, start
        for _ in range(40):
            mid = (lo + hi) / 2
            box = make(mid)
            if self.fits(box, 1e-9):
                lo, best = mid, box
            else:
                hi = mid
        return best, lo

    def _slide(self, start: Box, dx: float, dy: float) -> Box:
        def shifted(base: Box, ux: float, uy: float) -> Callable[[float], Box]:
            return lambda t: (base[0] + ux * t, base[1] + uy * t, base[2] + ux * t, base[3] + uy * t)

        box, t = self._best(shifted(start, dx, dy), start)
        if t < 1.0:  # hit the edge: the rest of the move continues along one axis (sliding along the edge)
            for ux, uy in ((dx * (1 - t), 0.0), (0.0, dy * (1 - t))):
                if ux or uy:
                    box, _ = self._best(shifted(box, ux, uy), box)
        return box

    def _resize_corner(self, start: Box, handle: str, x: float, y: float) -> Box:
        x0, y0, x1, y1 = start
        left, top = "l" in handle, "t" in handle
        ax, ay = (x1 if left else x0), (y1 if top else y0)  # the opposite corner stays fixed
        sx, sy = (-1 if left else 1), (-1 if top else 1)
        m = self.min_size()
        w, h = max(m, (x - ax) * sx), max(m, (y - ay) * sy)
        if self.aspect:
            a = self.aspect
            w = max(w, h * a, m * a)  # the axis along which the mouse is farther out wins
            h = w / a

        def box_of(bw: float, bh: float) -> Box:
            xa, xb = sorted((ax, ax + sx * bw))
            ya, yb = sorted((ay, ay + sy * bh))
            return xa, ya, xb, yb

        cw_, ch_ = x1 - x0, y1 - y0
        box, t = self._best(lambda t: box_of(cw_ + (w - cw_) * t, ch_ + (h - ch_) * t), start)
        if not self.aspect and t < 1.0:  # free ratio: the rest of the move continues along one axis
            bw, bh = box[2] - box[0], box[3] - box[1]
            box, _ = self._best(lambda t: box_of(bw + (w - bw) * t, bh), box)
            bw = box[2] - box[0]
            box, _ = self._best(lambda t: box_of(bw, bh + (h - bh) * t), box)
        return box

    def _resize_edge(self, start: Box, handle: str, x: float, y: float) -> Box:
        x0, y0, x1, y1 = start
        m = self.min_size()
        a = self.aspect
        sw, sh = x1 - x0, y1 - y0
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if handle in ("l", "r"):
            w = max((x1 - x) if handle == "l" else (x - x0), m, m * a if a else m)
            h = w / a if a else sh

            def box_of(bw: float, bh: float) -> Box:
                ya, yb = (cy - bh / 2, cy + bh / 2) if a else (y0, y1)
                return (x1 - bw, ya, x1, yb) if handle == "l" else (x0, ya, x0 + bw, yb)
        else:
            h = max((y1 - y) if handle == "t" else (y - y0), m, m / a if a else m)
            w = h * a if a else sw

            def box_of(bw: float, bh: float) -> Box:
                xa, xb = (cx - bw / 2, cx + bw / 2) if a else (x0, x1)
                return (xa, y1 - bh, xb, y1) if handle == "t" else (xa, y0, xb, y0 + bh)

        return self._best(lambda t: box_of(sw + (w - sw) * t, sh + (h - sh) * t), start)[0]


# ------------------------------------------------------------------ painting


def paint_crop(p: QPainter, canvas: QRectF, box: QRectF, fine_grid: bool) -> None:
    """Darkens the area outside the box; draws the rule-of-thirds lines (a dense grid while
    straightening), the border and the handles."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    dim = QPainterPath()
    dim.setFillRule(Qt.FillRule.OddEvenFill)
    dim.addRect(canvas)
    dim.addRect(box)
    p.fillPath(dim, QColor(0, 0, 0, 155))
    if fine_grid:
        p.setPen(QPen(QColor(255, 255, 255, 80), 1))
        step = max(18.0, min(box.width(), box.height()) / 10)
        x = box.left() + step
        while x < box.right() - 1:
            p.drawLine(QPointF(x, box.top()), QPointF(x, box.bottom()))
            x += step
        y = box.top() + step
        while y < box.bottom() - 1:
            p.drawLine(QPointF(box.left(), y), QPointF(box.right(), y))
            y += step
    else:
        p.setPen(QPen(QColor(255, 255, 255, 105), 1))
        for i in (1, 2):
            x = box.left() + box.width() * i / 3
            y = box.top() + box.height() * i / 3
            p.drawLine(QPointF(x, box.top()), QPointF(x, box.bottom()))
            p.drawLine(QPointF(box.left(), y), QPointF(box.right(), y))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 235), 1.4))
    p.drawRect(box)
    pen = QPen(QColor("white"), 4)
    pen.setCapStyle(Qt.PenCapStyle.SquareCap)
    p.setPen(pen)
    arm = max(6.0, min(22.0, box.width() / 3, box.height() / 3))
    for x, y, dx, dy in ((box.left(), box.top(), 1, 1), (box.right(), box.top(), -1, 1),
                         (box.left(), box.bottom(), 1, -1), (box.right(), box.bottom(), -1, -1)):
        p.drawLine(QPointF(x, y), QPointF(x + dx * arm, y))
        p.drawLine(QPointF(x, y), QPointF(x, y + dy * arm))
    half = min(12.0, box.width() / 6, box.height() / 6)
    c = box.center()
    p.drawLine(QPointF(c.x() - half, box.top()), QPointF(c.x() + half, box.top()))
    p.drawLine(QPointF(c.x() - half, box.bottom()), QPointF(c.x() + half, box.bottom()))
    p.drawLine(QPointF(box.left(), c.y() - half), QPointF(box.left(), c.y() + half))
    p.drawLine(QPointF(box.right(), c.y() - half), QPointF(box.right(), c.y() + half))
    p.restore()


# ------------------------------------------------------------ toolbar


def angle_text(angle: float) -> str:
    """The straightening angle: "+2,5°", "−1,5°" (with the language's decimal mark, like `edits.ev_text`)."""
    if not round(angle, 1):
        return "0°"
    return f"{'+' if angle > 0 else '−'}{decimal(abs(angle))}°"


class CropBar(glass.GlassSurface):
    """The bottom toolbar in crop mode, Artapa's floating pill: aspect ratio, 90° rotation,
    straightening, reset, cancel and apply (the silver button)."""

    aspectChosen = Signal(int)  # index in ASPECTS
    angleChanged = Signal(float)
    straightening = Signal(bool)  # whether the straighten slider is being dragged
    rotateRequested = Signal(int)
    flipRequested = Signal()
    resetRequested = Signal()
    cancelRequested = Signal()
    applyRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, kind=glass.MEDIA, radius=22)
        self.setObjectName("Media")
        self.aspect = ElidedComboBox()
        for label, _ in ASPECTS:
            label = t(label) if label.startswith("aspect.") else label
            self.aspect.addItem(label)
        self.aspect.setToolTip(t("crop.aspect_tip"))
        self.aspect.setAccessibleName(t("crop.aspect"))
        self.aspect.currentIndexChanged.connect(self.aspectChosen)
        self.left_btn = BarButton("", "rotate.left", tooltip=t("common.rotate_left_key"),
                                  accessible=t("common.rotate_left"))
        self.left_btn.clicked.connect(lambda: self.rotateRequested.emit(-1))
        self.right_btn = BarButton("", "rotate.right", tooltip=t("common.rotate_right_key"),
                                   accessible=t("common.rotate_right"))
        self.right_btn.clicked.connect(lambda: self.rotateRequested.emit(1))
        self.flip_btn = BarButton("", "flip", tooltip=t("common.flip_key"), accessible=t("common.flip"))
        self.flip_btn.clicked.connect(self.flipRequested)
        self.slider = Slider(centered=True)
        self.slider.setRange(-450, 450)  # tenths of a degree
        self.slider.setSingleStep(1)
        self.slider.setPageStep(10)
        self.slider.setMinimumWidth(140)  # can shrink on small screens
        self.slider.setMaximumWidth(320)
        self.slider.setToolTip(t("crop.tilt_tip"))
        self.slider.setAccessibleName(t("crop.tilt"))
        self.slider.valueChanged.connect(self._slider_moved)
        self.slider.sliderPressed.connect(lambda: self.straightening.emit(True))
        self.slider.sliderReleased.connect(lambda: self.straightening.emit(False))
        self.angle_label = QLabel("0°")
        self.angle_label.setMinimumWidth(52)
        self.angle_label.setFont(theme.tabular(self.angle_label.font()))
        self.reset_btn = BarButton(t("common.reset"), "undo")
        self.reset_btn.setToolTip(t("crop.reset_tip"))
        self.reset_btn.clicked.connect(self.resetRequested)
        self.size_label = ElidedLabel()
        self.size_label.setObjectName("PvMuted")
        self.size_label.setFont(theme.tabular(self.size_label.font()))
        self.cancel_btn = BarButton(t("common.cancel"), None)
        self.cancel_btn.setToolTip(t("crop.cancel_tip"))
        self.cancel_btn.clicked.connect(self.cancelRequested)
        self.apply_btn = PillButton(t("common.apply"), "checkmark", kind=SILVER)
        self.apply_btn.setObjectName("PvApply")
        self.apply_btn.setToolTip(t("crop.apply_tip"))
        self.apply_btn.clicked.connect(self.applyRequested)
        for w in (self.aspect, self.left_btn, self.right_btn, self.flip_btn, self.reset_btn, self.cancel_btn,
                  self.apply_btn):
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        row = QHBoxLayout()
        row.setSpacing(6)
        ratio = QLabel(t("crop.aspect_label"))
        ratio.setObjectName("PvMuted")
        row.addWidget(ratio)
        row.addWidget(self.aspect)
        row.addSpacing(6)
        row.addWidget(self.left_btn)
        row.addWidget(self.right_btn)
        row.addWidget(self.flip_btn)
        row.addSpacing(10)
        tilt = QLabel(t("crop.tilt_label"))
        tilt.setObjectName("PvMuted")
        row.addWidget(tilt)
        row.addWidget(self.slider, 2)
        row.addWidget(self.angle_label)
        row.addWidget(self.reset_btn)
        row.addStretch(1)
        row.addWidget(self.size_label)
        row.addSpacing(6)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.apply_btn)
        hint = ElidedLabel(t("crop.hint"))
        hint.setObjectName("PvMuted")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 10, 9)
        layout.setSpacing(4)
        layout.addLayout(row)
        layout.addWidget(hint, 0, Qt.AlignmentFlag.AlignHCenter)

    def _slider_moved(self, value: int) -> None:
        angle = value / 10
        self.angle_label.setText(angle_text(angle))
        self.angleChanged.emit(angle)

    def set_values(self, aspect_index: int, angle: float) -> None:
        for w in (self.aspect, self.slider):
            w.blockSignals(True)
        self.aspect.setCurrentIndex(aspect_index)
        self.slider.setValue(int(round(angle * 10)))
        self.angle_label.setText(angle_text(angle))
        for w in (self.aspect, self.slider):
            w.blockSignals(False)
        self.slider.update()
