"""Main window: browser, selection basket, options and starting the job."""

from __future__ import annotations

import html
import logging
import os
import shutil
import sys
import time

from PySide6.QtCore import (
    QByteArray,
    QProcess,
    QProcessEnvironment,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
)
from PySide6.QtGui import QActionGroup, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__, i18n
from ..core.edits import Edit
from ..core.folderscan import folder_key
from ..core.formats import EXTENSIONS, VIDEO_EXTENSIONS, Fmt, is_supported_name
from ..core.planner import (
    DATE_GROUPS,
    GROUP_DAY,
    GROUP_MONTH,
    GROUP_NONE,
    GROUP_SOURCE,
    GROUP_YEAR,
    NAME_NUMBER,
    SORT_DATE,
    build_plan,
    forget_listings,
    undated_count,
    undated_folder,
)
from ..core.processor import RAW_EDIT_NEEDS_JPEG, RAW_NEEDS_JPEG, RAW_RESIZE_NEEDS_JPEG
from ..core.rawdev import RAW_AUTO
from ..core.video.probe import duration_text
from ..core.video.process import VIDEO_COMPATIBLE, VIDEO_EDIT_NEEDS_MP4, VIDEO_KEEP
from ..i18n import capitalize, decapitalize, percent, t
from ..winutil import recycle_bin_capacity, volume_root
from . import count_text, glass, number, size_text, theme
from .browser import BrowserPage
from .chrome import ContentArea
from .controls import BarButton, IconView
from .folders import FolderSelection
from .info_panel import InfoPanel
from .nav import NavPanel
from .options_panel import OptionsPanel
from .preview_dialog import PreviewDialog, PreviewItem
from .run_dialog import RunDialog
from .selected_page import SelectedPage
from .selection import Selection
from .sheet import Sheet, ask, inform, offer
from .thumbs import ThumbnailLoader
from .widgets import menu as widget_menu

log = logging.getLogger(__name__)

TILE = QSize(188, 206)
THUMB = 164
BULK_CONFIRM = 10_000  # ask for confirmation when a folder selection adds more photos than this
LIVE_PLAN_LIMIT = 2000  # for larger selections the table is updated once reading finishes, not while info is read

GROUP_STEPS = {
    GROUP_MONTH: "confirm.group.month",
    GROUP_YEAR: "confirm.group.year",
    GROUP_DAY: "confirm.group.day",
    GROUP_SOURCE: "confirm.group.source",
}
EDIT_KINDS = (  # the kinds of edits named in the confirmation
    ("confirm.kind.rotate", lambda edit: edit.quarter),
    ("confirm.kind.flip", lambda edit: edit.mirror),
    ("confirm.kind.straighten", lambda edit: edit.angle),
    ("confirm.kind.crop", lambda edit: edit.crop is not None),
    ("confirm.kind.exposure", lambda edit: edit.exposure),
    ("confirm.kind.trim", lambda edit: edit.trim is not None),
    ("confirm.kind.cut", lambda edit: edit.cuts),
)


def _folder_name(path: str) -> str:
    return os.path.basename(path.rstrip("\\/")) or path


def _estimated_output(photo, content, video_plan=None) -> int:
    """Rough size of the output file (for the free-space check)."""
    if video_plan is not None:
        video = getattr(photo.info, "video", None)
        kept = 1.0
        if video_plan.trim is not None and getattr(video, "duration", 0):
            start, end = video_plan.trim
            kept = max(0.0, min(end, video.duration) - start) / video.duration
        if not video_plan.encode_video:
            return int(photo.size * kept)
        codec = getattr(video, "vcodec", "")
        # H.264 (CRF 18) comes out larger than HEVC/AV1/VP9 and smaller than MJPEG/DV.
        return int(photo.size * kept * (2 if codec in ("hevc", "av1", "vp9") else 1 if codec != "mjpeg" else 0.5))
    fmt = photo.fmt()
    if content.convert_to_jpeg and fmt in (Fmt.HEIF, Fmt.AVIF):
        return photo.size * 5 // 2  # HEIC/AVIF compress much better than JPEG
    if content.convert_to_jpeg and fmt == Fmt.RAW:
        return photo.size // 2
    return photo.size


