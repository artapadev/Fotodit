"""PySide6 user interface."""

from ..i18n import number, t
from ..i18n import size as size_text

__all__ = ["count_text", "number", "size_text"]


def count_text(photos: int, videos: int, accusative: bool = False, title: bool = False) -> str:
    """"12 fotoğraf, 3 video" / "12 photos, 3 videos"; as an object (Turkish accusative) "12 fotoğrafı
    ve 3 videoyu" / "12 photos and 3 videos"; `title` for a heading or a button: "12 Fotoğraf ve 3 Video",
    "12 Fotoğrafı ve 3 Videoyu" / "12 Photos and 3 Videos"."""
    form = ("object" if accusative else "plain") + ("_title" if title else "")
    parts = [t(f"count.{kind}.{form}", n=n) for kind, n in (("photos", photos), ("videos", videos)) if n]
    if not parts:
        return t(f"count.files.{form}", n=0)
    return t(f"count.join.{form}", first=parts[0], second=parts[1]) if len(parts) == 2 else parts[0]
