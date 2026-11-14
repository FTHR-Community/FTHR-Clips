"""Validate and install content packs and explicitly installed feature plugins."""

from __future__ import annotations

from dataclasses import fields
import io
import json
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import RLock
import zipfile
import zlib

from core.export_profiles import ExportPreset
from core.theme_manager import (
    BUILTIN_FONT_FAMILIES,
    CUSTOMIZABLE_ICONS,
    CUSTOMIZABLE_SOUNDS,
    DEFAULT_CAPTURE_CARD_COLORS,
    DEFAULT_COLORS,
    DEFAULT_FONTS,
    SUPPORTED_FONT_FORMATS,
    SUPPORTED_SOUND_FORMATS,
    ThemeManager,
)


API_VERSION = 1
PRESET_PREFIX = 'plugin:'
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_JSON_BYTES = 256 * 1024
_MAX_COVER_BYTES = 4 * 1024 * 1024
_MAX_MEMBERS = 128
_MAX_PRESETS = 64
_ID = re.compile(r'[a-z0-9]+(?:[.-][a-z0-9]+)*')
_VERSION = re.compile(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})')
_COLOR = re.compile(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?')
_WINDOWS_DEVICES = {
    'con', 'prn', 'aux', 'nul', 'conin$', 'conout$',
    *(f'{prefix}{number}' for prefix in ('com', 'lpt') for number in '123456789¹²³'),
}
_CONTRIBUTIONS = {'theme': ('theme', '.zip'), 'export-presets': ('export_presets', '.json'),
                  'feature': ('feature', '.zip')}
_PRESET_FIELDS = {field.name for field in fields(ExportPreset)} - {'built_in'}
_DISABLED = b'fthr-disabled'
_LOCK = RLock()
_ZIP_READ_ERRORS = (
    zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError,
    RuntimeError, zlib.error, EOFError, UnicodeError,
)


class PluginError(ValueError):
    """The pack or requested lifecycle operation is invalid."""


class PluginManager:
    """Manage local ZIP packs stored separately from personal preferences."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else Path.home() / '.fthr' / 'plugins'

    def list_plugins(self) -> list[dict]:
        with _LOCK:
            return [manifest for manifest, _ in self._installed()]

    def inspect_archive(self, path: str | Path) -> dict:
        manifest, _, _ = _read_pack(Path(path))
        return manifest

    def cover_image(self, plugin_id: str) -> bytes:
        """Read an installed cover, including for disabled plugins."""
        with _LOCK:
            return self._read_installed(self._path(plugin_id), wanted='cover')[1]

    def archive_cover(self, path: str | Path) -> bytes:
        return _read_pack(Path(path))[2]

    def install(self, path: str | Path) -> dict:
        manifest, payload, cover = _read_pack(Path(path))
        destination = self._path(manifest['id'])
        with _LOCK:
            enabled = True
            if destination.exists():
                current, _ = self._read_installed(destination)
                if current['type'] != manifest['type']:
                    raise PluginError('An update must keep the installed plugin type.')
                if _version(manifest['version']) < _version(current['version']):
                    raise PluginError('Uninstall the newer plugin before installing an older version.')
                enabled = current['enabled']
            self._write(destination, manifest, payload, enabled, cover)
        return {**manifest, 'enabled': enabled}

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise PluginError('Plugin enabled state must be true or false.')
        with _LOCK:
            destination = self._path(plugin_id)
            manifest, payload = self._read_installed(destination)
            if manifest.pop('enabled') != enabled:
                self._write(destination, manifest, payload, enabled, self.cover_image(plugin_id))

    def uninstall(self, plugin_id: str) -> None:
        with _LOCK:
            self._path(plugin_id).unlink(missing_ok=True)

    def apply_theme(self, plugin_id: str) -> None:
        """Import a separate editable copy using the app's theme importer."""
        with _LOCK:
            manifest, payload = self._read_installed(self._path(plugin_id))
            if manifest['type'] != 'theme' or not manifest['enabled']:
                raise PluginError('Only an enabled theme plugin can be applied.')
            try:
                _validate_theme(payload)
            except _ZIP_READ_ERRORS as exc:
                raise PluginError('Installed theme ZIP is damaged.') from exc
            manager = ThemeManager()
            with TemporaryDirectory(prefix='fthr-theme-plugin-') as directory:
                backup = Path(directory) / 'backup.zip'
                incoming = Path(directory) / 'theme.zip'
                incoming.write_bytes(payload)
                if not manager.export_theme(backup):
                    raise PluginError('Could not back up the current theme.')
                if not manager.import_theme(incoming):
                    if not manager.import_theme(backup):
                        raise PluginError('Theme import failed and the previous theme could not be restored.')
                    raise PluginError('Theme import failed. The previous theme was restored.')

    def export_presets(self) -> tuple[ExportPreset, ...]:
        with _LOCK:
            presets = []
            for manifest, payload in self._installed(presets_only=True):
                try:
                    presets.extend(_validate_presets(payload, manifest['id']))
                except PluginError:
                    # A damaged pack must not hide other packs or user presets.
                    continue
            return tuple(presets)

    def _path(self, plugin_id: str) -> Path:
        return self.root / f'{_identifier(plugin_id)}.zip'

    def feature_packages(self):
        """Return validated enabled features; importing code belongs to the runtime."""
        with _LOCK:
            result = []
            for path in sorted(self.root.glob('*.zip')):
                try:
                    manifest, payload = self._read_installed(path, wanted='feature')
                    if payload is not None:
                        _validate_feature(payload)
                        result.append((manifest, payload))
                except (PluginError, OSError, *_ZIP_READ_ERRORS):
                    # A damaged feature is excluded before the runtime can import its code.
                    continue
            return result

    def _installed(self, *, presets_only: bool = False):
        for path in sorted(self.root.glob('*.zip')):
            try:
                wanted = 'export-presets' if presets_only else None
                manifest, payload = self._read_installed(path, wanted=wanted)
                if not presets_only or payload is not None:
                    yield manifest, payload
            except (PluginError, OSError):
                # An unreadable ZIP does not prevent the remaining plugins from being listed.
                continue

    def _read_installed(self, path: Path, *, wanted: str | None = 'all'):
        if path.is_symlink() or not path.is_file():
            raise PluginError(f'Plugin "{path.stem}" is unavailable.')
        try:
            with path.open('rb') as source:
                if os.fstat(source.fileno()).st_size > _MAX_ARCHIVE_BYTES:
                    raise PluginError('Installed pack exceeds the size limit.')
                with zipfile.ZipFile(source) as archive:
                    members = _members(archive)
                    manifest = _manifest(_read_json(archive, members, 'manifest.json'))
                    if manifest['id'] != path.stem:
                        raise PluginError('Installed pack ID does not match its filename.')
                    enabled = archive.comment != _DISABLED
                    payload = None
                    if wanted == 'cover':
                        payload = _read_cover(archive, members, manifest)
                    elif wanted == 'all' or (enabled and wanted == manifest['type']):
                        payload = _read_contribution(archive, members, manifest)
                    return {**manifest, 'enabled': enabled}, payload
        except _ZIP_READ_ERRORS as exc:
            raise PluginError('Installed pack is damaged.') from exc

    def _write(self, destination: Path, manifest: dict, payload: bytes, enabled: bool,
               cover: bytes = b'') -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=self.root, suffix='.tmp', delete=False) as staged:
            temporary = Path(staged.name)
        try:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
                contribution = _contribution_path(manifest)
                if contribution is not None:
                    archive.writestr(contribution, payload)
                if manifest.get('cover'):
                    archive.writestr(manifest['cover'], cover)
                archive.comment = b'' if enabled else _DISABLED
            if temporary.stat().st_size > _MAX_ARCHIVE_BYTES:
                raise PluginError('Installed pack exceeds the size limit.')
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def _read_pack(path: Path) -> tuple[dict, bytes, bytes]:
    try:
        with path.open('rb') as source:
            if os.fstat(source.fileno()).st_size > _MAX_ARCHIVE_BYTES:
                raise PluginError('Plugin ZIP exceeds the 32 MiB size limit.')
            with zipfile.ZipFile(source) as archive:
                members = _members(archive)
                manifest = _manifest(_read_json(archive, members, 'manifest.json'))
                contribution = _contribution_path(manifest)
                expected = {'manifest.json'} | ({contribution} if contribution else set())
                if manifest.get('cover'):
                    expected.add(manifest['cover'])
                if set(members) != expected:
                    raise PluginError('Plugin ZIP must contain only manifest.json, its declared contribution and optional cover.')
                payload = _read_contribution(archive, members, manifest)
                cover = _read_cover(archive, members, manifest)
        if manifest['type'] == 'theme':
            _validate_theme(payload)
        elif manifest['type'] == 'export-presets':
            _validate_presets(payload, manifest['id'])
        elif manifest['type'] == 'feature':
            _validate_feature(payload)
        return manifest, payload, cover
    except _ZIP_READ_ERRORS as exc:
        raise PluginError('Could not read the plugin ZIP.') from exc


