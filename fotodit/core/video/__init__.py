"""Video support: format info (`probe`), metadata stripping and conversion to MP4 (`process`),
data embedded in the bitstream (`bitstream`), MP4/MOV boxes (`boxes`), HDR tone mapping
(`tonemap`) and thumbnails (`thumb`).

Videos are read and written with PyAV (FFmpeg 8). FFmpeg writes detailed info lines for every
file (encoder settings, stream dump); in a windowed app these only bloat the log file, so on
first use FFmpeg's log level is limited to errors.
"""

from __future__ import annotations

_quiet = False


def quiet_ffmpeg() -> None:
    """Turns off info lines in PyAV's copy of FFmpeg (Qt's own copy: `fotodit.ui.player`)."""
    global _quiet
    if _quiet:
        return
    _quiet = True
    try:
        import av.logging

        av.logging.set_libav_level(av.logging.ERROR)
    except (ImportError, AttributeError):
        pass

