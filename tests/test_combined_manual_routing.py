"""Manual finalization must honor the accepted audio mode, not live settings."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('publish_ui', [False, True])
@pytest.mark.parametrize('separated', [False, True])
def test_manual_combines_only_the_accepted_single_track_mode(
        tmp_path, monkeypatch, publish_ui, separated):
    import main
    from core import combined_audio_policy
    path = tmp_path / 'recording.mp4'
    path.write_bytes(b'closed recording')
    events = []
    monkeypatch.setattr(combined_audio_policy, 'combine_recording_audio',
                        lambda path: events.append('combine'))
    monkeypatch.setattr(main, 'probe_media', lambda path: events.append('probe'))
    monkeypatch.setattr(main, 'probe_video_presentation_duration', lambda path: 1.0)
    session = SimpleNamespace(request=SimpleNamespace(capture_config=SimpleNamespace(
        audio_enabled=True, separate_audio_enabled=separated)))
    host = SimpleNamespace(
        _manual_record_path=path, _manual_record_session=session,
        _manual_record_state='stopping',
        _manual_record_timer=SimpleNamespace(stop=lambda: None),
        settings_manager=SimpleNamespace(get=lambda *args: pytest.fail('must use frozen mode')),
        _ui_call=SimpleNamespace(emit=lambda callback: callback()),
        _persist_manual_recording_highlights=lambda path, identity, **kwargs: identity,
        _complete_manual_recording_file=lambda *args, **kwargs: events.append('complete'),
        _fail_manual_recording=lambda *args, **kwargs: pytest.fail(str(args)))
    main.MainWindow._finalize_manual_recording_file(host, publish_ui=publish_ui)
    if publish_ui:
        host._manual_record_finalize_thread.join(5)
        assert not host._manual_record_finalize_thread.is_alive()
    assert events == ([] if separated else ['combine']) + ['probe', 'complete']
