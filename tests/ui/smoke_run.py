"""UI smoke test: opens the window, browses, selects, edits, processes and takes screenshots.

Usage: python tests/ui/smoke_run.py <work_folder> [--visible] [--dark] [--en]
Does not touch the user's settings (they are written to an INI file in the work folder). `--en`: the
interface in English (Turkish otherwise).
"""

from __future__ import annotations

import io
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

work = os.path.abspath(sys.argv[1])
if "--visible" not in sys.argv:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

import builders as b  # noqa: E402
from PySide6.QtCore import QSettings, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fotodit import i18n  # noqa: E402
from fotodit.core.imaging import ensure_plugins  # noqa: E402
from fotodit.core.meta import scan_file  # noqa: E402
from fotodit.core.planner import build_plan  # noqa: E402
from fotodit.ui import theme  # noqa: E402
from fotodit.ui.browser import TileDelegate  # noqa: E402
from fotodit.ui.main_window import MainWindow  # noqa: E402
from fotodit.ui.run_dialog import RunDialog  # noqa: E402


def write(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def horizon(tilt: float) -> b.Image.Image:
    """A seascape with the horizon tilted by `tilt` degrees (+ = counterclockwise), for straightening."""
    from PIL import ImageDraw

    w, h = 1200, 800
    im = b.Image.new("RGB", (w * 2, h * 2))
    d = ImageDraw.Draw(im)
    for y in range(h * 2):
        t = y / (h * 2)
        d.line([(0, y), (w * 2, y)], fill=(int(120 + 110 * t), int(170 + 70 * t), 235) if y < h else
               (int(20 + 30 * t), int(60 + 40 * t), int(120 + 40 * t)))
    d.ellipse((w * 1.3, h * 0.55, w * 1.3 + 160, h * 0.55 + 160), fill=(255, 214, 102))
    d.polygon([(w * 0.6, h - 4), (w * 0.8, h - 4), (w * 0.76, h + 40), (w * 0.64, h + 40)], fill=(240, 240, 235))
    d.line([(w * 0.7, h - 4), (w * 0.7, h - 130)], fill=(60, 60, 60), width=6)
    d.polygon([(w * 0.705, h - 125), (w * 0.705, h - 20), (w * 0.79, h - 20)], fill=(250, 250, 250))
    im = im.rotate(tilt, resample=b.Image.Resampling.BICUBIC)
    return im.crop((w // 2, h // 2, w // 2 + w, h // 2 + h))


def make_samples() -> tuple[str, str]:
    a = os.path.join(work, "Photos", "Holiday 2024")
    c = os.path.join(work, "Photos", "Family")
    ensure_plugins()
    buf = io.BytesIO()
    horizon(4).save(buf, "JPEG", quality=90, exif=b"Exif\x00\x00" + b.rich_exif(1))
    write(os.path.join(a, "IMG_20240512_180000.jpg"), buf.getvalue())
    stored = b.photo(640, 480).transpose(b.Image.Transpose.ROTATE_90)
    write(os.path.join(a, "IMG_20240512_143022.jpg"), b.jpeg(stored, orientation=6))
    write(os.path.join(a, "IMG_20240513_091500.HEIC"), b.heic(b.photo(640, 480), icc=b.P3_ICC))
    # Turkish Windows screenshot name: date in the name; the non-ASCII path is intentional.
    write(os.path.join(a, "Ekran görüntüsü 2024-05-14 101010.png"), b.png(b.photo(640, 400)))
    write(os.path.join(a, "downloaded.webp"), b.webp(b.photo(480, 480)))
    write(os.path.join(a, "animation.gif"), b.gif())
    write(os.path.join(a, "broken.jpg"), b.jpeg(everything=False)[:600])
    write(os.path.join(a, "actually_text.jpg"), b"bu bir fotograf degil")
    from fotodit.core.dngwriter import make_dng

    raw_stored = b.photo(640, 480).transpose(b.Image.Transpose.ROTATE_90)
    write(os.path.join(a, "DSC_0042.dng"), make_dng(raw_stored, orientation=6, preview="full", xmp=b.XMP))
    write(os.path.join(a, "DSC_0043.dng"), make_dng(b.photo(640, 480), taken="2024:05:13 08:00:00"))
    for i in range(1, 9):
        buf = io.BytesIO()
        b.photo(400, 300).rotate(i * 20).save(buf, "JPEG", quality=85, exif=b"Exif\x00\x00" + b.rich_exif(1))
        write(os.path.join(a, "Subfolder", f"DSC_{i:04d}.jpg"), buf.getvalue())
    for name in ("IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"):
        write(os.path.join(c, name), b.jpeg(b.photo(500, 375)))
    # Videos: a portrait iPhone video (HEVC, location and device tags), Android (H.264, location),
    # an AVCHD camcorder (MTS: date/GPS record in the video stream), an old digital camera (MJPEG AVI).
    from video_builders import ANDROID_TAGS, APPLE_TAGS, make_video, mdpm_payload

    make_video(os.path.join(a, "IMG_0107.MOV"), fmt="mov", vcodec="libx265", size=(640, 360), rotation=90,
               tags=APPLE_TAGS, apple_keys=True, frames_n=48)
    make_video(os.path.join(a, "VID_20240512_190000.mp4"), size=(640, 360), tags=ANDROID_TAGS, frames_n=72)
    make_video(os.path.join(a, "00012.MTS"), fmt="mpegts", size=(640, 360), audio="ac3", sei=[mdpm_payload()],
               frames_n=48)
    make_video(os.path.join(a, "MVI_0033.AVI"), fmt="avi", vcodec="mjpeg", pix_fmt="yuvj420p", audio="pcm_s16le",
               size=(640, 480), frames_n=24)
    # AV1 (Qt cannot decode it: backup player) + a Turkish subtitle (non-ASCII and italics on purpose);
    # DJI drone: location/camera log in a subtitle track.
    cues = [(0, 900, "Merhaba!"), (1000, 1900, "{\\i1}Güzel{\\i0} bir gün")]
    make_video(os.path.join(a, "Clip_AV1.mp4"), vcodec="libsvtav1", size=(640, 360), frames_n=48,
               subtitle=cues, subtitle_language="tur")
    dji = [(i * 400, i * 400 + 390, f"F/2.8, SS 500, ISO 100, EV 0, GPS (28.9784, 41.0082, 19), D {i}.5m")
           for i in range(4)]
    make_video(os.path.join(a, "DJI_0001.MP4"), size=(640, 360), frames_n=48, subtitle=dji)
    return a, c


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def wait_scans(app, win, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while win.folders.busy() and time.time() < deadline:
        pump(app, 0.1)
    pump(app, 0.5)


def main() -> int:
    a, c = make_samples()
    i18n.set_language("en" if "--en" in sys.argv else "tr")
    app = QApplication(sys.argv)
    theme.apply(app, theme.DARK if "--dark" in sys.argv else theme.LIGHT)
    settings = QSettings(os.path.join(work, "ayarlar.ini"), QSettings.Format.IniFormat)
    settings.clear()  # a folder used before keeps the last run's options (it ends applying to the originals)
    settings.setValue("browse/folder", work)  # do not browse the user's Pictures folder
    win = MainWindow(settings)
    win.options.set_output_folder(os.path.join(work, "Output"))  # do not write into the user's Pictures folder
    win.resize(1440, 880)
    win.show()
    shots = os.path.join(work, "screenshots")
    os.makedirs(shots, exist_ok=True)

    win.browser.navigate(a)
    pump(app, 3.0)
    win.grab().save(os.path.join(shots, "1_browse.png"))
    menu = win.theme_btn.menu()  # the theme menu (light / dark / as Windows is set)
    menu.popup(win.theme_btn.mapToGlobal(win.theme_btn.rect().topLeft()))
    pump(app, 0.5)
    menu.grab().save(os.path.join(shots, "1b_theme_menu.png"))
    menu.hide()

    win.browser.select_all()  # photos + "Subfolder" (with its contents)
    wait_scans(app, win)
    win.info.show_path(os.path.join(a, "DSC_0042.dng"))
    pump(app, 2.0)
    win.grab().save(os.path.join(shots, "2_selection_info.png"))
    print("select all (incl. subfolders):", len(win.selection))
    win.selection.clear()

    # Selecting folders without opening them: click a tile's circle in the parent folder, tick a box in the tree.
    photos_root = os.path.dirname(a)
    win.browser.navigate(photos_root)
    pump(app, 0.5)
    win.grab()  # folders start being counted once the tiles are painted
    pump(app, 2.0)
    grid = win.browser.grid
    rows = {e.name: i for i, e in enumerate(win.browser.model.entries)}
    rect = grid.visualRect(win.browser.model.index(rows["Family"]))
    QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     TileDelegate.badge_rect(rect).center().toPoint())
    wait_scans(app, win)
    print("selected (Family tile):", len(win.selection), "· still in parent folder:", win.browser.folder == photos_root)
    tree_index = win.nav.fs.index(a.replace("\\", "/"))
    win.nav.fs.setData(tree_index, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    wait_scans(app, win)
    print("'Holiday 2024' also ticked in the tree, total:", len(win.selection))
    pump(app, 1.0)
    win.grab().save(os.path.join(shots, "3_folder_selection.png"))

    win.options.rename.setChecked(True)
    win.options.prefix.setText("Holiday_")
    win.options.video_prefix.setText("Video_")
    win.options.keep_structure()  # same as the hint's "Klasör yapısını koru" (keep folder structure) link
    win.tabs.setCurrentWidget(win.selected)
    deadline = time.time() + 30
    while win.selection.pending() and time.time() < deadline:
        pump(app, 0.2)
    pump(app, 2.0)
    win.grab().save(os.path.join(shots, "4_selected.png"))
    print("Selected tab hint:", win.selected.hint.text())

    # Large preview: the selected photos, arrow-key navigation, info panel.
    win.selected.table.setCurrentIndex(win.selected.model.index(0, 0))
    win.selected._preview_current()
    pump(app, 0.3)
    preview = QApplication.activeModalWidget()
    preview.resize(1440, 880)
    pump(app, 2.0)
    preview.grab().save(os.path.join(shots, "4b_preview.png"))
    QTest.keyClick(preview, Qt.Key.Key_Right)
    QTest.keyClick(preview, Qt.Key.Key_I)
    pump(app, 2.0)
    preview.grab().save(os.path.join(shots, "4c_preview_info.png"))
    print("preview:", preview.position.text(), preview.title.text(), "·", preview.target.text())
    QTest.keyClick(preview, Qt.Key.Key_Escape)
    pump(app, 0.3)

    # Rotate and crop: crop mode on the photo with the tilted horizon (4:3, +4° straightening), then
    # rotate DSC_0001.jpg clockwise. Edits are applied when processing starts.
    rows = {e.photo.name: i for i, e in enumerate(win.selected.model.plan)}
    win.selected.table.setCurrentIndex(win.selected.model.index(rows["IMG_20240512_180000.jpg"], 0))
    win.selected._preview_current()
    pump(app, 0.3)
    preview = QApplication.activeModalWidget()
    preview.resize(1440, 880)
    pump(app, 2.0)
    QTest.keyClick(preview, Qt.Key.Key_C)
    pump(app, 0.3)
    preview.crop_bar.aspect.setCurrentIndex(3)  # 4:3
    preview.crop_bar.slider.setValue(40)  # +4°
    preview.view.fine_grid = True  # the grid shown while the slider is dragged
    pump(app, 0.5)
    preview.grab().save(os.path.join(shots, "4d_crop.png"))
    preview.view.fine_grid = False
    QTest.keyClick(preview, Qt.Key.Key_Return)
    pump(app, 1.5)
    preview.grab().save(os.path.join(shots, "4e_cropped.png"))
    print("crop:", preview.edit_label.text())
    target = rows["DSC_0001.jpg"]
    preview.go(target)
    QTest.keyClick(preview, Qt.Key.Key_R)
    pump(app, 1.5)
    preview.grab().save(os.path.join(shots, "4f_rotated.png"))
    print("rotate:", preview.edit_label.text())
    # RAW exposure: DSC_0043.dng has no embedded preview; +1 EV, shown live from the RAW data.
    preview.go(rows["DSC_0043.dng"])
    pump(app, 2.0)
    preview.exposure_bar.slider.setValue(10)
    preview.exposure_bar._released()
    deadline = time.time() + 20
    while not preview._developed and time.time() < deadline:
        pump(app, 0.2)
    pump(app, 0.5)
    preview.grab().save(os.path.join(shots, "4f2_exposure.png"))
    print("exposure:", preview.edit_label.text(), "· developed preview:", preview._developed)
    QTest.keyClick(preview, Qt.Key.Key_Escape)
    pump(app, 0.5)
    win.replan()
    pump(app, 1.0)
    win.grab().save(os.path.join(shots, "4g_selected_edits.png"))
    win.tabs.setCurrentWidget(win.browser)
    win.browser.navigate(a)
    pump(app, 2.0)
    win.grab().save(os.path.join(shots, "4h_browse_edits.png"))
    win.tabs.setCurrentWidget(win.selected)
    pump(app, 0.5)

    # Video: playback in the preview (Android video) with the info panel.
    rows = {e.photo.name: i for i, e in enumerate(win.selected.model.plan)}
    win.selected.table.setCurrentIndex(win.selected.model.index(rows["VID_20240512_190000.mp4"], 0))
    win.selected._preview_current()
    pump(app, 0.3)
    preview = QApplication.activeModalWidget()
    preview.resize(1440, 880)
    pump(app, 1.5)
    preview.grab().save(os.path.join(shots, "4i_video_poster.png"))
    QTest.keyClick(preview, Qt.Key.Key_K)
    pump(app, 1.2)
    QTest.keyClick(preview, Qt.Key.Key_I)
    pump(app, 1.0)
    preview.grab().save(os.path.join(shots, "4j_video_playing.png"))
    print("video:", preview.details.text(), "· playing:", preview._player.is_playing(),
          "· frame:", preview.view.has_frame())
    # Trimming: keep 0.5 s … 2.5 s of the 3-second video.
    preview._player.pause()
    for at, mark in ((500, preview.trim_start), (2500, preview.trim_end)):
        preview._player.seek(at)
        pump(app, 0.8)
        mark()
    pump(app, 0.5)
    preview.grab().save(os.path.join(shots, "4j2_video_trim.png"))
    print("trim:", preview.edit_label.text())
    # Removing a part: 0.5 s … 1.0 s of the 2-second DJI clip (re-encoded; expected 1.5 s).
    preview.go(rows["DJI_0001.MP4"])
    pump(app, 1.0)
    preview._player.seek(500)
    pump(app, 0.8)
    preview.toggle_cut()
    preview._player.seek(1000)
    pump(app, 0.8)
    preview.grab().save(os.path.join(shots, "4j3_video_marking.png"))  # the part being marked, in red
    preview.toggle_cut()
    pump(app, 0.5)
    preview.grab().save(os.path.join(shots, "4j4_video_part_removed.png"))
    print("removed part:", preview.edit_label.text())
    # AV1: Qt cannot decode it, so it plays through the PyAV backup player.
    preview.go(rows["Clip_AV1.mp4"])
    pump(app, 1.0)
    QTest.keyClick(preview, Qt.Key.Key_K)
    pump(app, 1.2)
    preview.grab().save(os.path.join(shots, "4l_av1_backup_player.png"))
    print("AV1:", preview.details.text(), "· backup player:", preview._player.uses_backup(),
          "· playing:", preview._player.is_playing(), "· frame:", preview.view.has_frame())
    QTest.keyClick(preview, Qt.Key.Key_Escape)
    pump(app, 0.5)
    win.info.show_path(os.path.join(a, "IMG_0107.MOV"))
    pump(app, 1.5)
    win.grab().save(os.path.join(shots, "4k_video_info.png"))

    win.options.video_hw.setChecked(True)  # encode with the GPU (if there is one)
    deadline = time.time() + 15
    while win.options.video_hw_hint.text() == i18n.t("options.hw_trying") and time.time() < deadline:
        pump(app, 0.2)
    print("GPU encoder:", win.options.video_hw_hint.text())
    win.grab().save(os.path.join(shots, "4m_gpu_option.png"))
    opts = win.options.job_options()
    plan = build_plan(win.selection.photos(), opts)

    def grab_modal() -> None:
        box = QApplication.activeModalWidget()
        if box is not None:
            box.grab().save(os.path.join(shots, "5_confirm.png"))
            for button in box.buttons():
                if button.text() == i18n.t("common.cancel"):
                    button.click()

    QTimer.singleShot(800, grab_modal)
    print("confirmation dialog accepted:", win._confirm(plan, opts))

    report = run_dialog(app, win, plan, opts, os.path.join(shots, "6_result.png"))
    print("succeeded:", len(report.succeeded), "failed:", len(report.failed))
    for r in report.failed:
        print("  ERROR", r.entry.photo.name, "→", r.error)
    dirty = [r.final_path for r in report.succeeded if not scan_file(r.final_path).clean]
    print("outputs that are not clean:", dirty)
    from fotodit.core.video.probe import probe

    for r in report.succeeded:
        if r.entry.photo.is_video:
            v = probe(r.final_path)
            subs = [f"{s.codec}/{s.language}" for s in v.subtitle_tracks]
            print("  video:", r.entry.photo.name, "→", os.path.relpath(r.final_path, report.output_folder),
                  v.vcodec, v.acodec, v.display_size, f"{v.duration:.2f} s", "GPS" if v.has_gps else "no location",
                  f"subtitles {subs}" if subs else "no subtitles", r.notes)
        elif r.entry.edit is not None:
            with b.Image.open(r.final_path) as out:
                print("  edited:", r.entry.photo.name, "→", os.path.relpath(r.final_path, report.output_folder),
                      out.size, r.entry.edit.describe())
    print("output folder:", report.output_folder)
    for root, _, files in os.walk(report.output_folder):
        for f in sorted(files):
            print("  ", os.path.relpath(os.path.join(root, f), report.output_folder))

    # Selecting a folder with its subfolders + apply to originals (in place). Sample files only: the
    # test replaces them permanently instead of moving the originals to the Recycle Bin.
    sub = os.path.join(a, "Subfolder")
    win.selection.clear()
    win.add_recursive([sub])
    wait_scans(app, win)
    pump(app, 1.5)
    print("selected by the subfolder scan:", len(win.selection))
    win.options.in_place.setChecked(True)
    win.options.recycle.setChecked(False)
    win.options.group.setChecked(False)
    win.options.prefix.setText("")
    opts = win.options.job_options()
    plan = build_plan(win.selection.photos(), opts)
    report = run_dialog(app, win, plan, opts, os.path.join(shots, "7_in_place.png"))
    names = sorted(os.listdir(sub))
    clean = all(scan_file(os.path.join(sub, n)).clean for n in names)
    print("in place:", len(report.succeeded), "succeeded; files:", names, "clean:", clean)
    win._confirm_discard_edits = lambda count: True  # edits already processed into the new folder: do not ask
    win.close()
    return 0 if not dirty and clean else 1


def run_dialog(app, win, plan, opts, shot):
    dialog = RunDialog(plan, opts, win)
    dialog.show()
    deadline = time.time() + 120
    while dialog._running and time.time() < deadline:
        pump(app, 0.2)
    pump(app, 0.5)
    dialog.grab().save(shot)
    dialog.accept()
    return dialog.report


if __name__ == "__main__":
    raise SystemExit(main())
