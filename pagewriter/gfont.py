"""Read local gfont v5 ZIP glyphs; encrypted metadata is never interpreted.

Only glyphs actually requested by a document are retained in its descriptor.
The reference em is a font-wide empirical measurement, not a vendor point size.
"""
from pathlib import Path
import hashlib
import math
import struct
import zipfile

REFERENCE = '永国田中正方目日月大'
MAX_RECORD = 2_000_000
_verified = {}


def parse_glyph(data, codepoint):
    if len(data) < 10 or len(data) > MAX_RECORD:
        raise ValueError('gfont 字形长度无效')
    cp, count = struct.unpack_from('>Hi', data)
    if cp != codepoint or not 0 <= count <= 200_000 or count % 2:
        raise ValueError('gfont 字符编号或坐标数量无效')
    offset = 6 + count * 4
    if offset + 4 > len(data):
        raise ValueError('gfont 字形截断')
    coords = struct.unpack_from('>' + str(count) + 'f', data, 6)
    commands = struct.unpack_from('>i', data, offset)[0]
    if commands != count // 2 or offset + 4 + commands != len(data):
        raise ValueError('gfont 命令数量无效')
    cmds = data[offset + 4:]
    if any(c not in (0, 1) for c in cmds) or (cmds and cmds[0] != 0):
        raise ValueError('gfont 路径命令无效')
    if not all(math.isfinite(v) and abs(v) <= 1_000_000 for v in coords):
        raise ValueError('gfont 坐标无效')
    return [[cmd, coords[i*2], coords[i*2+1]] for i, cmd in enumerate(cmds)]


def _archive(path):
    path = Path(path)
    if path.stat().st_size > 512_000_000:
        raise ValueError('gfont 文件过大')
    with path.open('rb') as stream:
        header = stream.read(8)
    if len(header) != 8:
        raise ValueError('gfont 文件头截断')
    version, metadata_size = struct.unpack('>ii', header)
    if version != 5 or not 0 <= metadata_size <= path.stat().st_size - 8:
        raise ValueError('仅支持 gfont v5 本地字库')
    archive = zipfile.ZipFile(path)
    names = archive.namelist()
    if len(names) > 70_000 or len(names) != len(set(names)):
        archive.close()
        raise ValueError('gfont ZIP 目录无效')
    return archive


def _read(archive, cp):
    if cp > 0xffff:
        return None
    try:
        entry = archive.getinfo(str(cp))
    except KeyError:
        return None
    if entry.file_size > MAX_RECORD or entry.flag_bits & 1:
        raise ValueError('gfont 字形过大或已加密')
    return parse_glyph(archive.read(entry), cp)


def register_font(path):
    path = Path(path).resolve()
    samples = {}
    bounds = None
    with _archive(path) as archive:
        # Measure one common coordinate frame. Never normalize individual
        # glyphs: punctuation, bearings and relative body sizes stay intact.
        # The scan retains four extrema, not a redistributable font copy.
        for name in archive.namelist():
            if not name.isascii() or not name.isdecimal() or not 0<=int(name)<=65535:
                continue
            char=chr(int(name))
            glyph = _read(archive, ord(char))
            drawing=[]
            previous=None
            for cmd,x,y in glyph or []:
                if cmd==1 and previous is not None and previous!=(x,y):
                    drawing.extend((previous,(x,y)))
                previous=(x,y)
            if drawing:
                xs = [point[0] for point in drawing]
                ys = [point[1] for point in drawing]
                current=[min(xs),max(xs),min(ys),max(ys)]
                bounds=current if bounds is None else [min(bounds[0],current[0]),max(bounds[1],current[1]),min(bounds[2],current[2]),max(bounds[3],current[3])]
                if char in REFERENCE and max(ys) > min(ys):
                    samples[char] = max(ys) - min(ys)
    if len(samples) < 3:
        raise ValueError('字库缺少足够的常用参考字，无法建立统一参考字号')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    em_size=max(bounds[1]-bounds[0],bounds[3]-bounds[2])
    return {'family': 'PageWriter GFont ' + digest[:16], 'name': path.stem,
            'path': str(path), 'sha256': digest,
            'em_size': em_size, 'origin_x': -bounds[0],
            'reference': {'method': 'whole-font-common-envelope-v2', 'samples': samples,
                          'bounds': bounds, 'origin_x': -bounds[0], 'em_size': em_size},
            'glyphs': {}}


def glyph_for(descriptor, char):
    """Return portable raw centerline data, lazily reading just this character."""
    if descriptor.get('builtin_id'):
        from .stroke_fonts import glyph_for as builtin_glyph_for
        return builtin_glyph_for(descriptor, char)
    key = str(ord(char))
    cache = descriptor['glyphs']
    if key in cache:
        return cache[key]
    path = Path(descriptor.get('path', ''))
    if not path.is_file():
        return None
    # A changed local font must not silently combine with a frozen older subset.
    stat=path.stat()
    signature=(str(path),stat.st_size,stat.st_mtime_ns,descriptor['sha256'])
    if signature not in _verified and hashlib.sha256(path.read_bytes()).hexdigest() != descriptor['sha256']:
        raise ValueError('本地单线字库已改变，请重新导入')
    _verified[signature]=True
    with _archive(path) as archive:
        result = _read(archive, ord(char))
    if result is not None:
        cache[key] = result
    return result