def _read_contribution(archive: zipfile.ZipFile, members: dict, manifest: dict) -> bytes:
    if manifest['type'] == 'test':
        return b''
    limit = _MAX_ARCHIVE_BYTES if manifest['type'] in ('theme', 'feature') else _MAX_JSON_BYTES
    return _read_member(archive, members, _contribution_path(manifest), limit)


def _safe_name(value: str) -> bool:
    return (
        0 < len(value) <= 128 and value == value.strip(' .')
        and value.split('.')[0].rstrip().casefold() not in _WINDOWS_DEVICES
        and not any(char in '<>:"/\\|?*' or not char.isprintable() for char in value)
    )


def _members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) > _MAX_MEMBERS or sum(item.file_size for item in entries) > _MAX_ARCHIVE_BYTES:
        raise PluginError('ZIP exceeds the unpacked size or file count limit.')
    members = {}
    seen = set()
    for item in entries:
        name = item.filename.rstrip('/') if item.is_dir() else item.filename
        if item.orig_filename != item.filename or not all(_safe_name(part) for part in name.split('/')):
            raise PluginError(f'Unsafe ZIP path: {item.orig_filename!r}.')
        normalized = name.casefold()
        if normalized in seen:
            raise PluginError(f'Duplicate ZIP path: {name!r}.')
        seen.add(normalized)
        file_type = stat.S_IFMT(item.external_attr >> 16)
        if file_type not in (0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG):
            raise PluginError('ZIP members must be ordinary files or directories.')
        if item.flag_bits & 1 or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise PluginError('Only unencrypted stored or deflated ZIP members are supported.')
        members[item.filename] = item
    return members


