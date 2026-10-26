"""Original geometric gfont fixtures, generated from source without vendor data.

These abstract marks exercise format/layout behavior; they are not Chinese
letterforms or evidence about the speed/quality of any real handwriting face.
"""
from pathlib import Path
import struct
import zipfile

from pagewriter import gfont

TEXT = '读到这里慢一点把新的想法留在纸上今天学习新的知识在已有文字和图片的书页空白处添加批注记录重要内容'
COLUMN_TEXT = '读到这里，慢一点。在书页上直接编辑。重点可以加下划线。两栏文字连续流动。'


def glyph_record(codepoint, points):
    return (struct.pack('>Hi', codepoint, len(points)*2)
            + b''.join(struct.pack('>ff', x, y) for _, x, y in points)
            + struct.pack('>i', len(points)) + bytes(cmd for cmd, _, _ in points))


def make_font(directory, joined=False):
    """Four separated segments or one connected polyline per corpus character."""
    path = Path(directory) / ('synthetic-joined.gfont' if joined else 'synthetic-separated.gfont')
    path.write_bytes(struct.pack('>ii', 5, 0))
    points = [(0, -30, -100), (1, 30, -100), (0, 30, -75), (1, -30, -75),
              (0, -30, -50), (1, 30, -50), (0, 30, -25), (1, -30, 0)]
    if joined:
        points = [(0 if i == 0 else 1, x, y) for i, (_, x, y) in enumerate(points)]
    with zipfile.ZipFile(path, 'a') as archive:
        for char in sorted(set(gfont.REFERENCE + TEXT + COLUMN_TEXT)):
            marks = [(0, -3, -5), (1, 3, 0)] if char in '，。' else points
            entry = zipfile.ZipInfo(str(ord(char)), date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(entry, glyph_record(ord(char), marks))
    return gfont.register_font(path)
