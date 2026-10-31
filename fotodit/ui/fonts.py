"""The bundled typeface: Onest 2.001, under the SIL Open Font License (`licenses/Onest.txt`).

Onest is the typeface Artapa sets its text in (artapa.com); the app takes it with the rest of Artapa's
look. Artapa's other face, General Sans, stays out: its license forbids sharing the font through a
public repository, and Fotodit's source is public. The three hinted static faces (Regular, Medium,
SemiBold) are bundled rather than the variable font: Qt 6.11 renders variable fonts set by weight
wrongly (QTBUG-149199). The files are used as published, unmodified.
"""

from __future__ import annotations

import logging
import os

from PySide6.QtGui import QFontDatabase

log = logging.getLogger(__name__)

FAMILY = "Onest"
FILES = ("Onest-Regular.ttf", "Onest-Medium.ttf", "Onest-SemiBold.ttf")
# fotodit.spec ships fotodit/resources, so the folder is found the same way in the exe.
FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "fonts")

_loaded: bool | None = None


def load() -> bool:
    """Registers the faces once (QApplication must exist); True if Onest can be used."""
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
