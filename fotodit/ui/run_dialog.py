"""Job sheet: progress, cancellation and the result report, on a glass card over the window."""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QProgressBar, QTreeWidget, QTreeWidgetItem

from ..core.planner import JobOptions, PlanEntry
from ..core.runner import (
    STAGE_COMMIT,
    STAGE_FILE,
    STAGE_PROCESS,
    STAGE_RECYCLE,
    Runner,
    RunReport,
)
from ..i18n import percent, t
from ..winutil import reveal
from . import number, theme
from .sheet import Sheet

DETAIL_LIMIT = 1000  # maximum number of files shown in the result list (per section)


class RunWorker(QObject):
    progress = Signal(str, int, int, str)
    finished = Signal(object)

    def __init__(self, plan: list[PlanEntry], opts: JobOptions):
        super().__init__()
        self.plan = plan
        self.opts = opts
        self.cancel = threading.Event()

    @Slot()
    def run(self) -> None:
        runner = Runner(self.plan, self.opts, on_progress=self.progress.emit, cancel_event=self.cancel)
        try:
            report = runner.run()
        except Exception as exc:  # noqa: BLE001 - an unexpected error is shown in the dialog
            report = exc
        self.finished.emit(report)


class RunDialog(Sheet):
    def __init__(self, plan: list[PlanEntry], opts: JobOptions, parent=None):
        super().__init__(parent, t("run.processing"), None, width=620)
        self.setWindowTitle(t("run.window_processing"))
        self.report: RunReport | None = None
        self._opts = opts
        self._total = len(plan)
        self._running = True

        self.current = QLabel(" ")
        self.current.setObjectName("Muted")
        self.bar = QProgressBar()
        self.bar.setRange(0, max(1, self._total))
        self.bar.setTextVisible(False)
        self.count = QLabel(f"0 / {number(self._total)}")
        self.count.setObjectName("Muted")
        self.count.setFont(theme.tabular(self.count.font()))
        # Per-file progress for long videos (re-encoding can take minutes).
        self.file_bar = QProgressBar()
        self.file_bar.setRange(0, 1000)
        self.file_bar.setTextVisible(False)
        self.file_bar.setMaximumHeight(6)
        self.file_bar.hide()
        self.file_label = QLabel()
        self.file_label.setObjectName("Muted")
        self.file_label.hide()
        self.details = QTreeWidget()
        self.details.setObjectName("Results")
        self.details.setHeaderLabels([t("run.col_file"), t("run.col_result")])
        self.details.setRootIsDecorated(True)
        self.details.setColumnWidth(0, 230)
        self.details.setMinimumHeight(220)
        self.details.hide()
        for w in (self.current, self.bar, self.count, self.file_label, self.file_bar, self.details):
            self.body.addWidget(w)
        self.cancel_btn = self.add_button(t("run.stop"), "none")
        self.cancel_btn.clicked.connect(self._cancel)
        self.open_btn = self.add_button(t("run.open_folder"), "none")
        self.open_btn.hide()
        self.open_btn.clicked.connect(self._open_folder)
        self.close_btn = self.add_button(t("common.close"), "accept", prominent=True)
        self.close_btn.hide()
        self.details.setStyleSheet(
            f"QTreeWidget#Results {{ background: {theme.PANEL}; border-radius: 10px; padding: 4px; }}"
            "QTreeWidget#Results QHeaderView::section { background: transparent; }")

        self._thread = QThread(self)
        self._worker = RunWorker(plan, opts)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    # ------------------------------------------------------------------ progress

    @Slot(str, int, int, str)
    def _on_progress(self, stage: str, done: int, total: int, name: str) -> None:
        if stage == STAGE_FILE:
            if done >= total:
                self.file_bar.hide()
                self.file_label.hide()
                return
            self.file_bar.setValue(done)
            self.file_label.setText(t("run.video_progress", name=name, percent=percent(done // 10)))
            self.file_bar.show()
            self.file_label.show()
            return
        if stage == STAGE_PROCESS:
            self.title.setText(t("run.verifying"))
        elif stage == STAGE_RECYCLE:
            self.title.setText(t("run.recycling"))
        elif stage == STAGE_COMMIT:
            self.title.setText(t("run.saving"))
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)
        self.count.setText(f"{number(done)} / {number(total)}")
        if name:
            self.current.setText(name)

    def _cancel(self) -> None:
        self._worker.cancel.set()
        self.cancel_btn.setEnabled(False)
        self.title.setText(t("run.cancelling"))

    def reject(self) -> None:  # Esc / closing the window
        if self._running:
            self._cancel()
            return
        super().reject()

    def done(self, result: int) -> None:
        if self._running:  # the sheet stays until the job has ended (cancelling is Esc or İptal)
            return
        super().done(result)

    def closeEvent(self, event) -> None:
        if self._running:
            self._cancel()
            event.ignore()
            return
        super().closeEvent(event)

    # ------------------------------------------------------------------ result

    @Slot(object)
    def _on_finished(self, report) -> None:
        self._running = False
        self._thread.quit()
        self._thread.wait()
        self.cancel_btn.hide()
        self.file_bar.hide()
        self.file_label.hide()
        self.close_btn.show()
        self.close_btn.setDefault(True)
        self.setWindowTitle(t("run.window_results"))
        if isinstance(report, Exception):
            self.title.setText(t("run.unexpected"))
            self.current.setText(str(report))
            self.title.setStyleSheet(f"color:{theme.DANGER}")
            self._place_card()
            return
        self.report = report
        self.bar.setValue(self.bar.maximum())
        ok, failed = report.succeeded, report.failed
        if report.cancelled:
            self.title.setText(t("run.cancelled"))
            self.current.setText(t("run.nothing_changed"))
            self._place_card()
            return
        head = t("run.done", n=len(ok))
        if failed:
            head += " · " + t("run.failed_suffix", n=len(failed))
        self.title.setText(head)
        self.title.setStyleSheet(f"color:{theme.SUCCESS if not failed else theme.WARN}")
        self.count.setText(f"{number(len(ok) + len(failed))} / {number(self._total)}")
        lines = []
        if report.in_place and report.recycled:
            lines.append(t("run.recycled", n=report.recycled))
        elif report.output_folder and ok:
            lines.append(t("run.output_folder", folder=report.output_folder))
            self.open_btn.show()
        self.current.setText("\n".join(lines))
        self.current.setWordWrap(True)
        self._fill_details(report)
        self._place_card()

    def _fill_details(self, report: RunReport) -> None:
        failed = report.failed
        warned = [r for r in report.succeeded if r.notes]
        if not failed and not warned:
            return
        self.details.show()
        self._card_w = 780
        if failed:
            top = QTreeWidgetItem([t("run.errors", n=len(failed)), t("run.errors_note")])
            top.setForeground(0, QColor(theme.DANGER))
            for r in failed[:DETAIL_LIMIT]:
                item = QTreeWidgetItem([r.entry.photo.name, r.error or ""])
                item.setToolTip(0, r.entry.photo.path)
                item.setToolTip(1, r.error or "")
                top.addChild(item)
            self._add_more(top, len(failed))
            self.details.addTopLevelItem(top)
            top.setExpanded(True)
        if warned:
            top = QTreeWidgetItem([t("run.notes", n=len(warned)), t("run.notes_note")])
            top.setForeground(0, QColor(theme.WARN))
            shown = 0
            for r in warned:
                final = r.final_path or r.entry.target_path
                if report.output_folder:
                    name = os.path.relpath(final, report.output_folder)
                else:
                    name = os.path.basename(final)
                for note in r.notes:
                    item = QTreeWidgetItem([name, note])
                    item.setToolTip(0, final)
                    item.setToolTip(1, note)
                    top.addChild(item)
                shown += 1
                if shown >= DETAIL_LIMIT:
                    break
            self._add_more(top, len(warned))
            self.details.addTopLevelItem(top)
            top.setExpanded(len(warned) <= 20)
        # The list is as tall as its open rows, between 220 and 420 pixels (then it scrolls).
        tops = [self.details.topLevelItem(i) for i in range(self.details.topLevelItemCount())]
        rows = sum(1 + (top.childCount() if top.isExpanded() else 0) for top in tops)
        row_h = self.details.sizeHintForRow(0)
        if row_h <= 0:
            row_h = self.details.fontMetrics().height() + 4
        header = self.details.header().sizeHint().height()
        self.details.setMinimumHeight(max(220, min(420, rows * row_h + header + 10)))

    @staticmethod
    def _add_more(top: QTreeWidgetItem, total: int) -> None:
        """For jobs with thousands of files, the list is limited to the first DETAIL_LIMIT files."""
        if total > DETAIL_LIMIT:
            more = QTreeWidgetItem([t("run.more", n=total - DETAIL_LIMIT), ""])
            more.setForeground(0, QColor(theme.MUTED))
            top.addChild(more)

    def _open_folder(self) -> None:
        if self.report and self.report.output_folder and os.path.isdir(self.report.output_folder):
            reveal(self.report.output_folder)
