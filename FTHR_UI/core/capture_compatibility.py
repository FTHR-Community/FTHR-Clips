"""Owner-scoped, optional Windows desktop-capture compatibility policy.

Registrations belong to one host window and never write user preferences.
The resolved request must carry this policy before capture admission runs.
"""
from dataclasses import replace
import sys

from PySide6.QtCore import QTimer


def border_free_capture_enabled(window):
    return sys.platform == 'win32' and bool(
        getattr(window, '_border_free_capture_owners', ()))


def wgc_capture_enabled(window):
    return sys.platform == 'win32' and bool(
        getattr(window, '_wgc_capture_owners', ()))


def _capture_backend(window):
    if border_free_capture_enabled(window):
        return 'dxgi'
    if wgc_capture_enabled(window):
        return 'wgc'
    return 'auto'


def _changed(window, previous):
    if previous == _capture_backend(window):
        return
    # Coalesce package replacement and failed startup cleanup. Neither should
    # briefly launch a native process with a policy that never became active.
    if getattr(window, '_capture_compatibility_notification_pending', False):
        return
    window._capture_compatibility_notification_pending = True

    def apply():
        window._capture_compatibility_notification_pending = False
        current = _capture_backend(window)
        if (previous == current
                or getattr(window, '_shutdown_requested', False)):
            return
        hook = ('_on_wgc_capture_changed' if 'wgc' in (previous, current)
                else '_on_border_free_capture_changed')
        callback = getattr(window, hook, None)
        if callable(callback):
            callback()

    QTimer.singleShot(0, window, apply)


def register_border_free_capture(window, *, owner):
    if sys.platform != 'win32':
        raise RuntimeError('Border-free capture is available only on Windows')
    if owner is None:
        raise ValueError('Border-free capture requires a plugin owner')
    if wgc_capture_enabled(window):
        raise RuntimeError(
            'Border-free Capture cannot run while a plugin is forcing Windows Graphics Capture (WGC).')
    checker = getattr(window, '_admission_decision_for_settings', None)
    if callable(checker):
        decision = checker(source_mode_override='desktop')
        if decision is None or not decision.allowed:
            raise RuntimeError('Border-free capture requires desktop capture permission: '
                               + str(getattr(decision, 'reason', '') or
                                     'capture admission is unavailable'))
    if wgc_capture_enabled(window):
        raise RuntimeError(
            'Border-free Capture cannot run while a plugin is forcing Windows Graphics Capture (WGC).')
    previous = _capture_backend(window)
    owners = set(getattr(window, '_border_free_capture_owners', ()))
    owners.add(owner)
    window._border_free_capture_owners = owners
    _changed(window, previous)


def unregister_border_free_capture(window, *, owner):
    previous = _capture_backend(window)
    owners = set(getattr(window, '_border_free_capture_owners', ()))
    owners.discard(owner)
    window._border_free_capture_owners = owners
    _changed(window, previous)


def register_wgc_capture(window, *, owner):
    if sys.platform != 'win32':
        raise RuntimeError('Windows Graphics Capture (WGC) is available only on Windows')
    if owner is None:
        raise ValueError('Windows Graphics Capture requires a plugin owner')
    if border_free_capture_enabled(window):
        raise RuntimeError(
            'Windows Graphics Capture (WGC) cannot be forced while Border-free Capture is active.')
    previous = _capture_backend(window)
    owners = set(getattr(window, '_wgc_capture_owners', ()))
    owners.add(owner)
    window._wgc_capture_owners = owners
    _changed(window, previous)


def unregister_wgc_capture(window, *, owner):
    previous = _capture_backend(window)
    owners = set(getattr(window, '_wgc_capture_owners', ()))
    owners.discard(owner)
    window._wgc_capture_owners = owners
    _changed(window, previous)


def apply_border_free_capture(config):
    """Freeze strict DXGI, full-frame capture, and cursor inclusion together."""
    return replace(config, capture_backend='dxgi', capture_cursor=True,
                   crop_enabled=False, crop_x=0.0, crop_y=0.0,
                   crop_w=1.0, crop_h=1.0)


def apply_capture_policy(window, config):
    backend = _capture_backend(window)
    if backend == 'dxgi':
        return apply_border_free_capture(config)
    if backend == 'wgc':
        return replace(config, capture_backend=backend)
    return config


def capture_policy_is_current(window, request):
    config = request.capture_config
    backend = _capture_backend(window)
    if config.capture_backend != backend:
        return False
    if backend == 'dxgi':
        return (request.source.source_mode == 'desktop'
                and request.source.authority == 'manual'
                and config.capture_cursor and not config.crop_enabled)
    return True
