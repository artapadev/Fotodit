"""Application entry point."""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

from . import APP_NAME, __version__

log = logging.getLogger("fotodit")


def data_dir() -> str:
    """Settings and log folder: %LOCALAPPDATA%\\Fotodit (can be overridden with FOTODIT_DATA_DIR)."""
    path = os.environ.get("FOTODIT_DATA_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME
    )
    os.makedirs(path, exist_ok=True)
    return path


def resource(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", name)


def _setup_logging() -> str:
    log_path = os.path.join(data_dir(), "fotodit.log")
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # In the windowed exe sys.stdout/sys.stderr are None; they are redirected to a
    # file so that libraries trying to write to them do not crash.
    if sys.stdout is None or sys.stderr is None:
        stream = open(os.path.join(data_dir(), "fotodit-console.log"), "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    # On Windows faulthandler also reports exceptions that system components catch and
    # handle internally as "fatal" (e.g. 0x8001010e in the folder picker dialog); to
    # avoid misleading reports it is enabled only in debug mode.
    if os.environ.get("FOTODIT_DEBUG"):
        try:
            faulthandler.enable(file=open(os.path.join(data_dir(), "fotodit-crash.log"), "a"))  # noqa: SIM115
        except (OSError, RuntimeError):
            pass
    return log_path


def _set_app_id() -> None:
    """Show the Fotodit icon in the taskbar instead of the Python icon."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Fotodit.Fotodit")
        except (AttributeError, OSError):
            pass


def main() -> int:
    if "--self-test" in sys.argv:
        from .selftest import run

        idx = sys.argv.index("--self-test")
        target = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else None
        return run(target)

    log_path = _setup_logging()
    log.info("Starting Fotodit %s", __version__)
    _set_app_id()
    # While background threads read photo info, the UI thread waits less for the GIL
    # (the default switch interval is 5 ms; painting the table acquires it hundreds of times).
    sys.setswitchinterval(0.001)

    from PySide6.QtCore import QLibraryInfo, QLocale, QSettings, QTranslator
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from . import i18n

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setApplicationVersion(__version__)
    settings = QSettings(os.path.join(data_dir(), "ayarlar.ini"), QSettings.Format.IniFormat)
    # The language comes first: every window takes its texts from it as it is built.
    i18n.set_language(i18n.resolve(str(settings.value("appearance/language", i18n.SYSTEM) or i18n.SYSTEM)))
    log.info("Language: %s", i18n.language())

    from .core.imaging import ensure_plugins
    from .ui import theme
    from .ui.main_window import MainWindow

    theme.apply(app, str(settings.value("appearance/theme", theme.SYSTEM) or theme.SYSTEM))

    if i18n.language() == "tr":  # Qt's own texts (the file dialogs' buttons …) are English otherwise
        translator = QTranslator(app)
        translations = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if translator.load(QLocale(QLocale.Language.Turkish, QLocale.Country.Turkey), "qtbase", "_", translations):
            app.installTranslator(translator)

    icon = QIcon(resource("fotodit.ico"))
    app.setWindowIcon(icon)

    def excepthook(exc_type, exc, tb):
        log.critical("Unexpected error", exc_info=(exc_type, exc, tb))
        try:
            QMessageBox.critical(None, APP_NAME, i18n.t("app.crash", error=exc, log=log_path))
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = excepthook
    threading.excepthook = lambda args: log.critical(
        "Error in thread", exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
    )

    ensure_plugins()
    window = MainWindow(settings, icon)
    window.show()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        window.open_paths(args)
    return app.exec()
