"""Bundled pen paths and private Qt layout fonts, read one character at a time.

The SQLite files contain original pen centerlines. The companion empty-outline
TTFs only provide Qt's character advances; they are never installed system-wide.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import zlib
from contextlib import closing

from PySide6.QtGui import QFontDatabase, QGuiApplication

from .resources import resource_path

_catalog_cache = None
_verified = set()
_registered = {}
MAX_GLYPH_BYTES = 8_000_000
FAMILY_ALIASES = {'yozai': 'Maliang Stroke relaxed'}


def valid_family(font_id, family):
    """Allow only the conventional portable token or an approved RFN alias."""
    return isinstance(family,str) and (family == 'Maliang Stroke '+font_id or family == FAMILY_ALIASES.get(font_id))


def catalog():
    global _catalog_cache
    path = resource_path('fonts', 'catalog.json')
    if not path.is_file():
        return []
    signature = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    if _catalog_cache is None or _catalog_cache[0] != signature:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if payload.get('format') != 1 or not isinstance(payload.get('fonts'), list):
            raise ValueError('内置单线字体目录格式无效')
        fonts = payload['fonts']
        ids = [font['id'] for font in fonts]
        if len(set(ids)) != len(ids):
            raise ValueError('内置单线字体编号重复')
        _catalog_cache = signature, fonts
    return _catalog_cache[1]


def _entry(font_id):
    entry = next((font for font in catalog() if font['id'] == font_id), None)
    if entry is None:
        raise ValueError(f'内置单线字体不存在：{font_id}')
    return entry


def _path(entry, key):
    root = resource_path('fonts').resolve()
    path = (root / entry[key]).resolve()
    if not path.is_relative_to(root):
        raise ValueError('内置单线字体资源路径无效')
    return path


def _verify(path, digest):
    stat = path.stat()
    signature = str(path), stat.st_size, stat.st_mtime_ns, digest
    if signature not in _verified:
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('内置单线字体资源校验失败')
        _verified.add(signature)


def descriptor(font_id):
    entry = _entry(font_id)
    return {'builtin_id': font_id, 'family': entry['family'], 'name': entry['name'],
            'source_name': entry.get('source_name', ''), 'em_size': 1000,
            'origin_x': 0, 'sha256': entry['sha256'], 'glyphs': {},
            'resource_versions': {current: {key: _entry(current)[key]
                for key in ('sha256', 'layout_sha256') if key in _entry(current)}
                for current in _chain(font_id)}}


def descriptors():
    return [descriptor(font['id']) for font in catalog()]


def license_text(font_id):
    """The complete, locally bundled UTF-8 license, without opening a browser."""
    return _path(_entry(font_id), 'license_file').read_text(encoding='utf-8')


def _chain(font_id):
    """Explicit fallback order first, then transitive fallbacks breadth-first."""
    entries = {font['id']: font for font in catalog()}
    if font_id not in entries:
        raise ValueError(f'内置单线字体不存在：{font_id}')
    result = []
    pending = [font_id]
    while pending:
        current = pending.pop(0)
        if current in result:
            continue
        result.append(current)
        pending.extend(fallback for fallback in entries[current].get('fallback_ids', []) if fallback in entries)
    return result


def layout_families(font):
    if not font.get('builtin_id'):
        return [font['family']]
    families = []
    for font_id in _chain(font['builtin_id']):
        entry = _entry(font_id)
        expected = font.get('resource_versions', {}).get(font_id, {}).get('layout_sha256')
        if expected and expected != entry['layout_sha256']:
            raise ValueError('内置排版字库版本已改变；已保存笔迹保持不变，请重新选择字体后编辑')
        path = _path(entry, 'layout_file')
        _verify(path, entry['layout_sha256'])
        key = (str(path), entry['layout_sha256'])
        if key not in _registered:
            if QGuiApplication.instance() is None:
                raise RuntimeError('注册内置排版字体需要 Qt 应用')
            registered = QFontDatabase.addApplicationFont(str(path))
            if registered < 0 or entry['family'] not in QFontDatabase.applicationFontFamilies(registered):
                raise ValueError(f'内置字体排版资源无法加载：{entry["name"]}')
            _registered[key] = registered
        families.append(entry['family'])
    return families


def resolve_descriptor(fonts, requested, default=''):
    """Keep the selected primary family, independent of Qt's actual fallback."""
    for family in requested or []:
        family = family.strip().strip('\"\'')
        if family in fonts:
            return fonts[family]
        if family.startswith(('Maliang Stroke ', 'PageWriter GFont ')):
            return None
    return fonts.get(default)


def glyph_for(font, char):
    cp = ord(char)
    if not 0 <= cp <= 0x10ffff or 0xd800 <= cp <= 0xdfff:
        return None
    key = str(cp)
    if key in font['glyphs'] and (char.isspace() or _has_ink(font['glyphs'][key])):
        return font['glyphs'][key]
    entry = _entry(font['builtin_id'])
    if font['sha256'] != entry['sha256']:
        raise ValueError('内置单线字库版本已改变；已保存字形可继续使用，请重新选择字体后新增字符')
    for font_id in _chain(font['builtin_id']):
        entry = _entry(font_id)
        expected = font.get('resource_versions', {}).get(font_id, {}).get('sha256')
        if expected and expected != entry['sha256']:
            raise ValueError('内置回退字库版本已改变；请重新选择字体后新增字符')
        path = _path(entry, 'file')
        _verify(path, entry['sha256'])
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as database:
            row = database.execute('SELECT data FROM glyphs WHERE cp = ?', (cp,)).fetchone()
        if row is None:
            continue
        decoder = zlib.decompressobj()
        raw = decoder.decompress(row[0], MAX_GLYPH_BYTES + 1)
        if len(raw) > MAX_GLYPH_BYTES or not decoder.eof or decoder.unused_data:
            raise ValueError('内置单线字形压缩数据无效')
        commands = json.loads(raw)
        if (not isinstance(commands, list) or len(commands) > 200_000
                or any(not isinstance(p, list) or len(p) != 3
                       or type(p[0]) is not int or p[0] not in (0, 1)
                       or any(type(v) not in (int, float) or not math.isfinite(v)
                              or abs(v) > 1_000_000 for v in p[1:]) for p in commands)
                or (commands and commands[0][0] != 0)):
            raise ValueError('内置单线字形路径无效')
        if not char.isspace() and not _has_ink(commands):
            continue
        font['glyphs'][key] = commands
        font.setdefault('glyph_sources', {})[key] = font_id
        return commands
    return None


def _has_ink(commands):
    previous = None
    for kind, x, y in commands:
        if kind == 1 and previous is not None and previous != (x, y):
            return True
        previous = (x, y)
    return False