def _read_member(archive: zipfile.ZipFile, members: dict, name: str,
                 limit: int = _MAX_ARCHIVE_BYTES) -> bytes:
    item = members.get(name)
    if item is None:
        raise PluginError(f'Missing {name!r} in ZIP.')
    if item.file_size > limit:
        raise PluginError(f'{name!r} exceeds the file size limit.')
    with archive.open(item) as entry:
        data = entry.read(limit + 1)
    if len(data) != item.file_size:
        raise PluginError(f'Invalid ZIP member size for {name!r}.')
    return data


def _json(data: bytes, name: str):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PluginError(f'Duplicate JSON field in {name!r}: {key!r}.')
            result[key] = value
        return result

    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=unique_keys)
    except PluginError:
        raise
    except (ValueError, RecursionError) as exc:
        raise PluginError(f'{name!r} must contain valid UTF-8 JSON.') from exc


def _read_json(archive: zipfile.ZipFile, members: dict, name: str):
    return _json(_read_member(archive, members, name, _MAX_JSON_BYTES), name)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _safe_name(value) or len(value) > 64 or not _ID.fullmatch(value):
        raise PluginError('Plugin and preset IDs must be lowercase and use letters, numbers, dots or hyphens.')
    return value


def _version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise PluginError('Plugin version must have three numeric parts, such as 1.2.0.')
    return tuple(map(int, value.split('.')))


def _label(value, name: str) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= 120) or not value.isprintable() or not value.strip():
        raise PluginError(f'{name} must be a nonempty line of text up to 120 characters.')
    return value.strip()


