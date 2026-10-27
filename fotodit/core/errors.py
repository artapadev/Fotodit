"""Turns library error messages into explanations, in the app's language, shown to the user."""

from __future__ import annotations

from PIL import Image

from ..i18n import t


def explain(exc: BaseException) -> str:
    if isinstance(exc, Image.DecompressionBombError):
        return t("error.too_big")
    if isinstance(exc, MemoryError):
        return t("error.out_of_memory")
    if isinstance(exc, PermissionError):
        return t("error.no_access")
    if isinstance(exc, FileNotFoundError):
        return t("error.not_found")
    text = str(exc)
    low = text.lower()
    if "truncated" in low or "premature end" in low or "broken data stream" in low:
        return t("error.truncated")
    if "cannot identify image" in low or "unidentifiedimage" in low or "not a " in low:
        return t("error.unsupported")
    if "exif data is too long" in low or "xmp data is too long" in low:
        return t("error.too_much_meta")
    if "decoder" in low or "decoding" in low or "corrupt" in low:
        return t("error.decode")
    return t("error.other", error=text or type(exc).__name__)
