"""Duplicates within a selection: the same bytes, or the same picture with different metadata."""

from __future__ import annotations

import os
import shutil

import builders as b
import pytest
from helpers import write
from video_builders import make_video

from fotodit.core import duplicates
from fotodit.core.duplicates import EXACT, SAME_IMAGE, DuplicateGroup, find_duplicates
from fotodit.core.meta import jpeg as jpeg_meta
from fotodit.core.meta import png as png_meta
from fotodit.core.meta import webp as webp_meta
from fotodit.core.processor import ContentOptions, process_bytes

STRIP = ContentOptions(strip_metadata=True)


def cleaned(data: bytes) -> bytes:
    """The copy Fotodit makes when it strips the metadata losslessly."""
    r = process_bytes(data, STRIP)
    assert r.method == "lossless"
    return r.data


def copy(src: str, dst) -> str:
    shutil.copyfile(src, dst)
    return str(dst)


def test_exact_copies(tmp_path):
    a = write(tmp_path / "a.jpg", b.jpeg())
    other = write(tmp_path / "other.jpg", b.jpeg(b.photo().rotate(180)))
    os.makedirs(tmp_path / "sub")
    twin = copy(a, tmp_path / "sub" / "a (2).jpg")
    assert find_duplicates([a, other, twin]) == [DuplicateGroup([a, twin], EXACT)]
    assert find_duplicates([twin, other, a]) == [DuplicateGroup([twin, a], EXACT)]  # the given order is kept
    assert all(os.path.exists(p) for p in (a, other, twin))  # nothing is deleted


@pytest.mark.parametrize("name,make", [
    ("jpg", lambda: b.jpeg()), ("png", lambda: b.png()), ("webp", lambda: b.webp()),
    ("webp", lambda: b.animated_webp()), ("gif", lambda: b.gif()),
], ids=["jpeg", "png", "webp", "webp-animated", "gif"])
def test_same_picture_after_a_lossless_strip(tmp_path, name, make):
    data = make()
    original = write(tmp_path / f"original.{name}", data)
    clean = write(tmp_path / f"clean.{name}", cleaned(data))
    assert find_duplicates([clean, original]) == [DuplicateGroup([clean, original], SAME_IMAGE)]


def test_one_group_per_picture_with_its_exact_copies(tmp_path):
    data = b.jpeg()
    original = write(tmp_path / "IMG_0001.jpg", data)
    twin = write(tmp_path / "IMG_0001 - Kopya.jpg", data)
    clean = write(tmp_path / "clean.jpg", cleaned(data))
    heic = write(tmp_path / "a.heic", b.heic())
    heic_twin = write(tmp_path / "b.heic", open(heic, "rb").read())
    heic_clean = write(tmp_path / "c.heic", process_bytes(b.heic(), STRIP).data)  # re-encoded: not the same bytes
    groups = find_duplicates([heic, original, clean, heic_clean, twin, heic_twin])
    assert groups == [DuplicateGroup([heic, heic_twin], EXACT), DuplicateGroup([original, clean, twin], SAME_IMAGE)]


def test_different_pictures_are_not_grouped(tmp_path):
    base = b.photo()
    paths = [
        write(tmp_path / "a.jpg", b.jpeg(base)),
        write(tmp_path / "rotated.jpg", b.jpeg(base.rotate(180))),
        write(tmp_path / "requality.jpg", b.jpeg(base, quality=70)),  # the same scene saved again: other bytes
        write(tmp_path / "a.png", b.png(base)),  # the same pixels in another format
        write(tmp_path / "b.png", b.png(base.rotate(90))),
    ]
    assert find_duplicates(paths) == []


def test_videos_and_other_formats_by_their_bytes(tmp_path):
    video = make_video(str(tmp_path / "a.mp4"), frames_n=6)
    twin = copy(video, tmp_path / "b.mp4")
    other = make_video(str(tmp_path / "c.mp4"), frames_n=7)
    tif = write(tmp_path / "a.tif", b.tiff(pages=1))
    tif_twin = write(tmp_path / "b.tif", b.tiff(pages=1))
    assert find_duplicates([video, other, tif, twin, tif_twin]) == [
        DuplicateGroup([video, twin], EXACT), DuplicateGroup([tif, tif_twin], EXACT)]