def _manifest(value) -> dict:
    if not isinstance(value, dict):
        raise PluginError('manifest.json must contain a JSON object.')
    api = value.get('api_version')
    if type(api) is not int or api != API_VERSION:
        raise PluginError(f'Unsupported plugin API version. This app supports API {API_VERSION}.')
    kind = value.get('type')
    if not isinstance(kind, str) or kind not in {*_CONTRIBUTIONS, 'test'}:
        raise PluginError('Unsupported plugin type. Supported types are theme, export-presets, feature and test.')
    contributes = value.get('contributes')
    if kind == 'test':
        if contributes != {}:
            raise PluginError('Test plugins must have an empty contributes object.')
    else:
        key, extension = _CONTRIBUTIONS[kind]
        if not isinstance(contributes, dict) or set(contributes) != {key}:
            raise PluginError(f'{kind} requires a single {key!r} contribution.')
        path = contributes[key]
        if (not isinstance(path, str) or not _safe_name(path) or not path.lower().endswith(extension)
                or path.casefold() == 'manifest.json'):
            raise PluginError(f'The contribution must name a {extension} file at the ZIP root.')
    _version(value.get('version'))
    description = value.get('description')
    if description is not None:
        if (not isinstance(description, str) or not description.strip()
                or len(description) > 1000 or not description.isprintable()):
            raise PluginError('Plugin description must be a nonempty line of text up to 1000 characters.')
        description = description.strip()
    cover = value.get('cover')
    if cover is not None:
        if (not isinstance(cover, str) or not _safe_name(cover)
                or Path(cover).suffix.lower() not in ('.png', '.jpg', '.jpeg')):
            raise PluginError('Cover must name a PNG or JPEG file at the ZIP root.')
    return {
        'id': _identifier(value.get('id')),
        'name': _label(value.get('name'), 'Plugin name'),
        'version': value['version'],
        'api_version': api,
        'author': _label(value.get('author'), 'Plugin author'),
        'type': kind,
        'contributes': contributes,
        **({'description': description} if description is not None else {}),
        **({'cover': cover} if cover is not None else {}),
    }


def _read_cover(archive, members, manifest) -> bytes:
    name = manifest.get('cover')
    if not name:
        return b''
    data = _read_member(archive, members, name, _MAX_COVER_BYTES)
    if not data.startswith(_RASTER_SIGNATURES[Path(name).suffix.lower()]):
        raise PluginError('Cover content must match its PNG or JPEG extension.')
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImageReader

    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if not size.isValid() or max(size.width(), size.height()) > 4096:
        raise PluginError('Cover must be a valid image no larger than 4096 × 4096 pixels.')
    if reader.read().isNull():
        raise PluginError('Cover image could not be decoded.')
    return data


def _contribution_path(manifest: dict) -> str | None:
    if manifest['type'] == 'test':
        return None
    key, _ = _CONTRIBUTIONS[manifest['type']]
    return manifest['contributes'][key]


def _validate_presets(data: bytes, plugin_id: str) -> tuple[ExportPreset, ...]:
    entries = _json(data, 'Export presets')
    if not isinstance(entries, list) or not (1 <= len(entries) <= _MAX_PRESETS):
        raise PluginError(f'Export preset contribution must be an array of 1 to {_MAX_PRESETS} presets.')
    presets = []
    seen = set()
    for value in entries:
        if not isinstance(value, dict) or set(value) - _PRESET_FIELDS:
            raise PluginError('Invalid export preset fields.')
        preset_id = _identifier(value.get('preset_id'))
        if preset_id in seen:
            raise PluginError(f'Duplicate export preset ID: {preset_id!r}.')
        seen.add(preset_id)
        name = _label(value.get('name'), 'Preset name')
        try:
            presets.append(ExportPreset(**{
                **value, 'preset_id': f'{PRESET_PREFIX}{plugin_id}:{preset_id}', 'name': name,
            }).validated())
        except (ValueError, TypeError, OverflowError) as exc:
            raise PluginError(f'Invalid export preset {preset_id!r}: {exc}') from exc
    return tuple(presets)


_RASTER_SIGNATURES = {
    '.png': (b'\x89PNG\r\n\x1a\n',),
    '.jpg': (b'\xff\xd8\xff',),
    '.jpeg': (b'\xff\xd8\xff',),
    '.bmp': (b'BM',),
    '.ico': (b'\x00\x00\x01\x00',),
}


