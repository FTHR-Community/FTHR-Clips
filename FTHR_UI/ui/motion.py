"""Application preference for optional interface animations."""

from PySide6.QtWidgets import QApplication


def reduce_motion(widget=None):
    """Use a surface's settings when available, then the application default."""
    while widget is not None:
        settings = widget.__dict__.get('sm') or widget.__dict__.get('settings_manager')
        if settings is not None:
            return bool(settings.get('reduce_ui_animations', False))
        parent = getattr(widget, 'parentWidget', None)
        widget = parent() if parent is not None else None
    app = QApplication.instance()
    return bool(app is not None and app.property('fthrReduceMotion'))


def set_reduce_motion(enabled):
    app = QApplication.instance()
    if app is None:
        return
    app.setProperty('fthrReduceMotion', bool(enabled))
    if not enabled:
        return
    # Finish live geometry handoffs before abandoning their snapshots.
    for widget in app.allWidgets():
        fade = widget.__dict__.get('_hover_fade')
        if fade is not None:
            fade.dispose()
        slide = widget.__dict__.get('_slide') or widget.__dict__.get('_clip_slide')
        if slide is not None:
            slide.animation.setCurrentTime(slide.animation.duration())
        hover = widget.__dict__.get('_hover_animation')
        if hover is not None:
            hover.setCurrentTime(hover.duration())
        for name in ('_show_anim', '_fade_in_anim'):
            animation = widget.__dict__.get(name)
            if animation is not None:
                animation.setCurrentTime(animation.duration())
        flight = widget.__dict__.get('_editor_flight')
        if flight is not None:
            flight._animation.setCurrentTime(flight._animation.duration())
    # Slides normally keep one reusable Quick surface. Once animations are
    # disabled, retire that hidden render target as well as its source images.
    for widget in app.allWidgets():
        surface = widget.__dict__.get('_snapshot_slide_surface')
        if surface is not None:
            widget._snapshot_slide_surface = None
            surface.hide()
            surface.release_images()
            surface.deleteLater()
