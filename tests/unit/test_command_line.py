"""Paths on Fotodit's command line, as Explorer's Send To hands them over."""

from __future__ import annotations

import ctypes
import sys

import pytest

from fotodit import app

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="8.3 names exist only on Windows")


def _short(path: str) -> str:
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(path, buffer, 1024)
    return buffer.value


def test_an_8_3_name_from_send_to_becomes_the_long_name(tmp_path):
    target = tmp_path / "deneme fotoğrafı.jpg"
    target.write_bytes(b"x")
    long = str(target.resolve())
    short = _short(long)
    if short == long:
        pytest.skip("this volume keeps no 8.3 names")
    assert app.long_path(short) == long


def test_a_long_or_missing_path_stays_as_it_is(tmp_path):
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"x")
    assert app.long_path(str(target.resolve())) == str(target.resolve())
    missing = str(tmp_path / "DENEME~1.JPG")
    assert app.long_path(missing) == missing
