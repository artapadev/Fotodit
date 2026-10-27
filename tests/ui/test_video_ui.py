"""Videos in the UI: options, Browse tab tiles, Selected tab notes, playback in the preview."""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from helpers import jpeg_taken, write  # noqa: E402
from PySide6.QtCore import QSize, Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402
from video_builders import APPLE_TAGS, make_video  # noqa: E402

from fotodit.core.photoinfo import read_info  # noqa: E402
from fotodit.core.planner import (  # noqa: E402
    JobOptions,
    OutputOptions,
    Photo,
    RenameOptions,
    build_plan,
)
from fotodit.core.preview import render  # noqa: E402
from fotodit.core.processor import ContentOptions  # noqa: E402
from fotodit.core.video.process import VIDEO_KEEP  # noqa: E402
from fotodit.ui.browser import scan_folder  # noqa: E402
from fotodit.ui.options_panel import OptionsPanel, count_text  # noqa: E402
from fotodit.ui.selected_page import COL_DATE, COL_NOTE, SelectedPage  # noqa: E402
from fotodit.ui.selection import Selection  # noqa: E402
from fotodit.ui.thumbs import LOADING, ThumbnailLoader  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_until(app, condition, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_count_text():
    assert count_text(12, 3) == "12 fotoğraf, 3 video"
    assert count_text(0, 1) == "1 video" and count_text(5, 0) == "5 fotoğraf"
    assert count_text(2, 1, accusative=True) == "2 fotoğrafı ve 1 videoyu"


def test_options_panel_video_settings(app):
    panel = OptionsPanel()
    panel.set_state(3, 1, 0, 0, videos=1)
    assert panel.start_btn.text().endswith("2 Fotoğrafı ve 1 Videoyu İşle")
    panel.video_codec.setCurrentIndex(panel.video_codec.findData(VIDEO_KEEP))
    panel.rename.setChecked(True)
    panel.prefix.setText("Holiday_")
    panel.video_prefix.setText("Clip_")
    panel.video_start.setValue(7)
    o = panel.job_options()
    assert o.content.convert_video and o.content.video_codec == VIDEO_KEEP
    assert (o.rename.prefix, o.rename.video_prefix, o.rename.video_start) == ("Holiday_", "Clip_", 7)
    assert "Clip_7.mp4" in panel.example.text() and "Holiday_1.jpg" in panel.example.text()
    # With only videos selected, "convert to JPG" alone does not count as an operation.
    for w in (panel.strip, panel.video, panel.rename, panel.group):
        w.setChecked(False)
    panel.set_state(1, 1, 0, 0, videos=1)
    assert not panel.has_operation()
    panel.video.setChecked(True)
    assert panel.has_operation()


def test_options_persist_video_settings(app, tmp_path):
    from PySide6.QtCore import QSettings

    s = QSettings(str(tmp_path / "a.ini"), QSettings.Format.IniFormat)
    panel = OptionsPanel()
    panel.video.setChecked(False)
    panel.video_prefix.setText("V_")
    panel.video_hw.setChecked(True)
    panel.save(s)
    other = OptionsPanel()
    other.load(s)
    assert not other.video.isChecked() and other.video_prefix.text() == "V_"
    assert other.video_hw.isChecked() and other.job_options().content.video_hw


def test_gpu_option_shows_detected_encoder(app, monkeypatch):
    from fotodit.core.video import hwenc

    hwenc.reset()
    try:
        found = hwenc.Encoder("h264_nvenc", "NVIDIA NVENC", "h264")
        monkeypatch.setattr(hwenc, "find", lambda codec="h264": hwenc._found.setdefault(
            codec, found if codec == "h264" else None))
        panel = OptionsPanel()
        assert not panel.video_hw.isChecked() and panel.video_hw_hint.isHidden()
        changed = []
        panel.changed.connect(lambda: changed.append(1))
        panel.video_hw.setChecked(True)  # probed in the background
        assert wait_until(app, lambda: "NVIDIA NVENC" in panel.video_hw_hint.text())
        assert changed and not panel.video_hw_hint.isHidden()
        # A cropped video is re-encoded even without conversion, so the GPU option does not depend on it.
        panel.video.setChecked(False)
        assert panel.video_hw.isEnabled()
    finally:
        hwenc.reset()


def test_browser_marks_videos_and_thumbnails(app, tmp_path):
    write(tmp_path / "a.jpg", jpeg_taken("2024:07:12 09:00:00"))
    video = make_video(str(tmp_path / "b.mov"), fmt="mov", rotation=90)
    entries = scan_folder(str(tmp_path))
    assert [(e.name, e.video) for e in entries] == [("a.jpg", False), ("b.mov", True)]
    loader = ThumbnailLoader(96)
    mtime = os.path.getmtime(video)
    assert loader.get(video, mtime) is LOADING
    assert wait_until(app, lambda: loader.get(video, mtime) is not LOADING)
    pm = loader.get(video, mtime)
    assert pm is not None and pm.height() > pm.width()  # portrait video, portrait thumbnail
    assert loader.duration(video, mtime) == pytest.approx(1.0, abs=0.1)
    # Painting the tile (with the ▶ duration badge) does not fail.
    from fotodit.ui.browser import FolderModel, TileDelegate
    from fotodit.ui.folders import FolderSelection

    sel = Selection()
    folders = FolderSelection(sel)
    try:
        model = FolderModel(sel, folders, loader)
        model.set_entries(entries)
        delegate = TileDelegate(loader, folders, QSize(188, 206))
        canvas = QPixmap(188, 206)
        from PySide6.QtGui import QPainter

        painter = QPainter(canvas)
        option = QStyleOptionViewItem()
        option.rect = canvas.rect()
        delegate.paint(painter, option, model.index(1))
        painter.end()
        assert "Video" in model.data(model.index(1), 3)  # tooltip (ToolTipRole)
    finally:  # (a folder counter thread left running makes the test process exit with an error code)
        folders.shutdown()
        loader.shutdown()
        sel.shutdown()


def test_folder_tiles_count_photos_and_videos(app, tmp_path):
    """A folder tile counts photos and videos separately (subfolders included)."""
    from fotodit.ui.browser import folder_summaries
    from fotodit.ui.folders import FULL, FolderSelection

    root = tmp_path / "Holiday"
    (root / "Sub").mkdir(parents=True)
    for name in ("a.jpg", "b.jpg", "Sub/c.jpg"):
        write(root / name, jpeg_taken("2024:07:12 09:00:00"))
    for name in ("v.mp4", "Sub/w.mov"):
        (root / name).write_bytes(b"sayim yalnizca uzantiya bakar")
    entry = next(e for e in scan_folder(str(tmp_path)) if e.name == "Holiday")
    sel = Selection()
    folders = FolderSelection(sel)
    try:
        folders.count(entry.path)
        assert wait_until(app, lambda: folders.count(entry.path) is not None)
        assert folder_summaries(folders, entry) == ["3 fotoğraf, 2 video", "5 dosya"]
        folders.set_selected([entry.path], True)
        assert wait_until(app, lambda: folders.state(entry.path, entry.key) == FULL)
        assert folder_summaries(folders, entry)[:2] == ["3 fotoğraf, 2 video · tümü seçili", "5 dosya · tümü seçili"]
    finally:
        folders.shutdown()
        sel.shutdown()


def test_selected_page_notes_for_videos(app, tmp_path):
    video = make_video(str(tmp_path / "IMG_0001.MOV"), fmt="mov", vcodec="libx265", tags=APPLE_TAGS,
                       apple_keys=True, frames_n=8)
    photo = Photo.from_path(video, 0, read_info(video))
    opts = JobOptions(content=ContentOptions(convert_video=True),
                      rename=RenameOptions(enabled=True, video_prefix="Video_"),
                      output=OutputOptions(folder=str(tmp_path / "out")))
    plan = build_plan([photo], opts)
    page = SelectedPage(ThumbnailLoader(64))
    page.set_plan(plan, False, str(tmp_path / "out"), False, "tarih")
    note = page.model.data(page.model.index(0, COL_NOTE))
    assert note.startswith(("0:00", "0:01"))  # the film icon is drawn by the row's delegate
    assert "HEVC" in note and "H.264'e kodlanacak" in note and "GPS" in note
    assert page.model.target_text(plan[0]) == "Video_1.mp4"
    # The date tooltip explains how the video's local time was determined (iPhone: written with its time zone).
    tip = page.model.data(page.model.index(0, COL_DATE), Qt.ItemDataRole.ToolTipRole)
    assert tip == "Kaynak: çekim tarihi (yerel saatle)"


def test_render_video_poster(tmp_path):
    video = make_video(str(tmp_path / "a.mp4"), size=(320, 240), rotation=270)
    im = render(video, 200)
    assert im.size == (150, 200)  # orientation applied, fitted into the box
    # In MPEG-TS the seek lands past the only keyframe; the frame is still found (read from the start).
    mts = make_video(str(tmp_path / "00012.MTS"), fmt="mpegts", audio="ac3", frames_n=72)
    assert render(mts, 200).size == (200, 150)


def test_preview_starts_from_selected_row(app, tmp_path):
    from PySide6.QtCore import QItemSelectionModel

    paths = [write(tmp_path / f"{n}.jpg", jpeg_taken(f"2024:07:12 0{n}:00:00")) for n in (1, 2, 3)]
    photos = [Photo.from_path(p, i, read_info(p)) for i, p in enumerate(paths)]
    plan = build_plan(photos, JobOptions(output=OutputOptions(folder=str(tmp_path / "o"))))
    page = SelectedPage(ThumbnailLoader(64))
    page.set_plan(plan, False, str(tmp_path / "o"), False, "tarih")
    rows = []
    page.previewRequested.connect(rows.append)
    flags = QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
    page.table.selectionModel().select(page.model.index(2, 0), flags)  # focus unchanged (as with accessibility tools)
    page._preview_current()
    assert rows == [2]


def test_preview_plays_video(app, tmp_path):
    from fotodit.core.edits import Edit
    from fotodit.ui.preview_dialog import PreviewDialog, PreviewItem

    video = make_video(str(tmp_path / "IMG_0002.MOV"), fmt="mov", rotation=90, frames_n=48)
    photo = write(tmp_path / "a.jpg", jpeg_taken("2024:07:12 09:00:00"))
    items = [PreviewItem(p, os.path.getmtime(p), os.path.basename(p)) for p in (video, photo)]
    dialog = PreviewDialog(items, 0, Selection(), ThumbnailLoader(64))
    dialog.resize(900, 700)
    dialog.show()
    view = dialog.view
    assert view.video_mode and dialog.video_bar.isVisible()
    assert wait_until(app, view.has_image)
    dialog._player.play()
    assert wait_until(app, view.has_frame, 10)
    # The frame arrives as displayed (Qt applies the orientation): portrait, the red top-left corner now top right.
    frame = view._frame.convertToFormat(view._frame.Format.Format_RGB888)
    w, h = frame.width(), frame.height()
    assert h > w and view._frame_rotation == 0
    assert frame.pixelColor(w - 6, 6).red() > 150 and frame.pixelColor(w - 6, 6).blue() < 100
    assert frame.pixelColor(6, 6).blue() > 150 and frame.pixelColor(6, 6).red() < 100
    dialog.rotate(1)  # rotated while playing
    assert view.shown_edit() == Edit(quarter=1)
    dialog.start_crop()  # the video pauses in crop mode
    assert dialog._session is not None and not dialog._player.is_playing()
    dialog.cancel_crop()
    dialog.next()  # moving on to a photo makes the player release the file; no stale frame stays
    assert dialog._player.path is None and not view.video_mode and not dialog.video_bar.isVisible()
    assert not view.has_frame()
    dialog.prev()
    assert dialog._player.path == video
    dialog.done(0)
    assert dialog._player.path is None


def _run(app, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def test_backup_player_plays_av1(app, tmp_path):
    """Qt cannot decode AV1, so the PyAV backup player plays it: first frame, playback, seeking, end, file release."""
    from fotodit.ui.player import VideoPlayer

    path = make_video(str(tmp_path / "av1.mp4"), vcodec="libsvtav1", size=(160, 96), frames_n=36, fps=24,
                      rotation=90)
    player = VideoPlayer()
    frames, states = [], []
    player.frameReady.connect(lambda image, rotation: frames.append((image.width(), image.height(), rotation)))
    player.playingChanged.connect(states.append)
    try:
        player.load(path, "av1")
        assert player.uses_backup()
        assert wait_until(app, lambda: frames, 10)  # the first frame is shown while paused
        assert frames[0] == (160, 96, 90)  # portrait video: the frame arrives landscape and is shown rotated 90°
        assert player.duration() == pytest.approx(1500, abs=60)
        player.play()
        _run(app, 0.6)
        assert player.is_playing() and 250 <= player.position() <= 1000 and len(frames) > 5
        player.pause()
        shown = len(frames)
        player.seek(1000)
        assert wait_until(app, lambda: len(frames) > shown, 5) and player.position() == 1000
        player.play()
        assert wait_until(app, lambda: not player.is_playing(), 10)  # stops at the end
        assert states[-1] is False and player.position() >= 1400
        player.unload()
        os.replace(path, path + ".moved")  # the file was released
    finally:
        player.shutdown()


def test_player_falls_back_when_qt_cannot_decode(app, tmp_path):
    """If Qt produces no frames, playback continues from the same spot in the backup player (no error shown)."""
    from fotodit.ui.player import VideoPlayer

    path = make_video(str(tmp_path / "a.mp4"), size=(160, 96), frames_n=48, fps=24)
    player = VideoPlayer()
    frames, errors = [], []
    player.frameReady.connect(lambda image, rotation: frames.append(rotation))
    player.failed.connect(errors.append)
    try:
        player.load(path)
        player._no_frames()  # simulates a video that Qt cannot decode
        assert player.uses_backup() and player.is_playing()
        assert wait_until(app, lambda: len(frames) > 3, 10) and not errors
    finally:
        player.shutdown()


@pytest.mark.parametrize("backup", [False, True])
def test_a_mirrored_video_plays_mirrored(app, tmp_path, backup):
    """QuickTime's "Flip Horizontal" mirrors a video by its display matrix: both players show it
    mirrored (red and green swap places), not turned upside down."""
    from builders import dominant, marker_color
    from PIL import Image
    from PySide6.QtGui import QImage, QTransform
    from video_builders import HFLIP, set_matrix

    from fotodit.ui.player import VideoPlayer

    path = make_video(str(tmp_path / "flip.mov"), fmt="mov", size=(160, 96), frames_n=48)
    set_matrix(path, HFLIP)
    player = VideoPlayer()
    frames = []
    player.frameReady.connect(lambda image, rotation: frames.append((image.copy(), rotation)))
    try:
        player.load(path)
        player.play()
        if backup:
            assert wait_until(app, lambda: frames, 10)
            player._no_frames()  # as if Qt could not decode it: the backup player goes on
            frames.clear()
            assert player.uses_backup()
        assert wait_until(app, lambda: frames, 10)
        image, rotation = frames[-1]
        shown = image.transformed(QTransform().rotate(rotation)).convertToFormat(QImage.Format.Format_RGB888)
        pil = Image.frombuffer("RGB", (shown.width(), shown.height()), bytes(shown.constBits()), "raw", "RGB",
                               shown.bytesPerLine(), 1)
        corners = [dominant(marker_color(pil, corner)) for corner in ("tl", "tr", "bl")]
        assert corners == ["green", "red", "other"]  # blue is now bottom right
    finally:
        player.shutdown()
