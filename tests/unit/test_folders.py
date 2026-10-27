"""Folder selection: scanning subfolders, keeping the folder structure and large archives."""

from __future__ import annotations

import ctypes
import os
import time

from helpers import make_files  # noqa: E402

from fotodit.core.folderscan import folder_key, walk  # noqa: E402
from fotodit.core.meta import scan_file  # noqa: E402
from fotodit.core.planner import (  # noqa: E402
    GROUP_SOURCE,
    JobOptions,
    OutputOptions,
    Photo,
    RenameOptions,
    build_plan,
    skip_reason,
    source_layout,
)
from fotodit.core.processor import RAW_NEEDS_JPEG, ContentOptions  # noqa: E402
from fotodit.core.runner import RECYCLE_BATCH, TEMP_PREFIX, Runner  # noqa: E402
from fotodit.i18n import t
from fotodit.winutil import recycle_bin_capacity, volume_root  # noqa: E402

# ---------------------------------------------------------------- scanning


def test_walk_order_totals_and_skip_rules(tmp_path):
    root = tmp_path / "Root"
    make_files(root, "b.jpg", "a10.jpg", "a2.jpg", "._a.jpg", f"{TEMP_PREFIX}x.jpg",
         "Sub2/x.jpg", "Sub10/y.jpg", "Sub10/z.jpg", ".thumbnails/t.jpg", "$RECYCLE.BIN/r.jpg", "Hidden/h.jpg")
    (root / "notes.txt").write_text("x")
    (root / "Empty").mkdir()
    ctypes.windll.kernel32.SetFileAttributesW(str(root / "Hidden"), 0x2)
    import _winapi

    _winapi.CreateJunction(str(root / "Sub2"), str(root / "Link"))  # must not cause a loop or duplicates
    result = walk(str(root))
    rel = [os.path.relpath(p, root) for p, _, _ in result.photos]
    assert rel == ["a2.jpg", "a10.jpg", "b.jpg", "Sub2\\x.jpg", "Sub10\\y.jpg", "Sub10\\z.jpg"]
    assert result.complete and not result.partial
    t = result.totals
    assert t[folder_key(str(root))] == 6 and t[folder_key(str(root / "Sub10"))] == 2
    assert t[folder_key(str(root / "Empty"))] == 0
    for skipped in ("Hidden", ".thumbnails", "$RECYCLE.BIN", "Link"):
        assert folder_key(str(root / skipped)) not in t
    assert all(size > 0 and mtime > 0 for _, size, mtime in result.photos)


def test_walk_budget_and_cancel(tmp_path):
    root = tmp_path / "Large"
    make_files(root, *[f"K{i}/f{j}.jpg" for i in range(6) for j in range(3)])
    limited = walk(str(root), budget=5, collect=False)
    assert not limited.complete and folder_key(str(root)) in limited.partial
    assert limited.totals[folder_key(str(root))] < 18
    assert not walk(str(root), cancelled=lambda: True).complete
    assert walk(str(root)).totals[folder_key(str(root))] == 18
    # When counting, subfolders whose count is already known are not entered again (the total is still right).
    k0 = folder_key(str(root / "K0"))
    reused = walk(str(root), collect=False, known=lambda key: (3, 1) if key == k0 else None)
    assert reused.totals[folder_key(str(root))] == 18 and k0 not in reused.totals
    assert reused.videos[folder_key(str(root))] == 1  # the known subfolder's video is counted too


# ------------------------------------------------------ keeping the folder structure


