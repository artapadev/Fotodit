"""The bundled typeface: Inter 4.1, under the SIL Open Font License (`licenses/Inter.txt`).

Inter is the open typeface closest to Apple's SF Pro, whose license forbids using it outside
Apple's platforms. The four hinted static faces are bundled rather than the variable font: their
hints keep small text crisp with Windows ClearType, and Qt 6.11 renders variable fonts set by
weight wrongly (QTBUG-149199). The files are used as published, unmodified.
"""

from __future__ import annotations

import logging
import os

from PySide6.QtGui import QFontDatabase

log = logging.getLogger(__name__)

FAMILY = "Inter"
FILES = ("Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf", "Inter-Bold.ttf")
# fotodit.spec ships fotodit/resources, so the folder is found the same way in the exe.
FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "fonts")

_loaded: bool | None = None


def load() -> bool:
    """Registers the faces once (QApplication must exist); True if Inter can be used."""
    global _loaded
    if _loaded is None:
        families: set[str] = set()
        for name in FILES:
            font_id = QFontDatabase.addApplicationFont(os.path.join(FOLDER, name))
            if font_id < 0:
                log.warning("Font not loaded: %s", name)
                continue
            families.update(QFontDatabase.applicationFontFamilies(font_id))
        _loaded = FAMILY in families
    return _loaded
