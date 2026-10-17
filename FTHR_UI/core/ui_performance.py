"""UI-only performance choices; presets never change capture or saved edits."""

# Impact is an estimate of avoidable UI work, not a benchmark percentage.
UI_OPTIONS = (
    ('clip_editor_enabled', True, 'Full clip editor', 'Very high',
     'Off: open clips in a simple video player with playback and volume controls, '
     'without the editing tools. Clips already open in the editor stay as they are.'),
    ('clip_editor_live_preview', True, 'Live edit preview', 'High',
     'Off: show the original video while you edit, instead of showing your crops, '
     'stretching and color changes as you make them. Your edits are still saved '
     'and will appear in exported clips.'),
    ('editor_color_preview_enabled', True, 'Color effects in preview', 'High',
     'Off: hide color changes while a clip plays. Cropping and stretching still '
     'appear, and exported clips keep your color changes.'),
    ('timeline_preview_strip_enabled', True, 'Seek bar preview', 'High',
     'Off: hide the small preview pictures along the clip’s timeline.'),
    ('library_thumbnails_enabled', True, 'Library thumbnails', 'Medium',
     'Off: hide preview pictures in your clip library. You can still find, '
     'open and manage clips by name.'),
    ('settings_visual_previews_enabled', True, 'Animated settings previews', 'Medium',
     'Off: stop the moving camera and keyboard previews in Settings. This does '
     'not change what appears in recordings. After turning this back on, '
     'reopen Visuals to see the previews.'),
    ('reduce_ui_animations', False, 'Reduce interface animations', 'Medium',
     'On: use fewer animations when you move the mouse over buttons or switch '
     'between screens.'),
    ('pause_ui_in_background', True, 'Pause interface in background', 'Medium',
     'On: let the app do less work when you’re using another app or its window '
     'is minimized. Recording continues.'),
)
DEFAULTS = {key: default for key, default, *_ in UI_OPTIONS}
PRESETS = {
    'Full experience': dict(DEFAULTS),
    'Balanced': dict(DEFAULTS, reduce_ui_animations=True,
                     settings_visual_previews_enabled=False,
                     timeline_preview_strip_enabled=False),
    'Low power': dict(DEFAULTS, reduce_ui_animations=True,
                      settings_visual_previews_enabled=False,
                      timeline_preview_strip_enabled=False,
                      clip_editor_live_preview=False,
                      editor_color_preview_enabled=False,
                      library_thumbnails_enabled=False),
}
PRESETS['Minimal / viewer only'] = dict(PRESETS['Low power'], clip_editor_enabled=False)


def matching_preset(settings):
    values = {key: bool(settings.get(key, default)) for key, default in DEFAULTS.items()}
    return next((name for name, preset in PRESETS.items() if values == preset), 'Custom')