def test_unreadable_files_are_skipped(tmp_path):
    a = write(tmp_path / "a.jpg", b.jpeg())
    twin = write(tmp_path / "b.jpg", b.jpeg())
    folder = tmp_path / "folder.jpg"
    folder.mkdir()
    empty = write(tmp_path / "empty.jpg", b"")
    empty2 = write(tmp_path / "empty2.jpg", b"")
    broken = write(tmp_path / "broken.jpg", b.jpeg()[:300])
    paths = [str(tmp_path / "missing.jpg"), a, str(folder), broken, twin, empty, empty2]
    groups = find_duplicates(paths)
    assert DuplicateGroup([a, twin], EXACT) in groups and DuplicateGroup([empty, empty2], EXACT) in groups
    assert len(groups) == 2


def test_the_same_file_given_twice_counts_once(tmp_path):
    a = write(tmp_path / "a.jpg", b.jpeg())
    assert find_duplicates([a, a, a.upper(), str(tmp_path / "." / "a.jpg")]) == []


def test_large_metadata_before_the_image_data(tmp_path):
    """A JPEG whose metadata is larger than the first look (an XMP of 300 KB): read further to its image."""
    big = b.insert_after_soi(b.jpeg(everything=False),
                             *[b.seg(0xE1, b"http://ns.adobe.com/xmp/extension/\x00" + bytes(60000)) for _ in range(5)])
    original = write(tmp_path / "big.jpg", big)
    clean = write(tmp_path / "clean.jpg", cleaned(big))
    other = write(tmp_path / "other.jpg", b.insert_after_soi(b.jpeg(b.photo().rotate(90), everything=False),
                                                              b.seg(0xFE, bytes(65000)), b.seg(0xFE, bytes(65000))))
    assert find_duplicates([original, other, clean]) == [DuplicateGroup([original, clean], SAME_IMAGE)]


def test_only_look_alikes_are_read_in_full(tmp_path, monkeypatch):
    whole: list[str] = []
    real_read, real_hash = duplicates._read, duplicates._hash_file

    def read(path, limit=None):
        if limit is None:
            whole.append(path)
        return real_read(path, limit)

    def hash_file(path):
        whole.append(path)
        return real_hash(path)

    monkeypatch.setattr(duplicates, "_read", read)
    monkeypatch.setattr(duplicates, "_hash_file", hash_file)
    photos = [write(tmp_path / f"{i}.jpg", b.jpeg(b.photo(640, 480).rotate(i * 7), quality=95)) for i in range(4)]
    assert all(os.path.getsize(p) > duplicates.HEAD for p in photos)
    assert find_duplicates(photos) == [] and whole == []  # different pictures: only their first bytes were read
    clean = write(tmp_path / "clean.jpg", cleaned(open(photos[2], "rb").read()))
    assert find_duplicates(photos + [clean]) == [DuplicateGroup([photos[2], clean], SAME_IMAGE)]
    assert sorted(whole) == sorted([photos[2], clean])


def test_progress_and_cancel(tmp_path):
    paths = [write(tmp_path / f"{i}.jpg", b.jpeg(b.photo().rotate(90 * (i % 2)))) for i in range(4)]
    seen = []
    groups = find_duplicates(paths, progress=lambda done, total: seen.append((done, total)))
    assert [g.paths for g in groups] == [[paths[0], paths[2]], [paths[1], paths[3]]]
    assert seen[-1] == (8, 8) and {total for _, total in seen} == {8}
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)
    asked = []

    def cancelled() -> bool:
        asked.append(1)
        return len(asked) > 2  # cancelled while looking at the third file

    assert find_duplicates(paths, cancelled=cancelled) == []


# ===================================================================== image prefixes


@pytest.mark.parametrize("module,make", [
    (jpeg_meta, lambda: b.jpeg()), (jpeg_meta, lambda: b.jpeg(progressive=True)), (png_meta, lambda: b.png()),
    (webp_meta, lambda: b.webp()), (webp_meta, lambda: b.animated_webp()),
], ids=["jpeg", "jpeg-progressive", "png", "webp", "webp-animated"])
def test_image_prefix_ignores_metadata(module, make):
    data = make()
    clean = module.strip(data)
    for limit in (16, 1000, 10 ** 7):
        found = module.image_prefix(data, limit)
        assert found is not None and found == module.image_prefix(clean, limit)
    assert module.image_prefix(data[:200], 1000, complete=False) is None  # the start alone is not enough


def test_image_prefix_tells_pictures_apart():
    a, other = b.jpeg(b.photo()), b.jpeg(b.photo().rotate(180))
    assert jpeg_meta.image_prefix(a, 4096) != jpeg_meta.image_prefix(other, 4096)
    assert len(jpeg_meta.image_prefix(a, 4096)) == 4096
    assert jpeg_meta.image_prefix(b"not a jpeg", 10) is None and png_meta.image_prefix(b"x", 10) is None
    assert webp_meta.image_prefix(b"RIFF\x00\x00\x00\x00WEBQ", 10) is None
