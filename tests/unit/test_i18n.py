"""The two dictionaries and the texts in the code: every text is in both languages, with the same
placeholders, every key the code names exists, no key goes unused and no Turkish text is left in the
code outside the dictionaries."""

from __future__ import annotations

import ast
import json
import os
import re
import string
from datetime import datetime

import pytest

from fotodit import i18n
from fotodit.i18n import t

PACKAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "fotodit")
KEY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
TURKISH = re.compile(r"[çğıöşüÇĞİÖŞÜ]")
# Turkish in the code that is data, not text for the user.
ALLOWED_TURKISH = {
    "i18n/__init__.py": ("Türkçe", "İ", "ı"),  # language names; the Turkish capital and small i
    "selftest.py": ("Ayşe",),  # a file name with Turkish letters (Unicode names are checked)
    "core/dngwriter.py": ("Fotodit DNG yazıcısı",),  # test DNGs: UTF-8 in an ASCII tag is read correctly
    "core/photoinfo.py": ("ekran görüntüsü",),  # Windows' Turkish screenshot names give the date away
}


def _load(code: str) -> dict:
    with open(os.path.join(PACKAGE, "i18n", f"{code}.json"), encoding="utf-8") as fh:
        return json.load(fh)


TR, EN = _load("tr"), _load("en")


def _forms(entry) -> list[str]:
    return list(entry.values()) if isinstance(entry, dict) else [entry]


def _placeholders(entry) -> set[str]:
    return {name for form in _forms(entry) for _, name, _, _ in string.Formatter().parse(form) if name}


def _sources():
    for folder, _, files in os.walk(PACKAGE):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, PACKAGE).replace(os.sep, "/")
                with open(path, encoding="utf-8") as fh:
                    yield rel, ast.parse(fh.read())


def _docstrings(tree) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _strings():
    """(file, text, the f-string it is part of or None) for every string in the code but docstrings."""
    for rel, tree in _sources():
        docs = _docstrings(tree)
        parent = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                for value in node.values:
                    parent[id(value)] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                yield rel, node.value, parent.get(id(node))


def test_both_languages_have_the_same_texts():
    assert sorted(TR) == sorted(EN)
    for key in TR:
        assert _placeholders(TR[key]) == _placeholders(EN[key]), key
        for entry in (TR[key], EN[key]):
            if isinstance(entry, dict):
                assert set(entry) <= {"one", "other"} and "other" in entry, key


def test_every_text_formats():
    for texts in (TR, EN):
        for key, entry in texts.items():
            values = {name: 1 for name in _placeholders(entry)}
            for form in _forms(entry):
                assert form.format(**values), key
                assert form == form.strip(), key  # spaces and line breaks around a text belong to the code


def test_english_has_no_turkish_letters():
    for key, entry in EN.items():
        for form in _forms(entry):
            assert not TURKISH.search(form), key


def test_keys_named_in_the_code_exist():
    namespaces = {key.split(".")[0] for key in TR}
    missing = sorted({(rel, text) for rel, text, fstring in _strings()
                      if fstring is None and KEY.match(text) and text.split(".")[0] in namespaces and text not in TR
                      and rel != "ui/icons.py"})  # icon names ("folder.fill") look like keys
    assert not missing


def test_every_key_is_used():
    named, prefixes = set(), set()
    for _, text, fstring in _strings():
        if fstring is None:
            named.add(text)
        elif fstring.values and fstring.values[0] is not None and re.match(r"^[a-z][a-z0-9_.]*\.$", text):
            prefixes.add(text)  # t(f"edit.flip.{tense}") uses every key under "edit.flip."
    unused = sorted(key for key in TR if key not in named and not any(key.startswith(p) for p in prefixes))
    assert not unused


def test_no_turkish_text_outside_the_dictionaries():
    left = []
    for rel, text, _ in _strings():
        if TURKISH.search(text) and not any(allowed in text for allowed in ALLOWED_TURKISH.get(rel, ())):
            left.append((rel, text[:60]))
    assert not left


# Calls whose first argument is a text a person reads; a word written there straight (not through
# t()) would show in one language only, like "Kalite:" without Turkish letters did.
TEXT_CALLS = {"QLabel", "setText", "setToolTip", "setPlaceholderText", "setWindowTitle", "setAccessibleName",
              "_labeled", "_hint", "_title", "QCheckBox", "QRadioButton", "Switch", "ElidedLabel", "show_message",
              "addItem", "setItemText", "QAction"}
NEUTRAL = {"RAW:"}  # the same in every language


def test_texts_shown_go_through_the_dictionaries():
    straight = []
    for rel, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            first = node.args[0]
            if name in TEXT_CALLS and isinstance(first, ast.Constant) and isinstance(first.value, str):
                if re.search(r"[A-Za-z]{2,}", first.value) and first.value not in NEUTRAL:
                    straight.append((rel, node.lineno, first.value))
    assert not straight


@pytest.fixture
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("tr")


def test_numbers_dates_and_plurals_in_turkish():
    assert i18n.language() == "tr"
    assert i18n.number(12345) == "12.345" and i18n.decimal(1.25, 2) == "1,25" and i18n.decimal(2.0, trim=True) == "2"
    assert i18n.percent(95) == "%95" and i18n.size(3 << 20) == "3,0 MB"
    assert i18n.date_time(datetime(2024, 5, 12, 14, 30)) == "12.05.2024 14:30" and i18n.month_name(5) == "Mayıs"
    assert t("count.photos.plain", n=1) == "1 fotoğraf" and t("count.photos.plain", n=1200) == "1.200 fotoğraf"
    assert i18n.capitalize("ilk") == "İlk" and i18n.decapitalize("Işık") == "ışık"


def test_numbers_dates_and_plurals_in_english(english):
    assert i18n.number(12345) == "12,345" and i18n.decimal(1.25, 2) == "1.25" and i18n.percent(95) == "95%"
    assert i18n.date_time(datetime(2024, 5, 12, 14, 30)) == "2024-05-12 14:30" and i18n.month_name(5) == "May"
    assert t("count.photos.plain", n=1) == "1 photo" and t("count.photos.plain", n=1200) == "1,200 photos"
    assert i18n.capitalize("ilk") == "Ilk" and i18n.decapitalize("In the order I chose") == "in the order I chose"


def test_a_missing_text_falls_back_to_turkish_then_to_the_key(english):
    assert t("no.such.key") == "no.such.key"
    assert t("png.ai", key="prompt") == "prompt (AI generation details)"  # a placeholder may be called "key"


def test_the_language_follows_the_setting():
    assert i18n.resolve("en") == "en" and i18n.resolve("tr") == "tr"
    assert i18n.resolve(i18n.SYSTEM) == i18n.system_language() in i18n.LANGUAGES
