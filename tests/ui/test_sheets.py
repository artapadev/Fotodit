"""Sheets: the job sheet stays while its job runs, a sheet that deleted itself leaves nothing behind
that a theme change would call, and a closed sheet lets go of its picture of the window (offscreen Qt)."""

from __future__ import annotations

import os
import sys
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from helpers import wait_until  # noqa: E402
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from fotodit.core.planner import JobOptions  # noqa: E402
from fotodit.ui import run_dialog, theme  # noqa: E402
from fotodit.ui.sheet import Sheet, offer  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def host(app):
    window = QWidget()
    window.resize(800, 600)
    window.show()
    app.processEvents()
    yield window
    window.close()


def _enter(widget: QWidget) -> None:
    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))


def test_the_job_sheet_stays_while_its_job_runs(app, host, monkeypatch):
    release = threading.Event()

    class HeldRunner:
        def __init__(self, plan, opts, on_progress=None, cancel_event=None):
            pass

        def run(self):
            release.wait(10)
            raise RuntimeError("done")  # shown as an unexpected error: enough to end the job

    monkeypatch.setattr(run_dialog, "Runner", HeldRunner)
    dialog = run_dialog.RunDialog([], JobOptions(), host)
    dialog.show()
    app.processEvents()
    _enter(dialog)  # the Close button is the default already, but hidden while the job runs
    dialog.accept()
    app.processEvents()
    assert dialog.isVisible() and dialog._running
    release.set()
    assert wait_until(app, lambda: not dialog._running)
    _enter(dialog)  # now Close shows and Enter presses it
    assert not dialog.isVisible()
    dialog.deleteLater()


def test_a_deleted_sheet_is_not_called_by_a_theme_change(app, host, monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc))
    sheet = offer(host, "Title", "Text", "Yes", "No", lambda: None)
    app.processEvents()
    sheet.buttons()[0].click()  # No: the sheet closes and deletes itself
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    theme.changes().changed.emit()
    app.processEvents()
    assert not errors


def test_a_closed_sheet_lets_go_of_the_window_picture(app, host):
    sheet = Sheet(host, "Title")
    sheet.add_button("OK", "accept", prominent=True)
    sheet.show()
    app.processEvents()
    assert sheet._backdrop is not None  # the dimmed window behind the glass card
    sheet.accept()
    app.processEvents()
    assert sheet._backdrop is None and sheet._glass_image is None
    sheet.deleteLater()
