"""Interaction and Windows file-lifecycle regressions for the redesigned UI."""
import ctypes
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, Qt, QTimer

from core.clip_mutations import rename_clip, renamed_clip_path
from core.audio_manifest import manifest_path_for
from core.clip_metadata_manager import ClipMetadataManager
from core.theme_manager import ThemeManager, DEFAULT_COLORS
from core.upload_manager import UploadManager
from ui.clip_grid import ClipThumbnail, _WorkerCancellation
from ui.style import Colors


def test_upload_and_copy_hover_share_outline_and_survive_action_change(qtbot, tmp_path):
    from PySide6.QtWidgets import QApplication
    card = ClipThumbnail(str(tmp_path / 'clip.mp4'), upload_enabled=True)
    qtbot.addWidget(card)
    card.show()
    QApplication.sendEvent(card.share_btn, QEvent(QEvent.Type.Enter))
    assert card.share_btn.property('actionHovered')
    assert card._share_icon_key == ('upload', Colors.ACCENT)
    card.share_btn._hover_animation.setCurrentTime(120)
    upload_background = card.share_btn.grab().toImage().pixelColor(6, 6)
    card.set_upload_info({'url': 'https://example.com/clip'})
    assert card._share_icon_key == ('link', Colors.ACCENT)
    link_background = card.share_btn.grab().toImage().pixelColor(6, 6)
    assert upload_background == link_background
    assert upload_background.name() != Colors.ACCENT.lower()
    QApplication.sendEvent(card.share_btn, QEvent(QEvent.Type.Leave))
    assert not card.share_btn.property('actionHovered')
    assert card._share_icon_key == ('link', Colors.BG)


def test_upload_hover_reverses_smoothly_and_survives_style_refresh(qtbot, tmp_path, qapp):
    from PySide6.QtWidgets import QApplication
    from ui.hover_transition import HoverTransitions
    transitions = HoverTransitions(qapp)
    card = ClipThumbnail(str(tmp_path / 'clip.mp4'), upload_enabled=True)
    qtbot.addWidget(card)
    card.show()
    try:
        button = card.share_btn
        for _ in range(3):
            QApplication.sendEvent(card, QEvent(QEvent.Type.Enter))
            QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
            assert getattr(card, '_hover_fade', None) is None
            assert getattr(button, '_hover_fade', None) is None
            button._hover_animation.setCurrentTime(60)
            before = button._hover_progress
            assert 0 < before < 1
            color = button.grab().toImage().pixelColor(6, 6).name()
            assert color not in (Colors.ACCENT.lower(), Colors.ACCENT_SOFT.lower())
            QApplication.sendEvent(button, QEvent(QEvent.Type.StyleChange))
            assert button._hover_animation.state() == button._hover_animation.State.Running
            card.set_upload_info({'url': 'https://example.com/clip'})
            assert button._hover_progress == before
            QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
            assert button._hover_animation.startValue() == before
            button._hover_animation.setCurrentTime(120)
            assert button._hover_progress == 0
    finally:
        qapp.removeEventFilter(transitions)
        transitions.deleteLater()


def test_card_displays_actual_filename_and_static_bottom_fade(qtbot, tmp_path):
    from PySide6.QtGui import QColor, QImage
    name = 'Overwatch_clip_from_03Oct2026_14-06-06'
    card = ClipThumbnail(str(tmp_path / f'{name}.mp4'), card_width=320)
    qtbot.addWidget(card)
    assert card.title_label._full_text == name
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    card.set_image_thumbnail(image)
    rendered = card.thumb_label.pixmap().toImage()
    assert rendered.pixelColor(160, 40).name() == '#ff0000'
    lower = rendered.pixelColor(160, 170)
    background = rendered.pixelColor(160, 179)
    target = QColor(Colors.CARD_BG)
    assert lower.red() < 100  # Visible body-color fade, without an opacity effect.
    assert abs(background.red() - target.red()) < 5
    assert abs(background.green() - target.green()) < 5
    assert rendered.pixelColor(0, 0).alpha() < 255
    assert card.graphicsEffect() is None