def _existing_parent(path: str) -> str:
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return probe


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings, icon: QIcon | None = None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        if icon:
            self.setWindowIcon(icon)
        self.setAcceptDrops(True)
        self.settings = settings

        screen = QApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen else 1.0
        self.loader = ThumbnailLoader(THUMB, dpr, self)
        self.selection = Selection(self)
        self.edits = self.selection.edits  # rotate/crop edits: applied when the job is started
        self.folders = FolderSelection(self.selection, self, confirm=self._confirm_bulk, bulk_limit=BULK_CONFIRM)

        self.nav = NavPanel(self.folders)
        self.info = InfoPanel()
        self.browser = BrowserPage(self.selection, self.folders, self.loader, TILE)
        self.selected = SelectedPage(self.loader)
        self.options = OptionsPanel()

        # The sidebar: glass from the window's left edge (as in macOS 27), the folders above the file info.
        self.left = QSplitter(Qt.Orientation.Vertical)
        self.left.addWidget(self.nav)
        self.left.addWidget(self.info)
        self.left.setStretchFactor(0, 3)
        self.left.setStretchFactor(1, 2)
        self.left.setSizes([470, 320])
        self.left.setChildrenCollapsible(False)
        self.left.setHandleWidth(9)

        # A push button with a menu: it also opens with the keyboard (Space) and for screen readers.
        self.theme_btn = BarButton(t("main.appearance"), "theme", tooltip=t("main.appearance_tip"))
        menu = widget_menu(self.theme_btn)
        group = QActionGroup(menu)
        self.theme_actions = {}
        for mode in theme.MODES:
            action = menu.addAction(theme.mode_label(mode))
            action.setCheckable(True)
            action.setChecked(mode == theme.mode())
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, m=mode: self.set_theme(m))
            self.theme_actions[mode] = action
        menu.addSeparator()
        # Apple's three looks of the glass (iOS 27, macOS 27), and solid glass.
        levels = QActionGroup(menu)
        self.glass_actions = {}
        for value in glass.LEVELS:
            action = menu.addAction(t("main.glass", level=glass.level_label(value)))
            action.setCheckable(True)
            action.setChecked(value == glass.level())
            levels.addAction(action)
            action.triggered.connect(lambda _checked=False, v=value: self.set_glass_level(v))
            self.glass_actions[value] = action
        self.solid_action = menu.addAction(t("main.reduce_transparency"))
        self.solid_action.setCheckable(True)
        self.solid_action.setToolTip(t("main.reduce_transparency_tip"))
        self.solid_action.toggled.connect(self.set_reduce_transparency)
        menu.addSeparator()
        # The language: as Windows is set, Turkish or English; it takes effect at the next start.
        languages = QActionGroup(menu)
        self.language_actions = {}
        chosen = str(settings.value("appearance/language", i18n.SYSTEM) or i18n.SYSTEM)
        for code in (i18n.SYSTEM, *i18n.LANGUAGES):
            name = t("main.language_system") if code == i18n.SYSTEM else i18n.NAMES[code]
            action = menu.addAction(t("main.language", name=name))
            action.setCheckable(True)
            action.setChecked(code == chosen)
            languages.addAction(action)
            action.triggered.connect(lambda _checked=False, c=code: self.set_language(c))
            self.language_actions[code] = action
        self.theme_btn.setMenu(menu)
        self.about_btn = BarButton(f"{APP_NAME} {__version__}", None,
                                   tooltip=t("main.about_tip"))
        self.about_btn.clicked.connect(self.show_about)
        foot = QWidget()
        foot_layout = QHBoxLayout(foot)
        foot_layout.setContentsMargins(8, 2, 8, 8)
        foot_layout.setSpacing(2)
        foot_layout.addWidget(self.theme_btn)
        foot_layout.addStretch(1)
        foot_layout.addWidget(self.about_btn)

        self.sidebar = glass.GlassSurface(kind=glass.PANEL, radius=0, shadow=False, bleed=(40, 40, 0, 40))
        self.sidebar.setObjectName("Glass")
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(0, 0, 1, 0)
        side.setSpacing(0)
        side.addWidget(self.left, 1)
        side.addWidget(foot)
        # The folders keep 230 px; the sidebar itself takes its layout's minimum, so the footer's two
        # buttons always show their whole words (English needs more room than Turkish).
        self.left.setMinimumWidth(230)

        self.inspector = glass.GlassSurface(kind=glass.PANEL, radius=0, shadow=False, bleed=(0, 40, 40, 40))
        self.inspector.setObjectName("Glass")
        ins = QVBoxLayout(self.inspector)
        ins.setContentsMargins(1, 0, 0, 0)
        ins.addWidget(self.options)
        self.inspector.setMinimumWidth(340)
        self.inspector.setMaximumWidth(460)

        # The content: the pages with their toolbars floating above them as glass.
        self.content = ContentArea()
        self.content.add_page(self.browser, t("main.browse"))
        self.content.add_page(self.selected, t("main.selected_tab", n=0))
        self.tabs = self.content.tabs

        self.scan_label = QLabel()
        self.scan_label.setObjectName("BarText")
        self.scan_stop = BarButton(t("main.stop"), "xmark.circle", tooltip=t("main.stop_tip"))
        self.scan_stop.clicked.connect(self.folders.cancel_all)
        self.activity = glass.GlassSurface(kind=glass.BAR)
        act = QHBoxLayout(self.activity)
        act.setContentsMargins(16, 4, 4, 4)
        act.setSpacing(8)
        act.addWidget(self.scan_label)
        act.addWidget(self.scan_stop)
        self.content.set_activity(self.activity)

        self.content.setMinimumWidth(400)  # in a narrow window the side panels give way first
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(self.sidebar)
        self.split.addWidget(self.content)
        self.split.addWidget(self.inspector)
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setStretchFactor(2, 0)
        self.split.setSizes([270, 790, 380])
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(1)
        self.setCentralWidget(self.split)

        self._about = None
        self.selected.message.connect(lambda text: self.show_message(text, 9000))

        # The plan (the Selected tab's table) is only computed while that tab is visible; with tens
        # of thousands of photos, recomputing it on every change would slow the UI down.
        self._plan_stale = True
        self._plan_cost = 0.0
        self._replan_timer = QTimer(self)
        self._replan_timer.setSingleShot(True)
        self._replan_timer.timeout.connect(self.replan)

        self.nav.folderSelected.connect(self._open_folder)
        self.browser.folderChanged.connect(self._folder_changed)
        self.browser.photoFocused.connect(self.info.show_path)
        self.browser.addFilesRequested.connect(self.add_files_dialog)
        self.browser.previewRequested.connect(self._preview_folder)
        self.browser.cropRequested.connect(lambda path: self._preview_folder(path, crop=True))
        self.browser.grid.edited.connect(lambda _path: self._edited_message())
        self.selected.previewRequested.connect(self._preview_selected)
        self.selected.cropRequested.connect(lambda row: self._preview_selected(row, crop=True))
        self.selected.rotateRequested.connect(self._rotate_paths)
        self.selected.flipRequested.connect(self._flip_paths)
        self.selected.resetEditsRequested.connect(self.edits.clear)
        self.edits.changed.connect(self._edits_changed)
        self.selected.photoFocused.connect(self.info.show_path)
        self.selected.removeRequested.connect(self.selection.remove)
        self.selected.removeFolderRequested.connect(self._remove_folder_photos)
        self.selected.clearRequested.connect(self._clear_selection)
        self.selected.reorderRequested.connect(self.selection.reorder)
        self.selection.changed.connect(self._schedule_replan)
        self.selection.infoChanged.connect(self._info_arrived)
        self._scan_tally: list = [0, 0, 0, 0, 0, ""]  # roots, found, added, unreadable, stopped, last root
        self._scanning = False
        self._progress_at = 0.0
        self.folders.progress.connect(self._scan_progress)
        self.folders.scanned.connect(self._scan_done)
        self.folders.changed.connect(self._scan_state_changed)
        self.options.changed.connect(self._schedule_replan)
        self.options.startRequested.connect(self.start)
        self.tabs.currentChanged.connect(self._tab_changed)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.start)
        QShortcut(QKeySequence("F1"), self, activated=self.show_about)

        self._restore()
        self.replan()

    # ----------------------------------------------------------- settings

    def _restore(self) -> None:
        s = self.settings
        geometry = s.value("window/geometry")
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            self.restoreGeometry(geometry)
        else:
            self.resize(1440, 880)
        for key, splitter in (("window/split", self.split), ("window/left", self.left)):
            state = s.value(key)
            if isinstance(state, QByteArray) and not state.isEmpty():
                splitter.restoreState(state)
        self.split.setHandleWidth(1)
        self.left.setHandleWidth(9)
        self.set_glass_level(str(s.value("appearance/glass", glass.DEFAULT) or glass.DEFAULT))
        self.solid_action.setChecked(s.value("appearance/solid", False) in (True, "true", "1", 1))
        s.beginGroup("options")
        self.options.load(s)
        s.endGroup()
        folder = str(s.value("browse/folder", "") or "")
        if not folder or not os.path.isdir(folder):
            folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        self.browser.navigate(os.path.normpath(folder) if folder else "")

    def set_theme(self, mode: str) -> None:
        """Light, dark or as Windows is set (theme.SYSTEM); remembered for the next start."""
        theme.apply(QApplication.instance(), mode)
        self.settings.setValue("appearance/theme", theme.mode())
        for name, action in self.theme_actions.items():
            action.setChecked(name == theme.mode())

    def show_about(self) -> None:
        from .about import AboutDialog

        if self._about is None:
            self._about = AboutDialog(self)
        self._about.open()  # (window-modal, without blocking)

    def set_reduce_transparency(self, solid: bool) -> None:
        """Solid glass instead of see-through glass (remembered for the next start)."""
        glass.set_reduce_transparency(solid)
        self.settings.setValue("appearance/solid", bool(solid))
        if self.solid_action.isChecked() != bool(solid):
            self.solid_action.setChecked(bool(solid))

    def set_language(self, choice: str) -> None:
        """Follow Windows (i18n.SYSTEM), Turkish or English. The texts are set as the window is built,
        so a new language shows after a restart, which is offered."""
        self.settings.setValue("appearance/language", choice)
        for name, action in self.language_actions.items():
            action.setChecked(name == choice)
        if i18n.resolve(choice) != i18n.language():
            offer(self, t("main.restart_title"), t("main.restart_text"), t("main.restart"), t("main.later"),
                  self.restart)

    def restart(self) -> None:
        """Closes the window (asking about unapplied edits as usual) and starts Fotodit again."""
        if not self.close():
            return
        self.settings.sync()  # the new instance reads the settings at once
        if getattr(sys, "frozen", False):
            program, args, folder = sys.executable, [], os.path.dirname(sys.executable)
        else:
            package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            program, args, folder = sys.executable, ["-m", "fotodit"], os.path.dirname(package)
        process = QProcess()
        process.setProgram(program)
        process.setArguments(args)
        process.setWorkingDirectory(folder)
        environment = QProcessEnvironment.systemEnvironment()
        # A one-file build must not hand its unpacked folder (removed as it exits) to the new instance.
        environment.insert("PYINSTALLER_RESET_ENVIRONMENT", "1")
        process.setProcessEnvironment(environment)
        if not process.startDetached():
            log.error("Could not restart: %s", process.errorString())

    def set_glass_level(self, value: str) -> None:
        """Clearer, default or frostier glass (remembered for the next start)."""
        glass.set_level(value)
        self.settings.setValue("appearance/glass", glass.level())
        for name, action in self.glass_actions.items():
            action.setChecked(name == glass.level())

    def show_message(self, text: str, msec: int = 6000) -> None:
        """A short message in a glass capsule at the bottom of the content."""
        self.content.show_message(text, msec)

    def current_message(self) -> str:
        return self.content.toast.current_message()

    def closeEvent(self, event) -> None:
        pending = self.selection.pending_edits()
        if pending and not self._confirm_discard_edits(pending):
            event.ignore()
            return
        s = self.settings
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("window/split", self.split.saveState())
        s.setValue("window/left", self.left.saveState())
        s.setValue("browse/folder", self.browser.folder)
        s.beginGroup("options")
        self.options.save(s)
        s.endGroup()
        self.selected.cancel_duplicates()
        self.folders.shutdown()
        self.loader.shutdown()
        self.selection.shutdown()
        super().closeEvent(event)

    # ---------------------------------------------------------- navigation

    def _open_folder(self, path: str) -> None:
        self.tabs.setCurrentWidget(self.browser)
        self.browser.navigate(path)

    def _folder_changed(self, path: str) -> None:
        self.nav.sync(path)

    # --------------------------------------------------------------- adding

    def add_recursive(self, roots: list[str]) -> None:
        """Selects folders together with their subfolders (scanned in the background)."""
        self.folders.set_selected(roots, True)

    def _confirm_bulk(self, count: int) -> bool:
        return ask(self, t("main.bulk_title", n=count),
                   t("main.bulk_text"), t("main.select_all_found"), t("common.no"), icon="info")

    def _scan_progress(self, root: str, found: int, _folder: str) -> None:
        now = time.monotonic()
        if now - self._progress_at < 0.1:  # with thousands of small folders, update the label ≤10 times a second
            return
        self._progress_at = now
        scanner = self.folders.scanner
        if scanner.batch_total > 1:
            done = min(scanner.batch_done + 1, scanner.batch_total)
            self.scan_label.setText(t("scan.batch", done=number(done), total=number(scanner.batch_total),
                                      n=self._scan_tally[1] + found))
        else:
            self.scan_label.setText(t("scan.one", folder=_folder_name(root), n=found))

    def _scan_state_changed(self) -> None:
        busy = self.folders.busy()
        if busy == self._scanning:
            return
        self._scanning = busy
        if busy:
            self.scan_label.setText(t("scan.start"))
        self.activity.setVisible(busy)
        self.content.relayout()
        if not busy and self._scan_tally[0]:
            self._report_scans()
        self._update_state()  # the start button stays disabled while a scan is running

    def _scan_done(self, root: str, found: int, added: int, unreadable: int, cancelled: bool) -> None:
        """One folder's scan has finished; the result is reported in a single message once all scans are done."""
        roots, total_found, total_added, total_unreadable, stopped, last = self._scan_tally
        self._scan_tally = [roots + 1, total_found + found, total_added + added,
                            total_unreadable + unreadable, stopped + cancelled, root]

    def _report_scans(self) -> None:
        roots, found, added, unreadable, stopped, last = self._scan_tally
        self._scan_tally = [0, 0, 0, 0, 0, ""]
        where = f"‘{_folder_name(last)}’" if roots == 1 else t("count.folders", n=roots)
        if stopped == roots:
            text = t("scan.stopped", where=where)
        elif not found:
            text = t("scan.none", where=where)
        elif not added:
            text = t("scan.all_selected", where=where, n=found)
        else:
            text = t("scan.added", where=where, n=added)
            if stopped:
                text += " " + t("scan.some_stopped", n=stopped)
        if unreadable:
            text += " " + t("scan.unreadable", n=unreadable)
        self.show_message(text, 8000)

    def add_files_dialog(self) -> None:
        every = " ".join(f"*{ext}" for ext in EXTENSIONS)
        photos = " ".join(f"*{ext}" for ext in EXTENSIONS if ext not in VIDEO_EXTENSIONS)
        videos = " ".join(f"*{ext}" for ext in VIDEO_EXTENSIONS)
        start = self.browser.folder or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        files, _ = QFileDialog.getOpenFileNames(
            self, t("main.add_files_title"), start,
            t("main.file_filter", every=every, photos=photos, videos=videos)
        )
        files = [f for f in files if is_supported_name(f)]
        if files:
            added = self.selection.add(files)
            self.show_message(t("main.added", n=added), 6000)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        files = [p for p in paths if os.path.isfile(p) and is_supported_name(p)]
        folders = [p for p in paths if os.path.isdir(p)]
        if files:
            added = self.selection.add(files)
            self.show_message(t("main.added", n=added), 6000)
        if folders:
            self.folders.set_selected(folders, True)
        if files or folders:
            self.tabs.setCurrentWidget(self.selected)
            event.acceptProposedAction()

    def open_paths(self, paths: list[str]) -> None:
        """Paths from the command line (or from Windows Explorer's 'Send to' menu):
        a single folder → browse it; files → add them to the selection; several folders → select them."""
        paths = [os.path.abspath(p) for p in paths if os.path.exists(p)]
        folders = [p for p in paths if os.path.isdir(p)]
        files = [p for p in paths if os.path.isfile(p) and is_supported_name(p)]
        if len(folders) == 1 and not files:
            self.browser.navigate(folders[0])
            return
        if files:
            self.browser.navigate(os.path.dirname(files[0]))
            self.selection.add(files)
            self.tabs.setCurrentWidget(self.selected)
        if folders:
            self.folders.set_selected(folders, True)

    def _confirm_discard_edits(self, count: int) -> bool:
        return ask(self, t("main.discard_title"),
                   t("main.discard_text", n=count), t("main.quit_anyway"), t("common.cancel"))

    # ------------------------------------------------------------- preview

    def _preview_selected(self, row: int, crop: bool = False) -> None:
        """Shows the selected photos and videos in the large preview, in the Selected tab's order."""
        model = self.selected.model
        if self._replan_timer.isActive():
            # The list is brought up to date first; `row` was clicked in the old one, so the preview
            # starts at that file wherever it stands now.
            path = model.plan[row].photo.path if 0 <= row < len(model.plan) else None
            self.replan()
            moved = model.row_of(path) if path else None
            row = row if moved is None else moved
        plan = model.plan
        items = [PreviewItem(e.photo.path, e.photo.mtime, e.photo.name) for e in plan]
        self.open_preview(items, row, plan=self.selected.model, crop=crop)

    def _preview_folder(self, path: str, crop: bool = False) -> None:
        """Shows the photo/video double-clicked in the Browse tab together with the others in its folder."""
        photos = self.browser.model.photos()
        key = os.path.normcase(path)
        index = next((i for i, e in enumerate(photos) if os.path.normcase(e.path) == key), 0)
        self.open_preview([PreviewItem(e.path, e.mtime, e.name) for e in photos], index, crop=crop)

    def open_preview(self, items: list[PreviewItem], index: int, plan=None, crop: bool = False) -> PreviewDialog | None:
        if not items:
            return None
        dialog = PreviewDialog(items, index, self.selection, self.loader, self, plan=plan, crop=crop)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()  # window-modal; selection changes show up in the main window right away
        return dialog

    # -------------------------------------------------------------- editing

    def _rotate_paths(self, paths: list[str], delta: int) -> None:
        """Rotates the photos in the highlighted rows of the Selected tab by 90°."""
        items = [(p.path, p.mtime) for p in map(self.selection.photo, paths) if p is not None]
        if items:
            self.edits.update(items, lambda edit: edit.rotated(delta))
            self._edited_message(len(items))

    def _flip_paths(self, paths: list[str]) -> None:
        """Flips the photos and videos in the highlighted rows of the Selected tab left to right."""
        items = [(p.path, p.mtime) for p in map(self.selection.photo, paths) if p is not None]
        if items:
            self.edits.update(items, lambda edit: edit.mirrored())
            self._edited_message(len(items))

    def _edited_message(self, count: int = 1) -> None:
        self.show_message(t("main.edited", n=count), 8000)

    def _edits_changed(self, _keys: list[str]) -> None:
        self._update_state()
        self._schedule_replan()

    def _remove_folder_photos(self, folder: str) -> None:
        key = folder_key(folder)
        self.selection.remove([p.path for p in self.selection.items() if p.folder_key == key])

    def _clear_selection(self) -> None:
        if len(self.selection) > 20 and not ask(
                self, t("main.clear_title"),
                t("main.clear_text", n=len(self.selection)),
                t("main.clear_list"), t("common.cancel")):
            return
        self.folders.cancel_all()
        self.selected.cancel_duplicates()
        self.selection.clear()

    # ------------------------------------------------------------- planning

    def _schedule_replan(self) -> None:
        self._replan_timer.start(120)

    def _info_arrived(self) -> None:
        """As photo info arrives: the counts update right away, the table only now and then.
        With large selections the table is updated once, when reading finishes."""
        self._update_state()
        if self.tabs.currentWidget() is not self.selected:
            self._plan_stale = True
            return
        pending = self.selection.pending()
        if pending and len(self.selection) > LIVE_PLAN_LIMIT:
            self._plan_stale = True
            self.selected.show_loading()
            return
        if self._replan_timer.isActive():
            return
        self._replan_timer.start(max(1000, int(self._plan_cost * 8000)) if pending else 120)

    def _tab_changed(self, _index: int) -> None:
        if self.tabs.currentWidget() is self.selected and self._plan_stale:
            self._rebuild_plan()

    def replan(self) -> None:
        self._replan_timer.stop()
        self._update_state()
        if self.tabs.currentWidget() is self.selected:
            self._rebuild_plan()
        else:
            self._plan_stale = True

    def _rebuild_plan(self) -> None:
        started = time.perf_counter()
        busy = len(self.selection) > 5000
        if busy:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        # Planning is pure Python; the info readers are held meanwhile so they don't compete
        # for the GIL and make planning several times slower.
        self.selection.hold_info()
        try:
            opts = self.options.job_options()
            try:
                plan = build_plan(self.selection.photos(), opts)
            except Exception:  # noqa: BLE001 - a failing plan preview must not stop the app
                log.exception("Could not build the plan")
                plan = []
            manual = self.options.sort_is_manual()
            # The order matters only for the new names: without renaming there is nothing to explain.
            renaming = opts.rename.enabled
            sort_label = decapitalize(self.options.sort.currentText()) if renaming else ""
            undated = undated_count(plan) if renaming and opts.rename.sort_by == SORT_DATE else 0
            self.selected.set_plan(plan, opts.output.in_place, opts.output.folder, manual, sort_label, undated)
            pending = self.selection.pending()
            if pending and len(self.selection) > LIVE_PLAN_LIMIT:
                self.selected.show_loading()
        finally:
            self.selection.release_info()
            if busy:
                QApplication.restoreOverrideCursor()
        self._plan_stale = False
        self._plan_cost = time.perf_counter() - started

    def _update_state(self) -> None:
        # (Called after every batch of info: the selection keeps its counts, so this does not grow with it.)
        count = len(self.selection)
        folders = len(self.selection.folders())
        self.options.set_state(count, folders, self.selection.pending(), self.selection.gps_count(),
                               self.selection.raw_count(), scanning=self.folders.busy(),
                               edited=self.selection.edited_count(), videos=self.selection.video_count())
        tab_text = t("main.selected_tab", n=count)
        if self.tabs.tabText(1) != tab_text:
            self.tabs.setTabText(1, tab_text)
            self.content.relayout()

    # ------------------------------------------------------------ starting

    def start(self) -> None:
        if not len(self.selection) or not self.options.start_btn.isEnabled():
            return
        if self.folders.busy() or self.options.waiting_for_info():
            return
        opts = self.options.job_options()
        forget_listings()  # check name conflicts against the current state of the disk
        plan = build_plan(self.selection.photos(), opts)
        if not plan:
            return
        if not opts.output.in_place and not self._check_output_folder(opts.output.folder):
            return
        if not self._confirm(plan, opts):
            return
        # So no file stays open in another thread while the job runs.
        self.selected.cancel_duplicates()
        self.selection.pause_info()
        try:
            dialog = RunDialog(plan, opts, self)
            dialog.exec()
            report = dialog.report
            dialog.deleteLater()  # (it holds the whole plan and its results; the window would keep every one)
        finally:
            self.selection.resume_info()
        forget_listings()
        if report is not None:
            # Short summary for support; no file names or paths are logged.
            c = opts.content
            log.info(
                "Job: %d files, %d succeeded, %d failed, cancelled=%s · strip=%s keep_profile=%s jpg=%s raw=%s "
                "mp4=%s/%s rename=%s group=%s in_place=%s",
                len(report.results), len(report.succeeded), len(report.failed), report.cancelled,
                c.strip_metadata, c.keep_color_profile, c.convert_to_jpeg, c.raw_mode, c.convert_video, c.video_codec,
                opts.rename.enabled, opts.output.group_by, opts.output.in_place,
            )
        if report is None or report.cancelled:
            return
        edited = [r.entry.photo.path for r in report.succeeded if r.entry.edit is not None]
        if opts.output.in_place:
            self.edits.clear(edited)  # the edit is now part of the file itself
            # If metadata was kept, the new file has the same mtime as the old one: drop the cached
            # thumbnail and info (e.g. the unrotated version) so they don't linger under the same key.
            changed = [p for r in report.succeeded if r.content_changed
                       for p in (r.entry.photo.path, r.final_path) if p]
            self.loader.forget(changed)
            self.selection.forget_info(changed)
            self.selection.remove([r.entry.photo.path for r in report.succeeded])
            self.selection.drop_missing()
        else:
            self.edits.mark_applied(edited)  # originals are unchanged; the edit can be reused
        self.options.refresh_default_folder()
        self.folders.invalidate_counts()
        self.browser.refresh()
        self.replan()

    def _check_output_folder(self, folder: str) -> bool:
        if not folder:
            inform(self, t("main.no_folder_title"), t("main.no_folder_text"))
            return False
        probe = _existing_parent(folder)
        if not probe or not os.path.isdir(probe) or not os.access(probe, os.W_OK):
            inform(self, t("main.cannot_write_title"), folder)
            return False
        return True

    def _space_warnings(self, plan, opts) -> list[str]:
        """Checks free disk space and Recycle Bin capacity (for large archives)."""
        c, o = opts.content, opts.output
        work = [e for e in plan if not e.skip_reason]
        needed: dict[str, int] = {}
        binned: dict[str, int] = {}
        if o.in_place:
            if opts.changes_content():
                # The new files are written before the originals are moved to the Recycle Bin.
                roots: dict[str, str | None] = {}
                for e in work:
                    folder = e.photo.folder_key
                    if folder not in roots:
                        roots[folder] = volume_root(e.photo.folder)
                    root = roots[folder]
                    if root:
                        needed[root] = needed.get(root, 0) + _estimated_output(e.photo, c, e.video_plan)
                        binned[root] = binned.get(root, 0) + e.photo.size
        else:
            root = volume_root(_existing_parent(o.folder))
            if root:
                needed[root] = sum(_estimated_output(e.photo, c, e.video_plan) for e in work)
        warnings = []
        for root, need in needed.items():
            try:
                free = shutil.disk_usage(root).free
            except OSError:
                continue
            if need > free * 0.95:
                warnings.append(t("main.space_low", root=root, need=size_text(need), free=size_text(free)))
        if o.in_place and o.recycle:
            for root, total in binned.items():
                capacity = recycle_bin_capacity(root)
                if capacity == 0:
                    warnings.append(t("main.space_no_bin", root=root))
                elif capacity is not None and total > capacity:
                    warnings.append(
                        t("main.space_bin_full", total=size_text(total), root=root, capacity=size_text(capacity))
                    )
        return warnings

    def _confirm(self, plan, opts) -> bool:
        esc = html.escape
        n = len(plan)
        steps = []
        c, r, o = opts.content, opts.rename, opts.output
        videos = [e for e in plan if e.photo.is_video and not e.skip_reason]
        photos = [e for e in plan if not e.photo.is_video and not e.skip_reason]
        if c.strip_metadata:
            steps.append(t("confirm.strip_videos" if videos else "confirm.strip"))
        if c.strip_metadata and c.keep_date:
            steps.append(t("confirm.keep_date"))
        if c.strip_metadata and c.keep_color_profile and photos:
            steps.append(t("confirm.keep_profile"))
        if c.convert_to_jpeg and photos:
            steps.append(t("confirm.convert", quality=percent(c.jpeg_quality)))
        if c.max_side and photos:
            steps.append(t("confirm.shrink", side=number(c.max_side)))
        encoded = [e for e in videos if e.video_plan is not None and e.video_plan.encode_video]
        if c.convert_video and videos:
            how = "H.264 + AAC" if c.video_codec == VIDEO_COMPATIBLE else t("confirm.keep_codec")
            steps.append(t("confirm.video", n=len(videos), how=how))
        if encoded:
            seconds = sum((e.edit or Edit()).kept_duration(getattr(e.photo.info.video, "duration", 0) or 0)
                          for e in encoded)
            hardware = next((e.video_plan.encoder for e in encoded if e.video_plan.encoder), None)
            how = (t("confirm.gpu", encoder=hardware.label) if hardware
                   else t("confirm.slow"))
            steps.append(t("confirm.encode", n=len(encoded), duration=duration_text(seconds), how=how))
        undated = undated_count(plan)
        if r.enabled:
            def sample(entries):
                # Distinct names: with grouping, numbering restarts in every folder (Video_1.mp4 twice).
                return ", ".join(list(dict.fromkeys(e.target_name for e in entries))[:2]) + " …"
            named = [sample(group) for group in (photos, videos) if group]
            sort = decapitalize(self.options.sort.currentText())
            steps.append(capitalize(t("confirm.rename", sort=esc(sort), names=esc(" · ".join(named)))))
            if undated and r.sort_by == SORT_DATE:
                steps.append(t("confirm.rename_undated", n=undated))
        if o.group_by != GROUP_NONE and not (o.in_place and o.group_by == GROUP_SOURCE):
            steps.append(t(GROUP_STEPS[o.group_by]))
            if undated and o.group_by in DATE_GROUPS:
                steps.append(t("confirm.group_undated", n=undated, folder=undated_folder()))
        copy_only = not steps
        edited = [e for e in plan if e.edit is not None and not e.skip_reason]
        if edited:
            kinds = [t(name) for name, has in EDIT_KINDS if any(has(e.edit) for e in edited)]
            steps.append(t("confirm.edited", n=len(edited), kinds=", ".join(kinds)))
            if copy_only and not o.in_place and len(edited) < n:
                steps.append(t("confirm.copy_others"))
        if not steps:
            steps.append(t("confirm.copy_all"))
        all_videos = sum(1 for e in plan if e.photo.is_video)
        what = count_text(n - all_videos, all_videos, title=True)
        body = "<ul style='margin:0 0 0 -24px'>" + "".join(f"<li>{s}</li>" for s in steps) + "</ul>"
        folders = len({e.photo.folder_key for e in plan})
        if o.in_place:
            where = (t("confirm.recycle") if o.recycle
                     else t("confirm.delete"))
            changed = t("confirm.in_place_spread", n=folders) if folders > 1 else t("confirm.in_place")
            place = f"<p style='color:{theme.WARN}'><b>{changed}</b> {where}</p>"
        else:
            place = f"<p>{t('confirm.save_to', folder=esc(o.folder))}</p>"
        warnings = []
        if not o.in_place and folders > 1 and o.group_by == GROUP_NONE:
            warnings.append(t("confirm.one_folder", n=folders))
        raw_strip, raw_resize, raw_edit, video_edit = (
            t(key) for key in (RAW_NEEDS_JPEG, RAW_RESIZE_NEEDS_JPEG, RAW_EDIT_NEEDS_JPEG, VIDEO_EDIT_NEEDS_MP4))
        raw_skipped = [e for e in plan if e.skip_reason == raw_strip]
        if raw_skipped:
            warnings.append(t("confirm.raw_strip", n=len(raw_skipped)))
        raw_resized = [e for e in plan if e.skip_reason == raw_resize]
        if raw_resized:
            warnings.append(t("confirm.raw_resize", n=len(raw_resized)))
        raw_edited = [e for e in plan if e.skip_reason == raw_edit]
        if raw_edited:
            warnings.append(t("confirm.raw_edit", n=len(raw_edited)))
        video_edits = [e for e in plan if e.skip_reason == video_edit]
        if video_edits:
            warnings.append(t("confirm.video_edit", n=len(video_edits)))
        hdr = [e for e in encoded if e.video_plan.tonemap]
        if hdr:
            tip = (" " + t("confirm.hdr_tip_compatible") if c.video_codec == VIDEO_COMPATIBLE else "")
            if c.video_codec == VIDEO_KEEP and not c.video_hw:
                tip = (" " + t("confirm.hdr_tip_gpu"))
            warnings.append(t("confirm.hdr", n=len(hdr)) + tip)
        broken = [e for e in plan if e.skip_reason and e.skip_reason not in (raw_strip, raw_resize, raw_edit,
                                                                              video_edit)]
        if broken:
            warnings.append(t("confirm.broken", n=len(broken)))
        pending = sum(1 for e in plan if e.photo.info is None)
        if pending:
            warnings.append(t("confirm.pending", n=pending))
        raws = [e for e in plan if not e.skip_reason and e.photo.fmt() == Fmt.RAW]
        if raws and c.convert_to_jpeg:
            how = (t("confirm.raw_auto") if c.raw_mode == RAW_AUTO
                   else t("confirm.raw_develop"))
            exposed = sum(1 for e in raws if e.edit is not None and e.edit.exposure)
            note = (" " + t("confirm.raw_exposed", n=exposed)
                    if exposed and c.raw_mode == RAW_AUTO else "")
            warnings.append(t("confirm.raw", n=len(raws), how=how) + note)
        conflicts = [e for e in plan if e.renamed_for_conflict]
        if conflicts:
            warnings.append(t("confirm.conflicts", n=len(conflicts)))
        if r.enabled and r.pattern != NAME_NUMBER and c.strip_metadata:
            warnings.append(t("confirm.date_in_names"))
        leaks = [e for e in plan if getattr(e, "name_leak", None)]
        if leaks:
            warnings.append(t("confirm.name_leaks", n=len(leaks)))
        if not c.convert_to_jpeg:
            heavy = [e for e in plan if not e.skip_reason and e.photo.fmt() in (Fmt.HEIF, Fmt.AVIF)
                     and (c.strip_metadata or e.edit is not None)]
            if heavy:
                why = (t("confirm.heavy_strip") if c.strip_metadata
                       else t("confirm.heavy_edit"))
                warnings.append(t("confirm.heavy", n=len(heavy), why=why))
        warnings.extend(self._space_warnings(plan, opts))
        sheet = Sheet(self, t("confirm.title", what=what), "warning" if o.in_place else "checkmark.circle", width=600)
        summary = QLabel(body + place)
        summary.setTextFormat(Qt.TextFormat.RichText)
        summary.setWordWrap(True)
        summary.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        sheet.body.addWidget(summary)
        for warning in warnings:
            row = QHBoxLayout()
            row.setSpacing(8)
            label = QLabel(warning)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            icon = IconView("warning", "WARN", 16)
            icon.setFixedSize(16, label.fontMetrics().height())  # centered on the first line
            row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(label, 1)
            sheet.body.addLayout(row)
        sheet.add_button(t("common.cancel"), "reject")
        start = sheet.add_button(t("main.start"), "accept", prominent=True)
        sheet.exec()
        accepted = sheet.clickedButton() is start
        sheet.deleteLater()  # (a sheet belongs to its window, which would keep every one of them)
        return accepted
