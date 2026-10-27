"""Panel that shows which metadata the clicked photo or video contains."""

from __future__ import annotations

import html
import os
from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from ..core.formats import DISPLAY_NAME, Fmt
from ..core.meta import Kind, ScanResult, scan_file
from ..core.photoinfo import PhotoInfo, read_info
from ..i18n import date_time, t
from . import size_text, theme

CACHE_LIMIT = 200  # files whose findings are kept (shown again without reading the file)


class _Emitter(QObject):
    done = Signal(str, object, object, str)


class _ScanTask(QRunnable):
    def __init__(self, path: str, emitter: _Emitter):
        super().__init__()
        self.path, self.emitter = path, emitter

    def run(self) -> None:
        info = read_info(self.path)
        result, error = None, ""
        if info.error is None:
            try:
                result = scan_file(self.path)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
        self.emitter.done.emit(self.path, info, result, error or (info.error or ""))


# Order of importance: personal data first.
_ORDER = {k: i for i, k in enumerate((
    Kind.GPS, Kind.MAKERNOTE, Kind.IPTC, Kind.C2PA, Kind.TRAILER, Kind.EXIF, Kind.XMP, Kind.TAGS, Kind.TRACK,
    Kind.EMBEDDED, Kind.SUBTITLE, Kind.THUMBNAIL, Kind.EXTRA_IMAGE, Kind.COMMENT, Kind.TEXT, Kind.PHOTOSHOP,
    Kind.CHAPTERS, Kind.TIMESTAMP, Kind.ICC, Kind.OTHER,
))}


