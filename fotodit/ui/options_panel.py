"""Right-hand panel (the inspector): the operations, rename/grouping options, the output location
and the start button.

The operations are rows with a switch, grouped on rounded sections as in Apple's settings; the
options of an operation sit under its switch and are enabled with it. The start button is the
view's one prominent (accent-tinted) glass button.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime

from PySide6.QtCore import QObject, QSettings, QStandardPaths, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.planner import (
    GROUP_DAY,
    GROUP_MONTH,
    GROUP_NONE,
    GROUP_SOURCE,
    GROUP_YEAR,
    NAME_DATE_NUMBER,
    NAME_DATE_TIME,
    NAME_NUMBER,
    SORT_DATE,
    SORT_MANUAL,
    SORT_MTIME,
    SORT_NAME,
    SORT_SIZE,
    JobOptions,
    OutputOptions,
    RenameOptions,
)
from ..core.processor import ContentOptions
from ..core.rawdev import RAW_AUTO, RAW_DEVELOP
from ..core.video import hwenc
from ..core.video.process import VIDEO_COMPATIBLE, VIDEO_KEEP
from ..i18n import month_name, percent, t
from ..winutil import safe_filename_part
from . import count_text, glass, theme
from .controls import GlassButton, Switch
from .widgets import ElidedComboBox, NumberBox

SIZES = [
    (3840, "options.size.3840"),
    (2560, "options.size.2560"),
    (2048, "options.size.2048"),
    (1920, "options.size.1920"),
    (1600, "options.size.1600"),
    (1280, "options.size.1280"),
    (1080, "options.size.1080"),
]
PATTERNS = [
    (NAME_NUMBER, "options.pattern.number"),
    (NAME_DATE_TIME, "options.pattern.date_time"),
    (NAME_DATE_NUMBER, "options.pattern.date_number"),
]
PATTERN_TIPS = {
    NAME_NUMBER: "options.pattern_tip.number",
    NAME_DATE_TIME: "options.pattern_tip.date_time",
    NAME_DATE_NUMBER: "options.pattern_tip.date_number",
}
KEEP_DATE_TIP = "options.keep_date_tip"
RESIZE_TIP = "options.resize_tip"
QUALITIES = [  # {p}: the quality as a percentage
    (95, "options.quality.95"),
    (90, "options.quality.90"),
    (85, "options.quality.85"),
    (75, "options.quality.75"),
]
SORTS = [
    (SORT_DATE, "options.sort.date"),
    (SORT_NAME, "options.sort.name"),
    (SORT_MTIME, "options.sort.mtime"),
    (SORT_SIZE, "options.sort.size"),
    (SORT_MANUAL, "options.sort.manual"),
]
SORT_TIPS = {
    SORT_DATE: "options.sort_tip.date",
    SORT_NAME: "options.sort_tip.name",
    SORT_MTIME: "options.sort_tip.mtime",
    SORT_SIZE: "options.sort_tip.size",
    SORT_MANUAL: "options.sort_tip.manual",
}
ORDER_LABELS = {
    SORT_DATE: ("options.order.old_new", "options.order.new_old"),
    SORT_MTIME: ("options.order.old_new", "options.order.new_old"),
    SORT_NAME: ("options.order.a_z", "options.order.z_a"),
    SORT_SIZE: ("options.order.small_large", "options.order.large_small"),
    SORT_MANUAL: ("options.order.list", "options.order.list"),
}
VIDEO_CODECS = [
    (VIDEO_COMPATIBLE, "options.codec.compatible"),
    (VIDEO_KEEP, "options.codec.keep"),
]
VIDEO_CODEC_TIPS = {
    VIDEO_COMPATIBLE: "options.codec_tip.compatible",
    VIDEO_KEEP: "options.codec_tip.keep",
}

PROFILE_TIP = "options.profile_tip"
HW_TIP = "options.hw_tip"
STRIP_HINT = "options.strip_hint"
STRIP_TIP = "options.strip_tip"


class _HardwareProbe(QObject):
    """Probes the GPU (hardware) encoders in the background (can take a second or two while the driver loads)."""

    done = Signal()

    def start(self) -> None:
        threading.Thread(target=self._run, name="gpu-encoder-probe", daemon=True).start()

    def _run(self) -> None:
        hwenc.find("h264")
        hwenc.find("hevc")
        try:
            self.done.emit()
        except RuntimeError:  # the window was closed in the meantime
            pass


RAW_MODES = [
    (RAW_AUTO, "options.raw.auto"),
    (RAW_DEVELOP, "options.raw.develop"),
]
GROUPS = [
    (GROUP_MONTH, "options.group.month"),
    (GROUP_YEAR, "options.group.year"),
    (GROUP_DAY, "options.group.day"),
    (GROUP_SOURCE, "options.group.source"),
]
GROUP_TIPS = {
    GROUP_SOURCE: "options.group_tip.source",
}


def default_output_root() -> str:
    pictures = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
    return os.path.join(os.path.normpath(pictures or os.path.expanduser("~")), "Fotodit")


def _tip(tips: dict[str, str], key: str) -> str:
    """The tooltip for a choice, if it has one."""
    return t(tips[key]) if key in tips else ""


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Hint")
    label.setWordWrap(True)
    return label


class _Group(QFrame):
    """A rounded section of rows (Apple's grouped settings); rows are separated by hairlines."""

    def __init__(self):
        super().__init__()
        self.setObjectName("Group")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(14, 6, 14, 10)
        self._lay.setSpacing(6)
        self._rows = 0

    def add_row(self, *widgets: QWidget, indent: int = 0) -> None:
        if self._rows:
            line = QFrame()
            line.setObjectName("GroupLine")
            line.setFixedHeight(1)
            self._lay.addSpacing(4)
            self._lay.addWidget(line)
            self._lay.addSpacing(2)
        for w in widgets:
            if indent:
                holder = QWidget()
                lay = QVBoxLayout(holder)
                lay.setContentsMargins(indent, 0, 0, 0)
                lay.setSpacing(6)
                lay.addWidget(w)
                w = holder
            self._lay.addWidget(w)
        self._rows += 1


def _title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("CardTitle")
    return label


def _pair(edit: QLineEdit, spin: QSpinBox) -> QWidget:
    """Rename row: prefix and start number side by side."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    lay.addWidget(edit, 1)
    first = QLabel(t("options.first_no"))
    first.setObjectName("Muted")
    lay.addWidget(first)
    spin.setMaximumWidth(84)
    lay.addWidget(spin)
    return row


def _combo() -> QComboBox:
    """Combo box whose long option texts don't widen the panel (the text is shown elided)."""
    combo = ElidedComboBox()
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(10)
    return combo


def _labeled(label: str, widget: QWidget) -> QWidget:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    text = QLabel(label)
    text.setObjectName("Muted")
    lay.addWidget(text)
    lay.addWidget(widget, 1)
    return row


class OptionsPanel(QWidget):
    changed = Signal()
    startRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self._auto_folder = True
        self._count = 0
        self._videos = 0
        self._folders = 0
        self._pending = 0
        self._edited = 0
        self._scanning = False

        ops = _Group()
        self.strip = Switch(t("options.strip"))
        self.strip.setObjectName("Section")
        self.strip.setToolTip(t(STRIP_TIP))
        self.keep_profile = QCheckBox(t("options.keep_profile"))  # the tooltip names the color spaces
        self.keep_profile.setToolTip(t(PROFILE_TIP))
        self.keep_date = QCheckBox(t("options.keep_date"))
        self.keep_date.setToolTip(t(KEEP_DATE_TIP))
        strip_hint = _hint(t(STRIP_HINT))
        strip_hint.setToolTip(t(STRIP_TIP))
        ops.add_row(self.strip, strip_hint)
        self._strip_box = QWidget()
        sb = QVBoxLayout(self._strip_box)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(4)
        sb.addWidget(self.keep_date)
        sb.addWidget(self.keep_profile)
        ops._lay.addWidget(self._strip_box)

        self.convert = Switch(t("options.convert"))
        self.convert.setObjectName("Section")
        self.quality = _combo()
        for value, label in QUALITIES:
            self.quality.addItem(t(label, p=percent(value)), value)
        self.raw_mode = _combo()
        for value, label in RAW_MODES:
            self.raw_mode.addItem(t(label), value)
        self.raw_mode.setToolTip(t("options.raw_tip"))
        self.raw_row = _labeled("RAW:", self.raw_mode)
        self.raw_row.hide()
        self.convert_box = QWidget()
        cb = QVBoxLayout(self.convert_box)
        cb.setContentsMargins(0, 0, 0, 0)
        cb.setSpacing(6)
        cb.addWidget(_labeled(t("options.quality_label"), self.quality))
        cb.addWidget(self.raw_row)
        self.convert_hint = _hint(t("options.convert_hint"))
        cb.addWidget(self.convert_hint)
        ops.add_row(self.convert, self.convert_box)

        self.shrink = Switch(t("options.shrink"))
        self.shrink.setObjectName("Section")
        self.shrink.setToolTip(t(RESIZE_TIP))
        self.max_side = _combo()
        for value, label in SIZES:
            self.max_side.addItem(t(label), value)
        self.max_side.setCurrentIndex(self.max_side.findData(2048))
        self.shrink_box = QWidget()
        kb = QVBoxLayout(self.shrink_box)
        kb.setContentsMargins(0, 0, 0, 0)
        kb.setSpacing(6)
        kb.addWidget(_labeled(t("options.long_side"), self.max_side))
        kb.addWidget(_hint(t("options.shrink_hint")))
        ops.add_row(self.shrink, self.shrink_box)

        self.video = Switch(t("options.video"))
        self.video.setObjectName("Section")
        self.video_codec = _combo()
        for value, label in VIDEO_CODECS:
            self.video_codec.addItem(t(label), value)
            self.video_codec.setItemData(self.video_codec.count() - 1, t(VIDEO_CODEC_TIPS[value]),
                                         Qt.ItemDataRole.ToolTipRole)
        self.video_box = QWidget()
        vb = QVBoxLayout(self.video_box)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(6)
        vb.addWidget(self.video_codec)
        self.video_hint = _hint("")
        vb.addWidget(self.video_hint)
        # Cropped/straightened videos are re-encoded even without conversion, so this option is independent of it.
        self.video_hw = QCheckBox(t("options.video_hw"))
        self.video_hw.setToolTip(t(HW_TIP))
        self.video_hw_hint = _hint("")
        self.video_hw_hint.hide()
        hw_box = QWidget()
        hb = QVBoxLayout(hw_box)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(2)
        hb.addWidget(self.video_hw)
        hb.addWidget(self.video_hw_hint)
        self._hw_probe: _HardwareProbe | None = None
        ops.add_row(self.video, self.video_box, hw_box)

        self.rename = Switch(t("options.rename"))
        self.rename.setObjectName("Section")
        self.pattern = _combo()
        for value, label in PATTERNS:
            self.pattern.addItem(t(label), value)
            self.pattern.setItemData(self.pattern.count() - 1, t(PATTERN_TIPS[value]), Qt.ItemDataRole.ToolTipRole)
        self.sort = _combo()
        for value, label in SORTS:
            self.sort.addItem(t(label), value)
            self.sort.setItemData(self.sort.count() - 1, t(SORT_TIPS[value]), Qt.ItemDataRole.ToolTipRole)
        self.order = _combo()
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText(t("options.prefix_placeholder"))
        self.prefix.setMaxLength(60)
        self.prefix.setToolTip(t("options.prefix_tip"))
        self.start = NumberBox()
        self.start.setRange(0, 999999)
        self.start.setValue(1)
        self.start.setToolTip(t("options.start_tip"))
        self.video_prefix = QLineEdit()
        self.video_prefix.setPlaceholderText(t("options.video_prefix_placeholder"))
        self.video_prefix.setMaxLength(60)
        self.video_prefix.setToolTip(t("options.video_prefix_tip"))
        self.video_start = NumberBox()
        self.video_start.setRange(0, 999999)
        self.video_start.setValue(1)
        self.video_start.setToolTip(t("options.video_start_tip"))
        self.pad = QCheckBox(t("options.pad"))
        self.example = QLabel()
        self.example.setObjectName("Example")
        self.example.setWordWrap(True)
        self.rename_box = QWidget()
        form = QFormLayout(self.rename_box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        rows = (
            (t("options.pattern_label"), self.pattern),
            (t("options.sort_label"), self.sort),
            (t("options.order_label"), self.order),
            (t("options.photos_label"), _pair(self.prefix, self.start)),
            (t("options.videos_label"), _pair(self.video_prefix, self.video_start)),
        )
        for label, widget in rows:
            text = QLabel(label)
            text.setObjectName("Muted")
            form.addRow(text, widget)
        form.addRow(self.pad)
        form.addRow(self.example)
        ops.add_row(self.rename, self.rename_box)

        self.group = Switch(t("options.group"))
        self.group.setObjectName("Section")
        self.group_mode = _combo()
        for value, label in GROUPS:
            self.group_mode.addItem(t(label, example=f"2024-05 {month_name(5)}"), value)
            if value in GROUP_TIPS:
                self.group_mode.setItemData(self.group_mode.count() - 1, t(GROUP_TIPS[value]),
                                            Qt.ItemDataRole.ToolTipRole)
        self.group_box = QWidget()
        gb = QVBoxLayout(self.group_box)
        gb.setContentsMargins(0, 0, 0, 0)
        gb.setSpacing(6)
        gb.addWidget(self.group_mode)
        gb.addWidget(_hint(t("options.group_hint")))
        ops.add_row(self.group, self.group_box)

        out = _Group()
        self.to_folder = QRadioButton(t("options.to_folder"))
        self.in_place = QRadioButton(t("options.in_place"))
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.to_folder)
        self.mode_group.addButton(self.in_place)
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        self.folder_edit.textChanged.connect(self.folder_edit.setToolTip)
        self.folder_btn = QPushButton(t("options.change"))
        self.folder_btn.clicked.connect(self._choose_folder)
        folder_row = QWidget()
        fr = QHBoxLayout(folder_row)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.setSpacing(6)
        fr.addWidget(self.folder_edit, 1)
        fr.addWidget(self.folder_btn)
        self.folder_box = QWidget()
        fb = QVBoxLayout(self.folder_box)
        fb.setContentsMargins(0, 0, 0, 0)
        fb.setSpacing(4)
        fb.addWidget(folder_row)
        fb.addWidget(_hint(t("options.folder_hint")))
        self.structure_hint = _hint("")
        self.structure_hint.setObjectName("Warn")
        self.structure_hint.setTextFormat(Qt.TextFormat.RichText)
        self.structure_hint.linkActivated.connect(self.keep_structure)
        self.structure_hint.hide()
        self._color_links()
        theme.changes().changed.connect(self._color_links)
        fb.addWidget(self.structure_hint)
        self.recycle = QCheckBox(t("options.recycle"))
        self.recycle.setToolTip(t("options.recycle_tip"))
        self.recycle.setChecked(True)
        self.in_place_warn = QLabel(t("options.in_place_warn"))
        self.in_place_warn.setObjectName("Warn")
        self.in_place_warn.setWordWrap(True)
        self.in_place_box = QWidget()
        ib = QVBoxLayout(self.in_place_box)
        ib.setContentsMargins(0, 0, 0, 0)
        ib.setSpacing(4)
        ib.addWidget(self.in_place_warn)
        ib.addWidget(self.recycle)
        out.add_row(self.to_folder, self.folder_box)
        out.add_row(self.in_place, self.in_place_box)

        self.start_btn = GlassButton("", "play.fill", kind=glass.PROMINENT)
        self.start_btn.setObjectName("Primary")
        self.start_btn.setMinimumHeight(46)
        self.start_btn.clicked.connect(self.startRequested)
        self.summary = QLabel()
        self.summary.setObjectName("Muted")
        self.summary.setWordWrap(True)
        self.summary.setAlignment(Qt.AlignmentFlag.AlignCenter)

        content = QWidget()
        content.setObjectName("Sidebar")
        col = QVBoxLayout(content)
        col.setContentsMargins(14, 14, 10, 8)
        col.setSpacing(8)
        col.addWidget(_title(t("options.title")))
        col.addWidget(ops)
        col.addSpacing(6)
        col.addWidget(_title(t("options.save_to")))
        col.addWidget(out)
        col.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("Sidebar")
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)

        foot = QWidget()
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(14, 6, 14, 12)
        fl.setSpacing(6)
        fl.addWidget(self.start_btn)
        fl.addWidget(self.summary)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(scroll, 1)
        layout.addWidget(foot)

        for w in (self.strip, self.keep_profile, self.keep_date, self.convert, self.shrink, self.video, self.video_hw,
                  self.rename, self.group, self.pad, self.recycle):
            w.toggled.connect(self._on_change)
        for w in (self.quality, self.sort, self.order, self.group_mode, self.raw_mode, self.video_codec,
                  self.max_side, self.pattern):
            w.currentIndexChanged.connect(self._on_change)
        for w in (self.prefix, self.video_prefix):
            w.textChanged.connect(self._on_change)
        for w in (self.start, self.video_start):
            w.valueChanged.connect(self._on_change)
        self.to_folder.toggled.connect(self._on_change)
        self.sort.currentIndexChanged.connect(self._update_order_labels)

        self.strip.setChecked(True)
        self.convert.setChecked(True)
        self.video.setChecked(True)
        self.to_folder.setChecked(True)
        self._update_order_labels()
        self.refresh_default_folder()
        self._sync()

    # ---------------------------------------------------------------- state

    def _on_change(self, *_):
        self._sync()
        self.changed.emit()

    def _update_order_labels(self, *_):
        asc, desc = (t(key) for key in ORDER_LABELS[self.sort.currentData()])
        current = max(0, self.order.currentIndex())
        self.order.blockSignals(True)
        self.order.clear()
        self.order.addItems([asc, desc])
        self.order.setCurrentIndex(current)
        self.order.blockSignals(False)
        self.order.setEnabled(self.rename.isChecked() and self.sort.currentData() != SORT_MANUAL)

    def _sync(self) -> None:
        self.keep_profile.setEnabled(self.strip.isChecked())  # without stripping, profiles stay anyway
        self.keep_date.setEnabled(self.strip.isChecked())  # and so do the dates
        # A switched-off operation hides its options, as Apple's settings do: only what applies shows.
        self._strip_box.setVisible(self.strip.isChecked())
        self.convert_box.setVisible(self.convert.isChecked())
        self.shrink_box.setVisible(self.shrink.isChecked())
        self.pattern.setToolTip(_tip(PATTERN_TIPS, self.pattern.currentData()))
        numbered = self.pattern.currentData() == NAME_NUMBER
        self.start.setEnabled(numbered)
        self.video_start.setEnabled(numbered)
        self.video_box.setVisible(self.video.isChecked())
        self.video_codec.setToolTip(t(VIDEO_CODEC_TIPS[self.video_codec.currentData()]))
        if self.video_codec.currentData() == VIDEO_KEEP:
            self.video_hint.setText(t("options.video_hint.keep"))
        else:
            self.video_hint.setText(t("options.video_hint.compatible"))
        self.video_hint.setToolTip(t(VIDEO_CODEC_TIPS[self.video_codec.currentData()]))
        if self.video_hw.isChecked():
            self._probe_hardware()
        self._update_hw_hint()
        self.rename_box.setVisible(self.rename.isChecked())
        self.order.setEnabled(self.rename.isChecked() and self.sort.currentData() != SORT_MANUAL)
        self.sort.setToolTip(_tip(SORT_TIPS, self.sort.currentData()))
        self.group_box.setVisible(self.group.isChecked())
        in_place = self.in_place.isChecked()
        self.folder_box.setVisible(not in_place)
        self.in_place_box.setVisible(in_place)
        source_item = self.group_mode.model().item(self.group_mode.findData(GROUP_SOURCE))
        source_item.setEnabled(not in_place)
        if in_place and self.group_mode.currentData() == GROUP_SOURCE:
            self.group_mode.setCurrentIndex(self.group_mode.findData(GROUP_MONTH))
        if self.convert.isChecked() and not self.strip.isChecked():
            self.convert_hint.setText(t("options.convert_hint_keep"))
        else:
            self.convert_hint.setText(t("options.convert_hint"))
        self.in_place_warn.setText(
            t("options.in_place_recycle")
            if self.recycle.isChecked() else
            t("options.in_place_delete")
        )
        self.group_mode.setToolTip(_tip(GROUP_TIPS, self.group_mode.currentData()))
        self._update_structure_hint()
        self._update_example()
        self._update_button()

    def keep_structure(self, *_) -> None:
        """Keeps the source folder structure in the output (group into folders → keep source folder structure)."""
        self.group_mode.setCurrentIndex(self.group_mode.findData(GROUP_SOURCE))
        self.group.setChecked(True)

    def _update_structure_hint(self) -> None:
        self.structure_hint.setVisible(
            self._folders > 1 and not self.in_place.isChecked() and not self.group.isChecked()
        )

    def _update_example(self) -> None:
        pattern = self.pattern.currentData()
        photo_ext = ".jpg" if self.convert.isChecked() else ".…"
        video_ext = ".mp4" if self.video.isChecked() else ".…"
        photo_prefix = safe_filename_part(self.prefix.text())
        video_prefix = safe_filename_part(self.video_prefix.text())
        affixes = {"p": photo_prefix, "pe": photo_ext, "v": video_prefix, "ve": video_ext}
        if pattern == NAME_DATE_TIME:
            self.example.setText(t("options.example_date_time", **affixes))
            return
        if pattern == NAME_DATE_NUMBER:
            self.example.setText(t("options.example_date_number", **affixes))
            return

        def names(prefix: str, start: int, count: int, ext: str) -> str:
            last = start + max(count, 3) - 1
            width = max(2, len(str(last))) if self.pad.isChecked() else 0
            return ", ".join(f"{prefix}{start + i:0{width}d}{ext}" for i in range(2)) + " …"

        photos = names(photo_prefix, self.start.value(), self._count - self._videos, photo_ext)
        videos = names(video_prefix, self.video_start.value(), self._videos, video_ext)
        self.example.setText(t("options.example_number", photos=photos, videos=videos))

    def _color_links(self) -> None:
        link = f"<a href='#yapi' style='color:{theme.ACCENT_TEXT}; font-weight:600'>{t('options.keep_structure')}</a>"
        self.structure_hint.setText(f"{t('options.structure_hint')} {link}")

    def has_operation(self) -> bool:
        """Is there anything to do? Edited files (rotated, cropped, trimmed …) count as an operation too."""
        photos = self._count > self._videos
        convert = (self.convert.isChecked() or self.shrink.isChecked()) and photos
        video = self.video.isChecked() and self._videos > 0
        return self._edited > 0 or convert or video or any(w.isChecked() for w in (self.strip, self.rename, self.group))

    def set_state(self, count: int, folders: int, pending: int, gps: int, raw: int = 0,
                  scanning: bool = False, edited: int = 0, videos: int = 0) -> None:
        self._count = count
        self._videos = videos
        self._folders = folders
        self._pending = pending
        self._scanning = scanning
        self._edited = edited
        self.raw_row.setVisible(raw > 0)
        info = []
        if count:
            what = count_text(count - videos, videos)
            info.append(t("options.summary_folders", n=folders, what=what) if folders > 1
                        else t("options.summary", what=what))
        if edited:
            info.append(t("options.summary_edited", n=edited))
        if gps:
            info.append(t("options.summary_gps", n=gps))
        self.summary.setText(" · ".join(info))
        self._update_structure_hint()
        self._update_example()
        self._update_button()

    def _probe_hardware(self) -> None:
        """Probes this computer's GPU encoder in the background (once per session)."""
        if self._hw_probe is None and not hwenc.detected("h264"):
            self._hw_probe = _HardwareProbe(self)
            self._hw_probe.done.connect(self._hardware_found)
            self._hw_probe.start()

    def _hardware_found(self) -> None:
        self._update_hw_hint()
        self.changed.emit()  # so the plan and notes show the encoder

    def _update_hw_hint(self) -> None:
        text = ""
        if self.video_hw.isChecked():
            if not hwenc.detected("h264"):
                text = t("options.hw_trying")
            elif (encoder := hwenc.peek("h264")) is None:
                text = t("options.hw_none")
            else:
                hdr = hwenc.peek("hevc") is not None  # cropped HDR videos can stay HDR
                text = t("options.hw_encoder_hdr" if hdr else "options.hw_encoder", name=encoder.label)
        self.video_hw_hint.setText(text)
        self.video_hw_hint.setVisible(bool(text))

    def waiting_for_info(self) -> bool:
        """If the plan depends on capture dates (sort/group by date), the job waits until all info is read."""
        return bool(self._pending) and self.job_options().needs_dates()

    def _update_button(self) -> None:
        count = self._count
        if self._scanning:
            text, enabled = t("options.scanning"), False
        elif not count:
            text, enabled = t("options.choose_files"), False
        elif self.waiting_for_info():
            # No count: a text changing every second would make the panel width jump and redraw the table.
            text, enabled = t("options.reading_dates"), False
        elif self.in_place.isChecked() and not self.has_operation():
            text, enabled = t("options.choose_operation"), False
        else:
            what = count_text(count - self._videos, self._videos, accusative=True, title=True)
            button = "options.process_button" if self.has_operation() else "options.copy_button"
            text, enabled = t(button, what=what), True
        self.start_btn.setText(text)
        self.start_btn.set_icon_name("play.fill" if enabled else None)
        self.start_btn.setEnabled(enabled)

    # -------------------------------------------------------------- folder

    def refresh_default_folder(self) -> None:
        """Automatic folder: Pictures\\Fotodit\\<date time> (renewed for every job)."""
        if self._auto_folder:
            stamp = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
            self.folder_edit.setText(os.path.join(default_output_root(), stamp))

    def set_output_folder(self, path: str) -> None:
        self._auto_folder = False
        self.folder_edit.setText(os.path.normpath(path))
        self._on_change()

    def _choose_folder(self) -> None:
        start = self.folder_edit.text()
        while start and not os.path.isdir(start):
            parent = os.path.dirname(start)
            start = "" if parent == start else parent
        path = QFileDialog.getExistingDirectory(self, t("options.choose_folder"), start)
        if path:
            self._auto_folder = False
            self.folder_edit.setText(os.path.normpath(path))
            self._on_change()

    # --------------------------------------------------------------- options

    def job_options(self) -> JobOptions:
        return JobOptions(
            content=ContentOptions(
                strip_metadata=self.strip.isChecked(),
                convert_to_jpeg=self.convert.isChecked(),
                jpeg_quality=int(self.quality.currentData()),
                raw_mode=self.raw_mode.currentData(),
                convert_video=self.video.isChecked(),
                video_codec=self.video_codec.currentData(),
                video_hw=self.video_hw.isChecked(),
                keep_color_profile=self.keep_profile.isChecked(),
                keep_date=self.keep_date.isChecked(),
                max_side=int(self.max_side.currentData()) if self.shrink.isChecked() else 0,
            ),
            rename=RenameOptions(
                enabled=self.rename.isChecked(),
                sort_by=self.sort.currentData(),
                descending=self.order.currentIndex() == 1,
                prefix=self.prefix.text(),
                start=self.start.value(),
                zero_pad=self.pad.isChecked(),
                video_prefix=self.video_prefix.text(),
                video_start=self.video_start.value(),
                pattern=self.pattern.currentData(),
            ),
            output=OutputOptions(
                in_place=self.in_place.isChecked(),
                folder=self.folder_edit.text(),
                group_by=self.group_mode.currentData() if self.group.isChecked() else GROUP_NONE,
                recycle=self.recycle.isChecked(),
            ),
        )

    def sort_is_manual(self) -> bool:
        return self.sort.currentData() == SORT_MANUAL

    def load(self, s: QSettings) -> None:
        def flag(key, default):
            value = s.value(key, default)
            return value in (True, "true", "1", 1) if not isinstance(value, bool) else value

        self.strip.setChecked(flag("strip", True))
        self.keep_profile.setChecked(flag("keep_profile", False))
        self.keep_date.setChecked(flag("keep_date", False))
        self.shrink.setChecked(flag("shrink", False))
        self.max_side.setCurrentIndex(max(0, self.max_side.findData(int(s.value("max_side", 2048)))))
        self.pattern.setCurrentIndex(max(0, self.pattern.findData(s.value("pattern", NAME_NUMBER))))
        self.convert.setChecked(flag("convert", True))
        self.quality.setCurrentIndex(max(0, self.quality.findData(int(s.value("quality", 95)))))
        self.raw_mode.setCurrentIndex(max(0, self.raw_mode.findData(s.value("raw_mode", RAW_AUTO))))
        self.video.setChecked(flag("video", True))
        self.video_codec.setCurrentIndex(max(0, self.video_codec.findData(s.value("video_codec", VIDEO_COMPATIBLE))))
        self.video_hw.setChecked(flag("video_hw", False))
        self.rename.setChecked(flag("rename", False))
        self.sort.setCurrentIndex(max(0, self.sort.findData(s.value("sort", SORT_DATE))))
        self.order.setCurrentIndex(1 if flag("descending", False) else 0)
        self.prefix.setText(str(s.value("prefix", "")))
        self.start.setValue(int(s.value("start", 1)))
        self.video_prefix.setText(str(s.value("video_prefix", "")))
        self.video_start.setValue(int(s.value("video_start", 1)))
        self.pad.setChecked(flag("pad", False))
        self.group.setChecked(flag("group", False))
        self.group_mode.setCurrentIndex(max(0, self.group_mode.findData(s.value("group_mode", GROUP_MONTH))))
        self.recycle.setChecked(flag("recycle", True))
        (self.in_place if flag("in_place", False) else self.to_folder).setChecked(True)
        folder = str(s.value("folder", "") or "")
        if folder:
            self._auto_folder = False
            self.folder_edit.setText(folder)
        self._sync()

    def save(self, s: QSettings) -> None:
        o = self.job_options()
        s.setValue("strip", o.content.strip_metadata)
        s.setValue("keep_profile", o.content.keep_color_profile)
        s.setValue("keep_date", o.content.keep_date)
        s.setValue("shrink", self.shrink.isChecked())
        s.setValue("max_side", int(self.max_side.currentData()))
        s.setValue("pattern", o.rename.pattern)
        s.setValue("convert", o.content.convert_to_jpeg)
        s.setValue("quality", o.content.jpeg_quality)
        s.setValue("raw_mode", o.content.raw_mode)
        s.setValue("video", o.content.convert_video)
        s.setValue("video_codec", o.content.video_codec)
        s.setValue("video_hw", o.content.video_hw)
        s.setValue("rename", o.rename.enabled)
        s.setValue("sort", o.rename.sort_by)
        s.setValue("descending", o.rename.descending)
        s.setValue("prefix", o.rename.prefix)
        s.setValue("start", o.rename.start)
        s.setValue("video_prefix", o.rename.video_prefix)
        s.setValue("video_start", o.rename.video_start)
        s.setValue("pad", o.rename.zero_pad)
        s.setValue("group", self.group.isChecked())
        s.setValue("group_mode", self.group_mode.currentData())
        s.setValue("recycle", o.output.recycle)
        s.setValue("in_place", o.output.in_place)
        s.setValue("folder", "" if self._auto_folder else self.folder_edit.text())