def test_source_layout_keeps_structure(tmp_path, archive):
    other = make_files(tmp_path, "Other/Holiday/d.jpg", "Another/Holiday/e.jpg")
    photos = [Photo.from_path(p, i, root=archive) for i, (p, _, _) in enumerate(walk(archive).photos)]
    photos += [Photo.from_path(p, 10 + i) for i, p in enumerate(other)]
    layout = source_layout(photos)
    got = {os.path.relpath(p.path, tmp_path): layout[p.folder_key] for p in photos}
    assert got == {
        "Ayşe\\cover.jpg": "Ayşe",
        "Ayşe\\2019\\IMG_0001.jpg": "Ayşe\\2019",
        "Ayşe\\2019\\Holiday\\IMG_0001.jpg": "Ayşe\\2019\\Holiday",
        "Ayşe\\2019\\Holiday\\IMG_0002.jpg": "Ayşe\\2019\\Holiday",
        "Ayşe\\2020\\IMG_0001.jpg": "Ayşe\\2020",
        "Another\\Holiday\\e.jpg": "Holiday",  # two different folders with the same name are not mixed up
        "Other\\Holiday\\d.jpg": "Holiday (2)",
    }
    # An individually selected photo whose folder is inside a selected root joins that root's structure.
    lone = Photo.from_path(os.path.join(archive, "2020", "IMG_0001.jpg"), 0)
    inner = [Photo.from_path(p, 1, root=os.path.join(archive, "2019")) for p, _, _ in walk(os.path.join(archive, "2019")).photos]
    top = Photo.from_path(os.path.join(archive, "cover.jpg"), 2)
    layout = source_layout([lone, *inner, top])
    assert layout[lone.folder_key] == "Ayşe\\2020" and layout[top.folder_key] == "Ayşe"
    assert layout[inner[0].folder_key] == "Ayşe\\2019" and layout[inner[-1].folder_key] == "Ayşe\\2019\\Holiday"


def test_plan_with_structure_numbers_each_folder(tmp_path, archive):
    photos = [Photo.from_path(p, i, root=archive) for i, (p, _, _) in enumerate(walk(archive).photos)]
    out = str(tmp_path / "out")
    opts = JobOptions(rename=RenameOptions(enabled=True, sort_by="name"),
                      output=OutputOptions(folder=out, group_by=GROUP_SOURCE))
    got = sorted(os.path.relpath(e.target_path, out) for e in build_plan(photos, opts))
    assert got == ["Ayşe\\1.jpg", "Ayşe\\2019\\1.jpg", "Ayşe\\2019\\Holiday\\1.jpg", "Ayşe\\2019\\Holiday\\2.jpg",
                   "Ayşe\\2020\\1.jpg"]
    # Without the structure everything goes into one folder; identical names are numbered, no name collides.
    flat = build_plan(photos, JobOptions(output=OutputOptions(folder=out)))
    names = sorted(e.target_name for e in flat)
    assert names == ["IMG_0001 (2).jpg", "IMG_0001 (3).jpg", "IMG_0001.jpg", "IMG_0002.jpg", "cover.jpg"]


def test_in_place_rename_numbers_each_folder_like_one_by_one(tmp_path, archive):
    """Parent folder selected with "apply to originals + rename": each folder is numbered on its own,
    from the start number; the same result as processing the folders one by one, in a single step."""
    photos = [Photo.from_path(p, i, root=archive) for i, (p, _, _) in enumerate(walk(archive).photos)]
    opts = JobOptions(content=ContentOptions(strip_metadata=True, convert_to_jpeg=True),
                      rename=RenameOptions(enabled=True, sort_by="name", start=4),
                      output=OutputOptions(in_place=True))
    plan = build_plan(photos, opts)
    got = sorted(os.path.relpath(e.target_path, archive) for e in plan)
    assert got == ["2019\\4.jpg", "2019\\Holiday\\4.jpg", "2019\\Holiday\\5.jpg", "2020\\4.jpg", "4.jpg"]
    bin_ = BatchBin(str(tmp_path / "_bin"), fail_name="")
    report = Runner(plan, opts, recycle_func=bin_, workers=2).run()
    assert len(report.succeeded) == 5 and not report.failed
    assert sorted(os.listdir(os.path.join(archive, "2019", "Holiday"))) == ["4.jpg", "5.jpg"]
    assert bin_.batches == [5]  # the originals go to the Recycle Bin in a single batch