class InfoPanel(QWidget):
    """`colors`: fixed colors for the tokens it uses (the large preview's always-dark look);
    by default the theme's. `contentChanged` follows every new text (the preview's card fits it)."""

    contentChanged = Signal()

    def __init__(self, parent=None, title_name: str = "SectionTitle", colors: dict[str, str] | None = None):
        super().__init__(parent)
        self.colors = colors
        self._title = title = QLabel(t("info.title"))
        title.setObjectName(title_name)
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.RichText)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setMinimumWidth(10)
        self.body.setStyleSheet("background: transparent;")
        self.scroll = scroll = QScrollArea()
        scroll.setObjectName("Sidebar")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.body)
        scroll.viewport().setAutoFillBackground(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 10, 10)
        layout.setSpacing(6)
        layout.addWidget(title)
        layout.addWidget(scroll, 1)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._emitter = _Emitter()
        self._emitter.done.connect(self._on_done)
        self._current: str | None = None
        self._cache: OrderedDict[tuple[str, float], tuple] = OrderedDict()  # the last CACHE_LIMIT files
        self.clear()
        theme.changes().changed.connect(self._theme_changed)

    def _theme_changed(self) -> None:
        """The text carries its colors: written again with the new theme's."""
        path = self._current
        if path is None:
            self.clear()
        else:
            self._current = None
            self.show_path(path)

    def content_height(self, width: int) -> int:
        """The height that shows the whole text at `width`."""
        lay = self.layout()
        m = lay.contentsMargins()
        text_w = max(10, width - m.left() - m.right() - 2)
        return (m.top() + m.bottom() + self._title.sizeHint().height() + lay.spacing()
                + self.body.heightForWidth(text_w) + 4)

    def _set_text(self, text: str) -> None:
        self.body.setText(text)
        self.contentChanged.emit()

    def _c(self, token: str) -> str:
        if self.colors is not None and token in self.colors:
            return self.colors[token]
        return getattr(theme, token)

    def clear(self) -> None:
        self._current = None
        self._set_text(
            f"<span style='color:{self._c('MUTED')}'>{t('info.placeholder')}</span>"
        )

    def show_path(self, path: str) -> None:
        if path == self._current:
            return
        self._current = path
        try:
            key = (path, os.path.getmtime(path))
        except OSError:
            self._set_text(t("info.not_found"))
            return
        if key in self._cache:
            self._cache.move_to_end(key)
            self._render(path, *self._cache[key])
            return
        self._set_text(f"<b style='color:{self._c('TEXT')}'>{html.escape(os.path.basename(path))}</b><br>"
                          f"<span style='color:{self._c('MUTED')}'>{t('info.reading')}</span>")
        self._pool.clear()
        self._pool.start(_ScanTask(path, self._emitter))

    @Slot(str, object, object, str)
    def _on_done(self, path: str, info: PhotoInfo, result: ScanResult | None, error: str) -> None:
        try:
            self._cache[(path, os.path.getmtime(path))] = (info, result, error)
        except OSError:
            pass
        while len(self._cache) > CACHE_LIMIT:  # (browsing thousands of photos would keep them all)
            self._cache.popitem(last=False)
        if path == self._current:
            self._render(path, info, result, error)

    def _render(self, path: str, info: PhotoInfo, result: ScanResult | None, error: str) -> None:
        esc = html.escape
        name = esc(os.path.basename(path))
        muted, warn, ok, text = self._c("MUTED"), self._c("WARN"), self._c("SUCCESS"), self._c("TEXT")
        parts = [f"<div style='color:{text}'><b style='font-size:10.5pt'>{name}</b>"]
        details = []
        if info.width:
            details.append(f"{info.width} × {info.height}")
        if info.fmt == Fmt.RAW:
            details.append(f"RAW ({os.path.splitext(path)[1].lstrip('.').upper()})")
        elif info.fmt:
            details.append(DISPLAY_NAME[info.fmt])
        video = getattr(info, "video", None)
        if video is not None:
            details.append(video.short())
        try:
            details.append(size_text(os.path.getsize(path)))
        except OSError:
            pass
        parts.append(f"<br><span style='color:{muted}'>{' · '.join(details)}</span>")
        if error:
            parts.append(f"<p style='color:{self._c('DANGER')}'>{esc(error)}</p></div>")
            self._set_text("".join(parts))
            return
        s = result.summary if result else None
        lines = []
        if s and s.camera:
            lines.append(t("info.camera", value=esc(s.camera)))
        if s and s.taken:
            lines.append(t("info.taken", value=date_time(s.taken)))
        if s and s.gps:
            lines.append(f"<span style='color:{warn}'>{t('info.location', value=f'{s.gps[0]:.5f}, {s.gps[1]:.5f}')}</span>")
        if s and s.software:
            lines.append(t("info.software", value=esc(s.software)))
        if s and s.icc_description:
            lines.append(t("info.profile", value=esc(s.icc_description)))
        kept = [track.label for track in getattr(video, "subtitle_tracks", []) if not track.telemetry]
        if kept:
            lines.append(t("info.subtitles", value=esc(", ".join(kept)), muted=muted))
        if lines:
            parts.append("<p style='margin-top:8px'>" + "<br>".join(lines) + "</p>")
        if result is None or result.clean:
            parts.append(f"<p style='color:{ok}'><b>{t('info.clean')}</b></p>")
        else:
            items = []
            ordered = sorted(result.findings, key=lambda f: (not f.sensitive, _ORDER.get(f.kind, 99)))
            for f in ordered:
                color = warn if f.sensitive else text
                weight = "600" if f.sensitive else "400"
                detail = f" <span style='color:{muted}'>({esc(f.detail)})</span>" if f.detail else ""
                items.append(f"<span style='color:{color}; font-weight:{weight}'>•&nbsp;{esc(f.label)}</span>{detail}")
            parts.append(f"<p style='margin-top:8px'><b>{t('info.found', n=len(result.findings))}</b><br>")
            parts.append("<br>".join(items) + "</p>")
            hint = (t("info.hint_raw")
                    if info.fmt == Fmt.RAW else t("info.hint"))
            parts.append(f"<p style='color:{muted}'>{hint}</p>")
        parts.append("</div>")
        self._set_text("".join(parts))
