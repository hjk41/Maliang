"""PageWriter's offline, millimetre based document and path engine.

The same cubic outlines are used by the scene, collision checking and SVG.
Stroke-font resources are read lazily by stroke_fonts; this engine is offline.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import unicodedata
import zipfile
from html import unescape
from dataclasses import dataclass, field, asdict
from pathlib import Path
from xml.sax.saxutils import escape
from . import gfont, stroke_fonts

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication, QImage, QPainter,
                          QPainterPath, QPainterPathStroker, QPen, QPolygonF,
                          QTextCharFormat, QTextCursor, QTextDocument, QTransform)

PX_PER_MM = 96.0 / 25.4
MM_PER_PX = 1.0 / PX_PER_MM
_paint_device = None


@dataclass
class TextBlock:
    id: str = "text-1"
    name: str = "批注"
    html: str = '<p style="font-family:Microsoft YaHei;font-size:12pt;">双击编辑批注</p>'
    x: float = 20.0
    y: float = 20.0
    width: float = 65.0
    rotation: float = 0.0
    columns: int = 1
    column_height: float = 0.0  # zero is unlimited only for legacy single-column text
    column_gap: float = 6.0
    column_separators: bool = False
    # Frozen original outlines survive moving projects between computers.
    frozen_key: str = ""
    frozen_path: list = field(default_factory=list)
    fonts: list = field(default_factory=list)
    frozen_overflow: bool = False
    frozen_layout: dict = field(default_factory=dict)
    writing_mode: str = 'outline'
    stroke_font: str = ''
    stroke_fonts: dict = field(default_factory=dict)
    # Flow text around protected zones that overlap this box (in the box's
    # own direction, so rotated text grows toward its own "down").
    avoid_zones: bool = True
    # Derived by sync_flow(): overlapping zones as polygons in box-local mm,
    # and the distance text keeps from them. Part of the layout identity.
    flow_exclusions: list = field(default_factory=list)
    flow_clearance: float = 0.0


@dataclass
class Drawing:
    """An embedded picture written as single strokes (see lineart.convert)."""
    id: str = "drawing-1"
    name: str = "图画"
    image_data: str = ""
    x: float = 20.0
    y: float = 20.0
    width: float = 40.0
    rotation: float = 0.0
    threshold: int = 128          # pixels darker than this are lines
    detail: float = 1.0           # simplification tolerance (source pixels)
    min_stroke: float = 0.6       # mm; shorter bits are dropped
    fill: bool = False            # hatch dark filled areas
    fill_cutoff: float = 0.6      # darkness (0..1) from which areas are hatched
    fill_spacing: float = 0.5     # mm between the densest hatch lines
    fill_angle: float = 45.0
    fill_levels: int = 3          # 1..4: how many darkness steps change the density
    fill_min_width: float = 0.6   # mm; thinner dark areas are treated as lines
    frozen_key: str = ""
    paths: list = field(default_factory=list)   # box-local mm polylines
    height: float = 0.0
    stats: dict = field(default_factory=dict)
    # Erased spots [u, v, r] as fractions of the width (so they scale with it),
    # applied after conversion; settings can change without losing them.
    erase: list = field(default_factory=list)
    # SVG files keep their own vector geometry (svgimport.parse): lines, fills, aspect.
    vector: dict = field(default_factory=dict)


@dataclass
class ExclusionZone:
    id: str = "zone-1"
    name: str = "禁写区"
    points: list = field(default_factory=list)
    kind: str = "ink"  # obstacle additionally blocks pen-up travel


@dataclass
class Project:
    width: float = 148.0
    height: float = 210.0
    blocks: list[TextBlock] = field(default_factory=list)
    zones: list[ExclusionZone] = field(default_factory=list)
    image_data: str = ""
    image_opacity: float = 0.72
    image_locked: bool = True
    image_offset: list = field(default_factory=lambda: [0.0, 0.0])
    # Source pixels TL, TR, BR, BL; maps to entire mm page, independent of machine.
    image_quad: list | None = None
    # Optional curved-edge points (source pixels) per side: top/right/bottom/left.
    image_edges: dict | None = None
    # Optional traced text lines (source pixels) that should be straight and horizontal.
    image_guides: list | None = None
    pen_width: float = 0.3
    margin: float = 0.4
    machine_profile: dict = field(default_factory=lambda: {"validated": False})
    drawings: list = field(default_factory=list)


@dataclass
class Collision:
    block_id: str
    zone_id: str
    message: str


class ExportBlocked(ValueError):
    pass


def _device():
    global _paint_device
    if _paint_device is None:
        _paint_device = QImage(8, 8, QImage.Format.Format_ARGB32)
        _paint_device.setDotsPerMeterX(round(96 / 0.0254))
        _paint_device.setDotsPerMeterY(round(96 / 0.0254))
    return _paint_device


def make_document(block: TextBlock) -> QTextDocument:
    doc = QTextDocument()
    doc.documentLayout().setPaintDevice(_device())
    default = QFont("Microsoft YaHei", 12)
    doc.setDefaultFont(default)
    doc.setDocumentMargin(0)
    doc.setHtml(block.html)
    prepare_stroke_document(doc, block)
    geometry=_flow_geometry(block)
    if block.column_height>0:
        doc.setPageSize(QSizeF(geometry["page_width_px"],geometry["page_height_px"]))
    else:
        doc.setTextWidth(block.width * PX_PER_MM)
    doc.size()  # force shaping before reading glyph runs
    return doc


def prepare_stroke_document(doc, block):
    """Apply the same private font metrics to rendering and rich-text editing."""
    if block.writing_mode != 'single_line':
        return
    errors = {}
    def families_for(descriptor):
        try:
            return stroke_fonts.layout_families(descriptor)
        except (ValueError, OSError, KeyError) as exc:
            # A saved project still opens when an asset is absent or upgraded.
            # System metrics are only used to position the editing caret; the
            # pen renderer below marks this resource missing and draws no
            # replacement outlines, so export remains blocked after an edit.
            errors[descriptor['family']] = str(exc)
            return [descriptor['family'], 'Microsoft YaHei']
    default = block.stroke_fonts.get(block.stroke_font)
    if default and default.get('builtin_id'):
        font = QFont(doc.defaultFont())
        font.setFamilies(families_for(default))
        font.setKerning(False)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        doc.setDefaultFont(font)
    changes = []
    paragraph = doc.begin()
    while paragraph.isValid():
        it = paragraph.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid():
                descriptor = stroke_fonts.resolve_descriptor(block.stroke_fonts,
                    fragment.charFormat().fontFamilies(), block.stroke_font)
                if descriptor and descriptor.get('builtin_id'):
                    fmt = QTextCharFormat()
                    font = QFont()
                    font.setFamilies(families_for(descriptor))
                    font.setKerning(False)
                    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
                    fmt.setFontFamilies(font.families())
                    fmt.setFontKerning(False)
                    changes.append((fragment.position(), fragment.length(), fmt))
            it += 1
        paragraph = paragraph.next()
    for start, length, fmt in changes:
        cursor = QTextCursor(doc)
        cursor.setPosition(start)
        cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
        cursor.mergeCharFormat(fmt)
    doc.setProperty('_stroke_font_errors', errors)


def _flow_geometry(block):
    width=(block.width-(block.columns-1)*block.column_gap)/block.columns
    # Qt's text layout stores geometry as QFixed, in 1/64 px. Supplying exact
    # fixed-point dimensions avoids assigning a line at the next page's top to
    # the previous column due to rounded page heights.
    return {"columns":block.columns,"column_width":width,
            "height":block.column_height,"gap":block.column_gap,
            "page_width_px":math.floor(width*PX_PER_MM*64)/64,
            "page_height_px":math.floor(block.column_height*PX_PER_MM*64)/64}


def path_to_data(path: QPainterPath) -> list:
    return [[int(path.elementAt(i).type.value), path.elementAt(i).x,
             path.elementAt(i).y] for i in range(path.elementCount())]


def path_from_data(data: list) -> QPainterPath:
    if not isinstance(data,list) or len(data)>500_000:
        raise ValueError("字形路径必须为有界列表")
    for element in data:
        if not isinstance(element,(list,tuple)) or len(element)!=3:
            raise ValueError("字形路径元素必须包含类型与两个坐标")
        kind,x,y=element
        if type(kind) is not int or kind not in (0,1,2,3):
            raise ValueError("字形路径元素类型无效")
        if not _finite_number(x) or not _finite_number(y):
            raise ValueError("字形路径坐标必须为有限数值")
    if data and data[0][0]!=0:
        raise ValueError("字形路径必须从 MoveTo 开始")
    path = QPainterPath()
    i = 0
    while i < len(data):
        kind, x, y = data[i]
        if kind == 0:
            path.moveTo(x, y)
        elif kind == 1:
            path.lineTo(x, y)
        elif kind == 2:
            if i + 2 >= len(data) or data[i+1][0]!=3 or data[i+2][0]!=3:
                raise ValueError("无效字形曲线")
            _, x2, y2 = data[i + 1]
            _, x3, y3 = data[i + 2]
            path.cubicTo(x, y, x2, y2, x3, y3)
            i += 2
        else:
            raise ValueError("无效字形路径元素")
        i += 1
    return path


def _finite_number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _points(value, length=None, minimum=None, maximum=4096, label="坐标"):
    if not isinstance(value,(list,tuple)) or (length is not None and len(value)!=length) or len(value)>maximum or (minimum is not None and len(value)<minimum):
        raise ValueError(f"{label}数量无效")
    for point in value:
        if not isinstance(point,(list,tuple)) or len(point)!=2 or not all(_finite_number(v) for v in point):
            raise ValueError(f"{label}必须为有限的二维坐标")


def _validate_frozen_layout(block):
    cache=block.frozen_layout
    if not isinstance(cache,dict) or type(block.frozen_overflow) is not bool:
        raise ValueError("分栏排版缓存无效")
    for key in ("column_width","height","gap","page_width_px","page_height_px","flow_height"):
        if key in cache and (not _finite_number(cache[key]) or cache[key]<0):
            raise ValueError("分栏缓存尺寸必须为有限非负数值")
    if "columns" in cache and (type(cache["columns"]) is not int or cache["columns"] not in (1,2,3)):
        raise ValueError("分栏缓存栏数无效")
    if "overflow" in cache and (type(cache["overflow"]) is not bool or cache["overflow"]!=block.frozen_overflow):
        raise ValueError("分栏缓存溢出状态不一致")
    for key,minimum in (("page_count",1),("line_count",0)):
        if key in cache and (type(cache[key]) is not int or not minimum<=cache[key]<=100_000):
            raise ValueError("分栏缓存页数或行数无效")
    lines=cache.get("lines",[])
    if not isinstance(lines,list) or len(lines)>100_000:
        raise ValueError("分栏缓存行列表无效")
    previous_end=0
    for line in lines:
        if not isinstance(line,dict):
            raise ValueError("分栏缓存行结构无效")
        for key in ("start","length","column"):
            if key not in line or type(line[key]) is not int or not 0<=line[key]<=10_000_000:
                raise ValueError("分栏缓存行索引无效")
        for key in ("y","height","width"):
            if key not in line or not _finite_number(line[key]) or line[key]<-1e-6:
                raise ValueError("分栏缓存行几何无效")
        if line["start"]<previous_end:
            raise ValueError("分栏缓存文本顺序无效")
        previous_end=line["start"]+line["length"]
    if "lines" in cache:
        if "line_count" in cache and cache["line_count"]!=len(lines):
            raise ValueError("分栏缓存行数不一致")
        page_count=max((line["column"]+1 for line in lines),default=1)
        if "page_count" in cache and cache["page_count"]!=page_count:
            raise ValueError("分栏缓存页数不一致")


def _validate_project(project: Project):
    if not all(_finite_number(v) for v in (project.width,project.height,project.pen_width,project.margin,project.image_opacity)):
        raise ValueError("项目尺寸、笔宽、余量或透明度不是有限数值")
    if not (0<project.width<=2000 and 0<project.height<=2000 and 0<project.pen_width<=20 and 0<=project.margin<=50 and 0<=project.image_opacity<=1):
        raise ValueError("项目尺寸、笔宽、余量或透明度范围无效")
    if type(project.image_locked) is not bool:
        raise ValueError("底图锁定状态无效")
    if not isinstance(project.image_offset,(list,tuple)) or len(project.image_offset)!=2 or not all(_finite_number(v) for v in project.image_offset):
        raise ValueError("底图偏移必须包含两个有限坐标")
    if project.image_quad is not None:
        _points(project.image_quad,length=4,label="底图四点校正")
    if project.image_edges is not None:
        if project.image_quad is None or not isinstance(project.image_edges,dict) or not set(project.image_edges)<={"top","right","bottom","left"}:
            raise ValueError("底图弯边校正无效")
        for values in project.image_edges.values():
            _points(values,maximum=5,label="底图弯边校正点")
    if project.image_guides is not None:
        if project.image_quad is None or not isinstance(project.image_guides,list) or len(project.image_guides)>6:
            raise ValueError("底图参考线无效")
        for guide in project.image_guides:
            _points(guide,minimum=2,maximum=6,label="底图参考线点")
    if not isinstance(project.blocks,list) or len(project.blocks)>1000 or not isinstance(project.zones,list) or len(project.zones)>1000:
        raise ValueError("项目对象数量无效")
    total_elements=0
    for b in project.blocks:
        if not isinstance(b,TextBlock) or not all(_finite_number(v) for v in (b.x,b.y,b.width,b.rotation)) or not 0<b.width<=2000:
            raise ValueError("文字框坐标或宽度无效")
        if not isinstance(b.html,str) or len(b.html)>2_000_000 or not isinstance(b.id,str) or not isinstance(b.name,str):
            raise ValueError("文字框内容或标识无效")
        if type(b.columns) is not int or b.columns not in (1,2,3):
            raise ValueError("分栏数必须为 1、2 或 3")
        if type(b.column_separators) is not bool:
            raise ValueError("分栏分隔线状态必须为布尔值")
        if not _finite_number(b.column_height) or not 0<=b.column_height<=2000 or (b.columns>1 and b.column_height<=0):
            raise ValueError("多栏文字必须设置大于零的栏高")
        if not _finite_number(b.column_gap) or not 0<=b.column_gap<=2000 or b.width-(b.columns-1)*b.column_gap<=0:
            raise ValueError("栏间距必须小于文字框可用宽度")
        if b.column_height>0 and _flow_geometry(b)["page_height_px"]<1:
            raise ValueError("栏高过小")
        if _flow_geometry(b)["page_width_px"]<1:
            raise ValueError("栏宽过小")
        if type(b.frozen_overflow) is not bool or not isinstance(b.frozen_layout,dict):
            raise ValueError("分栏排版缓存无效")
        if type(b.avoid_zones) is not bool or not isinstance(b.flow_exclusions,list) or len(b.flow_exclusions)>1000:
            raise ValueError("文字避让设置无效")
        if not _finite_number(b.flow_clearance) or not 0<=b.flow_clearance<=50:
            raise ValueError("文字避让间距无效")
        for polygon in b.flow_exclusions:
            _points(polygon,minimum=3,label="文字避让区域顶点")
        _validate_frozen_layout(b)
        if b.writing_mode not in ('outline','single_line') or not isinstance(b.stroke_font,str) or not isinstance(b.stroke_fonts,dict) or len(b.stroke_fonts)>50:
            raise ValueError('单线字体设置无效')
        for token, font in b.stroke_fonts.items():
            if not isinstance(token,str) or not isinstance(font,dict) or font.get('family')!=token:
                raise ValueError('单线字体标识无效')
            if not _finite_number(font.get('em_size')) or not 0<font['em_size']<=1_000_000 or not isinstance(font.get('glyphs'),dict) or len(font['glyphs'])>200_000:
                raise ValueError('单线字体参考字身或缓存无效')
            if not isinstance(font.get('path',''),str) or not isinstance(font.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',font['sha256']):
                raise ValueError('单线字体来源无效')
            if 'origin_x' in font and (not _finite_number(font['origin_x']) or abs(font['origin_x'])>1_000_000):
                raise ValueError('单线字体共同原点无效')
            if 'builtin_id' in font and (not isinstance(font['builtin_id'],str) or not re.fullmatch('[a-z0-9-]{1,64}',font['builtin_id'])):
                raise ValueError('内置单线字体编号无效')
            if font.get('builtin_id') and (not stroke_fonts.valid_family(font['builtin_id'],font['family']) or font['em_size'] != 1000 or font.get('origin_x',0) != 0):
                raise ValueError('内置单线字体坐标系或族名无效')
            if 'resource_versions' in font:
                versions=font['resource_versions']
                if not isinstance(versions,dict) or len(versions)>50:
                    raise ValueError('内置单线字体资源版本无效')
                for font_id,version in versions.items():
                    if not isinstance(font_id,str) or not re.fullmatch('[a-z0-9-]{1,64}',font_id) or not isinstance(version,dict) or not set(version)<={'sha256','layout_sha256'} or any(not isinstance(v,str) or not re.fullmatch('[a-f0-9]{64}',v) for v in version.values()):
                        raise ValueError('内置单线字体资源版本无效')
            if 'glyph_sources' in font:
                sources=font['glyph_sources']
                if not isinstance(sources,dict) or len(sources)>200_000 or any(not isinstance(code,str) or not code.isascii() or not code.isdecimal() or not 0<=int(code)<=0x10ffff or 0xd800<=int(code)<=0xdfff or not isinstance(source,str) or not re.fullmatch('[a-z0-9-]{1,64}',source) for code,source in sources.items()):
                    raise ValueError('单线字体字形来源缓存无效')
            for code,data in font['glyphs'].items():
                if not isinstance(code,str) or not code.isascii() or not code.isdecimal() or not 0<=int(code)<=0x10ffff or 0xd800<=int(code)<=0xdfff:
                    raise ValueError('单线字体缓存字符编号无效')
                path_from_data(data)
                total_elements+=len(data)
                if total_elements>2_000_000:
                    raise ValueError('项目字形路径数量过多')
                if any(e[0] not in (0,1) for e in data):
                    raise ValueError('单线字体必须为 Move/Line 路径')
        if not isinstance(b.frozen_path,list):
            raise ValueError("保存的字形路径无效")
        total_elements+=len(b.frozen_path)
        if total_elements>2_000_000:
            raise ValueError("项目字形路径数量过多")
        # Validate even stale cached paths. An edited project's cache must not
        # smuggle malformed coordinates into a later undo or another consumer.
        path_from_data(b.frozen_path)
    for z in project.zones:
        if not isinstance(z,ExclusionZone) or z.kind not in ("ink","obstacle") or not isinstance(z.id,str) or not isinstance(z.name,str):
            raise ValueError("禁写区域类型或标识无效")
        _points(z.points,minimum=3,label="禁写区域顶点")
    if not isinstance(project.drawings,list) or len(project.drawings)>200:
        raise ValueError("图画数量无效")
    for d in project.drawings:
        if not isinstance(d,Drawing) or not isinstance(d.id,str) or not isinstance(d.name,str) or not isinstance(d.image_data,str) or len(d.image_data)>40_000_000:
            raise ValueError("图画内容或标识无效")
        numbers=(d.x,d.y,d.width,d.rotation,d.detail,d.min_stroke,d.fill_cutoff,d.fill_spacing,d.fill_angle,d.fill_min_width,d.height)
        if not all(_finite_number(v) for v in numbers) or not 1<=d.width<=2000 or d.height<0:
            raise ValueError("图画位置或尺寸无效")
        if type(d.threshold) is not int or not 1<=d.threshold<=255 or type(d.fill_levels) is not int or not 1<=d.fill_levels<=4 or type(d.fill) is not bool:
            raise ValueError("图画转换参数无效")
        if not (0<=d.detail<=20 and 0<=d.min_stroke<=50 and 0<=d.fill_cutoff<=1 and .1<=d.fill_spacing<=20 and 0<=d.fill_min_width<=50):
            raise ValueError("图画转换参数超出范围")
        if not isinstance(d.paths,list) or len(d.paths)>200_000 or not isinstance(d.stats,dict):
            raise ValueError("图画笔迹缓存无效")
        for path in d.paths:
            _points(path,minimum=2,maximum=100_000,label="图画笔迹点")
        if not isinstance(d.vector,dict) or (d.vector and (not isinstance(d.vector.get("lines"),list) or not _finite_number(d.vector.get("aspect")) or d.vector["aspect"]<=0)):
            raise ValueError("图画矢量数据无效")
        for line in d.vector.get("lines",[]):
            _points(line,minimum=2,maximum=200_000,label="图画矢量点")
        if not isinstance(d.erase,list) or len(d.erase)>20_000:
            raise ValueError("图画擦除记录无效")
        for spot in d.erase:
            if not isinstance(spot,(list,tuple)) or len(spot)!=3 or not all(_finite_number(v) for v in spot) or not 0<spot[2]<=1:
                raise ValueError("图画擦除点无效")


def _layout_key(block):
    source=block.html + "\0" + str(float(block.width))
    if block.writing_mode=='single_line':
        source+='\0single-line-v2:'+block.stroke_font+json.dumps({k:{'sha256':v.get('sha256'),'em_size':v.get('em_size'),'origin_x':v.get('origin_x')} for k,v in block.stroke_fonts.items()},sort_keys=True)
        builtin_versions={k:v['resource_versions'] for k,v in block.stroke_fonts.items() if v.get('builtin_id') and 'resource_versions' in v}
        if builtin_versions:
            source+='\0builtin-resources:'+json.dumps(builtin_versions,sort_keys=True)
        if any(unicodedata.category(char)=='Cf' for char in unescape(block.html)):
            source+='\0invisible-format-controls-v1'
    # Preserve the original v1 cache identity for unlimited single-column
    # projects, including when the original fonts are missing on a new PC.
    if block.columns!=1 or block.column_height>0:
        source+=f"\0columns:{block.columns}:{float(block.column_height)}:{float(block.column_gap)}"
    if _html_has_underline(block.html):
        # Earlier versions froze only glyph contours for the same HTML. Only
        # genuinely underlined text gets a new geometry key; all other legacy
        # frozen paths keep their exact identity, including unavailable fonts.
        source+="\0underline-centerlines-v1"
    if block.flow_exclusions:
        # Only boxes that actually flow around zones get a new identity.
        source+="\0flow-v1:"+json.dumps({"zones":block.flow_exclusions,"clearance":round(float(block.flow_clearance),4)},sort_keys=True)
    return hashlib.sha256(source.encode()).hexdigest()


def _html_has_underline(html):
    if "underline" not in html.lower() and not re.search(r"<\s*u(?:\s|>)",html,re.I):
        return False
    doc=QTextDocument()
    doc.setHtml(html)
    paragraph=doc.begin()
    while paragraph.isValid():
        it=paragraph.begin()
        while not it.atEnd():
            fragment=it.fragment()
            if fragment.isValid() and fragment.charFormat().fontUnderline():
                return True
            it+=1
        paragraph=paragraph.next()
    return False


def _line_underlines(paragraph,line):
    """Return UTF-16 text spans and their actual QTextLine x/baseline positions."""
    start,end=line.textStart(),line.textStart()+line.textLength()
    text=paragraph.text().encode("utf-16-le")
    units=[int.from_bytes(text[i:i+2],"little") for i in range(0,len(text),2)]
    it=paragraph.begin()
    segments=[]
    while not it.atEnd():
        fragment=it.fragment()
        if fragment.isValid() and fragment.charFormat().fontUnderline():
            left=max(start,fragment.position()-paragraph.position())
            right=min(end,fragment.position()-paragraph.position()+fragment.length())
            # Exclude explicit line separators and paragraph endings. Ordinary
            # spaces remain underlined, exactly like the editing selection.
            chunk=left
            ranges=[]
            for pos in range(left,right):
                if pos>=len(units) or units[pos] in (10,13,0x2028,0x2029):
                    if chunk<pos:ranges.append((chunk,pos))
                    chunk=pos+1
            if chunk<right:ranges.append((chunk,right))
            metrics=QFontMetricsF(fragment.charFormat().font(),_device())
            y=line.y()+line.ascent()+metrics.underlinePos()
            for a,b in ranges:
                x1=line.cursorToX(a)[0]
                x2=line.cursorToX(b)[0]
                if abs(x2-x1)>1e-9:
                    segments.append({"start":paragraph.position()+a,"end":paragraph.position()+b,
                                     "x1":x1,"x2":x2,"y":y})
        it+=1
    return segments


FLOW_STEP_MM = 0.5          # how far a line moves down when no gap is wide enough
FLOW_LIMIT_MM = 2000.0      # hard stop for downward flow and automatic growth
FLOW_EXTRA_MM = 0.2         # glyph ink may slightly exceed its line box
_flow_height_cache = {}


def _polygon_path(points, scale=1.0):
    path = QPainterPath()
    path.addPolygon(QPolygonF([QPointF(float(x)*scale, float(y)*scale) for x, y in points]))
    path.closeSubpath()
    return path


def _flow_regions(block):
    """Box-local exclusion regions in layout pixels, grown by the clearance."""
    grow = block.flow_clearance*PX_PER_MM
    regions = []
    for polygon in block.flow_exclusions:
        path = _polygon_path(polygon, PX_PER_MM)
        if grow > 0:
            path = path.united(stroke_envelope(path, 2*grow))
        regions.append(path)
    return regions


def _free_span(regions, left, width, top, bottom):
    """Widest horizontal gap of [left, left+width] not blocked within the band."""
    band = QRectF(left, top, width, max(bottom-top, 1e-3))
    band_path = QPainterPath()
    band_path.addRect(band)
    blocked = []
    for region in regions:
        if region.intersects(band):
            r = region.intersected(band_path).boundingRect()
            blocked.append((max(left, r.left()), min(left+width, r.right())))
    blocked.sort()
    gaps, cursor = [], left
    for a, b in blocked:
        if a > cursor:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < left+width:
        gaps.append((cursor, left+width))
    return max(gaps, key=lambda g: g[1]-g[0], default=(left, left))


def _flow_lines(doc, block, column_height_px):
    """Lay out every line around the exclusions, top to bottom in box-local space.

    Positions use Qt's paged column convention so all consumers treat flowed and
    ordinary text identically: x inside its column, y = column*height + y.
    """
    geometry = _flow_geometry(block)
    width = geometry["page_width_px"]
    pitch = (geometry["column_width"]+block.column_gap)*PX_PER_MM
    regions = _flow_regions(block)
    step, limit = FLOW_STEP_MM*PX_PER_MM, FLOW_LIMIT_MM*PX_PER_MM
    column, y, previous_bottom, overflow = 0, 0., None, False
    entries = []
    paragraph = doc.begin()
    while paragraph.isValid():
        fmt = paragraph.blockFormat()
        # Like Qt: the first paragraph's top margin is not applied; between
        # paragraphs the larger of bottom/top margin is used.
        if previous_bottom is not None:
            y += max(previous_bottom, fmt.topMargin())
        layout = paragraph.layout()
        layout.setPosition(QPointF(0, 0))
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            while True:
                line.setLineWidth(width)
                height = line.height()
                if column_height_px > 0 and y > 0 and y+height > column_height_px+1e-5:
                    column, y = column+1, 0.
                    continue
                if column >= block.columns or y > limit or (column_height_px > 0 and y+height > column_height_px+1e-5):
                    # Overflow: keep stacking like Qt's extra pages (blocked by preflight).
                    overflow = True
                    line.setPosition(QPointF(0, column*column_height_px+y))
                    break
                left = column*pitch
                start, end = _free_span(regions, left, width, y, y+height)
                available = end-start
                if available >= min(width, max(4*PX_PER_MM, 2*height))-1e-6:
                    line.setLineWidth(available)
                    if available >= width-1e-6 or line.naturalTextWidth() <= available+.02*PX_PER_MM:
                        line.setPosition(QPointF(start-left, column*column_height_px+y))
                        break
                y += step
            y += line.height()
            entries.append((paragraph, line, QPointF(0, 0)))
        layout.endLayout()
        previous_bottom = fmt.bottomMargin()
        paragraph = paragraph.next()
    return entries, overflow


def _column_geometry(block, height_mm):
    geometry = dict(_flow_geometry(block))
    geometry["height"] = height_mm
    geometry["page_height_px"] = math.floor(height_mm*PX_PER_MM*64)/64
    return geometry


def layout_lines(block):
    """(doc, geometry, entries, overflow) shared by stroke output and canvas editing.

    ``entries`` are (paragraph, QTextLine, origin) in Qt's paged convention.
    Without exclusions this is Qt's own document layout. With exclusions every
    line is placed in the widest free gap; a multi-column box that no longer
    fits grows downward (its own down, so rotated boxes grow along their text),
    reported as ``geometry['flow_height']``. The caller keeps ``doc`` alive.
    """
    doc = make_document(block)
    if not block.flow_exclusions:
        entries = []
        paragraph = doc.begin()
        while paragraph.isValid():
            origin = doc.documentLayout().blockBoundingRect(paragraph).topLeft()
            layout = paragraph.layout()
            entries.extend((paragraph, layout.lineAt(i), origin) for i in range(layout.lineCount()))
            paragraph = paragraph.next()
        return doc, _flow_geometry(block), entries, False
    if block.column_height <= 0:
        entries, overflow = _flow_lines(doc, block, 0)
        return doc, _flow_geometry(block), entries, overflow
    key = _layout_key(block)
    height = _flow_height_cache.get(key)
    if height is None:
        height = block.column_height
        _, overflow = _flow_lines(doc, block, _column_geometry(block, height)["page_height_px"])
        if overflow:
            # Grow downward: double until it fits, then bisect to 0.5 mm.
            low, high = height, height
            while high < FLOW_LIMIT_MM:
                low, high = high, min(FLOW_LIMIT_MM, high*2)
                if not _flow_lines(doc, block, _column_geometry(block, high)["page_height_px"])[1]:
                    break
            while high-low > FLOW_STEP_MM:
                middle = (low+high)/2
                if _flow_lines(doc, block, _column_geometry(block, middle)["page_height_px"])[1]:
                    low = middle
                else:
                    high = middle
            height = math.ceil(high/FLOW_STEP_MM)*FLOW_STEP_MM
        if len(_flow_height_cache) > 512:
            _flow_height_cache.clear()
        _flow_height_cache[key] = height
    geometry = _column_geometry(block, height)
    entries, overflow = _flow_lines(doc, block, geometry["page_height_px"])
    if height > block.column_height+1e-9:
        geometry["flow_height"] = height
    return doc, geometry, entries, overflow


def effective_column_height(block):
    """Column height after automatic downward growth (0 for unlimited text)."""
    if block.column_height <= 0:
        return 0.
    return layout_info(block)["height"]


def local_block_path(block: TextBlock) -> QPainterPath:
    key = _layout_key(block)
    if block.frozen_key == key:
        for descriptor in block.stroke_fonts.values():
            if 'glyph_sources' in descriptor:
                descriptor['glyph_sources']={k:v for k,v in descriptor['glyph_sources'].items() if k in descriptor['glyphs']}
        return path_from_data(block.frozen_path)
    if os.name == "nt" and QGuiApplication.platformName() == "offscreen":
        raise RuntimeError("Windows 的 Qt offscreen 插件不提供可靠的本机字形；请清除 QT_QPA_PLATFORM，使用原生 Windows 平台运行（测试可隐藏窗口）。")
    doc, geometry, entries, overflow = layout_lines(block)
    outline = QPainterPath()
    fonts = {}
    page_height=geometry["page_height_px"]
    line_metadata=[]
    underlines=[]
    char_boxes=[]
    used_glyphs={}
    resource_errors=doc.property('_stroke_font_errors') or {}
    # Qt 6.11's Python binding can return an empty list for the default
    # QTextLayout.glyphRuns(-1,-1). Per-line extraction shapes reliably.
    for paragraph, line, origin in entries:
        top=origin.y()+line.y()
        column=max(0,int(math.floor((top+1e-6)/page_height))) if page_height else 0
        column_y=top-column*page_height if page_height else top
        too_tall=bool(page_height and column_y+line.height()>page_height+1e-5)
        too_wide=bool(page_height and line.naturalTextWidth()>geometry["page_width_px"]+.02*PX_PER_MM)
        overflow=overflow or column>=block.columns or too_tall or too_wide
        line_metadata.append({"start":paragraph.position()+line.textStart(),
                              "length":line.textLength(),"column":column,
                              "y":column_y*MM_PER_PX,"height":line.height()*MM_PER_PX,
                              "width":line.naturalTextWidth()*MM_PER_PX})
        line_outline=QPainterPath()
        if block.writing_mode=='single_line':
            dx=column*(geometry['column_width']+block.column_gap)*PX_PER_MM
            dy=column*page_height if page_height else 0
            it=paragraph.begin()
            while not it.atEnd():
                fragment=it.fragment()
                if fragment.isValid():
                    fmt=fragment.charFormat()
                    requested=fmt.fontFamilies() or []
                    descriptor=stroke_fonts.resolve_descriptor(block.stroke_fonts,requested,block.stroke_font)
                    token=descriptor['family'] if descriptor else (requested[0] if requested else block.stroke_font)
                    size=fmt.fontPointSize() or doc.defaultFont().pointSizeF()
                    unit=fragment.position()-paragraph.position()
                    for char in fragment.text():
                        length=len(char.encode('utf-16-le'))//2
                        if line.textStart()<=unit<line.textStart()+line.textLength():
                            x=line.cursorToX(unit)[0]
                            endx=line.cursorToX(unit+length)[0]
                            char_boxes.append({'start':paragraph.position()+unit,'length':length,'column':column,
                                'x':(origin.x()+x+dx)*MM_PER_PX,'y':column_y*MM_PER_PX,
                                'width':abs(endx-x)*MM_PER_PX,'height':line.height()*MM_PER_PX})
                            formatting = unicodedata.category(char) == 'Cf'
                            # Qt can materialize a soft hyphen at a wrap using
                            # a system font, although cursorToX gives it zero
                            # width. Until a pen-font layout alias is available,
                            # block that case rather than omit visible ink or
                            # claim the system hyphen's metrics as pen metrics.
                            visible_soft_hyphen = (char == '\u00ad'
                                and unit+length == line.textStart()+line.textLength()
                                and unit+length < paragraph.length()-1)
                            if visible_soft_hyphen:
                                entry=fonts.setdefault(token,{'family':token or '未选择单线字库','style':'single_line','missing_glyphs':False,'missing_characters':''})
                                entry['missing_glyphs']=True
                                entry['error']='软连字符（U+00AD），请替换成普通连字符或加宽文字框'
                                if char not in entry['missing_characters']:entry['missing_characters']+=char
                            elif not formatting and not char.isspace() and char not in ('\u2028','\u2029'):
                                entry=fonts.setdefault(token,{'family':token or '未选择单线字库','style':'single_line','missing_glyphs':False,'missing_characters':''})
                                if descriptor and str(ord(char)) in descriptor['glyphs']:
                                    used_glyphs.setdefault(token,set()).add(str(ord(char)))
                                try:
                                    if token in resource_errors:
                                        raise ValueError(resource_errors[token])
                                    glyph=gfont.glyph_for(descriptor,char) if descriptor else None
                                except (ValueError,OSError,KeyError,zipfile.BadZipFile) as exc:
                                    glyph=None
                                    entry['error']=str(exc)
                                if glyph is None:
                                    entry['missing_glyphs']=True
                                    if char not in entry['missing_characters']:entry['missing_characters']+=char
                                else:
                                    used_glyphs.setdefault(token,set()).add(str(ord(char)))
                                    scale=size*96/72/descriptor['em_size']
                                    transform=QTransform(scale,0,0,scale,origin.x()+x+dx+descriptor.get('origin_x',descriptor['em_size']*.5)*scale,origin.y()+line.y()+line.ascent()-dy)
                                    line_outline.addPath(transform.map(path_from_data(glyph)))
                        unit+=length
                it+=1
        for run in ([] if block.writing_mode=='single_line' else line.glyphRuns()):
            raw = run.rawFont()
            font_id = hashlib.sha256(bytes(raw.fontTable("head")) + bytes(raw.fontTable("name"))).hexdigest()
            indexes=run.glyphIndexes()
            previous=fonts.get(font_id,{})
            fonts[font_id] = {"family": raw.familyName(), "style": raw.styleName(), "fingerprint": font_id,
                              "missing_glyphs": bool(previous.get("missing_glyphs")) or 0 in indexes
                                  or raw.familyName().startswith('Maliang Stroke ')}
            for glyph, pos in zip(indexes, run.positions()):
                glyph_path = raw.pathForGlyph(glyph)
                dx=column*(geometry["column_width"]+block.column_gap)*PX_PER_MM
                dy=column*page_height if page_height else 0
                shift = QTransform.fromTranslate(origin.x()+pos.x()+dx,origin.y()+pos.y()-dy)
                line_outline.addPath(shift.map(glyph_path))
        if page_height and not line_outline.isEmpty():
            bounds=line_outline.boundingRect()
            left=column*(geometry["column_width"]+block.column_gap)*PX_PER_MM
            right=left+geometry["page_width_px"]
            if bounds.left()<left-.02*PX_PER_MM or bounds.right()>right+.02*PX_PER_MM:
                overflow=True
        outline.addPath(line_outline)
        dx=column*(geometry["column_width"]+block.column_gap)*PX_PER_MM
        dy=column*page_height if page_height else 0
        for segment in _line_underlines(paragraph,line):
            x1=origin.x()+segment["x1"]+dx
            x2=origin.x()+segment["x2"]+dx
            y=origin.y()+segment["y"]-dy
            outline.moveTo(x1,y)
            outline.lineTo(x2,y)
            underlines.append({"start":segment["start"],"end":segment["end"],"column":column,
                               "x1":x1*MM_PER_PX,"x2":x2*MM_PER_PX,"y":y*MM_PER_PX})
    outline = QTransform.fromScale(MM_PER_PX, MM_PER_PX).map(outline)
    block.frozen_key, block.frozen_path = key, path_to_data(outline)
    block.fonts = list(fonts.values())
    if block.writing_mode=='single_line':
        for token,descriptor in block.stroke_fonts.items():
            used=used_glyphs.get(token,set())
            descriptor['glyphs']={k:v for k,v in descriptor['glyphs'].items() if k in used}
            if 'glyph_sources' in descriptor:
                descriptor['glyph_sources']={k:v for k,v in descriptor['glyph_sources'].items() if k in descriptor['glyphs']}
    block.frozen_overflow=overflow
    block.frozen_layout={**geometry,"overflow":overflow,
                         "page_count":max((line["column"]+1 for line in line_metadata),default=1),
                         "line_count":len(line_metadata),"lines":line_metadata,
                         "underlines":underlines}
    block.frozen_layout['char_boxes']=char_boxes
    return outline


def layout_info(block: TextBlock) -> dict:
    if block.frozen_key != _layout_key(block):
        # Only a stale cache needs a new layout; a fresh one is read directly
        # (parsing the cached stroke path here was the main refresh cost).
        local_block_path(block)
    _validate_frozen_layout(block)
    cache=block.frozen_layout
    lines=cache.get("lines",[])
    # Core fields always come from the actual model, even for incomplete old
    # caches or caches that misstated column dimensions. Frozen glyphs remain
    # untouched; asking for panel statistics never requires font regeneration.
    result={**cache,**_flow_geometry(block),"overflow":block.frozen_overflow,
            "page_count":cache.get("page_count",max((line["column"]+1 for line in lines),default=1)),
            "line_count":cache.get("line_count",len(lines)),"lines":lines}
    if cache.get("flow_height") and block.flow_exclusions:
        # Grown downward to fit around protected zones.
        result.update(_column_geometry(block,cache["flow_height"]),flow_height=cache["flow_height"])
    return result


def block_transform(block: TextBlock) -> QTransform:
    transform = QTransform()
    transform.translate(block.x, block.y)
    transform.rotate(block.rotation)
    return transform


def build_block_path(block: TextBlock) -> QPainterPath:
    return block_transform(block).map(composed_local_path(block))


DRAWING_PARAMS=("threshold","detail","min_stroke","fill","fill_cutoff","fill_spacing","fill_angle","fill_levels","fill_min_width")


def _drawing_key(drawing):
    source={"image":hashlib.sha256((drawing.image_data+json.dumps(drawing.vector,sort_keys=True)).encode()).hexdigest(),"width":round(float(drawing.width),4),
            **{name:getattr(drawing,name) for name in DRAWING_PARAMS},"v":1}
    return hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest()


def drawing_local_paths(drawing):
    """Box-local mm polylines; regenerated only when the picture, size or settings change."""
    key=_drawing_key(drawing)
    if drawing.frozen_key!=key:
        from . import lineart
        settings={name:getattr(drawing,name) for name in DRAWING_PARAMS}
        if drawing.vector:
            # SVG: use its own vectors; only fill hatching and short-bit removal apply.
            from . import svgimport
            lines=lineart.order_strokes(svgimport.strokes(drawing.vector,drawing.width,**settings))
            ink=sum(lineart.length(l) for l in lines)
            travel=sum(math.dist(a[-1],b[0]) for a,b in zip(lines,lines[1:]))
            result={"paths":lines,"height":drawing.vector["aspect"]*drawing.width,"strokes":len(lines),"ink_mm":ink,"travel_mm":travel,
                    "seconds":ink/lineart.WRITE_FEED*60+travel/lineart.TRAVEL_FEED*60+len(lines)*lineart.PEN_CYCLE_S,
                    "hatched":bool(drawing.fill and any(d>=drawing.fill_cutoff for _,d in drawing.vector.get("fills",[])))}
        else:
            result=lineart.convert(drawing.image_data,drawing.width,**settings)
        drawing.paths=[[[float(x),float(y)] for x,y in path] for path in result["paths"]]
        drawing.height=float(result["height"])
        drawing.stats={k:(bool(v) if k=="hatched" else float(v)) for k,v in result.items() if k in ("strokes","ink_mm","travel_mm","seconds","hatched")}
        drawing.frozen_key=key
    return drawing.paths


def apply_erase(paths, erase, width, step=.2):
    """Remove the parts of strokes inside erased circles, splitting strokes where cut."""
    if not erase:
        return paths
    import numpy as np
    circles=np.array([(u*width,v*width,r*width) for u,v,r in erase],float)
    out=[]
    for line in paths:
        xs=[p[0] for p in line]; ys=[p[1] for p in line]
        near=((circles[:,0]+circles[:,2]>=min(xs))&(circles[:,0]-circles[:,2]<=max(xs))&
              (circles[:,1]+circles[:,2]>=min(ys))&(circles[:,1]-circles[:,2]<=max(ys)))
        if not near.any():
            out.append(line); continue
        local=circles[near]
        points=[]
        for a,b in zip(line,line[1:]):
            n=max(1,int(math.dist(a,b)/step))
            points+=[(a[0]+(b[0]-a[0])*i/n,a[1]+(b[1]-a[1])*i/n) for i in range(n)]
        points.append(tuple(line[-1]))
        arr=np.array(points)
        hit=(((arr[:,None,0]-local[None,:,0])**2+(arr[:,None,1]-local[None,:,1])**2)<=local[None,:,2]**2).any(axis=1)
        run=[]
        for point,erased in zip(points,hit):
            if erased:
                if len(run)>=2: out.append(run)
                run=[]
            else:
                run.append([float(point[0]),float(point[1])])
        if len(run)>=2: out.append(run)
    return out


def drawing_strokes(drawing):
    """Strokes actually written: converted lines minus erased parts."""
    return apply_erase(drawing_local_paths(drawing),drawing.erase,drawing.width)


def drawing_local_path(drawing, strokes=None) -> QPainterPath:
    path=QPainterPath()
    for line in (drawing_strokes(drawing) if strokes is None else strokes):
        path.moveTo(*line[0])
        for point in line[1:]:
            path.lineTo(*point)
    return path


def build_drawing_path(drawing) -> QPainterPath:
    return block_transform(drawing).map(drawing_local_path(drawing))


def ink_items(project):
    """Every writable object with its page-space path: (object, path)."""
    return [(b,build_block_path(b)) for b in project.blocks]+[(d,build_drawing_path(d)) for d in project.drawings]


def separator_path(block: TextBlock) -> QPainterPath:
    """Real pen centerlines in gutter centers, independent of frozen fonts."""
    result=QPainterPath()
    if block.column_separators and block.columns>1 and block.column_height>0:
        width=_flow_geometry(block)["column_width"]
        height=effective_column_height(block)
        regions=_flow_regions(block) if block.flow_exclusions else []
        for index in range(1,block.columns):
            x=index*width+(index-.5)*block.column_gap
            # Like the text, the separator skips protected zones (with clearance).
            blocked=[]
            band=QRectF((x-.005)*PX_PER_MM,0,.01*PX_PER_MM,height*PX_PER_MM)
            band_path=QPainterPath()
            band_path.addRect(band)
            for region in regions:
                if region.intersects(band):
                    r=region.intersected(band_path).boundingRect()
                    blocked.append((r.top()*MM_PER_PX,r.bottom()*MM_PER_PX))
            cursor=0.
            for top,bottom in sorted(blocked)+[(height,height)]:
                if top-cursor>=.5:
                    result.moveTo(x,cursor)
                    result.lineTo(x,top)
                cursor=max(cursor,bottom)
    return result


def _text_extent(block):
    """How far down (box-local mm) the text currently reaches."""
    info=layout_info(block)
    if block.column_height>0:
        return info["height"]
    return max((line["y"]+line["height"] for line in info["lines"]),default=0.)


def _zones_reaching(block, zones, depth, clearance):
    """Zones within ``clearance`` of the box area [0,width] x [0,depth], as box-local polygons."""
    inverse,ok=block_transform(block).inverted()
    if not ok:
        return []
    area=QPainterPath()
    area.addRect(QRectF(-clearance,-clearance,block.width+2*clearance,depth+2*clearance))
    found=[]
    for zone in zones:
        local=[inverse.map(QPointF(float(x),float(y))) for x,y in zone.points]
        polygon=[[round(p.x(),4),round(p.y(),4)] for p in local]
        if _polygon_path(polygon).intersects(area):
            found.append(polygon)
    return found


def sync_block_flow(block, project):
    """Set the zones this box must flow around.

    Avoiding one zone can push text further down into another, so repeat
    until the set of reached zones stops changing.
    """
    if not block.avoid_zones or not project.zones:
        block.flow_exclusions,block.flow_clearance=[],0.
        return
    clearance=round(project.pen_width/2+project.margin+FLOW_EXTRA_MM,4)
    # Start from the last result: an unchanged page then needs one cached lookup.
    exclusions=[[list(p) for p in polygon] for polygon in block.flow_exclusions]
    for _ in range(8):
        block.flow_exclusions,block.flow_clearance=exclusions,(clearance if exclusions else 0.)
        found=_zones_reaching(block,project.zones,_text_extent(block),clearance)
        if found==exclusions:
            break
        exclusions=found
    block.flow_exclusions,block.flow_clearance=exclusions,(clearance if exclusions else 0.)


def sync_flow(project):
    """Recompute flow-around exclusions for every box; deterministic and idempotent."""
    for block in project.blocks:
        sync_block_flow(block,project)


def composed_local_path(block: TextBlock) -> QPainterPath:
    result=local_block_path(block)
    result.addPath(separator_path(block))
    return result


def zone_path(zone: ExclusionZone) -> QPainterPath:
    p = QPainterPath()
    if len(zone.points) >= 3:
        p.addPolygon(QPolygonF([QPointF(float(x), float(y)) for x, y in zone.points]))
        p.closeSubpath()
    return p


def stroke_envelope(path: QPainterPath, width: float) -> QPainterPath:
    stroker = QPainterPathStroker()
    stroker.setWidth(max(width, 0.000001))
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    stroker.setCurveThreshold(0.01)
    return stroker.createStroke(path)


def path_subpaths(path: QPainterPath, tolerance: float = 0.02) -> list[list[tuple[float, float]]]:
    """Flatten cubics with an explicit 0.02 mm control-polygon error bound.

    Recursive de Casteljau splitting stops only when both control points are
    within tolerance of the endpoint *segment*. Convexity bounds every point
    of the Bezier curve, including loops and reversing collinear curves.
    """
    if not (0 < tolerance <= 1):
        raise ValueError("曲线细分公差无效")

    def distance(p, a, b):
        dx, dy = b[0]-a[0], b[1]-a[1]
        denom = dx*dx+dy*dy
        t = max(0.0, min(1.0, ((p[0]-a[0])*dx+(p[1]-a[1])*dy)/denom)) if denom else 0
        return math.hypot(p[0]-a[0]-t*dx, p[1]-a[1]-t*dy)

    def mid(a, b):
        return ((a[0]+b[0])/2, (a[1]+b[1])/2)

    def cubic(a, b, c, d, dest, depth=0):
        if max(distance(b, a, d), distance(c, a, d)) <= tolerance:
            dest.append(d)
            return
        if depth >= 24:
            raise ValueError("曲线过于复杂，无法保证细分精度")
        ab, bc, cd = mid(a,b), mid(b,c), mid(c,d)
        abc, bcd = mid(ab,bc), mid(bc,cd)
        center = mid(abc,bcd)
        cubic(a,ab,abc,center,dest,depth+1)
        cubic(center,bcd,cd,d,dest,depth+1)

    result, current, i = [], [], 0
    while i < path.elementCount():
        e=path.elementAt(i)
        point=(e.x,e.y)
        if e.isMoveTo():
            if len(current)>1:
                result.append(current)
            current=[point]
        elif e.isLineTo():
            current.append(point)
        elif e.type == QPainterPath.ElementType.CurveToElement:
            b,c=path.elementAt(i+1),path.elementAt(i+2)
            cubic(current[-1],point,(b.x,b.y),(c.x,c.y),current)
            i+=2
        i+=1
    if len(current)>1:
        result.append(current)
    return result


def all_subpaths(project: Project) -> list[list[tuple[float, float]]]:
    sync_flow(project)
    return [sub for _,path in ink_items(project) for sub in path_subpaths(path)]


def preflight(project: Project) -> list[Collision]:
    failures = []
    try:
        _validate_project(project)
    except ValueError as exc:
        return [Collision("","project",f"项目无效：{exc}")]
    # Text boxes flow around protected zones before anything is checked.
    sync_flow(project)
    if not (0 < project.width <= 2000 and 0 < project.height <= 2000):
        return [Collision("", "page", "页面尺寸必须在 0–2000 mm 内")]
    if not (0 < project.pen_width <= 20 and 0 <= project.margin <= 50):
        return [Collision("", "page", "笔宽或安全余量无效")]
    page = QPainterPath()
    page.addRect(QRectF(0, 0, project.width, project.height))
    obstacles = [(z, zone_path(z)) for z in project.zones]
    try:
        items = ink_items(project)
    except ValueError as exc:   # e.g. an unreadable picture
        return [Collision("","drawing",f"图画无法转换：{exc}")]
    for block, path in items:
        if isinstance(block, TextBlock):
            if block.frozen_overflow:
                failures.append(Collision(block.id,"overflow",f"「{block.name}」文字溢出 {block.columns} 栏区域（栏高或栏宽不足），请增加栏高/总宽、调整字号或减少内容"))
            if any(f.get("missing_glyphs") for f in block.fonts):
                missing=''.join(f.get('missing_characters','') for f in block.fonts)
                detail='；'.join(dict.fromkeys(f['error'] for f in block.fonts if f.get('missing_glyphs') and f.get('error')))
                failures.append(Collision(block.id,"font",f"「{block.name}」包含字体无法描绘的字符{('：'+missing) if missing else ''}，请替换字体或字符"+(f'。{detail}' if detail else '')))
        if path.isEmpty():
            continue
        envelope = stroke_envelope(path, project.pen_width + 2 * project.margin)
        if not page.contains(envelope):
            failures.append(Collision(block.id, "page", f"「{block.name}」笔迹含安全余量超出纸页"))
        for zone, region in obstacles:
            if region.intersects(envelope) or region.contains(envelope):
                failures.append(Collision(block.id, zone.id, f"「{block.name}」与「{zone.name}」冲突"))
    # Pen-up movement is a straight connector in the offline job. It can cross
    # printed ink zones, but never an obstacle marked as no-travel.
    subs = all_subpaths(project)
    for first, second in zip(subs, subs[1:]):
        travel = QPainterPath(QPointF(*first[-1]))
        travel.lineTo(QPointF(*second[0]))
        envelope = stroke_envelope(travel, project.pen_width + 2 * project.margin)
        for zone, region in obstacles:
            if zone.kind == "obstacle" and region.intersects(envelope):
                message = f"抬笔移动经过障碍「{zone.name}」"
                if not any(c.message == message for c in failures):
                    failures.append(Collision("", zone.id, message))
    return failures


def _require_clear(project):
    errors = preflight(project)
    if errors:
        raise ExportBlocked("\n".join(c.message for c in errors))


def svg_path_data(path: QPainterPath) -> str:
    commands, i = [], 0
    while i < path.elementCount():
        e = path.elementAt(i)
        if e.isMoveTo():
            commands.append(f"M {e.x:.9f} {e.y:.9f}")
        elif e.isLineTo():
            commands.append(f"L {e.x:.9f} {e.y:.9f}")
        elif e.type == QPainterPath.ElementType.CurveToElement:
            b, c = path.elementAt(i + 1), path.elementAt(i + 2)
            commands.append(f"C {e.x:.9f} {e.y:.9f} {b.x:.9f} {b.y:.9f} {c.x:.9f} {c.y:.9f}")
            i += 2
        i += 1
    return " ".join(commands)


def export_svg(project: Project) -> str:
    _require_clear(project)
    paths = '\n'.join(f'<path id="{escape(b.id)}" d="{svg_path_data(path)}"/>' for b, path in ink_items(project))
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{project.width}mm" height="{project.height}mm" '
            f'viewBox="0 0 {project.width} {project.height}">\n'
            '<title>PageWriter 轮廓描绘；页面坐标毫米；无机器映射</title>\n'
            f'<g fill="none" stroke="#172b4d" stroke-width="{project.pen_width}" stroke-linecap="round" stroke-linejoin="round">\n{paths}\n</g></svg>\n')


def offline_gcode(project: Project) -> str:
    """Non-executable annotated path list. No Z values or machine mapping guessed."""
    _require_clear(project)
    lines = ["; PAGEWRITER OFFLINE PREVIEW ONLY — 不可发送机器", "; PAGE_X/PAGE_Y 为页面毫米，并非机器坐标", "; 未进行机器映射；无抬落笔高度；所有动作均注释", "; 曲线预览细分，原始 SVG 保存完整三次曲线"]
    for index, sub in enumerate(all_subpaths(project), 1):
        lines.append(f"; PATH {index} PEN_UP")
        lines.append(f"; G0 PAGE_X{sub[0][0]:.4f} PAGE_Y{sub[0][1]:.4f}")
        lines.append("; PEN_DOWN [UNCALIBRATED]")
        lines.extend(f"; G1 PAGE_X{x:.4f} PAGE_Y{y:.4f}" for x, y in sub[1:])
    lines.append("; PEN_UP [UNCALIBRATED] / END OFFLINE PREVIEW")
    return "\n".join(lines) + "\n"


def image_to_data(image: QImage) -> str:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buf, "PNG"):
        raise ValueError("无法编码底图")
    return base64.b64encode(bytes(data)).decode("ascii")


def data_to_image(data: str) -> QImage:
    image = QImage()
    if data and not image.loadFromData(base64.b64decode(data, validate=True)):
        raise ValueError("底图损坏")
    return image


def project_to_dict(project: Project) -> dict:
    _validate_project(project)
    for block in project.blocks:
        local_block_path(block)
    return {"format": "pagewriter", "version": 1, "project": asdict(project)}


def project_from_dict(payload: dict) -> Project:
    if not isinstance(payload,dict) or payload.get("format") != "pagewriter" or type(payload.get("version")) is not int or payload.get("version") != 1:
        raise ValueError("不支持的 PageWriter 项目格式")
    try:
        data = dict(payload["project"])
        data["blocks"] = [TextBlock(**b) for b in data.get("blocks", [])]
        data["zones"] = [ExclusionZone(**z) for z in data.get("zones", [])]
        data["drawings"] = [Drawing(**d) for d in data.get("drawings", [])]
        project = Project(**data)
    except (KeyError,TypeError) as exc:
        raise ValueError("项目结构或字段无效") from exc
    _validate_project(project)
    if project.image_data:
        data_to_image(project.image_data)
    return project


def save_project(project: Project, filename) -> None:
    target = Path(filename)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(project_to_dict(project), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def load_project(filename) -> Project:
    return project_from_dict(json.loads(Path(filename).read_text(encoding="utf-8")))


def font_warnings(project: Project) -> list[str]:
    """Frozen paths remain exact; warn before editing where fonts are missing."""
    families = {family.casefold() for family in QFontDatabase.families()}
    missing = set()
    stroke_missing=set()
    builtin_missing=set()
    for block in project.blocks:
        if block.writing_mode=='single_line':
            for font in block.stroke_fonts.values():
                if font.get('builtin_id'):
                    try:
                        stroke_fonts.layout_families(font)
                    except (ValueError, OSError, KeyError):
                        builtin_missing.add(font.get('name',font['family']))
                    continue
                if not Path(font.get('path','')).is_file():
                    stroke_missing.add(font.get('name','本地单线字库'))
            continue
        doc=make_document(block)
        paragraph=doc.begin()
        while paragraph.isValid():
            it=paragraph.begin()
            while not it.atEnd():
                fragment=it.fragment()
                if fragment.isValid():
                    requested=fragment.charFormat().fontFamilies()
                    if requested and not any(f.casefold() in families for f in requested):
                        missing.add('/'.join(requested))
                it+=1
            paragraph=paragraph.next()
    return ([f"内置字体 {family} 的排版资源缺失或版本改变；已保存笔迹保持不变，请重新选择字体后编辑。" for family in sorted(builtin_missing)]+
            [f"缺少单线字库 {family}；已保存字形仍可使用，新增未保存的字符会阻止导出，请重新导入原字库。" for family in sorted(stroke_missing)]+
            [f"缺少字体 {family}；已保存轮廓可原样预览/导出，编辑后将使用系统替代字体。" for family in sorted(missing)])


# Portrait width x height in mm.
PAPER_SIZES = [
    ("A4", 210, 297), ("A5", 148, 210), ("A6", 105, 148), ("A3", 297, 420),
    ("B5（国际 176×250）", 176, 250), ("B5（日本 182×257）", 182, 257),
    ("16开（185×260）", 185, 260), ("大32开（140×203）", 140, 203), ("32开（130×184）", 130, 184),
    ("Letter", 215.9, 279.4),
]


def blank_project(width=210.0, height=297.0) -> Project:
    """Empty page (A4 portrait by default): no photo, text or protected zones."""
    return Project(width=float(width), height=float(height))


def starter_project() -> Project:
    """Generate original demonstration artwork using installed system fonts."""
    image = QImage(888, 1260, QImage.Format.Format_RGB32)
    image.fill(QColor("#faf6e9"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#6a6257"))
    painter.setFont(QFont("Microsoft YaHei", 25, QFont.Weight.Bold))
    painter.drawText(QRectF(90, 95, 700, 60), Qt.AlignmentFlag.AlignLeft, "第三章 · 留白与观察")
    painter.setFont(QFont("Microsoft YaHei", 13))
    painter.drawText(90, 180, "这是一张本地生成的示例书页，不含真实书籍内容。")
    painter.setPen(QColor("#938b7f"))
    words = ["一页纸的边缘，留着思考的位置。", "阅读时停下来，记住一个新问题。", "观察细节，再用自己的语言重新表达。", "留白让纸上的观点继续生长。"]
    for i in range(12):
        painter.drawText(90, 245 + i * 37, words[i % len(words)])
    painter.setPen(QPen(QColor("#cdc3b4"), 1))
    painter.drawLine(90, 1080, 800, 1080)
    painter.setFont(QFont("Microsoft YaHei", 10))
    painter.drawText(90, 1125, "示例底图 / 原创演示素材                    042")
    painter.end()
    project = Project(image_data=image_to_data(image))
    project.zones = [ExclusionZone("print", "原书印刷正文", [(13, 14), (135, 14), (135, 116), (13, 116)])]
    project.blocks = [TextBlock("note-1", "阅读批注", '<p style="font-family:Microsoft YaHei;font-size:12pt;">读到这里，<span style="font-family:KaiTi;font-size:17pt;">慢一点。</span><br/>把新的想法留在纸上。</p>', 22, 130, 106), TextBlock("note-2", "页边短句", '<p style="font-family:KaiTi;font-size:13pt;">留白，也是思考的空间。</p>', 22, 161, 110)]
    return project