def _validate_feature(data: bytes) -> None:
    """Bound and check source/assets without executing any package code."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = _members(archive)
        if '__init__.py' not in members:
            raise PluginError('Feature ZIP requires __init__.py at its root.')
        for name, member in members.items():
            if member.is_dir():
                continue
            extension = Path(name).suffix.lower()
            if extension == '.py':
                source = _read_member(archive, members, name, _MAX_JSON_BYTES)
                try:
                    compile(source, name, 'exec')
                except (SyntaxError, ValueError) as exc:
                    raise PluginError(f'Invalid Python source: {name}.') from exc
            elif extension in _RASTER_SIGNATURES:
                if not _read_member(archive, members, name).startswith(_RASTER_SIGNATURES[extension]):
                    raise PluginError(f'Invalid image: {name}.')
            elif extension in ('.rnnn', '.txt'):
                # Models and attribution are inert assets, not native libraries.
                # The feature verifies its model hash before passing it to FFmpeg.
                limit = 4 * 1024 * 1024 if extension == '.rnnn' else _MAX_JSON_BYTES
                data = _read_member(archive, members, name, limit)
                try:
                    text = data.decode('utf-8')
                except UnicodeDecodeError as exc:
                    raise PluginError(f'Invalid UTF-8 feature asset: {name}.') from exc
                if '\x00' in text:
                    raise PluginError(f'Invalid text feature asset: {name}.')
            else:
                raise PluginError(f'Unsupported feature file: {name}.')
_ASSETS = {
    'icons': ('icons', _RASTER_SIGNATURES),
    'sounds': ('sounds', SUPPORTED_SOUND_FORMATS),
    'font_files': ('fonts', SUPPORTED_FONT_FORMATS),
}


def _theme_mapping(config: dict, key: str, allowed_keys=None) -> dict:
    mapping = config.get(key, {})
    if not isinstance(mapping, dict) or (allowed_keys is not None and set(mapping) - set(allowed_keys)):
        raise PluginError(f'Invalid theme {key!r} mapping.')
    return mapping


def _validate_theme(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = _members(archive)
        config = _read_json(archive, members, 'theme.json')
        if not isinstance(config, dict):
            raise PluginError('theme.json must contain a JSON object.')
        for key, allowed in (
            ('colors', DEFAULT_COLORS),
            ('capture_card', DEFAULT_CAPTURE_CARD_COLORS),
            ('icon_tints', {*CUSTOMIZABLE_ICONS, '_global'}),
        ):
            for color in _theme_mapping(config, key, allowed).values():
                if not isinstance(color, str) or not (_COLOR.fullmatch(color) or key == 'icon_tints' and color == ''):
                    raise PluginError(f'Invalid theme color in {key!r}. Use a hex color.')
        icons = _theme_mapping(config, 'icons', CUSTOMIZABLE_ICONS)
        sounds = _theme_mapping(config, 'sounds', CUSTOMIZABLE_SOUNDS)
        fonts = _theme_mapping(config, 'font_files')
        for family in fonts:
            if not (1 <= len(family) <= 120 and all(c.isalnum() or c in ' ._()-' for c in family)):
                raise PluginError('Invalid theme font family name.')
        for family in _theme_mapping(config, 'fonts', DEFAULT_FONTS).values():
            if not isinstance(family, str) or family not in (*BUILTIN_FONT_FAMILIES, *fonts):
                raise PluginError('Theme fonts must be built-in families or reference an included font.')
        expected = {'theme.json'}
        for key, references in (('icons', icons), ('sounds', sounds), ('font_files', fonts)):
            directory, formats = _ASSETS[key]
            expected.add(f'{directory}/')
            for filename in references.values():
                if not isinstance(filename, str) or not _safe_name(filename):
                    raise PluginError('Unsafe theme asset filename.')
                extension = Path(filename).suffix.lower()
                if extension not in formats:
                    raise PluginError(f'Unsupported theme asset format: {filename!r}.')
                name = f'{directory}/{filename}'
                if name in expected:
                    continue
                asset = _read_member(archive, members, name)
                if directory == 'icons' and not asset.startswith(_RASTER_SIGNATURES[extension]):
                    raise PluginError(f'Theme icon content does not match its file extension: {filename!r}.')
                expected.add(name)
        if set(members) - expected:
            raise PluginError('Theme ZIP contains files unreferenced by theme.json.')
