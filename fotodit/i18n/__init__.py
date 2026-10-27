"""Fotodit's texts in Turkish and English.

Every string a person reads comes from the dictionaries `tr.json` and `en.json` through `t()`; code
never holds it. A text with a count picks its form by the count: in English "{n} photo" or "{n}
photos" (`{"one": …, "other": …}`), in Turkish one form serves both. Numbers, decimals, sizes,
percentages and dates are written the language's way. The language is set once at start
(`set_language`): following Windows unless the user chose one; changing it takes a restart.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime

LANGUAGES = ("tr", "en")
NAMES = {"tr": "Türkçe", "en": "English"}  # each in its own language
SYSTEM = "system"  # the setting that follows Windows' display language
_FOLDER = os.path.dirname(os.path.abspath(__file__))
_language = "tr"
_texts: dict[str, dict[str, object]] = {}


def _dictionary(code: str) -> dict[str, object]:
    texts = _texts.get(code)
    if texts is None:
        with open(os.path.join(_FOLDER, f"{code}.json"), encoding="utf-8") as fh:
            texts = _texts[code] = json.load(fh)
    return texts


def language() -> str:
    return _language


def set_language(code: str) -> None:
    """Uses `code` ("tr" or "en"; anything else means Turkish) from now on."""
    global _language
    _language = code if code in LANGUAGES else "tr"


def system_language() -> str:
    """Windows' display language: Turkish if it is Turkish, English otherwise."""
    try:
        import ctypes

        lang = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF  # primary language id
        return "tr" if lang == 0x1F else "en"
    except (AttributeError, OSError):
        return "tr"


def resolve(choice: str) -> str:
    """The language a setting means: "tr" or "en" as chosen, Windows' language for anything else."""
    return choice if choice in LANGUAGES else system_language()


def t(key: str, /, **values: object) -> str:
    """The text for `key` in the current language, with `{name}` filled in from `values`. An int
    `n` chooses the plural form and is written as a number (12.345 / 12,345)."""
    entry = _dictionary(_language).get(key)
    if entry is None:
        entry = _dictionary("tr").get(key, key)
    if isinstance(entry, dict):
        count = values.get("n")
        entry = entry["one"] if count == 1 and "one" in entry else entry["other"]
    if not values:
        return str(entry)
    if isinstance(values.get("n"), int):
        values = {**values, "n": number(values["n"])}
    return str(entry).format(**values)


def has(key: str) -> bool:
    return key in _dictionary(_language) or key in _dictionary("tr")


def capitalize(text: str) -> str:
    """The first letter in upper case, the Turkish way in Turkish (i → İ, ı → I)."""
    if not text:
        return text
    first = {"i": "İ", "ı": "I"}.get(text[0], text[0].upper()) if _language == "tr" else text[0].upper()
    return first + text[1:]


def decapitalize(text: str) -> str:
    """The first letter in lower case, the Turkish way in Turkish (İ → i, I → ı)."""
    if not text:
        return text
    first = {"İ": "i", "I": "ı"}.get(text[0], text[0].lower()) if _language == "tr" else text[0].lower()
    return first + text[1:]


# ------------------------------------------------------------------ numbers, sizes, dates


def number(n: int) -> str:
    """12345 → "12.345" (Turkish) or "12,345" (English)."""
    text = f"{n:,}"
    return text.replace(",", ".") if _language == "tr" else text


def decimal_mark() -> str:
    """"," (Turkish) or "." (English)."""
    return "," if _language == "tr" else "."


def decimal(x: float, digits: int = 1, trim: bool = False) -> str:
    """1.5 → "1,5" (Turkish) or "1.5" (English); `trim` drops trailing zeros (2.0 → "2")."""
    text = f"{x:.{digits}f}"
    if trim and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", decimal_mark())


def percent(p: float | int) -> str:
    """95 → "%95" (Turkish) or "95%" (English)."""
    value = str(p) if isinstance(p, int) else decimal(p)
    return f"%{value}" if _language == "tr" else f"{value}%"


def size(n: int) -> str:
    """A byte count: "1,5 GB", "820,0 MB", "12 KB" (with the language's decimal mark)."""
    if n >= 1 << 30:
        return f"{decimal(n / (1 << 30))} GB"
    if n >= 1 << 20:
        return f"{decimal(n / (1 << 20))} MB"
    return f"{max(1, n >> 10)} KB"


def date_time(when: datetime) -> str:
    """"12.05.2024 14:30" (Turkish) or "2024-05-12 14:30" (English)."""
    return when.strftime("%d.%m.%Y %H:%M" if _language == "tr" else "%Y-%m-%d %H:%M")


def day(when: date) -> str:
    """"12.05.2024" (Turkish) or "2024-05-12" (English)."""
    return when.strftime("%d.%m.%Y" if _language == "tr" else "%Y-%m-%d")


def month_name(month: int) -> str:
    """1 → "Ocak" / "January"."""
    return t(f"month.{month}")