def test_plan_scales_to_large_archives(tmp_path):
    """20,000 photos with the same names in 200 folders: the plan must be built in linear time
    (name collisions and the "in place" mode used to take quadratic time: 5 s and 133 s)."""
    base = str(tmp_path / "missing")  # folders that do not exist on disk: listing them returns nothing
    photos = [Photo(os.path.join(base, f"K{d:03d}", f"IMG_{i:04d}.jpg"), 1000, 1_700_000_000 + d * 100 + i, d * 100 + i)
              for d in range(200) for i in range(100)]
    for opts in (JobOptions(output=OutputOptions(folder=str(tmp_path / "out"))),
                 JobOptions(output=OutputOptions(in_place=True)),
                 JobOptions(rename=RenameOptions(enabled=True), output=OutputOptions(in_place=True)),
                 JobOptions(rename=RenameOptions(enabled=True),
                            output=OutputOptions(folder=str(tmp_path / "out"), group_by=GROUP_SOURCE))):
        started = time.perf_counter()
        plan = build_plan(photos, opts)
        assert time.perf_counter() - started < 5
        assert len({e.target_path.casefold() for e in plan}) == len(plan) == 20000
    flat = build_plan(photos, JobOptions(output=OutputOptions(folder=str(tmp_path / "out"))))
    same = sorted((e.target_name for e in flat if e.target_name.startswith("IMG_0007")), key=len)
    assert same[0] == "IMG_0007.jpg" and same[1] == "IMG_0007 (2).jpg" and same[-1] == "IMG_0007 (200).jpg"


def test_skip_reason_uses_extension_until_info_is_read():
    raw = Photo(r"C:\missing\DSC_0001.NEF", 1, 1.0)
    assert raw.fmt() is not None and raw.info is None
    assert skip_reason(raw, ContentOptions(strip_metadata=True)) == t(RAW_NEEDS_JPEG)
    assert skip_reason(raw, ContentOptions(strip_metadata=True, convert_to_jpeg=True)) is None


# ------------------------------------------------------------ in-place processing


class BatchBin:
    """A fake Recycle Bin called in batches; it cannot move the file with the given name."""

    def __init__(self, root, fail_name):
        self.root, self.fail_name = root, fail_name
        self.batches: list[int] = []
        os.makedirs(root, exist_ok=True)

    def __call__(self, paths):
        self.batches.append(len(paths))
        failed = set()
        for i, p in enumerate(paths):
            if os.path.basename(p) == self.fail_name:
                failed.add(os.path.abspath(p))
                continue
            os.rename(p, os.path.join(self.root, f"{len(self.batches)}_{i}_{os.path.basename(p)}"))
        return failed


def test_in_place_recycles_in_batches_and_isolates_failures(tmp_path, archive):
    photos = [Photo.from_path(p, i, root=archive) for i, (p, _, _) in enumerate(walk(archive).photos)]
    opts = JobOptions(content=ContentOptions(strip_metadata=True), output=OutputOptions(in_place=True))
    plan = build_plan(photos, opts)
    bin_ = BatchBin(str(tmp_path / "_bin"), "cover.jpg")
    report = Runner(plan, opts, recycle_func=bin_, workers=2).run()
    assert bin_.batches == [5] and 5 <= RECYCLE_BATCH
    assert [os.path.basename(r.entry.photo.path) for r in report.failed] == ["cover.jpg"]
    assert len(report.succeeded) == 4
    assert not scan_file(os.path.join(archive, "cover.jpg")).clean  # the original that could not be moved is untouched
    for r in report.succeeded:
        assert scan_file(r.final_path).clean
    assert not any(f.startswith(TEMP_PREFIX) for _, _, fs in os.walk(archive) for f in fs)


def test_volume_helpers(tmp_path):
    root = volume_root(str(tmp_path))
    assert root and root.endswith("\\") and str(tmp_path).lower().startswith(root.lower())
    capacity = recycle_bin_capacity(str(tmp_path))
    assert capacity is None or capacity >= 0