def test_filename_collision_and_windows_names_preserve_original(tmp_path):
    clip = tmp_path / 'original.mp4'
    clip.write_bytes(b'original media')
    other = tmp_path / 'existing.mp4'
    other.write_bytes(b'other media')
    with pytest.raises(FileExistsError):
        renamed_clip_path(str(clip), 'existing')
    for invalid in ('CON', 'Lpt1', 'bad/name', 'bad:name', '..', 'bad.'):
        with pytest.raises(ValueError):
            renamed_clip_path(str(clip), invalid)
    assert clip.read_bytes() == b'original media'
    assert other.read_bytes() == b'other media'


def test_rename_preserves_manifest_binding_and_editor_draft(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    clip = tmp_path / 'old.mp4'
    clip.write_bytes(b'unchanged media')
    manager = ClipMetadataManager()
    manager.set(str(clip), tag='highlight', description='A good round')
    manager.set_editor_draft(str(clip), {'trim': [2, 5]})
    manifest_path_for(clip).write_text(json.dumps({'media_file': clip.name, 'sources': ['preserved']}))
    destination = tmp_path / 'new.mp4'
    rename_clip(str(clip), str(destination))
    manager.rename_path(str(clip), str(destination))
    assert destination.read_bytes() == b'unchanged media'
    assert not clip.exists()
    assert not manifest_path_for(clip).exists()
    assert json.loads(manifest_path_for(destination).read_text()) == {
        'media_file': 'new.mp4', 'sources': ['preserved']}
    assert manager.get(str(destination))['tag'] == 'highlight'
    assert manager.get_editor_draft(str(destination)) == {'trim': [2, 5]}


def test_failed_manifest_move_rolls_back_media_rename(tmp_path, monkeypatch):
    import core.clip_mutations as mutations
    clip, destination = tmp_path / 'old.mp4', tmp_path / 'new.mp4'
    clip.write_bytes(b'original')
    sidecar = manifest_path_for(clip)
    sidecar.write_text(json.dumps({'media_file': clip.name}))
    real_rename = mutations.os.rename

    def fail_manifest(source, target):
        if Path(source) == sidecar:
            raise PermissionError('manifest in use')
        real_rename(source, target)

    monkeypatch.setattr(mutations.os, 'rename', fail_manifest)
    with pytest.raises(PermissionError):
        rename_clip(str(clip), str(destination))
    assert clip.read_bytes() == b'original'
    assert not destination.exists()
    assert json.loads(sidecar.read_text())['media_file'] == 'old.mp4'


def test_card_name_and_thumbnail_have_separate_click_targets(qtbot, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.touch()
    card = ClipThumbnail(str(clip), upload_enabled=True)
    qtbot.addWidget(card)
    card.show()
    opened, uploaded = [], []
    card.opened.connect(lambda path, *_: opened.append(path))
    card.upload_requested.connect(uploaded.append)
    qtbot.mouseDClick(card.title_label, Qt.MouseButton.LeftButton)
    assert card._title_stack.currentIndex() == 1
    assert opened == []
    qtbot.keyClick(card.title_edit, Qt.Key.Key_Escape)
    qtbot.mouseClick(card._thumb_frame, Qt.MouseButton.LeftButton)
    assert opened == [str(clip)]
    card.share_btn.click()
    assert uploaded == [str(clip)]
    card.set_upload_info({'url': 'https://example.com/clip.mp4'})
    card.share_btn.click()
    assert card.share_btn.accessibleName() == 'Copy link'
    assert card.window().windowTitle() == ''
    assert uploaded == [str(clip)]
    from PySide6.QtWidgets import QApplication
    assert QApplication.clipboard().text() == 'https://example.com/clip.mp4'


def test_inline_rename_enter_changes_the_actual_file(qtbot, tmp_path):
    clip = tmp_path / 'old.mp4'
    clip.write_bytes(b'media')
    card = ClipThumbnail(str(clip))
    qtbot.addWidget(card)
    card.show()
    renamed = []
    card.renamed.connect(lambda old, new: renamed.append((old, new)))
    qtbot.mouseDClick(card.title_label, Qt.MouseButton.LeftButton)
    card.title_edit.setText('New clip name')
    qtbot.keyClick(card.title_edit, Qt.Key.Key_Return)
    destination = tmp_path / 'New clip name.mp4'
    assert destination.read_bytes() == b'media'
    assert not clip.exists()
    assert renamed == [(str(clip), str(destination))]


@pytest.mark.skipif(sys.platform != 'win32', reason='uses a real Windows sharing lock')
@pytest.mark.parametrize('operation', ['rename', 'delete'])
def test_windows_sharing_lock_retries_without_freezing_ui(qtbot, tmp_path, operation):
    clip = tmp_path / 'locked.mp4'
    clip.write_bytes(b'media')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                                  ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(str(clip), 0x80000000, 0, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    card = ClipThumbnail(str(clip), clips_root=str(tmp_path))
    qtbot.addWidget(card)
    released, ticks = [], []
    card._finish_mutation = released.append
    destination = tmp_path / 'renamed.mp4'
    try:
        card._begin_mutation(operation, str(destination))
        assert card._mutation_busy
        assert clip.exists()
        QTimer.singleShot(10, lambda: ticks.append(True))
        qtbot.waitUntil(lambda: bool(ticks))
    finally:
        kernel.CloseHandle(handle)
    qtbot.waitUntil(lambda: not card._mutation_busy, timeout=4000)
    assert not clip.exists()
    assert destination.exists() == (operation == 'rename')
    assert released == [str(clip)]


def test_single_reader_cancel_does_not_cancel_other_workers():
    parent = threading.Event()
    first, second = _WorkerCancellation(parent), _WorkerCancellation(parent)
    first.set()
    assert first.is_set()
    assert not second.is_set()
    parent.set()
    assert second.is_set()


def test_local_mutation_excludes_upload_and_retains_returned_link(qapp, tmp_path, monkeypatch):
    import core.upload_manager as uploads
    monkeypatch.setattr(uploads, '_HISTORY_FILE', tmp_path / 'history.json')
    monkeypatch.setattr(UploadManager, '_load_settings', lambda _: {})
    manager = UploadManager(SimpleNamespace(get=lambda _key, default=None: default))
    old, new = str(tmp_path / 'old.mp4'), str(tmp_path / 'new.mp4')
    manager._in_flight.add(old)
    assert not manager.begin_local_mutation(old)
    manager._in_flight.clear()
    assert manager.begin_local_mutation(old)
    manager._enqueue(old, threading.Event())
    assert manager._queue.empty()
    manager.end_local_mutation(old)
    uploads._HISTORY_FILE.write_text(json.dumps({old: {'status': 'ok', 'url': 'https://example.com/upload'}}))
    manager.rename_history_path(old, new)
    assert manager.get_upload_info(new)['url'] == 'https://example.com/upload'
    assert manager.get_upload_info(old) is None


def test_old_shipped_theme_migrates_to_current_palette(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(ThemeManager, '_instance', None)
    theme_dir = tmp_path / '.fthr' / 'theme'
    theme_dir.mkdir(parents=True)
    (theme_dir / 'theme.json').write_text(json.dumps({
        'colors': {'BG': '#000000', 'ACCENT': '#123456'},
        'capture_card': {'CAPTURE_CARD_BG': '#000000'}}))
    theme = ThemeManager()
    assert theme.get_color('BG') == DEFAULT_COLORS['BG']
    assert theme.get_color('ACCENT') == '#123456'
    assert theme.get_capture_card_color('CAPTURE_CARD_BG') == DEFAULT_COLORS['SURFACE_1']
    for token, default in DEFAULT_COLORS.items():
        assert getattr(Colors, token) == default


def test_card_shows_copy_request_and_upload_feedback(qtbot, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.touch()
    card = ClipThumbnail(str(clip), upload_enabled=True)
    qtbot.addWidget(card)
    card.show()
    requests = []
    card.upload_requested.connect(requests.append)
    card.share_btn.click()
    assert requests == [str(clip)]
    assert card._action_feedback.text() == 'Upload requested'
    assert card._action_feedback.isVisible()
    card.set_upload_status('Uploading…')
    assert not card.share_btn.isEnabled()
    assert not card._feedback_timer.isActive()
    card.set_upload_info({'url': 'https://example.com/clip.mp4'})
    card.set_upload_status('', feedback='Uploaded')
    card.share_btn.click()
    assert card._action_feedback.text() == 'Link copied'
    assert len(requests) == 1
    card._clear_action_feedback()
    assert not card._action_feedback.isVisible()
