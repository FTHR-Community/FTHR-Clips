import shutil

from core import compositor


def _detect(monkeypatch, env, which=lambda name: None):
    for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'XDG_SESSION_TYPE', 'XDG_CURRENT_DESKTOP',
                'XDG_SESSION_DESKTOP', 'HYPRLAND_INSTANCE_SIGNATURE', 'GNOME_DESKTOP_SESSION_ID'):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(shutil, 'which', which)
    compositor._reset_cache_for_tests()
    try:
        return compositor.detect_compositor()
    finally:
        compositor._reset_cache_for_tests()


def test_gnome_session_with_plasma_installed_is_gnome(monkeypatch):
    assert _detect(monkeypatch, {'WAYLAND_DISPLAY': 'wayland-0', 'XDG_CURRENT_DESKTOP': 'GNOME'},
                   which=lambda name: '/usr/bin/kwin_wayland') == 'gnome'


def test_sway_session_with_plasma_installed_is_unknown_not_kwin(monkeypatch):
    assert _detect(monkeypatch, {'WAYLAND_DISPLAY': 'wayland-1', 'XDG_CURRENT_DESKTOP': 'sway'},
                   which=lambda name: '/usr/bin/kwin_wayland') == 'wayland-unknown'


def test_kde_env_wins_before_any_binary_probe(monkeypatch):
    assert _detect(monkeypatch, {'WAYLAND_DISPLAY': 'wayland-0', 'XDG_CURRENT_DESKTOP': 'KDE'}) == 'kwin'


def test_xdg_session_type_wayland_counts_as_wayland_even_without_display_var(monkeypatch):
    assert _detect(monkeypatch, {'DISPLAY': ':0', 'XDG_SESSION_TYPE': 'wayland',
                                 'XDG_CURRENT_DESKTOP': 'Hyprland',
                                 'HYPRLAND_INSTANCE_SIGNATURE': 'x'}) == 'hyprland'


def test_pure_x11(monkeypatch):
    assert _detect(monkeypatch, {'DISPLAY': ':0', 'XDG_SESSION_TYPE': 'x11'}) == 'x11'
